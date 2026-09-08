from pathlib import Path

import pytest

from forge.context import ContextBudgetExceeded, ContextBuilder, EvidenceLedger, InvalidTranscript
from forge.domain.types import (
    CanonicalMessage,
    CompactionSummary,
    ContextConfig,
    FileReferencePart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    canonical_hash,
)


def transaction(call_id: str, result: str, *, parallel: bool = False) -> list[CanonicalMessage]:
    ids = [call_id, f"{call_id}-b"] if parallel else [call_id]
    return [CanonicalMessage(role="assistant", parts=[
        ToolCallPart(call=ToolCall(call_id=key, name="read_file", arguments={"path": "a.py"}))
        for key in ids
    ]), *[CanonicalMessage(role="tool", parts=[ToolResultPart(call_id=key, content=result)]) for key in ids]]


def rendered(bundle: object) -> str:
    return "\n".join(message.model_dump_json() for message in bundle.messages)


def test_guard_ablates_stale_observation_leak_without_breaking_tool_protocol(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("rate = OLD_SENTINEL\n")
    ledger = EvidenceLedger(tmp_path)
    old = ledger.register("a.py", origin_call_id="read-1")
    history = transaction("read-1", old.excerpt)
    source.write_text("rate = NEW_SENTINEL\n")
    config = ContextConfig(input_budget=12_000)
    guarded = ContextBuilder().build("fix", [], history, [old], tmp_path, config, output_reserve=500)
    unguarded = ContextBuilder(evidence_guard=False).build("fix", [], history, [old], tmp_path, config, output_reserve=500)
    assert "OLD_SENTINEL" not in rendered(guarded)
    assert "OLD_SENTINEL" in rendered(unguarded)
    assert guarded.stale_evidence_ids == [old.evidence_id]
    assert guarded.context_hash != unguarded.context_hash
    calls = [part.call.call_id for message in guarded.messages for part in message.parts if isinstance(part, ToolCallPart)]
    results = [part.call_id for message in guarded.messages for part in message.parts if isinstance(part, ToolResultPart)]
    assert calls == results == ["read-1"]


def test_stale_file_part_does_not_need_registered_anchor(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("current = 2\n")
    history = [CanonicalMessage(role="user", parts=[FileReferencePart(
        path="a.py", sha256="old", start_line=1, end_line=1, text="STALE_SENTINEL\n",
    )])]
    bundle = ContextBuilder().build("fix", [], history, [], tmp_path, ContextConfig(), output_reserve=10)
    assert "STALE_SENTINEL" not in rendered(bundle)


def test_constraints_followup_and_parallel_pairs_survive_compaction(tmp_path: Path) -> None:
    history = []
    for index in range(12):
        history += transaction(str(index), "very long output " * 80, parallel=True)
    history.insert(3, CanonicalMessage.text("user", "Never change the public function signature."))
    bundle = ContextBuilder().build(
        "Repair Unicode parsing", ["必须保留 UTF-8", "No network access"], history, [], tmp_path,
        ContextConfig(input_budget=11_000, recent_turns=2), output_reserve=500,
        history_artifact_ref="transcript-artifact-123",
    )
    text = rendered(bundle)
    assert "必须保留 UTF-8" in text and "No network access" in text
    assert "Never change the public function signature." in text
    assert bundle.compacted
    assert bundle.summary.artifact_refs == ["transcript-artifact-123"]
    calls = {part.call.call_id for message in bundle.messages for part in message.parts if isinstance(part, ToolCallPart)}
    results = {part.call_id for message in bundle.messages for part in message.parts if isinstance(part, ToolResultPart)}
    assert calls == results
    assert "11" in calls and "11-b" in calls
    assert sum(bundle.token_breakdown.values()) <= 11_000
    assert bundle.token_estimate + 500 == sum(bundle.token_breakdown.values())


def test_pending_tool_call_must_fit_in_full(tmp_path: Path) -> None:
    pending = transaction("pending", "ignored", parallel=True)[:1]
    builder = ContextBuilder()
    bundle = builder.build("fix", [], pending, [], tmp_path, ContextConfig(input_budget=5000), output_reserve=10)
    assert "pending-b" in rendered(bundle)
    with pytest.raises(ContextBudgetExceeded):
        builder.build("fix", [], pending, [], tmp_path, ContextConfig(input_budget=100), output_reserve=10)


def test_tool_schema_and_output_reserve_are_inside_window(tmp_path: Path) -> None:
    tools = [ToolSpec(name="large", description="x" * 3000, parameters={"type": "object"})]
    with pytest.raises(ContextBudgetExceeded) as error:
        ContextBuilder().build("fix", [], [], [], tmp_path, ContextConfig(input_budget=3200), tools, 500)
    assert error.value.required > error.value.available


@pytest.mark.parametrize("history", [
    [CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="orphan", content="x")])],
    transaction("a", "one") + transaction("a", "two"),
    transaction("a", "one")[:1] + [CanonicalMessage.text("user", "interruption")],
])
def test_invalid_tool_causality_rejected(tmp_path: Path, history: list[CanonicalMessage]) -> None:
    with pytest.raises(InvalidTranscript):
        ContextBuilder().build("fix", [], history, [], tmp_path, ContextConfig(), output_reserve=10)


