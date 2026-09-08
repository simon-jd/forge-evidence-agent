"""Run explicitly with FORGE_TEST_DOCKER_HOST and FORGE_TEST_IMAGE after image setup.

These tests exercise a real daemon. No local process substitute is accepted.
Containers are removed only when their unique per-test run label matches.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from forge.domain.types import RepoProfile
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation
from forge.execution.workspace import Workspace, WorkspaceManager

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[tuple[Workspace, DockerExecutor, RepoProfile]]:
    host = (os.environ.get("FORGE_TEST_DOCKER_HOST") or os.environ.get("FORGE_DOCKER_HOST")
            or os.environ.get("DOCKER_HOST"))
    image = os.environ.get("FORGE_TEST_IMAGE")
    if not host or not image:
        pytest.skip("Set FORGE_TEST_DOCKER_HOST and FORGE_TEST_IMAGE for actual rootless Docker tests")
    executor = DockerExecutor(docker_host=host, allow_rootful=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1")
    client = executor._client()  # An explicitly configured unusable daemon fails.
    repository = tmp_path / "repo"
    repository.mkdir()
    for argv in (["init", "-q"], ["config", "user.name", "Sandbox fixture"],
                 ["config", "user.email", "sandbox@example.invalid"]):
        subprocess.run(["git", "-C", str(repository), *argv], check=True)
    (repository / "answer.py").write_text("ANSWER = 41\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Baseline"], check=True)
    run_id = "sandbox-" + uuid.uuid4().hex
    workspace = WorkspaceManager(tmp_path / "workspaces").prepare(run_id, repository, "HEAD")
    profile = RepoProfile(name="sandbox", image=image, validation_argv=["python", "--version"], command_timeout_seconds=20)
    yield workspace, executor, profile
    for container in client.containers.list(all=True, filters={"label": f"forge.run_id={run_id}"}):
        assert container.labels["forge.run_id"] == run_id
        container.remove(force=True)


async def test_sandbox_real_boundary_and_original_repository_unchanged(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    script = """import os,json,socket
from pathlib import Path
probe={'uid':os.getuid(),'secret':os.environ.get('OPENAI_API_KEY'),'socket':Path('/var/run/docker.sock').exists()}
try: Path('/outside-workspace').write_text('bad'); probe['rootfs_writable']=True
except OSError: probe['rootfs_writable']=False
try: socket.create_connection(('1.1.1.1',443),timeout=.2); probe['network']=True
except OSError: probe['network']=False
Path('answer.py').write_text('ANSWER = 42\\n')
Path('created.txt').write_text('new\\n')
print(json.dumps(probe))
"""
    receipt = await executor.execute(workspace, ["python", "-c", script], 10, "boundary", profile, 1)
    assert receipt.status == "ok"
    assert receipt.rootless or os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1"
    probe = json.loads(receipt.stdout)
    assert probe == {"uid": 1000, "secret": None, "socket": False, "rootfs_writable": False, "network": False}
    assert workspace.files.read("answer.py") == b"ANSWER = 42\n"
    assert (Path(workspace.metadata["source"]) / "answer.py").read_text() == "ANSWER = 41\n"
    assert "created.txt" in workspace.get_diff()
    container = executor._client().containers.get(receipt.container_id)
    config = container.attrs["HostConfig"]
    assert config["ReadonlyRootfs"] and config["NetworkMode"] == "none"
    assert config["Memory"] == profile.memory_bytes and config["PidsLimit"] == profile.pid_limit
    assert config["NanoCpus"] == int(profile.cpu_limit * 1e9)
    assert container.attrs["Config"]["User"] == "1000:1000"
    assert [mount["Destination"] for mount in container.attrs["Mounts"]] == ["/workspace"]
    assert receipt.workspace_revision == 1


async def test_sandbox_long_output_drains_both_streams(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    executor.max_log_bytes = 1024
    receipt = await executor.execute(workspace, ["python", "-c", "import sys;sys.stdout.write('x'*1000000);sys.stderr.write('y'*1000000)"],
                                     10, "output", profile, 1)
    assert receipt.status == "ok" and receipt.truncated
    assert len(receipt.stdout) == len(receipt.stderr) == 1024
    assert receipt.stdout_bytes == receipt.stderr_bytes == 1_000_000


async def test_sandbox_read_only_grader_workspace_blocks_test_rewriting(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    workspace.files.write("trusted_test.py", b"assert 1 == 2\n")
    before = workspace.snapshot()
    script = """import json
