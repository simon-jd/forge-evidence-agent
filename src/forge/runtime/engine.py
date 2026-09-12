from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select

from forge.artifacts import LocalArtifactStore
from forge.context import (
    BudgetManager,
    ContextBuilder,
    EvidenceLedger,
    RuleRouter,
    ValidationEvidence,
)
from forge.context.budget import BudgetExceeded
from forge.context.builder import ContextBudgetExceeded
from forge.domain.types import (
    CanonicalMessage,
    CompactRequest,
    CompactResult,
    ContextBundle,
    EvidenceAnchor,
    ModelRequest,
    ModelSpec,
    ModelTurn,
    NativeState,
    ProviderError,
    RepoProfile,
    RunConfig,
    TextDelta,
    TextPart,
    TokenUsage,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
    TurnCompleted,
    canonical_hash,
)
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation, SandboxUnavailable, StaleEpoch
from forge.execution.workspace import Workspace, WorkspaceManager
from forge.persistence import LeaseLost, Repository
from forge.persistence.models import (
    ApprovalRow,
    ArtifactRow,
    MessageRow,
    ModelAttemptRow,
    ModelTurnRow,
    ToolRow,
)
from forge.providers import ProviderAdapter, create_provider
from forge.runtime.verification import CandidateVerifier
from forge.settings import Settings
from forge.telemetry import Telemetry
from forge.tools import ToolDispatcher


class GraphState(TypedDict):
    run_id: str
    next: str


ProviderFactory = Callable[[ModelSpec, Callable[[dict[str, Any]], Awaitable[None]], int], ProviderAdapter]
FaultHook = Callable[[str, str], Awaitable[None]]
NODES = ("bootstrap", "baseline", "context", "model", "gate", "execute", "ingest", "verify", "finalize")


class ApprovalExpired(RuntimeError):
    pass


