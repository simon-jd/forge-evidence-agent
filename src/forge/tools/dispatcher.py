from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import subprocess
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from forge.artifacts.store import LocalArtifactStore
from forge.domain.types import (
    EvidenceAnchor,
    RepoProfile,
    ToolCall,
    ToolResult,
    ToolSpec,
    canonical_hash,
)
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation, UnsafePath
from forge.execution.safe_files import relative_parts
from forge.execution.workspace import Workspace
from forge.retrieval.code import FileSnapshot
from forge.retrieval.symbols import SymbolIndex
from forge.tools.patching import prepare_patch


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STRING = {"type": "string", "minLength": 1, "maxLength": 4096}
PATH = {"type": "string", "minLength": 1, "maxLength": 512}
SPECS = [
    ToolSpec(name="read_file", description="Read current UTF-8 file lines with hashes, or reread an artifact belonging to this run.", read_only=True,
             parameters={**_object({"relative_path": PATH,
                                     "artifact_ref": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                                     "start_line": {"type": "integer", "minimum": 1},
                                     "end_line": {"type": "integer", "minimum": 1}}, []),
                         "oneOf": [{"required": ["relative_path"]}, {"required": ["artifact_ref"]}]}),
    ToolSpec(name="list_files", description="List indexed repository files, excluding caches and control directories.", read_only=True,
             parameters=_object({"relative_path": PATH, "glob": STRING,
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, [])),
    ToolSpec(name="search_code", description="Search repository text with ripgrep; results include content hashes.", read_only=True,
             parameters=_object({"query": STRING, "path_glob": STRING, "mode": {"type": "string", "enum": ["text", "symbol"]},
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"])),
    ToolSpec(name="apply_patch", description="Apply a unified diff after validating every expected file hash (null for new files).",
             parameters=_object({"patch": {"type": "string", "minLength": 1, "maxLength": 2_097_152},
                                 "expected_file_hashes": {"type": "object", "minProperties": 1,
                                    "additionalProperties": {"anyOf": [{"type": "string", "pattern": "^[0-9a-f]{64}$"}, {"type": "null"}]}}},
                                ["patch", "expected_file_hashes"])),
    ToolSpec(name="run_command", description="Execute argv only inside the isolated task container. Full retained logs become artifacts.",
             parameters=_object({"argv": {"type": "array", "items": {"type": "string", "maxLength": 32768}, "minItems": 1, "maxItems": 128},
                                 "cwd": PATH, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800}}, ["argv"])),
    ToolSpec(name="get_diff", description="Export the complete current patch, including added and deleted files.", read_only=True,
             parameters=_object({"paths": {"type": "array", "items": PATH, "maxItems": 1000}}, [])),
    ToolSpec(name="finish", description="Propose task completion; independent validation still determines success.", read_only=True,
             parameters=_object({"summary": STRING,
                                 "claimed_changes": {"type": "array", "items": STRING, "maxItems": 100}}, ["summary"])),
]


class ToolDispatcher:
    def __init__(
        self, workspace: Workspace, profile: RepoProfile, executor: DockerExecutor,
        artifacts: LocalArtifactStore,
    ) -> None:
        self.workspace, self.profile = workspace, profile
        self.executor, self.artifacts = executor, artifacts
        self._specs = {spec.name: spec for spec in SPECS}
        self.symbols = SymbolIndex(workspace.path, snapshot_id=workspace.base_commit)

    def specs(self) -> list[ToolSpec]:
        return [spec.model_copy(deep=True) for spec in SPECS]

    def validate(self, call: ToolCall) -> None:
        if call.name not in self._specs:
            raise ValueError(f"Unknown tool: {call.name}")
        Draft202012Validator(self._specs[call.name].parameters).validate(call.arguments)

    async def execute(self, call: ToolCall, operation_id: str, epoch: int) -> ToolResult:
        try:
            self.validate(call)
            if call.name == "run_command":
                return await self._command(call, operation_id, epoch)
            # Shared reads may run concurrently; every write and every command
            # obtains the same cross-process exclusive lock.
            async with self.workspace.lock(shared=self._specs[call.name].read_only):
                self.workspace.assert_epoch(epoch)
                self.workspace.require_idle(operation_id)
                previous = self.workspace.operation(operation_id)
                args_hash = canonical_hash({"name": call.name, "arguments": call.arguments, "call_id": call.call_id})
                if previous is not None:
                    if previous.get("args_hash") != args_hash:
                        raise NeedsReconciliation("Operation ID reused with changed tool arguments")
                    if previous.get("result") is not None:
                        return ToolResult.model_validate(previous["result"])
                    if call.name == "apply_patch":
                        return self._recover_patch(call, operation_id, previous)
                    raise NeedsReconciliation("Tool result is not durably available")
                if call.name == "apply_patch":
                    return self._apply_patch(call, operation_id, epoch, args_hash)
                result = await asyncio.to_thread(self._read_tool, call)
                self.workspace.save_operation(operation_id, {
                    "kind": call.name, "operation_id": operation_id, "epoch": epoch,
                    "args_hash": args_hash, "state": "completed", "result": result.model_dump(mode="json"),
                })
                return result
        except UnsafePath as error:
            return ToolResult(call_id=call.call_id, status="denied", summary=str(error),
                              workspace_revision=self.workspace.revision)
        except (ValueError, ValidationError, FileNotFoundError, UnicodeDecodeError) as error:
            return ToolResult(call_id=call.call_id, status="error", summary=str(error)[:2000],
                              workspace_revision=self.workspace.revision)

    def _result(self, call: ToolCall, summary: str, **kwargs: Any) -> ToolResult:
        return ToolResult(call_id=call.call_id, status="ok", summary=summary,
                          workspace_revision=self.workspace.revision, **kwargs)

    def _read_tool(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        if call.name == "read_file":
            is_artifact = "artifact_ref" in args
            path = args.get("relative_path", "artifact:" + args.get("artifact_ref", ""))
            if is_artifact:
                metadata = self.artifacts.metadata(args["artifact_ref"])
                if metadata["run_id"] != self.workspace.run_id:
                    raise UnsafePath("Artifact belongs to a different run")
                data = self.artifacts.read_bytes(args["artifact_ref"])
            else:
                data = self.workspace.files.read(path)
            if b"\x00" in data:
                raise ValueError("read_file accepts text files only")
            text = data.decode()
            sha = hashlib.sha256(data).hexdigest()
            lines = text.splitlines()
            start, end = args.get("start_line", 1), args.get("end_line", len(lines) or 1)
            if end < start:
                raise ValueError("end_line must be at least start_line")
            if start > max(1, len(lines)):
                raise ValueError("start_line is beyond the available content")
            selected = lines[start - 1:min(end, start + 399)]
            excerpt = "\n".join(selected)
            truncated = end > start + 399 or len(excerpt) > 24_000
            excerpt = excerpt[:24_000]
            artifact = args["artifact_ref"] if is_artifact else self.artifacts.put_text(self.workspace.run_id, "file_read", text)
            actual_end = max(start, start + len(excerpt.splitlines()) - 1)
            evidence = FileSnapshot(path=path, sha256=sha, text=text).anchor(start, actual_end, call.call_id)
            numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(excerpt.splitlines()))
            return self._result(call, f"{path} sha256={sha}\n{numbered}", artifact_refs=[artifact],
                                truncated=truncated, evidence=[] if is_artifact else [evidence])
        if call.name == "list_files":
            relative = args.get("relative_path", ".")
            relative_parts(relative, allow_root=True)
            with self.workspace.files.directory(relative_parts(relative, allow_root=True)):
                pass
            pattern, limit = args.get("glob", "**"), args.get("limit", 200)
            files = [path for path in self.workspace.files.files()
                     if (relative == "." or path.startswith(relative + "/")) and self._glob(path, pattern)]
            artifact = self.artifacts.put_text(self.workspace.run_id, "file_listing", "\n".join(files))
            return self._result(call, "\n".join(files[:limit]), artifact_refs=[artifact], truncated=len(files) > limit)
        if call.name == "search_code":
            return self._search(call)
        if call.name == "get_diff":
            diff = self.workspace.get_diff(args.get("paths"))
            artifact = self.artifacts.put_text(self.workspace.run_id, "patch", diff)
            return self._result(call, diff[:24_000] or "No changes", artifact_refs=[artifact], truncated=len(diff) > 24_000)
        if call.name == "finish":
            return self._result(call, json.dumps({"candidate_only": True, **args}, ensure_ascii=False))
        raise ValueError("Unsupported read tool")

    @staticmethod
    def _glob(path: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))

    def _search(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        limit = args.get("limit", 30)
        if args.get("mode", "text") == "symbol":
            found = self.symbols.find(args["query"], limit=limit)
            symbol_evidence = []
            symbol_matches = []
            for symbol in found:
                if not self._glob(symbol.evidence.path, args.get("path_glob", "**")):
                    continue
                anchor = symbol.evidence.model_copy(update={
                    "origin_call_id": call.call_id,
                    "evidence_id": canonical_hash([symbol.evidence.evidence_id, call.call_id]),
                })
                symbol_evidence.append(anchor)
                symbol_matches.append({"name": symbol.qualified_name, "kind": symbol.kind,
                    "path": anchor.path, "start_line": anchor.start_line, "end_line": anchor.end_line,
                    "sha256": anchor.sha256, "excerpt": anchor.excerpt[:4000]})
            payload = json.dumps({"mode": "symbol", "matches": symbol_matches,
                                   "parser_version": self.symbols.parser_version,
                                   "truncated": len(found) == limit}, ensure_ascii=False)
            artifact = self.artifacts.put_text(self.workspace.run_id, "search_results", payload)
            return self._result(call, payload, artifact_refs=[artifact], evidence=symbol_evidence, truncated=len(found) == limit)
        matches: list[dict[str, Any]] = []
        evidence: list[EvidenceAnchor] = []
        scanned = 0
        truncated = False
        for path in self.workspace.files.files():
            if not self._glob(path, args.get("path_glob", "**")):
                continue
            data = self.workspace.files.read(path)
            scanned += len(data)
            if scanned > 64 * 1024 * 1024:
                truncated = True
                break
            if b"\x00" in data:
                continue
            try:
                text = data.decode()
            except UnicodeDecodeError:
                continue
            # Feeding secured bytes on stdin prevents rg from following any
            # file path, configuration or symlink outside the workspace.
            completed = subprocess.run(
                ["rg", "--json", "--no-config", "--max-count", str(limit + 1), "--regexp", args["query"]],
                input=text.encode(), capture_output=True, timeout=5, check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"},
            )
            if completed.returncode not in (0, 1):
                raise ValueError("Invalid search expression: " + completed.stderr.decode(errors="replace")[:1000])
            sha = hashlib.sha256(data).hexdigest()
            for raw in completed.stdout.splitlines():
                event = json.loads(raw)
                if event["type"] != "match":
                    continue
                line = event["data"]["line_number"]
                excerpt = event["data"]["lines"].get("text", "").rstrip("\n")[:2000]
                if len(matches) == limit:
                    truncated = True
                    break
                matches.append({"path": path, "line": line, "text": excerpt, "sha256": sha})
                evidence.append(FileSnapshot(path=path, sha256=sha, text=text).anchor(line, line, call.call_id))
            if truncated:
                break
        payload = json.dumps({"matches": matches, "truncated": truncated, "scanned_bytes": scanned}, ensure_ascii=False)
        artifact = self.artifacts.put_text(self.workspace.run_id, "search_results", payload)
        return self._result(call, payload, artifact_refs=[artifact], evidence=evidence, truncated=truncated)

    def _apply_patch(self, call: ToolCall, operation_id: str, epoch: int, args_hash: str) -> ToolResult:
        changes = prepare_patch(self.workspace.files, call.arguments["patch"], call.arguments["expected_file_hashes"], self.profile)
        manifest = [{"path": change.path, "before_hash": change.before_hash, "after_hash": change.after_hash}
                    for change in changes]
        patch_ref = self.artifacts.put_text(self.workspace.run_id, "applied_patch", call.arguments["patch"])
        operation = {"kind": "patch", "operation_id": operation_id, "epoch": epoch, "args_hash": args_hash,
                     "state": "planned", "manifest": manifest, "artifact_refs": [patch_ref]}
        self.workspace.save_operation(operation_id, operation)
        self.workspace.set_active(operation_id)
        try:
            for change in changes:
                self.workspace.assert_epoch(epoch)
                # This second check makes the write precondition explicit even
                # if an out-of-band host process modified a file after preflight.
                if self.workspace.files.digest(change.path) != change.before_hash:
                    raise NeedsReconciliation("Workspace changed during patch application")
                if change.after is None:
                    self.workspace.files.remove(change.path)
                else:
                    executable = (self.workspace.path / change.path).is_file() and bool(
                        (self.workspace.path / change.path).stat(follow_symlinks=False).st_mode & 0o111)
                    self.workspace.files.write(change.path, change.after, executable=executable)
            self.workspace.refresh_revision()
        except (OSError, ValueError) as error:
            raise NeedsReconciliation("Patch may be partially applied; inspect the before/after manifest") from error
        return self._finish_patch(call, operation_id, operation, recovered=False)

    def _recover_patch(self, call: ToolCall, operation_id: str, operation: dict[str, Any]) -> ToolResult:
        if operation.get("kind") != "patch":
            raise NeedsReconciliation("Recorded operation is not a patch")
        current = {item["path"]: self.workspace.files.digest(item["path"]) for item in operation["manifest"]}
        if all(current[item["path"]] == item["after_hash"] for item in operation["manifest"]):
            self.workspace.refresh_revision()
            return self._finish_patch(call, operation_id, operation, recovered=True)
        raise NeedsReconciliation("Patch after-state is incomplete or changed; refusing replay")

    def _finish_patch(self, call: ToolCall, operation_id: str, operation: dict[str, Any], *, recovered: bool) -> ToolResult:
        result = self._result(call, json.dumps({"changed_files": operation["manifest"], "recovered": recovered}),
                              artifact_refs=operation["artifact_refs"])
        operation.update(state="completed", result=result.model_dump(mode="json"))
        self.workspace.save_operation(operation_id, operation)
        self.workspace.set_active(None)
        return result

    async def _command(self, call: ToolCall, operation_id: str, epoch: int) -> ToolResult:
        args = call.arguments
        receipt = await self.executor.execute(
            self.workspace, args["argv"], args.get("timeout_seconds", self.profile.command_timeout_seconds),
            operation_id, self.profile, epoch, cwd=args.get("cwd", "."),
        )
        refs = [
            self.artifacts.put_text(self.workspace.run_id, "command_stdout", receipt.stdout),
            self.artifacts.put_text(self.workspace.run_id, "command_stderr", receipt.stderr),
            self.artifacts.put_text(self.workspace.run_id, "command_receipt", receipt.model_dump_json()),
        ]
        summary = json.dumps({
            "exit_code": receipt.exit_code, "status": receipt.status,
            "stdout": receipt.stdout[:12_000], "stderr": receipt.stderr[:8000],
            "stdout_bytes": receipt.stdout_bytes, "stderr_bytes": receipt.stderr_bytes,
            "log_retention_bytes": receipt.log_retention_bytes,
        }, ensure_ascii=False)
        return ToolResult(call_id=call.call_id, status=receipt.status, summary=summary,
                          artifact_refs=refs, workspace_revision=receipt.workspace_revision,
                          exit_code=receipt.exit_code,
                          truncated=receipt.truncated or len(receipt.stdout) > 12_000 or len(receipt.stderr) > 8000)