from pathlib import Path
try:
    Path('trusted_test.py').write_text('assert True\\n')
    result={'write_denied':False}
except OSError as error:
    result={'write_denied':True,'errno':error.errno}
print(json.dumps(result))
"""
    receipt = await executor.execute(
        workspace, ["python", "-c", script], 10, "read-only-grader", profile, 1,
        read_only_workspace=True,
    )
    assert receipt.status == "ok"
    assert json.loads(receipt.stdout)["write_denied"]
    assert receipt.limits["read_only_workspace"] is True
    assert workspace.snapshot() == before
    mounts = executor._client().containers.get(receipt.container_id).attrs["Mounts"]
    assert len(mounts) == 1 and mounts[0]["Destination"] == "/workspace" and not mounts[0]["RW"]
    with pytest.raises(NeedsReconciliation, match="different command"):
        await executor.execute(
            workspace, ["python", "-c", script], 10, "read-only-grader", profile, 1,
            read_only_workspace=False,
        )


async def test_sandbox_timeout_terminates_children_and_replay_does_not_restart(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    argv = ["python", "-c", "import subprocess,time;subprocess.Popen(['python','-c','import time;time.sleep(60)']);print('started',flush=True);time.sleep(60)"]
    receipt = await executor.execute(workspace, argv, 1, "timeout", profile, 1)
    assert receipt.status == "timeout"
    container = executor._client().containers.get(receipt.container_id)
    assert not container.attrs["State"]["Running"]
    replay = await executor.execute(workspace, argv, 1, "timeout", profile, 2)
    assert replay.recovered and replay.container_id == receipt.container_id


async def test_sandbox_cancel_stops_original_container(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    task = asyncio.create_task(executor.execute(workspace, ["python", "-c", "import time;print('running',flush=True);time.sleep(60)"],
                                                20, "cancel", profile, 1))
    for _ in range(100):
        operation = workspace.operation("cancel")
        if operation and operation.get("state") == "running":
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Container never started")
    await executor.cancel(workspace, "cancel")
    receipt = await task
    assert receipt.status == "cancelled"
    assert not executor._client().containers.get(receipt.container_id).attrs["State"]["Running"]


async def test_sandbox_completed_container_recovered_after_receipt_loss(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    argv = ["python", "-c", "from pathlib import Path;p=Path('counter');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');print('recorded')"]
    receipt = await executor.execute(workspace, argv, 10, "receipt-loss", profile, 1)
    operation = workspace.operation("receipt-loss")
    assert operation is not None
    operation.pop("receipt")
    operation["state"] = "running"
    workspace.save_operation("receipt-loss", operation)
    workspace.set_active("receipt-loss")
    recovered = await executor.reconcile(workspace, "receipt-loss", 2)
    assert recovered.container_id == receipt.container_id
    assert recovered.recovered and "recorded" in recovered.stdout
    assert workspace.files.read("counter") == b"1"


async def test_sandbox_missing_command_refuses_replay(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    argv = ["python", "-c", "print('once')"]
    receipt = await executor.execute(workspace, argv, 10, "missing", profile, 1)
    operation = workspace.operation("missing")
    assert operation is not None
    operation.pop("receipt")
    workspace.save_operation("missing", operation)
    executor._client().containers.get(receipt.container_id).remove()
    with pytest.raises(NeedsReconciliation, match="missing; refusing replay"):
        await executor.execute(workspace, argv, 10, "missing", profile, 2)


async def test_sandbox_actual_worker_exit_adopts_original_live_job(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    child_script = """import asyncio,json,os,sys
from pathlib import Path
from forge.domain.types import RepoProfile
from forge.execution.workspace import Workspace
from forge.execution.docker import DockerExecutor
workspace=Workspace(Path(sys.argv[1]))
executor=DockerExecutor(docker_host=sys.argv[2],allow_rootful=sys.argv[3]=='1')
profile=RepoProfile.model_validate_json(sys.argv[4])
save=workspace.save_operation
def crash_after_launch(operation_id,value):
    save(operation_id,value)
    if value.get('state')=='running': os._exit(29)