class Runtime:
    def __init__(self, repository: Repository, workspaces: WorkspaceManager,
                 executor: DockerExecutor, artifacts: LocalArtifactStore, *,
                 epoch: int, provider_factory: ProviderFactory | None = None,
                 telemetry: Telemetry | None = None, fault_hook: FaultHook | None = None,
                 settings: Settings | None = None) -> None:
        self.repo, self.workspaces, self.executor, self.artifacts = repository, workspaces, executor, artifacts
        self.epoch, self.provider_factory, self.telemetry, self.fault_hook = epoch, provider_factory, telemetry, fault_hook
        self.verifier = CandidateVerifier(workspaces, executor, artifacts)
        self.settings = settings or Settings()
        self._dispatchers: dict[str, ToolDispatcher] = {}

    async def fault(self, point: str, run_id: str) -> None:
        if self.fault_hook:
            await self.fault_hook(point, run_id)

    def graph(self, checkpointer: Any) -> Any:
        graph = StateGraph(GraphState)
        for name in NODES:
            async def node(state: GraphState, stage: str = name) -> GraphState:
                return await self.step(state["run_id"], stage)
            graph.add_node(name, node)
            graph.add_conditional_edges(name, lambda state: state["next"],
                                        {**{item: item for item in NODES}, "end": END})
        graph.add_edge(START, "bootstrap")
        return graph.compile(checkpointer=checkpointer)

    async def step(self, run_id: str, stage: str) -> GraphState:
        run = await self.repo.get_run(run_id)
        if run["status"] != "running":
            return {"run_id": run_id, "next": "end"}
        # Graph replay can lag a committed business operation. The next stage
        # comes from the ledger, never from re-running its external effect.
        if run["stage"] != stage:
            return {"run_id": run_id, "next": run["stage"]}
        try:
            async with self.repo.locked(run_id, epoch=self.epoch):
                pass
            if run["cancel_requested"]:
                await self._cancel(run)
            else:
                if stage != "bootstrap":
                    BudgetManager(RunConfig.model_validate(run["config"]).budget,
                                  run["state"].get("budget")).check_runtime()
                handler = getattr(self, "_" + stage)
                if self.telemetry:
                    with self.telemetry.span(stage, run_id=run_id, epoch=self.epoch,
                                             iteration=run["state"].get("iteration", 0)):
                        await handler(run)
                else:
                    await handler(run)
        except ApprovalExpired:
            current = await self.repo.get_run(run_id)
            await self._save(run_id, current["state"], "gate", [("approval.expired", {})])
        except (NeedsReconciliation, StaleEpoch) as error:
            if isinstance(error, StaleEpoch):
                raise LeaseLost(str(error)) from error
            await self._stop(run_id, "needs_reconciliation", str(error))
        except (BudgetExceeded, ContextBudgetExceeded) as error:
            await self._stop(run_id, "budget_exhausted", str(error))
        except SandboxUnavailable as error:
            await self._stop(run_id, "failed", str(error), failure_class="infrastructure")
        current = await self.repo.get_run(run_id)
        return {"run_id": run_id, "next": current["stage"] if current["status"] == "running" else "end"}

    async def _save(self, run_id: str, state: dict[str, Any], stage: str,
                    events: list[tuple[str, dict[str, Any]]] | None = None,
                    refs: list[str] | None = None) -> None:
        async with self.repo.locked(run_id, epoch=self.epoch) as (session, row):
            row.state = copy.deepcopy(state)
            if row.stage != stage:
                row.stage = stage
                await self.repo.event(session, row, "stage.changed", {"stage": stage})
            for kind, payload in events or []:
                await self.repo.event(session, row, kind, payload)
            for ref in refs or []:
                metadata = self.artifacts.metadata(ref)
                if metadata["run_id"] != run_id:
                    raise ValueError("Artifact must be explicitly copied into its parent run before registration")
                if await session.get(ArtifactRow, ref) is None:
                    session.add(ArtifactRow(id=ref, run_id=run_id, owner_id=row.owner_id,
                                            kind=metadata["kind"], details=metadata))

    def _workspace(self, run: dict[str, Any]) -> Workspace:
        workspace = self.workspaces.load(run["id"])
        workspace.fence(self.epoch)
        return workspace

    def _dispatcher(self, run: dict[str, Any]) -> ToolDispatcher:
        if run["id"] not in self._dispatchers:
            self._dispatchers[run["id"]] = ToolDispatcher(self._workspace(run), RepoProfile.model_validate(run["profile"]),
                                                        self.executor, self.artifacts)
        return self._dispatchers[run["id"]]

    async def _bootstrap(self, run: dict[str, Any]) -> None:
        workspace = await asyncio.to_thread(self.workspaces.prepare, run["id"], Path(run["source"]), run["base_commit"])
        workspace.fence(self.epoch)
        config = RunConfig.model_validate(run["config"])
        state = {"iteration": 0, "transcript": [], "evidence": [], "validations": [],
                 "constraints": [], "budget": BudgetManager(config.budget).state.model_dump(mode="json"),
                 "router": RuleRouter(config).state.model_dump(mode="json"), "cursor": 0,
                 "consecutive_failures": 0, "invalid_tool_calls": 0, "consecutive_invalid_tool_calls": 0,
                 "stalled_turns": 0,
                 "workspace_revision": workspace.revision, "last_snapshot": canonical_hash(workspace.snapshot()),
                 "used_call_ids": [], "native_cursor": 0}
        await self._save(run["id"], state, "baseline")

    async def _baseline(self, run: dict[str, Any]) -> None:
        result = await self.verifier.verify(run_id=run["id"], source=Path(run["source"]),
            base_commit=run["base_commit"], candidate=self._workspace(run),
            profile=RepoProfile.model_validate(run["profile"]), epoch=self.epoch, baseline=True)
        state = copy.deepcopy(run["state"])
        state["baseline"] = result.model_dump(mode="json")
        state["transcript"].append(CanonicalMessage.text("user", "Platform baseline validation:\n" + result.summary).model_dump(mode="json"))
        await self._save(run["id"], state, "context", [("validation.finished", {
            "kind": "baseline", **result.model_dump(mode="json")})], result.artifact_refs)

    async def _context(self, run: dict[str, Any]) -> None:
        # Applying supplemental messages and preserving their exact text is one
        # transaction. A node restart cannot append the same instruction twice.
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            state = copy.deepcopy(row.state)
            messages = await session.scalars(select(MessageRow).where(
                MessageRow.run_id == row.id, MessageRow.applied_iteration.is_(None)).order_by(MessageRow.created_at, MessageRow.id))
            for message in messages:
                state["constraints"].append(message.text)
                state["transcript"].append(CanonicalMessage.text("user", message.text).model_dump(mode="json"))
                message.applied_iteration = state["iteration"]
                await self.repo.event(session, row, "message.applied", {"message_id": message.id,
                                                                      "iteration": state["iteration"]})
            row.state = state
        workspace = self._workspace(run)
        config = RunConfig.model_validate(run["config"])
        evidence = EvidenceLedger(workspace.path, [EvidenceAnchor.model_validate(item) for item in state["evidence"]])
        state["evidence"] = [item.model_dump(mode="json") for item in evidence.refresh()]
        transcript = [CanonicalMessage.model_validate(item) for item in state["transcript"]]
        history = self.artifacts.put_text(run["id"], "transcript", json.dumps(state["transcript"]))
        snapshot_hash = canonical_hash(workspace.snapshot())
        bundle = ContextBuilder(evidence_guard=config.context.evidence_guard).build(
            task=run["task"], constraints=state["constraints"], transcript=transcript,
            evidence=evidence.anchors, workspace=workspace.path, config=config.context,
            tools=self._dispatcher(run).specs(), output_reserve=config.budget.max_output_tokens,
            history_artifact_ref=history,
            validation_evidence=[ValidationEvidence.model_validate(item) for item in state["validations"]],
            workspace_revision=workspace.revision, workspace_snapshot_sha256=snapshot_hash,
            profile_sha256=canonical_hash(run["profile"]),
        )
        manager = BudgetManager(config.budget, state["budget"])
        router = RuleRouter(config, state["router"])
        route = router.select(manager, bundle.token_estimate, config.budget.max_output_tokens,
            consecutive_failures=state["consecutive_failures"],
            invalid_tool_calls=state.get("consecutive_invalid_tool_calls", 0),
            context_exceeded=state.pop("context_exceeded", False))
        state["router"] = router.state.model_dump(mode="json")
        state["selected_model"] = route.model_index
        state["context"] = bundle.model_dump(mode="json")
        state["workspace_revision"] = workspace.revision
        context_ref = self.artifacts.put_text(run["id"], "context_snapshot", bundle.model_dump_json())
        state["context_ref"] = context_ref
        events = [("context.built", {"context_hash": bundle.context_hash, "token_breakdown": bundle.token_breakdown,
                                      "token_count_kind": bundle.token_count_kind, "artifact_ref": context_ref})]
        if bundle.compacted:
            events.append(("context.compacted", {"strategy": "portable_summary", "context_ref": context_ref}))
        if route.escalated:
            state["native_state"] = None
            events.append(("model.switched", route.model_dump(mode="json")))
        await self._save(run["id"], state, "model", events, [history, context_ref])

    def _provider(self, run: dict[str, Any], spec: ModelSpec,
                  sink: Callable[[dict[str, Any]], Awaitable[None]]) -> ProviderAdapter:
        iteration = run["state"]["iteration"]
        if self.provider_factory:
            return self.provider_factory(spec, sink, iteration)
        scripts = run["manifest"].get("scripted_turns")
        scripted = [ModelTurn.model_validate(item) for item in scripts[iteration:]] if scripts is not None else None
        secret = {"openai": self.settings.openai_api_key, "anthropic": self.settings.anthropic_api_key,
                  "deepseek": self.settings.deepseek_api_key}.get(spec.provider)
        return create_provider(spec, scripted_turns=scripted if spec.provider == "fake" else None,
                               attempt_sink=sink, api_key=secret.get_secret_value() if secret else None)

    async def _attempt(self, run_id: str, iteration: int, spec: ModelSpec,
                       input_tokens: int, output_tokens: int, payload: dict[str, Any]) -> None:
        async with self.repo.locked(run_id, epoch=self.epoch) as (session, run):
            state = copy.deepcopy(run.state)
            manager = BudgetManager(RunConfig.model_validate(run.config).budget, state["budget"])
            attempt_id = payload["attempt_id"]
            if payload["status"] == "started":
                if run.cancel_requested:
                    raise asyncio.CancelledError
                manager.reserve(attempt_id, spec, input_tokens, output_tokens)
                session.add(ModelAttemptRow(id=attempt_id, run_id=run_id, iteration=iteration,
                    status="started", payload=payload, created_at=await self.repo.now(session)))
                await self.repo.event(session, run, "model.started", {"attempt_id": attempt_id,
                    "provider": spec.provider, "model_id": spec.model_id, "iteration": iteration})
            else:
                usage = TokenUsage.model_validate(payload.get("usage", {}))
                if payload["status"] != "completed" and not payload.get("usage_final", False) and (usage.input_tokens is not None or usage.output_tokens is not None):
                    usage = usage.model_copy(update={"estimated": True})
                manager.reconcile(attempt_id, usage)
                attempt = await session.get(ModelAttemptRow, attempt_id)
                if attempt is None:
                    raise ValueError("Provider ended an attempt that was never reserved")
                attempt.status, attempt.payload = payload["status"], payload
            state["budget"] = manager.state.model_dump(mode="json")
            run.state = state

    async def _model(self, run: dict[str, Any]) -> None:
        state = copy.deepcopy(run["state"])
        iteration = state["iteration"]
        config = RunConfig.model_validate(run["config"])
        spec = config.models[state["selected_model"]]
        bundle = ContextBundle.model_validate(state["context"])
        native = None
        messages = bundle.messages
        input_tokens = bundle.token_estimate
        if config.context.continuation_mode == "provider_native" and state.get("native_state"):
            previous = NativeState.model_validate(state["native_state"])
            if (previous.provider == spec.provider and previous.model_id == spec.model_id
                    and not bundle.stale_evidence_ids and not bundle.compacted):
                native = previous
                messages = [CanonicalMessage.model_validate(item) for item in state["transcript"][state["native_cursor"]:]]
                input_tokens = len(native.model_dump_json().encode()) + len(json.dumps([item.model_dump(mode="json") for item in messages]).encode()) + bundle.token_breakdown.get("tools", 0) + 512
        request = ModelRequest(run_id=run["id"], turn_id=f"{run['id']}:{iteration}", model_id=spec.model_id,
            canonical_messages=messages, native_state=native, tools=self._dispatcher(run).specs(),
            sampling_config=config.sampling_config,
            output_limit=config.budget.max_output_tokens,
            deadline_seconds=min(120, config.budget.max_runtime_seconds))
        async with self.repo.sessions() as session:
            recorded = await session.get(ModelTurnRow, (run["id"], iteration))
            if recorded is not None and recorded.status == "completed":
                turn = ModelTurn.model_validate(recorded.response)
                await self._accept_turn(run, turn)
                return
            if recorded is not None and recorded.status == "started":
                # A worker crashed during sampling. An unconfirmed model request
                # has no tools to replay, but its spend remains occupied. A new
                # bounded attempt is visible in the same logical turn ledger.
                request = ModelRequest.model_validate(recorded.request)
        async def sink(payload: dict[str, Any]) -> None:
            await self._attempt(run["id"], iteration, spec, input_tokens, request.output_limit, payload)
        provider = self._provider(run, spec, sink)
        if native is not None and input_tokens > int(config.context.input_budget * config.context.compact_threshold):
            if spec.capabilities.native_compaction:
                compacted = await self._compact(run, spec, request, provider, input_tokens)
                request.native_state, request.canonical_messages = compacted.native_state, []
                input_tokens = len(compacted.native_state.model_dump_json().encode()) + bundle.token_breakdown.get("tools", 0) + 512
            else:
                request.native_state, request.canonical_messages = None, bundle.messages
                input_tokens = bundle.token_estimate
        wire_bound = getattr(provider, "input_token_bound", None)
        if wire_bound is not None:
            input_tokens = max(input_tokens, wire_bound(request))
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            recorded = await session.get(ModelTurnRow, (run["id"], iteration))
            if recorded is None:
                session.add(ModelTurnRow(run_id=run["id"], iteration=iteration,
                    request=request.model_dump(mode="json"), status="started", created_at=await self.repo.now(session)))
            else:
                recorded.request = request.model_dump(mode="json")
        completed: ModelTurn | None = None
        failure: ProviderError | None = None
        buffer = ""
        async def flush() -> None:
            nonlocal buffer
            while buffer:
                chunk, buffer = buffer[:2048], buffer[2048:]
                async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                    await self.repo.event(session, row, "text.delta", {"iteration": iteration, "text": chunk})
        try:
            async for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    buffer += event.text
                    if len(buffer) >= 2048:
                        await flush()
                elif isinstance(event, TurnCompleted):
                    completed = event.turn
                elif isinstance(event, ProviderError):
                    failure = event
                # ToolCallReady is a preview. No execution until a complete
                # ModelTurn has been durably recorded below.
            await flush()
        finally:
            close = getattr(provider, "aclose", None)
            if close:
                await close()
        if completed is None:
            await self._model_error(run, failure or ProviderError(code="stream_interrupted", message="Unconfirmed stream"))
            return
        raw_ref = self.artifacts.put_text(run["id"], "model_response", completed.model_dump_json())
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            recorded = await session.get(ModelTurnRow, (run["id"], iteration))
            assert recorded is not None
            recorded.status, recorded.response, recorded.artifact_ref = "completed", completed.model_dump(mode="json"), raw_ref
            session.add(ArtifactRow(id=raw_ref, run_id=run["id"], owner_id=row.owner_id,
                                    kind="model_response", details=self.artifacts.metadata(raw_ref)))
        await self.fault("model_recorded_before_checkpoint", run["id"])
        await self._accept_turn(await self.repo.get_run(run["id"]), completed)

    async def _compact(self, run: dict[str, Any], spec: ModelSpec, request: ModelRequest,
                       provider: ProviderAdapter, input_tokens: int) -> CompactResult:
        iteration = -(run["state"]["iteration"] + 1)
        attempt_id = f"{run['id']}-compact-{abs(iteration)}"
        async with self.repo.sessions() as session:
            recorded = await session.get(ModelTurnRow, (run["id"], iteration))
            if recorded is not None:
                if recorded.status == "completed":
                    return CompactResult.model_validate(recorded.response)
                raise NeedsReconciliation("Native compaction result unknown; its cost is still reserved")
        compact_request = CompactRequest(model_id=spec.model_id, native_state=request.native_state,
                                         canonical_messages=request.canonical_messages)
        payload = {"attempt_id": attempt_id, "status": "started", "kind": "native_compaction"}
        await self._attempt(run["id"], iteration, spec, input_tokens, request.output_limit, payload)
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            session.add(ModelTurnRow(run_id=run["id"], iteration=iteration,
                request=compact_request.model_dump(mode="json"), status="started", created_at=await self.repo.now(session)))
        result = await provider.compact(compact_request)
        await self._attempt(run["id"], iteration, spec, input_tokens, request.output_limit,
                            {**payload, "status": "completed", "usage": result.usage.model_dump(mode="json")})
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            recorded = await session.get(ModelTurnRow, (run["id"], iteration))
            assert recorded is not None
            recorded.status, recorded.response = "completed", result.model_dump(mode="json")
            await self.repo.event(session, row, "context.compacted", {"strategy": "provider_native", "attempt_id": attempt_id})
        return result

    async def _accept_turn(self, run: dict[str, Any], turn: ModelTurn) -> None:
        state = copy.deepcopy(run["state"])
        ids = [call.call_id for call in turn.complete_tool_calls]
        if len(ids) != len(set(ids)) or set(ids) & set(state["used_call_ids"]):
            await self._stop(run["id"], "failed", "Provider returned reused tool call IDs", failure_class="protocol")
            return
        parts: list[Any] = [TextPart(text=turn.visible_text)] if turn.visible_text else []
        parts.extend(ToolCallPart(call=call) for call in turn.complete_tool_calls)
        if parts:
            state["transcript"].append(CanonicalMessage(role="assistant", parts=parts).model_dump(mode="json"))
        state["used_call_ids"].extend(ids)
        state["consecutive_invalid_tool_calls"] = 0
        state["native_state"] = turn.native_state.model_dump(mode="json") if turn.native_state else None
        state["native_cursor"] = len(state["transcript"])
        state["pending_calls"] = [call.model_dump(mode="json") for call in turn.complete_tool_calls]
        state["cursor"], state["tool_results"] = 0, []
        state["finish_requested"] = not bool(turn.complete_tool_calls)
        await self._save(run["id"], state, "gate" if turn.complete_tool_calls else "verify")

    async def _model_error(self, run: dict[str, Any], error: ProviderError) -> None:
        fresh = await self.repo.get_run(run["id"])
        state = copy.deepcopy(fresh["state"])
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            recorded = await session.get(ModelTurnRow, (run["id"], state["iteration"]))
            if recorded is not None:
                recorded.status, recorded.response = "failed", {"error": error.model_dump(mode="json")}
            await self.repo.event(session, row, "model.failed", error.model_dump(mode="json"))
        if error.code == "invalid_tool_arguments":
            state["iteration"] += 1
            state["invalid_tool_calls"] += 1
            state["consecutive_invalid_tool_calls"] = state.get("consecutive_invalid_tool_calls", 0) + 1
            state["native_state"] = None
            state["transcript"].append(CanonicalMessage.text("user",
                "Platform feedback: a generated tool call failed validation and no tools from that response ran. "
                + error.message + " Correct the call using the declared tool schema.").model_dump(mode="json"))
            await self._save(run["id"], state, "context")
        elif error.code == "context_exceeded":
            state["iteration"] += 1
            state["context_exceeded"], state["native_state"] = True, None
            await self._save(run["id"], state, "context")
        else:
            await self._stop(run["id"], "failed", error.message,
                             failure_class="infrastructure" if error.code in {"authentication_error", "rate_limited", "transient_provider_error"} else "provider")

    @staticmethod
    def _operation_id(run: dict[str, Any], ordinal: int) -> str:
        return f"{run['id']}-t{run['state']['iteration']}-c{ordinal}"

    async def _plan(self, run: dict[str, Any], ordinal: int) -> ToolRow:
        call = ToolCall.model_validate(run["state"]["pending_calls"][ordinal])
        operation_id = self._operation_id(run, ordinal)
        async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
            previous = await session.get(ToolRow, operation_id)
            if previous is not None:
                if previous.call != call.model_dump(mode="json"):
                    raise NeedsReconciliation("Planned tool identity changed")
                return previous
            state = copy.deepcopy(row.state)
            manager = BudgetManager(RunConfig.model_validate(row.config).budget, state["budget"])
            manager.consume_tools(1)
            state["budget"] = manager.state.model_dump(mode="json")
            row.state = state
            tool = ToolRow(id=operation_id, run_id=row.id, iteration=state["iteration"],
                ordinal=ordinal, call_id=call.call_id, call=call.model_dump(mode="json"),
                args_hash=canonical_hash(call), workspace_revision=self._workspace(run).revision,
                epoch=self.epoch, status="planned", created_at=await self.repo.now(session))
            session.add(tool)
            await self.repo.event(session, row, "tool.planned", {"operation_id": operation_id,
                "name": call.name, "call_id": call.call_id, "arguments_hash": tool.args_hash})
            await session.flush()
            return tool

    async def _gate(self, run: dict[str, Any]) -> None:
        cursor = run["state"]["cursor"]
        if cursor >= len(run["state"]["pending_calls"]):
            await self._save(run["id"], run["state"], "ingest")
            return
        planned = await self._plan(run, cursor)
        run = await self.repo.get_run(run["id"])
        state = copy.deepcopy(run["state"])
        call = ToolCall.model_validate(planned.call)
        dispatcher = self._dispatcher(run)
        config = RunConfig.model_validate(run["config"])
        rejected: ToolResult | None = None
        try:
            dispatcher.validate(call)
        except Exception as error:
            from jsonschema.exceptions import ValidationError
            if not isinstance(error, (ValueError, ValidationError)):
                raise
            rejected = ToolResult(call_id=call.call_id, status="error", summary=str(error)[:2000],
                                  workspace_revision=dispatcher.workspace.revision)
            state["invalid_tool_calls"] += 1
            state["consecutive_invalid_tool_calls"] = state.get("consecutive_invalid_tool_calls", 0) + 1
        if call.name == "run_command" and not config.allow_commands:
            rejected = ToolResult(call_id=call.call_id, status="denied", summary="Commands disabled by the frozen run policy",
                                  workspace_revision=dispatcher.workspace.revision)
        specs = {spec.name: spec for spec in dispatcher.specs()}
        requires_approval = (rejected is None and call.name in specs and not specs[call.name].read_only
                             and config.require_write_approval)
        if requires_approval and planned.result is None:
            revision = dispatcher.workspace.refresh_revision()
            async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                approval = await session.scalar(select(ApprovalRow).where(
                    ApprovalRow.tool_id == planned.id, ApprovalRow.workspace_revision == revision))
                if approval is None:
                    approval = ApprovalRow(id=uuid.uuid4().hex, run_id=row.id, tool_id=planned.id,
                        args_hash=planned.args_hash, workspace_revision=revision,
                        policy_hash=canonical_hash({"config": row.config, "profile": row.profile}),
                        version=1, created_at=await self.repo.now(session))
                    session.add(approval)
                if (approval.args_hash != canonical_hash(call) or approval.policy_hash !=
                    canonical_hash({"config": row.config, "profile": row.profile})):
                    raise NeedsReconciliation("Approval is not bound to this exact operation and policy")
                state["approval_id"] = approval.id
                row.state = state
                if approval.decision is None:
                    row.status, row.lease_expires_at = "waiting_approval", 0
                    await self.repo.event(session, row, "approval.required", {"approval_id": approval.id,
                        "version": approval.version, "workspace_revision": revision,
                        "call": call.model_dump(mode="json"), "arguments_hash": planned.args_hash})
                elif approval.decision == "deny":
                    rejected = ToolResult(call_id=call.call_id, status="denied", summary="User denied this operation",
                                          workspace_revision=revision)
                approval_payload = {"approval_id": approval.id, "version": approval.version}
                pending = approval.decision is None
            if pending:
                # Durable approval creation is idempotent. This interrupt has no
                # external tool effects and re-enters the same validated gate.
                interrupt(approval_payload)
                return
        if rejected is not None:
            async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                tool = await session.get(ToolRow, planned.id)
                assert tool is not None
                tool.status, tool.result = "rejected", rejected.model_dump(mode="json")
        await self._save(run["id"], state, "execute")

    async def _execute(self, run: dict[str, Any]) -> None:
        state = copy.deepcopy(run["state"])
        cursor = state["cursor"]
        config = RunConfig.model_validate(run["config"])
        dispatcher = self._dispatcher(run)
        specs = {spec.name: spec for spec in dispatcher.specs()}
        calls = [ToolCall.model_validate(item) for item in state["pending_calls"]]
        batch = [cursor]
        if calls[cursor].name in specs and specs[calls[cursor].name].read_only:
            for ordinal in range(cursor + 1, min(len(calls), cursor + config.max_concurrent_read_tools)):
                if calls[ordinal].name not in specs or not specs[calls[ordinal].name].read_only:
                    break
                await self._plan(run, ordinal)
                batch.append(ordinal)
        async def execute_one(ordinal: int) -> ToolResult:
            operation_id = self._operation_id(run, ordinal)
            async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                if row.cancel_requested:
                    raise asyncio.CancelledError
                tool = await session.get(ToolRow, operation_id)
                assert tool is not None
                if tool.result is not None:
                    return ToolResult.model_validate(tool.result)
                if config.require_write_approval and not specs[calls[ordinal].name].read_only:
                    approval = await session.get(ApprovalRow, row.state.get("approval_id", ""))
                    revision = dispatcher.workspace.refresh_revision()
                    if (approval is None or approval.decision != "allow" or
                        approval.args_hash != canonical_hash(calls[ordinal]) or
                        approval.workspace_revision != revision):
                        raise ApprovalExpired("Workspace changed after approval; operation was not executed")
                tool.status, tool.epoch = "running", self.epoch
            await self.fault("tool_planned_before_execute", run["id"])
            result = await dispatcher.execute(calls[ordinal], operation_id, self.epoch)
            await self.fault("tool_effect_before_database", run["id"])
            async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                tool = await session.get(ToolRow, operation_id)
                assert tool is not None
                tool.status, tool.result = result.status, result.model_dump(mode="json")
                await self.repo.event(session, row, "tool.finished", {"operation_id": operation_id,
                    "call_id": result.call_id, "status": result.status, "workspace_revision": result.workspace_revision,
                    "artifact_refs": result.artifact_refs})
                for ref in result.artifact_refs:
                    if await session.get(ArtifactRow, ref) is None:
                        metadata = self.artifacts.metadata(ref)
                        session.add(ArtifactRow(id=ref, run_id=row.id, owner_id=row.owner_id,
                                                kind=metadata["kind"], details=metadata))
            return result
        # Writes and commands are singletons; read tools hold shared flock locks.
        results = await asyncio.gather(*(execute_one(ordinal) for ordinal in batch))
        fresh = await self.repo.get_run(run["id"])
        state = copy.deepcopy(fresh["state"])
        state["tool_results"].extend(item.model_dump(mode="json") for item in results)
        state["cursor"] += len(batch)
        if any(calls[ordinal].name == "finish" and result.status == "ok"
               for ordinal, result in zip(batch, results, strict=True)):
            state["finish_requested"] = True
        await self._save(run["id"], state, "gate" if state["cursor"] < len(calls) else "ingest")

    async def _ingest(self, run: dict[str, Any]) -> None:
        state = copy.deepcopy(run["state"])
        workspace = self._workspace(run)
        ledger = EvidenceLedger(workspace.path, [EvidenceAnchor.model_validate(item) for item in state["evidence"]])
        results = [ToolResult.model_validate(item) for item in state["tool_results"]]
        for result in results:
            state["transcript"].append(CanonicalMessage(role="tool", parts=[ToolResultPart(
                call_id=result.call_id, content=result.summary + "\nArtifact references: " + ", ".join(result.artifact_refs),
                is_error=result.status != "ok")]).model_dump(mode="json"))
            for anchor in result.evidence:
                ledger.add(anchor)
        state["evidence"] = [item.model_dump(mode="json") for item in ledger.refresh()]
        state["workspace_revision"] = workspace.refresh_revision()
        current_snapshot = canonical_hash(workspace.snapshot())
        fingerprint = canonical_hash({"calls": [{"name": item["name"], "arguments": item["arguments"]}
                                                 for item in state["pending_calls"]], "snapshot": current_snapshot})
        state["stalled_turns"] = state["stalled_turns"] + 1 if state.get("last_turn_fingerprint") == fingerprint else 0
        state["last_turn_fingerprint"], state["last_snapshot"] = fingerprint, current_snapshot
        if state["stalled_turns"] >= RunConfig.model_validate(run["config"]).stalled_turn_limit:
            await self._save(run["id"], state, "finalize")
            await self._stop(run["id"], "failed", "Repeated identical tools without a workspace change", failure_class="stalled")
            return
        if state["finish_requested"]:
            await self._save(run["id"], state, "verify")
        else:
            state["iteration"] += 1
            await self._save(run["id"], state, "context")

    async def _verify(self, run: dict[str, Any]) -> None:
        if await self._defer_for_messages(run["id"]):
            return
        workspace = self._workspace(run)
        result = await self.verifier.verify(run_id=run["id"], source=Path(run["source"]),
            base_commit=run["base_commit"], candidate=workspace,
            profile=RepoProfile.model_validate(run["profile"]), epoch=self.epoch)
        result.baseline_exit_code = run["state"].get("baseline", {}).get("baseline_exit_code")
        baseline_checks = run["state"].get("baseline", {}).get("checks", {})
        if result.status == "verified":
            for kind in ("regression", "target"):
                before = baseline_checks.get(kind, {}).get("record", {}).get("collected")
                after = result.checks.get(kind, {}).get("record", {}).get("collected")
                if not before or sorted(before) != sorted(after or []):
                    result.status = "unverified"
                    result.summary += f"\n{kind} test identity changed or baseline collection evidence is unavailable"
        state = copy.deepcopy(run["state"])
        state["verification"] = result.model_dump(mode="json")
        if result.artifact_refs:
            state["validations"].append(ValidationEvidence(validation_id=f"verify-{state['iteration']}",
                passed=result.status == "verified", workspace_revision=workspace.revision,
                snapshot_sha256=canonical_hash(workspace.snapshot()), profile_sha256=canonical_hash(run["profile"]),
                artifact_ref=result.artifact_refs[-1]).model_dump(mode="json"))
        events = [("validation.finished", {"kind": "candidate", **result.model_dump(mode="json")})]
        if result.status == "verified":
            state["terminal_status"], state["terminal_reason"] = "completed", "Pinned acceptance and regression passed independently"
            await self._save(run["id"], state, "finalize", events, result.artifact_refs)
        elif result.status == "regression_only":
            state["terminal_status"], state["terminal_reason"] = "failed", "Regression passes, but this profile has no pinned acceptance target"
            await self._save(run["id"], state, "finalize", events, result.artifact_refs)
        else:
            config = RunConfig.model_validate(run["config"])
            manager = BudgetManager(config.budget, state["budget"])
            if manager.state.repair_rounds >= config.budget.max_repair_rounds:
                state["terminal_status"], state["terminal_reason"] = "failed", "Independent verification failed after the allowed repairs"
                await self._save(run["id"], state, "finalize", events, result.artifact_refs)
            else:
                manager.consume_repair()
                state["budget"] = manager.state.model_dump(mode="json")
                state["consecutive_failures"] += 1
                state["iteration"] += 1
                state["transcript"].append(CanonicalMessage.text("user", "Independent verification failed. Inspect and repair:\n" + result.summary).model_dump(mode="json"))
                await self._save(run["id"], state, "context", events, result.artifact_refs)

    async def _finalize(self, run: dict[str, Any]) -> None:
        await self._stop(run["id"], run["state"].get("terminal_status", "failed"),
                         run["state"].get("terminal_reason", "Run finished without verified acceptance"))

    async def _defer_for_messages(self, run_id: str) -> bool:
        async with self.repo.locked(run_id, epoch=self.epoch) as (session, row):
            pending = await session.scalar(select(MessageRow.id).where(
                MessageRow.run_id == run_id, MessageRow.applied_iteration.is_(None)).limit(1))
            if pending is None:
                return False
            state = copy.deepcopy(row.state)
            state["iteration"] += 1
            row.state, row.stage = state, "context"
            await self.repo.event(session, row, "stage.changed", {"stage": "context", "reason": "new_user_message"})
            return True

    async def _cancel(self, run: dict[str, Any], *, terminal_status: str = "cancelled",
                      reason: str = "Cancellation confirmed; no active task command remains") -> None:
        try:
            workspace = self._workspace(run)
        except FileNotFoundError:
            workspace = None
        if workspace is not None and workspace.active_operation():
            operation_id = workspace.active_operation()
            assert operation_id is not None
            operation = workspace.operation(operation_id)
            if operation is not None and operation.get("kind") == "command":
                await self.executor.cancel(workspace, operation_id)
                await self.executor.reconcile(workspace, operation_id, self.epoch)
            else:
                # An applied patch may have a durable before/after manifest but
                # no DB result. Reconcile through its planned tool below.
                pass
        if workspace is not None:
            async with self.repo.sessions() as session:
                pending_tools = list(await session.scalars(select(ToolRow).where(
                    ToolRow.run_id == run["id"], ToolRow.result.is_(None))))
            for tool in pending_tools:
                operation = workspace.operation(tool.id)
                if operation is None:
                    result = ToolResult(call_id=tool.call_id, status="cancelled",
                        summary="Cancelled before an execution receipt was planned", workspace_revision=workspace.revision)
                else:
                    result = await self._dispatcher(run).execute(ToolCall.model_validate(tool.call), tool.id, self.epoch)
                async with self.repo.locked(run["id"], epoch=self.epoch) as (session, row):
                    recorded = await session.get(ToolRow, tool.id)
                    assert recorded is not None
                    recorded.result, recorded.status = result.model_dump(mode="json"), result.status
                    for ref in result.artifact_refs:
                        if await session.get(ArtifactRow, ref) is None:
                            metadata = self.artifacts.metadata(ref)
                            session.add(ArtifactRow(id=ref, run_id=row.id, owner_id=row.owner_id,
                                                    kind=metadata["kind"], details=metadata))
                    await self.repo.event(session, row, "tool.finished", {"operation_id": tool.id,
                        "status": result.status, "reason": "cancellation_reconciliation", "artifact_refs": result.artifact_refs})
        await self._stop(run["id"], terminal_status, reason)

    async def _stop(self, run_id: str, status: str, reason: str, failure_class: str | None = None) -> None:
        run = await self.repo.get_run(run_id)
        refs: list[str] = []
        try:
            workspace = self._workspace(run)
            if workspace.active_operation() and status in {"completed", "cancelled"}:
                raise NeedsReconciliation("Cannot finalize while a workspace operation is active")
            refs.append(self.artifacts.put_text(run_id, "patch", workspace.get_diff()))
        except (FileNotFoundError, ValueError, PermissionError):
            if status == "completed":
                raise NeedsReconciliation("A verified run must have an exportable complete patch") from None
        async with self.repo.locked(run_id, epoch=self.epoch) as (session, row):
            if status == "completed" and not row.cancel_requested:
                pending = await session.scalar(select(MessageRow.id).where(
                    MessageRow.run_id == run_id, MessageRow.applied_iteration.is_(None)).limit(1))
                if pending is not None:
                    state = copy.deepcopy(row.state)
                    state["iteration"] += 1
                    row.state, row.stage = state, "context"
                    await self.repo.event(session, row, "stage.changed", {"stage": "context", "reason": "message_before_final_commit"})
                    return
            if row.cancel_requested and status == "completed":
                # Completion raced cancellation after validation. No task job is
                # active here, so preserve the proof and honor the cancellation.
                status, reason = "cancelled", "Cancellation won before completion was committed"
            row.status, row.lease_expires_at = status, 0
            state = copy.deepcopy(row.state)
            state["stop_reason"], state["failure_class"] = reason, failure_class
            row.state = state
            row.verification = state.get("verification", {"status": "unverified", "summary": reason})
            summary = {"run_id": run_id, "status": status, "reason": reason,
                       "failure_class": failure_class, "verification": row.verification,
                       "budget": state.get("budget", {}), "manifest": row.manifest}
            refs.append(self.artifacts.put_text(run_id, "run_summary", json.dumps(summary)))
            for ref in refs:
                metadata = self.artifacts.metadata(ref)
                session.add(ArtifactRow(id=ref, run_id=run_id, owner_id=row.owner_id,
                                        kind=metadata["kind"], details=metadata))
            await self.repo.event(session, row, "run.finished" if status != "needs_reconciliation" else "run.paused",
                                  {"status": status, "reason": reason, "artifact_refs": refs})
