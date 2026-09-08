"""CLI controls run lifecycle through public HTTP endpoints only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer

from forge.cli.client import ClientError, ForgeClient

app = typer.Typer(help="Submit, observe, and independently verify coding-agent tasks.", no_args_is_help=True)
eval_app = typer.Typer(help="Submit a bounded experiment or inspect its report.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@contextmanager
def api() -> Iterator[ForgeClient]:
    client = None
    try:
        client = ForgeClient()
        yield client
    except ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        if client is not None:
            client.close()


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def project(source: Annotated[Path, typer.Argument(help="Repository path on the API host")],
            name: Annotated[str, typer.Option()], profile: Annotated[str, typer.Option()]) -> None:
    """Register an allowed repository with an administrator's validation profile."""
    with api() as client:
        emit(client.request("POST", "/v1/projects", body={"name": name, "source": str(source), "profile_id": profile}).json())


@app.command()
def run(project_id: str, task: str, config: Annotated[str, typer.Option()],
        base_commit: Annotated[str, typer.Option()] = "HEAD",
        key: Annotated[str | None, typer.Option(help="Reuse this key only for an identical request")] = None) -> None:
    """Queue a task and return its run ID; watching is independent of execution."""
    request_key = key or uuid.uuid4().hex
    with api() as client:
        result = client.request("POST", "/v1/runs", body={"project_id": project_id, "task": task,
                                 "base_commit": base_commit, "config_id": config}, key=request_key).json()
        emit({**result, "idempotency_key": request_key})


@app.command()
def status(run_id: str) -> None:
    """Show status, usage, verification, and reviewable pending approvals."""
    with api() as client:
        emit(client.request("GET", f"/v1/runs/{run_id}").json())


@app.command()
def watch(run_id: str, after: Annotated[int, typer.Option(min=0)] = 0) -> None:
    """Replay durable events; reconnect from the last processed sequence."""
    with api() as client:
        for event in client.watch(run_id, after=after):
            typer.echo(json.dumps(event, ensure_ascii=False))


@app.command()
def message(run_id: str, text: str, key: Annotated[str | None, typer.Option()] = None) -> None:
    """Append a user instruction for the next safe execution boundary."""
    request_key = key or uuid.uuid4().hex
    with api() as client:
        result = client.request("POST", f"/v1/runs/{run_id}/messages", body={"text": text}, key=request_key).json()
        emit({**result, "idempotency_key": request_key})


@app.command()
def approve(approval_id: str, version: Annotated[int, typer.Option(min=1)],
            deny: Annotated[bool, typer.Option(help="Reject the reviewed operation")] = False) -> None:
    """Decide the exact operation and approval version shown by forge status."""
    with api() as client:
        emit(client.request("POST", f"/v1/approvals/{approval_id}/decision", body={
            "decision": "deny" if deny else "allow", "version": version}).json())


@app.command()
def cancel(run_id: str) -> None:
    """Request cancellation; the worker confirms active jobs have stopped."""
    with api() as client:
        emit(client.request("POST", f"/v1/runs/{run_id}/cancel").json())


@app.command()
def resume(run_id: str, version: Annotated[int, typer.Option(min=1)]) -> None:
    """Reconcile an existing operation using the reviewed run version."""
    with api() as client:
        emit(client.request("POST", f"/v1/runs/{run_id}/resume", body={"version": version}).json())


@app.command()
def diff(run_id: str) -> None:
    """Download the most recent retained patch."""
    with api() as client:
        artifacts = client.request("GET", f"/v1/runs/{run_id}/artifacts").json()
        patches = [item for item in artifacts if item["kind"] in {"patch", "diff"}]
        if not patches:
            raise ClientError("No patch artifact is available yet")
        latest = max(patches, key=lambda item: item.get("details", {}).get("created_at", ""))
        typer.echo(client.request("GET", f"/v1/artifacts/{latest['id']}").text, nl=False)


@eval_app.command("submit")
def eval_submit(config: str, experiment: Annotated[str, typer.Option()],
                tasks: Annotated[str, typer.Option(help="Comma-separated frozen task IDs")],
                budget: Annotated[str, typer.Option(help="Maximum total experiment budget in USD")],
                conditions: Annotated[str, typer.Option()] = "B0,B1,B2,B3,strong_only",
                repeats: Annotated[int, typer.Option(min=1, max=10)] = 3,
                seed: int = 1729) -> None:
    try:
        amount = Decimal(budget)
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
    except InvalidOperation as exc:
        raise typer.BadParameter("budget must be a positive USD amount") from exc
    with api() as client:
        emit(client.request("POST", "/v1/evaluations", body={"experiment_id": experiment,
            "task_ids": [item.strip() for item in tasks.split(",") if item.strip()], "repeats": repeats,
            "conditions": [item.strip() for item in conditions.split(",") if item.strip()],
            "config_id": config, "budget_usd": str(amount), "seed": seed}).json())


@eval_app.command("status")
def eval_status(evaluation_id: str) -> None:
    with api() as client:
        emit(client.request("GET", f"/v1/evaluations/{evaluation_id}").json())