workspace.save_operation=crash_after_launch
argv=['python','-c',"import time;from pathlib import Path;time.sleep(1);p=Path('once');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');print('survived observer')"]
asyncio.run(executor.execute(workspace,argv,15,'process-crash',profile,1))
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", child_script, str(workspace.root), str(executor.docker_host),
        "1" if executor.allow_rootful else "0", profile.model_dump_json(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(child.communicate(), timeout=15)
    assert child.returncode == 29, stderr.decode()
    operation = workspace.operation("process-crash")
    assert operation is not None and operation["state"] == "running" and "receipt" not in operation
    container_id = operation["container_id"]
    workspace.fence(2)
    with pytest.raises(NeedsReconciliation, match="reconciled first"):
        await executor.execute(workspace, ["python", "-c", "print('must not start')"], 10, "duplicate", profile, 2)
    recovered = await executor.reconcile(workspace, "process-crash", 2)
    assert recovered.recovered and recovered.container_id == container_id
    assert recovered.status == "ok" and "survived observer" in recovered.stdout
    assert workspace.files.read("once") == b"1"


async def test_sandbox_private_subordinate_uid_file_is_not_silently_skipped(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    if not executor._rootless:
        pytest.skip("Subordinate-UID file ownership requires a real rootless daemon")
    argv = ["python", "-c", "from pathlib import Path;p=Path('unreadable');p.write_text('private');p.chmod(0o600)"]
    try:
        with pytest.raises(NeedsReconciliation, match="fully inventoried"):
            await executor.execute(workspace, argv, 10, "private", profile, 1)
        assert workspace.active_operation() == "private"
    finally:
        # The host owner can unlink a subordinate-UID file from the shared
        # workspace directory without reading or accepting it as valid output.
        (workspace.path / "unreadable").unlink(missing_ok=True)


async def test_sandbox_memory_cgroup_kills_only_oversized_job(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    profile = profile.model_copy(update={"memory_bytes": 64 * 1024 * 1024})
    receipt = await executor.execute(
        workspace, ["python", "-c", "allocated = bytearray(256 * 1024 * 1024);print(len(allocated))"],
        10, "memory-limit", profile, 1,
    )
    assert receipt.status == "error" and receipt.exit_code == 137
    assert receipt.resource_statistics["oom_killed"] is True
    assert not executor._client().containers.get(receipt.container_id).attrs["State"]["Running"]


async def test_sandbox_pid_cgroup_refuses_bounded_child_spawns(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    profile = profile.model_copy(update={"pid_limit": 16, "memory_bytes": 256 * 1024 * 1024})
    script = """import errno,json,subprocess
children=[]
denied=False
try:
    for _ in range(24):
        try: children.append(subprocess.Popen(['python','-c','import time;time.sleep(5)']))
        except OSError as error:
            if error.errno != errno.EAGAIN: raise
            denied=True
            break
    print(json.dumps({'spawned':len(children),'denied':denied}))
finally:
    for child in children: child.terminate()
    for child in children: child.wait()
"""
    receipt = await executor.execute(workspace, ["python", "-c", script], 10, "pid-limit", profile, 1)
    assert receipt.status == "ok", receipt.stderr
    result = json.loads(receipt.stdout)
    assert result["denied"] is True
    assert 0 < result["spawned"] < profile.pid_limit


async def test_sandbox_cpu_cgroup_observably_throttles_job(sandbox: tuple[Workspace, DockerExecutor, RepoProfile]) -> None:
    workspace, executor, profile = sandbox
    profile = profile.model_copy(update={"cpu_limit": 0.25})
    script = """import json,time
from pathlib import Path
def stats(): return dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
before=stats()
wall=time.monotonic()
cpu=time.process_time()
while time.process_time()-cpu < .3: pass
after=stats()
print(json.dumps({'cpu_seconds':time.process_time()-cpu,'wall_seconds':time.monotonic()-wall,
                  'throttled_periods':int(after['nr_throttled'])-int(before['nr_throttled']),
                  'cpu_max':Path('/sys/fs/cgroup/cpu.max').read_text().strip()}))
"""
    receipt = await executor.execute(workspace, ["python", "-c", script], 10, "cpu-limit", profile, 1)
    assert receipt.status == "ok", receipt.stderr
    result = json.loads(receipt.stdout)
    quota, period = map(int, result["cpu_max"].split())
    assert quota / period == profile.cpu_limit
    assert result["throttled_periods"] > 0
    assert result["wall_seconds"] > result["cpu_seconds"] * 2
