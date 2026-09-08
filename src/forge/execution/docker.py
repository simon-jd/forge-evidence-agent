"""Named Docker jobs with durable observations; no host execution fallback.

The container itself is the recoverable job. It remains after exit until an
explicit retention sweep; ``--rm`` would erase the evidence needed after a
worker crash. An unavailable job is an uncertain side effect, never a retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from docker.client import DockerClient
from docker.errors import DockerException, NotFound
from docker.models.containers import Container
from docker.types import LogConfig
from pydantic import BaseModel, Field

from forge.artifacts.store import atomic_write
from forge.domain.types import RepoProfile, canonical_hash
from forge.execution.errors import NeedsReconciliation, SandboxUnavailable
from forge.execution.safe_files import relative_parts
from forge.execution.workspace import Workspace


class ExecutionReceipt(BaseModel):
    operation_id: str
    container_id: str
    container_name: str
    epoch: int
    status: Literal["ok", "error", "timeout", "cancelled"]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    duration_seconds: float
    workspace_revision: int
    image_id: str
    image_digests: list[str] = Field(default_factory=list)
    rootless: bool
    recovered: bool = False
    limits: dict[str, Any] = Field(default_factory=dict)
    resource_statistics: dict[str, Any] = Field(default_factory=dict)
    # Container log retention is bounded even if a producer floods output while
    # the worker is offline. This is never represented as lossless archival.
    log_retention_bytes: int = 64 * 1024 * 1024


class DockerExecutor:
    def __init__(
        self, docker_host: str | None = None, allow_rootful: bool = False,
        max_log_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.docker_host = docker_host
        self.allow_rootful = allow_rootful
        self.max_log_bytes = max_log_bytes
        self._docker: DockerClient | None = None
        self._rootless = False

    def _client(self) -> DockerClient:
        if self._docker is None:
            try:
                client = (DockerClient(base_url=self.docker_host, timeout=15)
                          if self.docker_host else DockerClient.from_env(timeout=15))
                info = client.info()
            except DockerException as error:
                raise SandboxUnavailable(f"Docker daemon unavailable: {error}") from error
            self._rootless = any("rootless" in str(option) for option in info.get("SecurityOptions", []))
            if not self._rootless and not self.allow_rootful:
                client.close()
                raise SandboxUnavailable("Rootless Docker is required; the selected daemon is rootful")
            unsupported = [
                name for name in ("MemoryLimit", "PidsLimit", "CpuCfsQuota")
                if not info.get(name, False)
            ]
            if unsupported:
                client.close()
                raise SandboxUnavailable("Docker cannot enforce required limits: " + ", ".join(unsupported))
            self._docker = client
        return self._docker

    @staticmethod
    def job_name(workspace: Workspace, operation_id: str) -> str:
        key = f"{workspace.run_id}:{operation_id}"
        return "forge-" + hashlib.sha256(key.encode()).hexdigest()[:40]

    async def execute(
        self, workspace: Workspace, argv: list[str], timeout_seconds: float,
        operation_id: str, profile: RepoProfile, epoch: int, cwd: str = ".",
        read_only_workspace: bool = False,
    ) -> ExecutionReceipt:
        if (not argv or len(argv) > 128 or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
                or sum(len(arg) for arg in argv) > 64_000):
            raise ValueError("Command must be a bounded nonempty argv without NUL characters")
        relative_parts(cwd, allow_root=True)
        with workspace.files.directory(relative_parts(cwd, allow_root=True)):
            pass
        if timeout_seconds <= 0 or timeout_seconds > profile.command_timeout_seconds:
            raise ValueError("Command timeout exceeds the trusted repository profile")
        digest = canonical_hash({"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds,
                                 "read_only_workspace": read_only_workspace,
                                 "profile": profile.model_dump(mode="json")})
        async with workspace.lock():
            workspace.assert_epoch(epoch)
            workspace.require_idle(operation_id)
            previous = workspace.operation(operation_id)
            if previous is not None:
                if previous.get("args_hash") != digest or previous.get("kind") != "command":
                    raise NeedsReconciliation("Operation identity was reused for a different command")
                return await self._reconcile_locked(workspace, operation_id, epoch)
            client = await asyncio.to_thread(self._client)
            # Image access is local. Dependency installation/pulls are trusted
            # setup steps and never triggered by model commands.
            try:
                image = await asyncio.to_thread(client.images.get, profile.image)
            except DockerException as error:
                raise SandboxUnavailable(f"Prebuilt image unavailable: {profile.image}") from error
            name = self.job_name(workspace, operation_id)
            limits = {
                "memory_bytes": profile.memory_bytes, "cpu_limit": profile.cpu_limit,
                "pid_limit": profile.pid_limit, "network": "none", "read_only": True,
                "user": "1000:1000", "cap_drop": ["ALL"], "no_new_privileges": True,
                "read_only_workspace": read_only_workspace,
            }
            operation = {
                "kind": "command", "operation_id": operation_id, "args_hash": digest,
                "epoch": epoch, "state": "planned", "container_name": name,
                "argv": argv, "cwd": cwd, "started_at": time.time(),
                "timeout_seconds": timeout_seconds, "image_id": image.id,
                "image_digests": image.attrs.get("RepoDigests", []), "rootless": self._rootless,
                "limits": limits,
            }
            workspace.save_operation(operation_id, operation)
            workspace.set_active(operation_id)
            try:
                workspace.assert_epoch(epoch)
                container = await asyncio.to_thread(
                    client.containers.create, image.id, command=argv, name=name,
                    # This trusted wrapper sets the bind-mount collaboration
                    # umask then replaces itself. The image must contain Python.
                    entrypoint=["python", "-c", "import os,sys;os.umask(0);os.execvp(sys.argv[1],sys.argv[1:])"],
                    working_dir="/workspace" + ("/" + cwd if cwd != "." else ""),
                    user="1000:1000", read_only=True, network_mode="none", privileged=False,
                    cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                    pids_limit=profile.pid_limit, mem_limit=profile.memory_bytes,
                    memswap_limit=profile.memory_bytes, nano_cpus=int(profile.cpu_limit * 1e9),
                    tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777"},
                    volumes={str(workspace.path): {"bind": "/workspace", "mode": "ro" if read_only_workspace else "rw"}},
                    environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
                                 "PYTHONUNBUFFERED": "1", "FORGE_SANDBOX": "1"},
                    labels={"forge.managed": "true", "forge.run_id": workspace.run_id,
                            "forge.operation_id": operation_id, "forge.epoch": str(epoch)},
                    log_config=LogConfig(type="json-file", config={"max-size": "64m", "max-file": "1"}),
                    detach=True, stdin_open=False, tty=False,
                )
                operation.update(state="created", container_id=container.id)
                workspace.save_operation(operation_id, operation)
                if self._cancel_requested(workspace, operation_id):
                    # A never-started container is safe to classify cancelled;
                    # preserve it as evidence instead of launching the command.
                    return self._not_started_cancelled(workspace, operation, container)
                workspace.assert_epoch(epoch)
                await asyncio.to_thread(container.start)
                operation["state"] = "running"
                workspace.save_operation(operation_id, operation)
            except (DockerException, OSError) as error:
                # A create/start timeout does not prove the daemon did nothing.
                raise NeedsReconciliation(f"Container launch result is uncertain for {name}") from error
            return await self._observe(workspace, operation, container, recovered=False)

    async def reconcile(
        self, workspace: Workspace, operation_id: str, epoch: int | None = None,
    ) -> ExecutionReceipt:
        async with workspace.lock():
            if epoch is not None:
                workspace.assert_epoch(epoch)
            workspace.require_idle(operation_id)
            return await self._reconcile_locked(workspace, operation_id, epoch)

    async def _reconcile_locked(
        self, workspace: Workspace, operation_id: str, epoch: int | None,
    ) -> ExecutionReceipt:
        operation = workspace.operation(operation_id)
        if operation is None or operation.get("kind") != "command":
            raise NeedsReconciliation("No durable command operation exists")
        if "receipt" in operation:
            receipt = ExecutionReceipt.model_validate(operation["receipt"])
            workspace.set_active(None)
            return receipt.model_copy(update={"recovered": True})
        client = await asyncio.to_thread(self._client)
        try:
            container = await asyncio.to_thread(client.containers.get, operation["container_name"])
        except NotFound as error:
            raise NeedsReconciliation("Original command container is missing; refusing replay") from error
        except DockerException as error:
            raise NeedsReconciliation("Original command container cannot be observed") from error
        labels = container.labels
        if (labels.get("forge.run_id") != workspace.run_id or
                labels.get("forge.operation_id") != operation_id):
            raise NeedsReconciliation("Container ownership labels do not match the planned operation")
        if container.status == "created":
            # Even a created state is reconciled, not blindly started: launch
            # was already attempted and may have failed for an unknown reason.
            if self._cancel_requested(workspace, operation_id):
                return self._not_started_cancelled(workspace, operation, container)
            raise NeedsReconciliation("Original container was created but launch was not confirmed")
        return await self._observe(workspace, operation, container, recovered=True)

    def _collect_logs(self, workspace: Workspace, operation: dict[str, Any], container: Container) -> dict[str, Any]:
        key = hashlib.sha256(operation["operation_id"].encode()).hexdigest()
        stdout_path = workspace.control_path / "logs" / f"{key}.stdout"
        stderr_path = workspace.control_path / "logs" / f"{key}.stderr"
        def drain(path: Any, *, stderr: bool) -> int:
            count = kept = 0
            with path.open("wb") as stream:
                # logs(follow=True) closes for exited containers; attach can
                # wait for a future restart when reconciling an already exited
                # container and is consequently the wrong recovery protocol.
                for chunk in container.logs(stream=True, follow=True, stdout=not stderr, stderr=stderr):
                    count += len(chunk)
                    accepted = chunk[:max(0, self.max_log_bytes - kept)]
                    stream.write(accepted)
                    kept += len(accepted)
                    # Continue draining both streams even after retention caps.
                stream.flush()
                os.fsync(stream.fileno())
            return count

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="forge-log") as pool:
            stdout_future = pool.submit(drain, stdout_path, stderr=False)
            stderr_future = pool.submit(drain, stderr_path, stderr=True)
            counts = [stdout_future.result(), stderr_future.result()]
        return {
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "stdout": stdout_path.read_text(errors="replace"),
            "stderr": stderr_path.read_text(errors="replace"),
            "stdout_bytes": counts[0], "stderr_bytes": counts[1],
            "truncated": any(count > self.max_log_bytes for count in counts),
        }

    async def _observe(
        self, workspace: Workspace, operation: dict[str, Any], container: Container, *, recovered: bool,
    ) -> ExecutionReceipt:
        operation_id = operation["operation_id"]
        remaining = max(0.01, operation["started_at"] + operation["timeout_seconds"] - time.time())
        logs_task = asyncio.create_task(asyncio.to_thread(self._collect_logs, workspace, operation, container))
        status: Literal["ok", "error", "timeout", "cancelled"] = "ok"
        try:
            await asyncio.wait_for(asyncio.to_thread(container.wait), timeout=remaining)
        except TimeoutError:
            status = "timeout"
            await self._kill_and_confirm(container)
        except asyncio.CancelledError:
            await asyncio.shield(self._kill_and_confirm(container))
            await asyncio.shield(logs_task)
            raise
        except DockerException as error:
            # Preserve running state so another observer can reconnect.
            logs_task.cancel()
            raise NeedsReconciliation("Lost contact with original command") from error
        try:
            logs = await asyncio.wait_for(asyncio.shield(logs_task), timeout=20)
            await asyncio.to_thread(container.reload)
        except (TimeoutError, DockerException, OSError) as error:
            raise NeedsReconciliation("Command finished but complete retained logs are unavailable") from error
        state = container.attrs["State"]
        if state.get("Running"):
            raise NeedsReconciliation("Container has not reached a terminal state")
        if self._cancel_requested(workspace, operation_id):
            status = "cancelled"
        elif status == "ok" and int(state["ExitCode"]) != 0:
            status = "error"
        try:
            revision = workspace.refresh_revision()
        except (OSError, ValueError) as error:
            # A command may deliberately chmod a subordinate-UID file to 0600,
            # create symlinks or special files. Never skip them and claim that
            # a complete snapshot or independently verified patch was produced.
            raise NeedsReconciliation("Command left a workspace that cannot be fully inventoried") from error
        receipt = ExecutionReceipt(
            operation_id=operation_id, container_id=container.id,
            container_name=operation["container_name"], epoch=operation["epoch"],
            status=status, exit_code=int(state["ExitCode"]),
            duration_seconds=max(0, time.time() - operation["started_at"]),
            workspace_revision=revision, image_id=operation["image_id"],
            image_digests=operation["image_digests"], rootless=operation["rootless"],
            recovered=recovered, limits=operation["limits"],
            resource_statistics={"oom_killed": bool(state.get("OOMKilled", False)),
                                 "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt")},
            **logs,
        )
        operation.update(state="completed", receipt=receipt.model_dump(mode="json"))
        workspace.save_operation(operation_id, operation)
        workspace.set_active(None)
        return receipt

    @staticmethod
    async def _kill_and_confirm(container: Container) -> None:
        try:
            await asyncio.to_thread(container.reload)
            if container.attrs["State"].get("Running"):
                await asyncio.to_thread(container.kill, signal="SIGKILL")
            await asyncio.wait_for(asyncio.to_thread(container.wait), timeout=15)
            await asyncio.to_thread(container.reload)
            if container.attrs["State"].get("Running"):
                raise NeedsReconciliation("Cancellation did not stop the container")
        except DockerException as error:
            raise NeedsReconciliation("Cannot confirm original container stopped") from error

    @staticmethod
    def _cancel_requested(workspace: Workspace, operation_id: str) -> bool:
        return workspace.operation_path(operation_id).with_suffix(".cancel").exists()

    async def cancel(self, workspace: Workspace, operation_id: str) -> ExecutionReceipt | None:
        # Cancellation cannot take the workspace lock held by the job observer.
        atomic_write(workspace.operation_path(operation_id).with_suffix(".cancel"), b"cancel requested")
        operation = workspace.operation(operation_id)
        if operation is None:
            raise NeedsReconciliation("Cancellation has no durable operation to inspect")
        if "receipt" in operation:
            return ExecutionReceipt.model_validate(operation["receipt"])
        client = await asyncio.to_thread(self._client)
        try:
            container = await asyncio.to_thread(client.containers.get, operation["container_name"])
        except NotFound as error:
            raise NeedsReconciliation("Cannot yet confirm whether the command launched") from error
        if (container.labels.get("forge.operation_id") != operation_id or
                container.labels.get("forge.run_id") != workspace.run_id):
            raise NeedsReconciliation("Refusing to cancel a container with different ownership")
        if container.status != "created":
            await self._kill_and_confirm(container)
        # The current observer or a recovery worker persists the final receipt.
        return None

    def _not_started_cancelled(
        self, workspace: Workspace, operation: dict[str, Any], container: Container,
    ) -> ExecutionReceipt:
        receipt = ExecutionReceipt(
            operation_id=operation["operation_id"], container_id=container.id,
            container_name=operation["container_name"], epoch=operation["epoch"],
            status="cancelled", exit_code=137, duration_seconds=0,
            workspace_revision=workspace.revision, image_id=operation["image_id"],
            image_digests=operation["image_digests"], rootless=operation["rootless"],
            limits=operation["limits"],
        )
        operation.update(state="completed", receipt=receipt.model_dump(mode="json"))
        workspace.save_operation(operation["operation_id"], operation)
        workspace.set_active(None)
        return receipt
