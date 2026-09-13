"""Best-effort bounded trace export, isolated from the worker's event loop.

The SDK owns each sink's queue and worker thread. Optional SDK queue metrics use
OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true; unavailable measurements are null.
Process-scoped logging filters sanitize OTLP SDK messages and suppress synchronous
span queue warnings; they remain installed for other active Telemetry instances.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

_SPAN_NAMES = frozenset({
    "run", "bootstrap", "baseline", "context", "model", "gate", "execute", "ingest",
    "verify", "finalize", "operation",
})
_ATTRIBUTE_KEYS = (
    "run_id", "epoch", "iteration", "provider", "model", "status", "failure_class",
    "tool_name", "duration_ms", "error_type",
)


_LOG_FILTER_LOCK = threading.Lock()


class _OtlpPrivacyFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__("forge.telemetry.otlp-privacy.v1")

    def filter(self, record: logging.LogRecord) -> bool:
        # The real OTLP exporter logs requests exceptions and response reasons
        # before our export adapter sees FAILURE. Both can contain credentials.
        record.msg = "OTLP trace exporter diagnostic; inspect telemetry sink counters"
        record.message = record.msg
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class _BatchProducerFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__("forge.telemetry.span-producer.v1")

    def filter(self, record: logging.LogRecord) -> bool:
        # These SDK records execute on the span producer, where any logging
        # handler can block. Queue losses are measured by SDK metrics instead.
        return not (
            record.msg in ("Queue full, dropping %s.", "Shutdown called, ignoring %s.")
            and record.args == ("Span",)
        )


def _install_sdk_log_filters() -> None:
    """Install only on specific SDK loggers, once per process, using logging APIs.

    Deliberately retained after close: removing a filter could expose a different
    live instance or an exporter still completing shutdown. No SDK private API
    or global logging factory is changed; other application loggers are untouched.
    Logger names/formats are covered by real-exporter and saturation regressions.
    """
    with _LOG_FILTER_LOCK:
        for name, log_filter in (
            ("opentelemetry.exporter.otlp.proto.http.trace_exporter", _OtlpPrivacyFilter()),
            ("opentelemetry.sdk._shared_internal", _BatchProducerFilter()),
        ):
            logger = logging.getLogger(name)
            if not any(getattr(existing, "name", None) == log_filter.name for existing in logger.filters):
                logger.addFilter(log_filter)


class JsonlExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        self.path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # Directory/file failures stay on the exporter thread.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A new local trace file is owner-readable, independently of umask.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            for span in spans:
                context = span.get_span_context()
                if context is None:
                    continue
                stream.write(json.dumps({
                    "name": span.name, "trace_id": format(context.trace_id, "032x"),
                    "span_id": format(context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                    "start_time": span.start_time, "end_time": span.end_time,
                    "attributes": dict(span.attributes or {}), "status": span.status.status_code.name,
                }) + "\n")
        return SpanExportResult.SUCCESS


class _UnavailableExporter(SpanExporter):
    """A configured sink that could not initialize; never report successful export."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.FAILURE


class _ObservedExporter(SpanExporter):
    """Count outcomes without letting an export exception leak data or escape.

    SDK processed-item counters do not distinguish a returned FAILURE from SUCCESS
    in 1.44, so these counters are intentionally separate from SDK queue drops.
    Counts describe exporter outcomes, not an exactly-once collector receipt.
    """

    def __init__(self, delegate: SpanExporter, initialization_failures: int = 0) -> None:
        self.delegate = delegate
        self.lock = threading.Lock()
        self.counts = {
            "exported_spans": 0, "export_failed_spans": 0, "export_batches": 0,
            "export_failed_batches": 0, "in_flight_spans": 0, "shutdown_failures": 0,
            "initialization_failures": initialization_failures,
        }

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.lock:
            self.counts["in_flight_spans"] += len(spans)
        try:
            result = self.delegate.export(spans)
        except Exception:
            # Never forward exception text/tracebacks from exporter implementations.
            result = SpanExportResult.FAILURE
        with self.lock:
            self.counts["in_flight_spans"] -= len(spans)
            self.counts["export_batches"] += 1
            if result == SpanExportResult.SUCCESS:
                self.counts["exported_spans"] += len(spans)
            else:
                self.counts["export_failed_spans"] += len(spans)
                self.counts["export_failed_batches"] += 1
        return result

    def shutdown(self) -> None:
        try:
            self.delegate.shutdown()
        except Exception:
            with self.lock:
                self.counts["shutdown_failures"] += 1

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)


@dataclass
class _Sink:
    exporter: _ObservedExporter
    reader: InMemoryMetricReader
    meter_provider: MeterProvider


