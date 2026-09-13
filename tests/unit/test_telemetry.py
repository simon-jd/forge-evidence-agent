from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import ValidationError

from forge.settings import Settings
from forge.telemetry import Telemetry


class MemoryExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.shutdown_calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.changed:
            self.spans.extend(spans)
            self.changed.notify_all()
        return SpanExportResult.SUCCESS

    def count(self) -> int:
        with self.lock:
            return len(self.spans)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class BlockingExporter(MemoryExporter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.entered.set()
        if not self.release.wait(5):
            return SpanExportResult.FAILURE
        return super().export(spans)


async def wait_for_count(exporter: MemoryExporter, expected: int) -> None:
    def wait() -> bool:
        with exporter.changed:
            return exporter.changed.wait_for(lambda: len(exporter.spans) >= expected, timeout=2)

    assert await asyncio.to_thread(wait)


@pytest.mark.asyncio
async def test_blocked_remote_has_bounded_sdk_queue_and_does_not_stop_local_or_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    local = MemoryExporter()
    remote = BlockingExporter()
    telemetry = Telemetry(tmp_path / "traces.jsonl", local_exporter=local,
                          remote_exporter=remote, max_queue_size=4, max_export_batch_size=1,
                          schedule_delay_millis=1)
    try:
        with telemetry.span("model"):
            pass
        assert await asyncio.to_thread(remote.entered.wait, 1)
        await wait_for_count(local, 1)
        async with asyncio.timeout(2):
            for index in range(12):
                with telemetry.span("model", iteration=index):
                    pass
                # Drain the independent healthy sink while the remote stays blocked.
                await wait_for_count(local, index + 2)
        assert not remote.release.is_set()
        counters = telemetry.diagnostics()["sinks"]
        assert counters["local"]["exported_spans"] == 13
        assert counters["local"]["queue_dropped_spans"] == 0
        assert counters["otlp"]["queue_size"] == counters["otlp"]["queue_capacity"] == 4
        assert counters["otlp"]["in_flight_spans"] == 1
        assert counters["otlp"]["queue_dropped_spans"] == 8
        assert counters["otlp"]["export_failed_spans"] == 0
    finally:
        remote.release.set()
        await telemetry.aclose()
    counters = telemetry.diagnostics()["sinks"]["otlp"]
    assert remote.count() == 5
    assert counters["exported_spans"] + counters["queue_dropped_spans"] == 13
    assert counters["queue_size"] == counters["in_flight_spans"] == 0


@pytest.mark.asyncio
async def test_async_close_keeps_event_loop_live_and_legacy_close_is_idempotent(tmp_path: Path) -> None:
    remote = BlockingExporter()
    telemetry = Telemetry(tmp_path / "traces.jsonl", remote_exporter=remote,
                          max_export_batch_size=1, schedule_delay_millis=1)
    with telemetry.span("run"):
        pass
    assert await asyncio.to_thread(remote.entered.wait, 1)
    closing = asyncio.create_task(telemetry.aclose())
    try:
        for _ in range(5):
            await asyncio.sleep(0.005)
        assert not closing.done()
        assert not remote.release.is_set()
    finally:
        remote.release.set()
        await asyncio.wait_for(closing, 2)
    telemetry.close()
    assert remote.shutdown_calls == 1
    assert telemetry.diagnostics()["closed"] is True


@pytest.mark.parametrize("raises", [False, True])
def test_export_failure_and_shutdown_failure_are_isolated_and_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")

    class FailingExporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            if raises:
                raise RuntimeError("EXPORTER_SECRET_CANARY")
            return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            raise RuntimeError("SHUTDOWN_SECRET_CANARY")

    healthy = MemoryExporter()
    telemetry = Telemetry(tmp_path / "unused.jsonl", local_exporter=FailingExporter(),
                          remote_exporter=healthy)
    with telemetry.span("model"):
        pass
    telemetry.close()
    counters = telemetry.diagnostics()["sinks"]
    assert healthy.count() == 1
    assert counters["local"]["export_failed_spans"] == 1
    assert counters["local"]["export_failed_batches"] == 1
    assert counters["local"]["shutdown_failures"] == 1
    assert counters["local"]["queue_dropped_spans"] == 0
    assert counters["otlp"]["exported_spans"] == 1
    assert "SECRET_CANARY" not in caplog.text


def test_sensitive_exception_attributes_and_resource_env_are_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "secret=RESOURCE_SECRET_CANARY")
    remote = MemoryExporter()
    path = tmp_path / "trace.jsonl"
    telemetry = Telemetry(path, remote_exporter=remote, max_attribute_length=16, max_attributes=3)
    with pytest.raises(RuntimeError, match="EXCEPTION_SECRET_CANARY"):
        with telemetry.span("run", run_id="safe-run", epoch=2):
            with telemetry.span("model", provider="deepseek", model="a" * 1000,
                                prompt="PROMPT_SECRET_CANARY", tool_arguments={"key": "ARG_SECRET_CANARY"},
                                custom="UNKNOWN_SECRET_CANARY", duration_ms=math.inf):
                raise RuntimeError("EXCEPTION_SECRET_CANARY")
    with telemetry.span("NAME_SECRET_CANARY", model={"key": "NESTED_SECRET_CANARY"}):
        pass
    telemetry.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    child, parent, unknown = rows
    assert child["parent_span_id"] == parent["span_id"]
    assert child["trace_id"] == parent["trace_id"]
    assert child["attributes"] == {"provider": "deepseek", "model": "a" * 16}
    assert parent["attributes"] == {"run_id": "safe-run", "epoch": 2}
    assert child["status"] == parent["status"] == "ERROR"
    assert unknown["name"] == "operation" and unknown["attributes"] == {}
    assert "SECRET_CANARY" not in path.read_text()
    for span in remote.spans:
        assert not span.events
        assert span.status.description is None
        assert "SECRET_CANARY" not in str(span.resource.attributes)
        assert len(span.attributes or {}) <= 3
    assert path.stat().st_mode & 0o777 == 0o600


