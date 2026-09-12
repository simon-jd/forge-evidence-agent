"""Run the fixed DeepSeek pilot under one durable USD 10 authorization.

Default invocation prepares/inspects the plan without network or paid requests.
Use --execute after configuring DEEPSEEK_API_KEY locally. Existing paid progress
is never restarted automatically. All private runtime data stays under .forge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import yaml
from psycopg import sql
from sqlalchemy.engine import make_url

from forge.application.services import RunFile, freeze_local_image
from forge.context.budget import BudgetManager
from forge.domain.types import RepoProfile, RunConfig, canonical_hash
from forge.evals.campaign import CampaignLedger, CampaignPlan, CampaignRefused, CampaignSlot
from forge.evals.contracts import AgentTrialOutcome, ExperimentCondition, PublicTrialRequest
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.grader import DockerGrader
from forge.evals.harness import EvalHarness, standard_conditions
from forge.evals.report import write_report
from forge.evals.worker import ServiceRunCallback
from forge.execution.docker import DockerExecutor
from forge.persistence.repository import Repository
from forge.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
DESCRIPTION = ROOT / "configs/campaigns/deepseek-10usd.json"


def load_plan() -> tuple[CampaignPlan, dict[str, Any], dict[str, RunConfig], list[ExperimentCondition]]:
    description = json.loads(DESCRIPTION.read_text())
    run_file = ROOT / "configs/runs" / (description["run_config"] + ".yaml")
    config = RunFile.model_validate(yaml.safe_load(run_file.read_text())).run
    conditions = standard_conditions(config)
    if [item.name for item in conditions] != description["conditions"]:
        raise ValueError("Campaign conditions differ from the fixed experimental matrix")
    dataset = Dataset(ROOT / "evals")
    tasks = {task.task_id: task for task in dataset.tasks}
    selected = description["task_ids"]
    if len(selected) != 12 or len(set(selected)) != 12 or any(tasks[key].split != "heldout" for key in selected):
        raise ValueError("This authorization is for twelve distinct, frozen heldout tasks")
    if description["repeats"] != 1 or Decimal(description["authorization_usd"]) != Decimal("10"):
        raise ValueError("Changing the authorized matrix or total requires a separate reviewed plan")
    smoke_configs = {}
    slots = {}
    identifier = description["campaign_id"]
    for slot_id, definition in description.get("historical_slots", {}).items():
        slots[slot_id] = CampaignSlot.model_validate(definition)
    for model_id in description["smoke_models"]:
        smoke = config.model_copy(deep=True)
        smoke.models = [next(model for model in config.models if model.model_id == model_id)]
        smoke.initial_model, smoke.max_escalations = 0, 0
        smoke.budget.max_cost_usd = Decimal(description["smoke_limit_each_usd"])
        smoke_configs[model_id] = smoke
        slot_id = description.get("smoke_slot_ids", {}).get(model_id, "smoke-" + model_id)
        slots[slot_id] = CampaignSlot(config_hash=canonical_hash(smoke),
                                                limit_usd=smoke.budget.max_cost_usd, kind="smoke")
    for task_id in selected:
        for condition in conditions:
            slot_id = f"{identifier}-{task_id}-{condition.name}-0"
            slots[slot_id] = CampaignSlot(config_hash=canonical_hash(condition.config),
                                          limit_usd=condition.config.budget.max_cost_usd, kind="evaluation")
    plan = CampaignPlan(campaign_id=identifier, authorization_usd=Decimal("10"), slots=slots,
                        dataset_hash=canonical_hash(json.loads((ROOT / "evals/manifest.json").read_text())))
    allocated = sum((slot.limit_usd for slot in slots.values()), Decimal(0))
    if allocated != Decimal(description["total_allocation_usd"]):
        raise ValueError("This pilot must retain its reviewed allocation")
    return plan, description, smoke_configs, conditions


def validate_smoke(evidence: dict[str, Any], config: RunConfig, run_id: str | None = None) -> None:
    """A reconciled bill alone does not certify that a development smoke passed."""
    if (evidence.get("status") != "completed"
            or evidence.get("verification", {}).get("status") != "verified"
            or evidence.get("execution_mode") != "live"
            or evidence.get("manifest", {}).get("config_hash") != canonical_hash(config)
            or not evidence.get("source_unchanged")
            or not evidence.get("receipts")
            or not all(receipt.get("rootless") for receipt in evidence["receipts"])
            or not evidence.get("run_id")
            or (run_id is not None and evidence["run_id"] != run_id)):
        raise CampaignRefused("A current-configuration smoke lacks matching independent verification")


def check_resume(plan: CampaignPlan, ledger: CampaignLedger, resume: bool) -> None:
    state = ledger.snapshot()
    if state["halted"]:
        raise CampaignRefused("Campaign is halted; inspect accounting before further work")
    if any(row["state"] != "pending" for row in state["slots"].values()) and not resume:
        raise CampaignRefused("Campaign has prior progress; reconcile it and explicitly resume the development gate")
    if resume and (any(row["state"] in {"claimed", "uncertain"} for row in state["slots"].values())
                   or any(plan.slots[key].kind == "evaluation" and row["state"] != "pending"
                          for key, row in state["slots"].items())):
        raise CampaignRefused("Only reconciled development progress can resume before any heldout trial starts")


def provision_database(settings: Settings, name: str, env: dict[str, str]) -> Settings:
    """Create only the campaign database if absent; never drop or reset data."""
    url = make_url(settings.database_url)
    target = url.set(drivername="postgresql+psycopg", database=name)
    admin_url = url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    result = settings.model_copy(update={"database_url": target.render_as_string(hide_password=False)})
    env["FORGE_DATABASE_URL"] = result.database_url
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                   env=env, check=True, timeout=60, capture_output=True)
    return result


async def execute(plan: CampaignPlan, description: dict[str, Any], smokes: dict[str, RunConfig],
                  conditions: list[ExperimentCondition], ledger: CampaignLedger, root: Path,
                  *, image: str, resume: bool = False) -> None:
    settings = Settings()
    key = settings.deepseek_api_key
    if key is None or not key.get_secret_value():
        raise CampaignRefused("Set DEEPSEEK_API_KEY in the local .env; no paid request was sent")
    check_resume(plan, ledger, resume)
    # Reject an unsuitable daemon before reserving or contacting the model API.
    executor = DockerExecutor(settings.docker_host, allow_rootful=False)
    await asyncio.to_thread(executor._client)
    image_id = await asyncio.to_thread(freeze_local_image, image, settings.docker_host)
    dataset = Dataset(ROOT / "evals")
    private = PrivateSpecStore(settings.eval_private_dir or ROOT / ".forge/private-evals")
    tasks = [task for task in dataset.tasks if task.task_id in description["task_ids"]]
    for task in tasks:
        private.load(task)  # Missing/tampered private specs must fail before paid work.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as http:
        response = await http.get("https://api.deepseek.com/models",
                                  headers={"Authorization": "Bearer " + key.get_secret_value()})
        if response.status_code != 200:
            raise CampaignRefused(f"DeepSeek credential/model preflight failed with HTTP {response.status_code}; no generation requested")
        available = {item["id"] for item in response.json()["data"]}
    if not set(description["smoke_models"]).issubset(available):
        raise CampaignRefused("The enabled account does not expose both frozen model IDs")
    (root / "preflight.json").write_text(json.dumps({"model_ids": sorted(available), "image_id": image_id,
                                                    "plan_hash": canonical_hash(plan)}, indent=2) + "\n")
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = key.get_secret_value()
    settings = await asyncio.to_thread(provision_database, settings,
        "forge_deepseek_10usd_20260911_v1", env)
    settings = settings.model_copy(update={"data_dir": root / "runtime", "config_dir": root / "config",
                                           "owner_id": plan.campaign_id, "allowed_repo_roots": [root]})
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    env.update({"FORGE_DATA_DIR": str(settings.data_dir), "FORGE_CONFIG_DIR": str(settings.config_dir),
                "FORGE_OWNER_ID": settings.owner_id, "FORGE_ALLOWED_REPO_ROOTS": json.dumps([str(root)]),
                "FORGE_ALLOW_ROOTFUL_DEVELOPMENT": "false"})
    if settings.docker_host:
        env["FORGE_DOCKER_HOST"] = settings.docker_host
    for model_id, config in smokes.items():
        slot = description.get("smoke_slot_ids", {}).get(model_id, "smoke-" + model_id)
        config_path, output = root / (slot + ".yaml"), root / (slot + ".json")
        previous = ledger.snapshot()["slots"][slot]
        if previous["state"] == "finished":
            validate_smoke(json.loads(output.read_text()), config, previous.get("run_id"))
            continue
        config_path.write_text(yaml.safe_dump({"execution_mode": "live", "run": config.model_dump(mode="json")}))
        ledger.claim(slot, canonical_hash(config))
        try:
            argv = [sys.executable, str(ROOT / "scripts/demo.py"), "--root", str(root / slot),
                    "--output", str(output), "--live-config", str(config_path),
                    "--authorized-budget-usd", str(config.budget.max_cost_usd),
                    "--image", image_id, "--timeout", "600"]
            with (root / (slot + ".log")).open("w") as log:
                await asyncio.to_thread(subprocess.run, argv, cwd=ROOT, env=env, check=True,
                                         timeout=700, stdout=log, stderr=subprocess.STDOUT)
            evidence = json.loads(output.read_text())
            validate_smoke(evidence, config)
            ledger.attach_run(slot, evidence["run_id"])
            budget = BudgetManager(config.budget, evidence["budget"])
            ledger.settle(slot, budget.committed_usd, budget.cost_status, evidence=str(output))
            print(json.dumps({"smoke": model_id, "status": evidence["status"],
                              "cost_upper_or_unknown_usd": str(budget.committed_usd)}), flush=True)
        except Exception:
            ledger.settle(slot, None, "unknown", evidence=str(root / (slot + ".log")))
            # Investigate compatibility using dev evidence before heldout work.
            raise CampaignRefused("A live smoke did not verify; its full allocation remains occupied. Inspect the private log before further work") from None
    repository = Repository(settings.database_url)
    profile = RepoProfile(name="deepseek-pilot", image=image_id, command_timeout_seconds=30,
        validation_argv=["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_public.py"])
    grader = DockerGrader(root / "private-grader", executor)
    harness = EvalHarness(dataset, private, grader, root / "repositories", profile)

    async def progress(update: dict[str, Any]) -> None:
        if update.get("active_run_id"):
            ledger.attach_run(update["active_trial_id"], update["active_run_id"])

    callback = ServiceRunCallback(settings, repository, plan.campaign_id, progress)

    async def paid_trial(request: PublicTrialRequest) -> AgentTrialOutcome:
        ledger.claim(request.trial_id, canonical_hash(request.condition.config))
        try:
            outcome = await callback(request)
        except BaseException:
            ledger.settle(request.trial_id, None, "unknown")
            raise
        amount = (outcome.cost_usd or Decimal(0)) + outcome.reserved_unknown_usd
        ledger.settle(request.trial_id, amount, outcome.cost_status)
        print(json.dumps({"trial": request.trial_id, "agent_status": outcome.status,
                          "campaign_accounted_upper_or_unknown_usd": ledger.snapshot()["committed_upper_or_unknown_usd"]}), flush=True)
        return outcome

    try:
        with (root / "worker.log").open("w") as log:
            process = await asyncio.to_thread(subprocess.Popen, [sys.executable, "-m", "forge.worker"],
                                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                trials = await harness.run(tasks, conditions, paid_trial, root / "trials.jsonl",
                    repeats=1, seed=description["seed"], experiment_id=plan.campaign_id,
                    authorized_total_cost_usd=Decimal(description["evaluation_allocation_usd"]))
                await asyncio.to_thread(write_report, trials, root / "report")
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
    finally:
        await repository.close()
        if executor._docker is not None:
            await asyncio.to_thread(executor._docker.close)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="Start the previously authorized paid campaign")
    modes.add_argument("--resume", action="store_true", help="Continue reconciled development smoke progress; never replay a claimed run")
    modes.add_argument("--amend-plan", action="store_true", help="Record a reviewed pending-only development amendment; makes no model calls")
    parser.add_argument("--image", default="forge-runner:dev", help="Trusted local runner image, frozen before calls")
    args = parser.parse_args()
    plan, description, smokes, conditions = load_plan()
    root = ROOT / ".forge/campaigns" / plan.campaign_id
    path = root / "ledger.json"
    if args.amend_plan:
        previous = CampaignPlan.model_validate(json.loads(path.read_text())["plan"])
        ledger = CampaignLedger(path, previous)
        ledger.amend_plan(plan, description["amendment_reason"])
    else:
        ledger = CampaignLedger(path, plan)
    print(json.dumps({"campaign": plan.campaign_id, "runs": len(plan.slots),
                      "authorization_usd": str(plan.authorization_usd),
                      "allocation_usd": description["total_allocation_usd"],
                      "ledger": str(ledger.path), "execute_requested": args.execute or args.resume}), flush=True)
    if args.execute or args.resume:
        asyncio.run(execute(plan, description, smokes, conditions, ledger, root, image=args.image,
                            resume=args.resume))


if __name__ == "__main__":
    main()