def test_summary_that_drops_constraints_is_rejected_with_deterministic_fallback(tmp_path: Path) -> None:
    def bad_summary(seed: CompactionSummary, messages: list[CanonicalMessage]) -> CompactionSummary:
        return seed.model_copy(update={"constraints": [], "decisions": ["All tests passed"]})

    history = [CanonicalMessage.text("assistant", "historical " * 200) for _ in range(12)]
    history.append(CanonicalMessage.text("assistant", "Inspect current code next."))
    bundle = ContextBuilder(summarizer=bad_summary).build(
        "Fix", ["Keep API stable"], history, [], tmp_path,
        ContextConfig(input_budget=6000, recent_turns=1), output_reserve=100,
    )
    assert bundle.compacted
    assert bundle.summary.constraints == ["Keep API stable"]
    assert "Generated summary rejected" in rendered(bundle)
    assert "All tests passed" not in rendered(bundle)


def test_hash_covers_schema_guard_and_actual_content(tmp_path: Path) -> None:
    builder = ContextBuilder()
    args = ("fix", [], [], [], tmp_path, ContextConfig())
    a = builder.build(*args, output_reserve=100)
    b = builder.build(*args, output_reserve=100)
    tool = ToolSpec(name="tool", description="description", parameters={"type": "object"})
    c = builder.build(*args, tools=[tool], output_reserve=100)
    assert a.context_hash == b.context_hash != c.context_hash


def test_current_evidence_added_in_b1_and_not_b0(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("MARKER = 17\n")
    anchor = EvidenceLedger(tmp_path).register("a.py")
    b0 = ContextBuilder().build("fix", [], [], [anchor], tmp_path,
                                ContextConfig(strategy="recent"), output_reserve=100)
    b1 = ContextBuilder().build("fix", [], [], [anchor], tmp_path,
                                ContextConfig(strategy="evidence"), output_reserve=100)
    assert "MARKER" not in rendered(b0)
    assert "MARKER" in rendered(b1)
    assert len(b1.evidence) == 1


def test_large_current_file_is_loaded_as_hash_bound_line_excerpt(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("".join(f"value_{index} = {index}\n" for index in range(300)))
    anchor = EvidenceLedger(tmp_path).register("a.py")
    bundle = ContextBuilder().build("fix", [], [], [anchor], tmp_path,
                                    ContextConfig(strategy="evidence", max_file_tokens=600),
                                    output_reserve=100)
    assert len(bundle.evidence) == 1
    loaded = bundle.evidence[0]
    assert loaded.end_line < anchor.end_line
    assert loaded.sha256 == anchor.sha256
    assert EvidenceLedger(tmp_path, [loaded]).refresh()[0].status == "current"
    assert bundle.token_breakdown["retrieval"] <= 600


def test_deterministic_summary_keeps_literal_notes_with_original_message_hash(tmp_path: Path) -> None:
    note = CanonicalMessage.text("assistant", "The public API must stay stable; inspect the parser next.")
    history = [note, *transaction("read", "log " * 1800),
               CanonicalMessage.text("assistant", "I will inspect current input validation.")]
    bundle = ContextBuilder().build("fix", [], history, [], tmp_path,
                                    ContextConfig(input_budget=6000, recent_turns=1), output_reserve=100)
    assert bundle.compacted
    assert f"[message:{canonical_hash(note)}]" in bundle.summary.decisions[0]
    assert "Historical assistant note (unverified)" in bundle.summary.decisions[0]