class Telemetry:
    def __init__(
        self, path: Path, otlp_endpoint: str | None = None, *,
        local_exporter: SpanExporter | None = None,
        remote_exporter: SpanExporter | None = None,
        max_queue_size: int = 256,
        max_export_batch_size: int = 32,
        schedule_delay_millis: float = 200,
        export_timeout_seconds: float = 2.0,
        max_attributes: int = 12,
        max_attribute_length: int = 128,
    ) -> None:
        for name, value, maximum in (
            ("max_queue_size", max_queue_size, 65536),
            ("max_export_batch_size", max_export_batch_size, 4096),
            ("max_attributes", max_attributes, 32),
            ("max_attribute_length", max_attribute_length, 1024),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        if max_export_batch_size > max_queue_size:
            raise ValueError("max_export_batch_size must not exceed max_queue_size")
        for name, duration, limit in (
            ("schedule_delay_millis", schedule_delay_millis, 60000),
            ("export_timeout_seconds", export_timeout_seconds, 60),
        ):
            if isinstance(duration, bool) or not math.isfinite(duration) or not 0 < duration <= limit:
                raise ValueError(f"{name} must be finite, positive and at most {limit}")
        _install_sdk_log_filters()
        self.max_attributes = max_attributes
        self.max_attribute_length = max_attribute_length
        self.max_queue_size = max_queue_size
        self._close_lock = threading.Lock()
        self._closed = False
        self._final_diagnostics: dict[str, Any] | None = None
        self._sinks: dict[str, _Sink] = {}
        self.provider = TracerProvider(
            # Do not import arbitrary OTEL_RESOURCE_ATTRIBUTES or host detectors.
            resource=Resource({"service.name": "forge-worker"}),
            span_limits=SpanLimits(max_attributes=max_attributes,
                                   max_attribute_length=max_attribute_length,
                                   max_events=0, max_links=0),
        )
        try:
            exporters: dict[str, SpanExporter] = {
                "local": local_exporter if local_exporter is not None else JsonlExporter(path),
            }
            initialization_failures = 0
            if remote_exporter is not None:
                exporters["otlp"] = remote_exporter
            elif otlp_endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                    # BSP export_timeout_millis does not enforce network timeouts.
                    exporters["otlp"] = OTLPSpanExporter(
                        endpoint=otlp_endpoint, timeout=export_timeout_seconds,
                    )
                except Exception:
                    exporters["otlp"] = _UnavailableExporter()
                    initialization_failures = 1
            for name, exporter in exporters.items():
                reader = InMemoryMetricReader()
                meter_provider = MeterProvider(metric_readers=[reader],
                                               resource=Resource({"service.name": "forge-worker"}))
                observed = _ObservedExporter(exporter, initialization_failures if name == "otlp" else 0)
                self._sinks[name] = _Sink(observed, reader, meter_provider)
                self.provider.add_span_processor(BatchSpanProcessor(
                    observed, max_queue_size=max_queue_size,
                    max_export_batch_size=max_export_batch_size,
                    schedule_delay_millis=schedule_delay_millis,
                    meter_provider=meter_provider,
                ))
        except Exception:
            self.close()
            raise
        self.tracer = self.provider.get_tracer("forge.runtime")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        safe: dict[str, str | int | float | bool] = {}
        for key in _ATTRIBUTE_KEYS:
            value = attributes.get(key)
            if isinstance(value, str):
                safe[key] = value[:self.max_attribute_length]
            elif isinstance(value, (int, bool)):
                safe[key] = value
            elif isinstance(value, float) and math.isfinite(value):
                safe[key] = value
            if len(safe) >= self.max_attributes:
                break
        with self.tracer.start_as_current_span(
            name if name in _SPAN_NAMES else "operation", attributes=safe,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield
            except BaseException:
                # Status alone communicates failure; the exception message may
                # contain credentials, prompts, subprocess output or source text.
                span.set_status(Status(StatusCode.ERROR))
                raise

    def diagnostics(self) -> dict[str, Any]:
        """Bounded per-sink labels; snapshots are not atomic across SDK/exporter.

        No private SDK queue access. A missing metric remains unknown (null),
        while an observed active queue with no drop samples has zero drops.
        """
        if self._final_diagnostics is not None:
            return json.loads(json.dumps(self._final_diagnostics))
        sinks: dict[str, Any] = {}
        for name, sink in self._sinks.items():
            queue_size: int | None = None
            dropped = 0
            try:
                data = sink.reader.get_metrics_data()
                for resource in data.resource_metrics if data else ():
                    for scope in resource.scope_metrics:
                        for metric in scope.metrics:
                            for point in getattr(metric.data, "data_points", ()):
                                if metric.name == "otel.sdk.processor.span.queue.size":
                                    queue_size = int(point.value)
                                elif metric.name == "otel.sdk.processor.span.processed" and (
                                    point.attributes or {}
                                ).get("error.type") == "queue_full":
                                    dropped += int(point.value)
            except Exception:
                queue_size = None
            sinks[name] = {
                **sink.exporter.snapshot(), "queue_capacity": self.max_queue_size,
                "queue_size": queue_size,
                "queue_dropped_spans": dropped if queue_size is not None else None,
                "queue_metrics_available": queue_size is not None,
            }
        return {"closed": self._closed, "sinks": sinks}

    def close(self) -> None:
        """Drain best-effort SDK queues; may block on an exporter.

        SDK shutdown and exporter timeouts do not terminate arbitrary Python
        threads. Async worker code must await aclose() instead of calling this.
        """
        with self._close_lock:
            if self._closed:
                return
            self.provider.shutdown()
            self._closed = True
            self._final_diagnostics = self.diagnostics()
            for sink in self._sinks.values():
                sink.meter_provider.shutdown()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


def log_event(kind: str, **attributes: Any) -> None:
    logging.getLogger("forge").info(json.dumps({"event": kind, **attributes}, default=str))