def test_unavailable_sdk_counters_stay_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", raising=False)
    telemetry = Telemetry(tmp_path / "traces.jsonl")
    with telemetry.span("run"):
        pass
    telemetry.close()
    counters = telemetry.diagnostics()["sinks"]["local"]
    assert counters["exported_spans"] == 1
    assert counters["export_failed_spans"] == 0
    assert counters["queue_metrics_available"] is False
    assert counters["queue_size"] is None
    assert counters["queue_dropped_spans"] is None


@pytest.mark.parametrize("options", [
    {"max_queue_size": 0}, {"max_queue_size": 2, "max_export_batch_size": 3},
    {"max_export_batch_size": 0}, {"max_queue_size": True},
    {"schedule_delay_millis": 0}, {"schedule_delay_millis": math.nan},
    {"export_timeout_seconds": 0}, {"export_timeout_seconds": math.inf},
    {"max_attributes": 0}, {"max_attribute_length": 0},
])
def test_invalid_constructor_configuration_fails_before_exporter_creation(
    tmp_path: Path, options: dict[str, int | float],
) -> None:
    with pytest.raises(ValueError):
        Telemetry(tmp_path / "unused" / "traces.jsonl", **options)  # type: ignore[arg-type]
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("options", [
    {"telemetry_max_queue_size": 0},
    {"telemetry_max_queue_size": 2, "telemetry_max_export_batch_size": 3},
    {"telemetry_export_timeout_seconds": math.inf},
    {"telemetry_schedule_delay_millis": 0},
])
def test_settings_reject_invalid_telemetry_configuration(options: dict[str, int | float]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)  # type: ignore[arg-type]


