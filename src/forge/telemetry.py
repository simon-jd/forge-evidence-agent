"""Low-cardinality spans; prompts and credentials remain outside telemetry."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonlExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path

    def export(self, spans: Any) -> SpanExportResult:
        with self.path.open("a") as stream:
            for span in spans:
                stream.write(json.dumps({
                    "name": span.name, "trace_id": format(span.context.trace_id, "032x"),
                    "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                    "start_time": span.start_time, "end_time": span.end_time,
                    "attributes": dict(span.attributes or {}), "status": span.status.status_code.name,
                }) + "\n")
        return SpanExportResult.SUCCESS


class Telemetry:
    def __init__(self, path: Path, otlp_endpoint: str | None = None) -> None:
        self.provider = TracerProvider(resource=Resource.create({"service.name": "forge-worker"}))
        self.provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            self.provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
        self.tracer = self.provider.get_tracer("forge.runtime")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        safe = {key: value for key, value in attributes.items()
                if value is not None and not any(word in key.lower() for word in ("secret", "token", "prompt", "content"))}
        with self.tracer.start_as_current_span(name, attributes=safe):
            yield

    def close(self) -> None:
        self.provider.shutdown()


def log_event(kind: str, **attributes: Any) -> None:
    logging.getLogger("forge").info(json.dumps({"event": kind, **attributes}, default=str))
