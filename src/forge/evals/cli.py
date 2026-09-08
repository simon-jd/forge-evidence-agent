from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from forge.domain.types import RepoProfile
from forge.evals.contracts import EvalTrial
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.grader import DockerGrader
from forge.evals.qualification import qualify_dataset
from forge.evals.report import write_report
from forge.execution.docker import DockerExecutor
from forge.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Forge independent evaluation utilities")
    commands = parser.add_subparsers(dest="command", required=True)
    qualify = commands.add_parser("qualify", help="Validate defects and private references; no model requests")
    qualify.add_argument("--dataset", type=Path, default=Path("evals"))
    qualify.add_argument("--private", type=Path, default=Path(".forge/private-evals"))
    qualify.add_argument("--image", required=True, help="Prebuilt immutable image ID or locally resolvable tag")
    qualify.add_argument("--output", type=Path, default=Path(".forge/eval-qualification.json"))
    report = commands.add_parser("report", help="Recompute CSV, JSON metrics and HTML from original trial JSONL")
    report.add_argument("trials", type=Path)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "report":
        trials = [EvalTrial.model_validate_json(line) for line in args.trials.read_text().splitlines() if line.strip()]
        paths = write_report(trials, args.output)
        print(json.dumps({name: str(path) for name, path in paths.items()}))
        return
    settings = Settings()

    async def run() -> None:
        identifier = "qualification-" + uuid.uuid4().hex[:12]
        executor = DockerExecutor(settings.docker_host, settings.allow_rootful_development)
        result = await qualify_dataset(
            Dataset(args.dataset), PrivateSpecStore(args.private),
            DockerGrader(args.private / "workspaces" / identifier, executor),
            settings.data_dir / "qualifications" / identifier,
            RepoProfile(name="diagnostic-python", image=args.image, command_timeout_seconds=30,
                        validation_argv=["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_public.py"]),
            args.output, qualification_id=identifier,
        )
        if executor._docker is not None:
            await asyncio.to_thread(executor._docker.close)
        print(json.dumps({"checked": result["tasks_checked"], "qualified": result["qualified"], "report": str(args.output)}))
        if result["qualified"] != len(result["rows"]):
            raise SystemExit(1)

    asyncio.run(run())


if __name__ == "__main__":
    main()