def test_settings_environment_and_http_timeout_are_passed_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp

    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> SpanExporter:
        captured.update(kwargs)
        return MemoryExporter()

    monkeypatch.setattr(otlp, "OTLPSpanExporter", factory)
    monkeypatch.setenv("FORGE_TELEMETRY_MAX_QUEUE_SIZE", "8")
    monkeypatch.setenv("FORGE_TELEMETRY_MAX_EXPORT_BATCH_SIZE", "2")
    monkeypatch.setenv("FORGE_TELEMETRY_EXPORT_TIMEOUT_SECONDS", "0.25")
    settings = Settings(_env_file=None)
    assert settings.telemetry_max_queue_size == 8
    assert settings.telemetry_max_export_batch_size == 2
    telemetry = Telemetry(tmp_path / "traces.jsonl", "http://collector/v1/traces",
                          export_timeout_seconds=settings.telemetry_export_timeout_seconds)
    telemetry.close()
    assert captured == {"endpoint": "http://collector/v1/traces", "timeout": 0.25}


def test_local_path_failure_does_not_stop_remote(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("existing file")
    remote = MemoryExporter()
    telemetry = Telemetry(parent / "traces.jsonl", remote_exporter=remote)
    with telemetry.span("run"):
        pass
    telemetry.close()
    counters = telemetry.diagnostics()["sinks"]
    assert counters["local"]["export_failed_spans"] == 1
    assert counters["otlp"]["exported_spans"] == 1
    assert remote.count() == 1
    assert parent.read_text() == "existing file"


def test_remote_initialization_failure_does_not_stop_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp

    def unavailable(**kwargs: object) -> SpanExporter:
        raise ValueError("INITIALIZATION_SECRET_CANARY")

    monkeypatch.setattr(otlp, "OTLPSpanExporter", unavailable)
    path = tmp_path / "traces.jsonl"
    telemetry = Telemetry(path, "http://collector/v1/traces")
    with telemetry.span("run"):
        pass
    telemetry.close()
    counters = telemetry.diagnostics()["sinks"]
    assert counters["local"]["exported_spans"] == 1
    assert counters["otlp"]["initialization_failures"] == 1
    assert counters["otlp"]["export_failed_spans"] == 1
    assert len(path.read_text().splitlines()) == 1
    assert "INITIALIZATION_SECRET_CANARY" not in caplog.text


@pytest.mark.asyncio
async def test_queue_saturation_does_not_reach_slow_logging_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    remote = BlockingExporter()
    telemetry = Telemetry(tmp_path / "traces.jsonl", remote_exporter=remote,
                          max_queue_size=2, max_export_batch_size=1)
    producer_messages: list[str] = []

    class SlowHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.msg == "Queue full, dropping %s.":
                producer_messages.append(record.getMessage())
                threading.Event().wait(0.05)

    logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    handler = SlowHandler()
    logger.addHandler(handler)
    try:
        with telemetry.span("model"):
            pass
        assert await asyncio.to_thread(remote.entered.wait, 1)
        for _ in range(10):
            with telemetry.span("model"):
                pass
        assert producer_messages == []
        assert telemetry.diagnostics()["sinks"]["otlp"]["queue_dropped_spans"] == 8
    finally:
        logger.removeHandler(handler)
        remote.release.set()
        await telemetry.aclose()


def test_sdk_log_filters_are_idempotent_persistent_and_scoped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    first = Telemetry(tmp_path / "first.jsonl")
    second = Telemetry(tmp_path / "second.jsonl")
    first.close()
    logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    try:
        try:
            raise ValueError("TRACEBACK_SECRET_CANARY")
        except ValueError:
            logger.exception("REASON_SECRET_CANARY %s", "ARG_SECRET_CANARY")
        logging.getLogger("forge.application").warning("UNRELATED_APPLICATION_WARNING")
        assert len([item for item in logger.filters if item.name == "forge.telemetry.otlp-privacy.v1"]) == 1
        assert "SECRET_CANARY" not in caplog.text
        assert "UNRELATED_APPLICATION_WARNING" in caplog.text
        assert "inspect telemetry sink counters" in caplog.text
    finally:
        second.close()
