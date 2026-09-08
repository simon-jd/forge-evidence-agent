"""Completion evidence for supported test runners, distinct from exit codes.

The probe catches premature exits, missing collection, skips and changed test
selection. It does not claim to secure a Python interpreter against hostile
source that deliberately attacks the runner itself.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from forge.execution.docker import ExecutionReceipt

RUNNER = r'''import json, os, sys
framework, encoded_args, nonce = sys.argv[1:]
args = json.loads(encoded_args)
sys.path.insert(0, os.getcwd())
report = dict(version=1, nonce=nonce, framework=framework, collected=[], executed=[],
              failures=0, errors=0, skipped=0, expected_failures=0, unexpected_successes=0)
if framework == "unittest":
    import unittest
    class Result(unittest.TextTestResult):
        def startTest(self, test):
            report["executed"].append(test.id())
            super().startTest(test)
    def ids(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from ids(test)
            else:
                yield test.id()
    class Runner(unittest.TextTestRunner):
        def __init__(self, *a, **kw):
            kw["resultclass"] = Result
            super().__init__(*a, **kw)
        def run(self, suite):
            report["collected"] = list(ids(suite))
            return super().run(suite)
    program = unittest.TestProgram(module=None, argv=["unittest", *args], testRunner=Runner, exit=False)
    result = program.result
    report.update(failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), expected_failures=len(result.expectedFailures),
                  unexpected_successes=len(result.unexpectedSuccesses))
    code = 0 if result.wasSuccessful() else 1
elif framework == "pytest":
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest
    observations = report
    class Probe:
        def pytest_collection_finish(self, session):
            report["collected"] = [item.nodeid for item in session.items]
        def pytest_runtest_logreport(self, report):
            if report.when == "call":
                observations["executed"].append(report.nodeid)
            if report.failed:
                observations["failures" if report.when == "call" else "errors"] += 1
            if report.skipped:
                observations["skipped"] += 1
            if getattr(report, "wasxfail", None):
                observations["expected_failures"] += 1
        def pytest_collectreport(self, report):
            if report.failed:
                observations["errors"] += 1
    code = int(pytest.main(args, plugins=[Probe()]))
else:
    raise ValueError("unsupported test framework")
report["exit_code"] = code
print("FORGE_TEST_COMPLETION=" + json.dumps(report, sort_keys=True), flush=True)
sys.exit(code)
'''


def instrument(argv: list[str], nonce: str) -> tuple[list[str], str | None]:
    framework: str | None = None
    args: list[str] = []
    if argv and PurePosixPath(argv[0]).name in {"python", "python3"}:
        python_args = list(argv[1:])
        while python_args and python_args[0] in {"-I", "-B", "-u", "-s", "-E", "-P"}:
            python_args.pop(0)
        if len(python_args) >= 2 and python_args[0] == "-m" and python_args[1] in {"unittest", "pytest"}:
            framework, args = python_args[1], python_args[2:]
    elif argv and PurePosixPath(argv[0]).name == "pytest":
        framework, args = "pytest", argv[1:]
    if framework is None:
        return argv, None
    return ["python", "-I", "-c", RUNNER, framework, json.dumps(args), nonce], framework


def completion(receipt: ExecutionReceipt, framework: str | None, nonce: str) -> dict[str, Any]:
    result: dict[str, Any] = {"supported": framework is not None, "complete": False, "passed": False}
    if framework is None:
        return result
    rows = [line.removeprefix("FORGE_TEST_COMPLETION=") for line in receipt.stdout.splitlines()
            if line.startswith("FORGE_TEST_COMPLETION=")]
    if len(rows) != 1:
        return {**result, "reason": "Missing or ambiguous test-runner completion record"}
    try:
        record = json.loads(rows[0])
        if (record["version"] != 1 or record["nonce"] != nonce or record["framework"] != framework
                or record["exit_code"] != receipt.exit_code):
            raise ValueError("Completion identity mismatch")
        collected, executed = record["collected"], record["executed"]
        if (not isinstance(collected, list) or not isinstance(executed, list) or not collected
                or not all(isinstance(item, str) for item in collected + executed)):
            raise ValueError("No test cases were collected")
        counts = [record[key] for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes")]
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("Invalid test counts")
        return {**result, "complete": True, "passed": receipt.status == "ok" and receipt.exit_code == 0
                and sorted(collected) == sorted(executed) and not any(counts), "record": record}
    except (ValueError, TypeError, KeyError) as error:
        return {**result, "reason": str(error)}
