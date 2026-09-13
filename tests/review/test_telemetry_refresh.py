"""Independent telemetry adversarial regressions; no network or model calls."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from forge.telemetry import Telemetry


@pytest.mark.parametrize("failure_kind", ["request_exception", "collector_reason"])
def test_real_otlp_failure_does_not_disclose_exception_or_collector_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure_kind: str,
) -> None:
    """Exercise public SDK export with a fake HTTP transport, not a fake exporter."""
    class Transport(requests.Session):
        def post(self, *args: Any, **kwargs: Any) -> requests.Response:
            if failure_kind == "request_exception":
                raise requests.ReadTimeout("OTLP_TRANSPORT_SECRET_CANARY")
            response = requests.Response()
            response.status_code = 403
            response.reason = "OTLP_COLLECTOR_SECRET_CANARY"
            return response

    telemetry = Telemetry(
        tmp_path / "trace.jsonl",
        remote_exporter=OTLPSpanExporter(endpoint="http://unused/v1/traces", timeout=0.05,
                                         session=Transport()),
    )
    with telemetry.span("run", run_id="independent-review"):
        pass
    telemetry.close()
    sinks = telemetry.diagnostics()["sinks"]
    assert sinks["local"]["exported_spans"] == 1
    assert sinks["otlp"]["export_failed_spans"] == 1
    assert "SECRET_CANARY" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_cancel_drain_or_duplicate_shutdown(tmp_path: Path) -> None:
    """Cancellation releases an async caller while the SDK owns its current export."""
    entered, release = threading.Event(), threading.Event()
    shutdowns = 0

    class Exporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            entered.set()
            assert release.wait(5), "review failed to release exporter"
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            nonlocal shutdowns
            shutdowns += 1

    telemetry = Telemetry(tmp_path / "trace.jsonl", remote_exporter=Exporter(),
                          max_export_batch_size=1)
    with telemetry.span("run"):
        pass
    assert await asyncio.to_thread(entered.wait, 1)
    closer = asyncio.create_task(telemetry.aclose())
    try:
        await asyncio.sleep(0.01)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert not release.is_set()
        # A second waiter must converge on the same close, not run shutdown twice.
        second_closer = asyncio.create_task(telemetry.aclose())
        await asyncio.sleep(0.01)
        assert not second_closer.done()
        release.set()
        await asyncio.wait_for(second_closer, 2)
    finally:
        release.set()
        await telemetry.aclose()
    assert shutdowns == 1
    counters = telemetry.diagnostics()["sinks"]
    assert all(sink["exported_spans"] == 1 for sink in counters.values())
    assert telemetry.diagnostics()["closed"] is True


def test_cancelled_span_does_not_export_message_or_stacktrace(tmp_path: Path) -> None:
    captured: list[ReadableSpan] = []

    class Exporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            captured.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    telemetry = Telemetry(tmp_path / "trace.jsonl", remote_exporter=Exporter())
    with pytest.raises(asyncio.CancelledError):
        with telemetry.span("run", run_id="review-cancelled"):
            raise asyncio.CancelledError("CANCELLED_SECRET_CANARY")
    telemetry.close()
    assert len(captured) == 1
    span = captured[0]
    assert not span.events and span.status.description is None
    assert span.status.status_code.name == "ERROR"
    assert "SECRET_CANARY" not in (tmp_path / "trace.jsonl").read_text()


@pytest.mark.asyncio
async def test_queue_overflow_does_not_log_on_producer_path_but_keeps_observable_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    entered, release = threading.Event(), threading.Event()

    class Exporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            entered.set()
            assert release.wait(5), "review failed to release exporter"
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    telemetry = Telemetry(tmp_path / "trace.jsonl", remote_exporter=Exporter(),
                          max_queue_size=2, max_export_batch_size=1)
    try:
        with telemetry.span("run"):
            pass
        assert await asyncio.to_thread(entered.wait, 1)
        producer_thread = threading.get_ident()
        for _ in range(8):
            with telemetry.span("model"):
                pass
        counters = telemetry.diagnostics()["sinks"]["otlp"]
        assert counters["queue_size"] == 2
        assert counters["queue_dropped_spans"] == 6
        # SDK warning handlers are synchronous and can themselves block this thread.
        assert not any(record.thread == producer_thread and "Queue full" in record.getMessage()
                       for record in caplog.records)
        logging.getLogger("forge.review").warning("unrelated application warning")
        assert "unrelated application warning" in caplog.text
    finally:
        release.set()
        await telemetry.aclose()
