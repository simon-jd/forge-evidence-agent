"""Compare synchronous and bounded background export against a local slow collector.

Each variant runs in a fresh process. Batch size is one in both variants to avoid
mistaking fewer HTTP requests for scheduling isolation. No model API is called.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import importlib.metadata
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def child(args: argparse.Namespace) -> dict[str, Any]:
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline_commit}:src/forge/telemetry.py"], cwd=ROOT,
    ) if args.variant == "baseline" else (ROOT / "src/forge/telemetry.py").read_bytes()
    if args.variant == "baseline":
        module = types.ModuleType("forge_telemetry_baseline")
        exec(compile(source, "archived-telemetry.py", "exec"), module.__dict__)
        telemetry_type = module.Telemetry
    else:
        from forge.telemetry import Telemetry
        telemetry_type = Telemetry
    completed: list[int] = []

    class Collector(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/v1/traces"
            length = int(self.headers["Content-Length"])
            assert 0 < length <= 1_000_000
            self.rfile.read(length)
            time.sleep(args.delay_seconds)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()
            completed.append(length)

        def log_message(self, format: str, *values: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="forge-telemetry-bench-") as directory:
            path = Path(directory) / "traces.jsonl"
            endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
            kwargs = {} if args.variant == "baseline" else {
                "max_queue_size": 64, "max_export_batch_size": 1,
                "schedule_delay_millis": 10, "export_timeout_seconds": 2,
            }
            telemetry = telemetry_type(path, endpoint, **kwargs)

            async def measure() -> dict[str, Any]:
                lags: list[float] = []
                done = False

                async def ticker() -> None:
                    while not done:
                        deadline = time.perf_counter() + 0.005
                        await asyncio.sleep(0.005)
                        lags.append(max(0.0, time.perf_counter() - deadline))

                ticking = asyncio.create_task(ticker())
                await asyncio.sleep(0)
                started = time.perf_counter()
                for index in range(args.spans):
                    with telemetry.span("model", run_id="synthetic-benchmark", epoch=1, iteration=index):
                        pass
                    await asyncio.sleep(0.005)
                producer_seconds = time.perf_counter() - started
                done = True
                await ticking
                drain_started = time.perf_counter()
                if args.variant == "baseline":
                    await asyncio.to_thread(telemetry.close)
                else:
                    await telemetry.aclose()
                drain_seconds = time.perf_counter() - drain_started
                records = [json.loads(line) for line in path.read_text().splitlines()]
                assert len(records) == len(completed) == args.spans
                assert len(lags) >= 3
                return {
                    "variant": args.variant, "spans": args.spans,
                    "local_records": len(records), "remote_http_requests": len(completed),
                    "producer_seconds": producer_seconds, "drain_seconds": drain_seconds,
                    "producer_plus_drain_seconds": producer_seconds + drain_seconds,
                    "ticker_observations": len(lags), "event_loop_lag_seconds": lags,
                    "event_loop_lag_p95_seconds": percentile(lags, 0.95),
                    "event_loop_lag_max_seconds": max(lags),
                    "telemetry_source_sha256": hashlib.sha256(source).hexdigest(),
                }

            return asyncio.run(measure())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-commit", default="8b923a145a161b95608b95fd38b1d15e2e385618")
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--spans", type=int, default=24)
    parser.add_argument("--delay-seconds", type=float, default=0.05)
    parser.add_argument("--variant", choices=["baseline", "candidate"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.pairs <= 20 or not 3 <= args.spans <= 48 or not 0.001 <= args.delay_seconds <= 0.2:
        parser.error("Use 1–20 pairs, 3–48 spans and 0.001–0.2 seconds collector delay")
    if args.variant:
        print(json.dumps(child(args)))
        return
    if args.output is None or args.output.exists():
        parser.error("Supply a new --output file; previous measurements are never overwritten")
    rng = random.Random(20260913)
    results = []
    for pair in range(args.pairs):
        order = ["baseline", "candidate"]
        rng.shuffle(order)
        for variant in order:
            output = subprocess.check_output([
                sys.executable, str(Path(__file__).resolve()), "--variant", variant,
                "--baseline-commit", args.baseline_commit, "--spans", str(args.spans),
                "--delay-seconds", str(args.delay_seconds),
            ], cwd=ROOT, text=True, timeout=30)
            results.append({"pair": pair, "order": order.index(variant), **json.loads(output)})
    versions = {package: importlib.metadata.version(package) for package in
                ["opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http"]}
    summary = {}
    for variant in ["baseline", "candidate"]:
        runs = [run for run in results if run["variant"] == variant]
        summary[variant] = {key: statistics.median(run[key] for run in runs) for key in [
            "producer_seconds", "drain_seconds", "producer_plus_drain_seconds",
            "event_loop_lag_p95_seconds", "event_loop_lag_max_seconds",
        ]}
    report = {
        "schema_version": 1, "mode": "local_cpu_real_loopback_http_fault_fixture",
        "baseline_commit": args.baseline_commit, "pairs": args.pairs, "seed": 20260913,
        "collector_delay_seconds_per_request": args.delay_seconds, "batch_size_both_variants": 1,
        "candidate_queue_size_per_sink": 64, "spans_per_run": args.spans,
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "cpu": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                                     if line.startswith("model name")), "unknown"), "dependencies": versions},
        "summary_median_across_runs": summary, "runs": results,
        "limitations": ["Injected 50ms collector delay by default, not production traffic or model latency.",
                        "Producer responsiveness is measured separately from draining the export backlog.",
                        "Same request count in both variants; batching throughput gains are not claimed.",
                        "Shared local workstation; scheduling noise and other workloads can affect timing."],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "pairs": args.pairs, "summary": summary}))


if __name__ == "__main__":
    main()
