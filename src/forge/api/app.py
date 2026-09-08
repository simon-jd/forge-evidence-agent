"""Single-user authenticated HTTP control plane with durable event replay."""

import asyncio
import hmac
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge.application.services import PUBLIC_ARTIFACT_KINDS, ForgeService, Unavailable
from forge.artifacts.store import ArtifactError, LocalArtifactStore
from forge.persistence.repository import TERMINAL, Conflict, NotFound, Repository
from forge.settings import Settings


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Body):
    name: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=4096)
    profile_id: str = Field(min_length=1, max_length=128)


class RunCreate(Body):
    project_id: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=32_768)
    base_commit: str = Field(default="HEAD", min_length=1, max_length=200)
    config_id: str = Field(min_length=1, max_length=128)


class MessageCreate(Body):
    text: str = Field(min_length=1, max_length=32_768)


class Resume(Body):
    version: int = Field(ge=1)


class Decision(Body):
    decision: Literal["allow", "deny"]
    version: int = Field(ge=1)


class EvaluationCreate(Body):
    experiment_id: str = Field(min_length=1, max_length=128)
    task_ids: list[str] = Field(min_length=1, max_length=500)
    repeats: int = Field(default=3, ge=1, le=10)
    conditions: list[Literal["B0", "B1", "B2", "B3", "strong_only", "B2_guard_off"]] = ["B0", "B1", "B2", "B3", "strong_only"]
    config_id: str = Field(min_length=1, max_length=128)
    budget_usd: Decimal = Field(gt=0, le=10_000)
    seed: int = 1729


PRIVATE_KEYS = frozenset({"raw_response", "native_state", "native_state_ref", "transcript", "canonical_messages",
                          "api_key", "authorization", "secret", "private_root", "grader_spec", "scripted_turns"})


def public_payload(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[nested data omitted]"
    if isinstance(value, dict):
        return {key: public_payload(item, depth + 1) for key, item in list(value.items())[:100]
                if key.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [public_payload(item, depth + 1) for item in value[:100]]
    return value


def event_frame(event: dict[str, Any]) -> bytes:
    payload = public_payload(event["payload"])
    data = json.dumps({"seq": event["seq"], "type": event["kind"], "payload": payload,
                       "created_at": event["created_at"]}, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode()) > 65_000:
        data = json.dumps({"seq": event["seq"], "type": event["kind"],
                           "payload": {"omitted": True, "reason": "Event exceeds 64 KiB; read its retained artifact."},
                           "created_at": event["created_at"]}, separators=(",", ":"))
    kind = event["kind"] if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", event["kind"]) else "run.event"
    return f"id: {event['seq']}\nevent: {kind}\ndata: {data}\n\n".encode()


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    settings = settings or Settings()
    owns_repository = repository is None
    repo = repository or Repository(settings.database_url)
    service = ForgeService(settings, repo)
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.api_token.get_secret_value():
            raise RuntimeError("Set FORGE_API_TOKEN before starting the control API")
        yield
        if owns_repository:
            await repo.close()

    app = FastAPI(title="Forge Coding Agent API", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.repository, app.state.artifacts = service, repo, artifacts
    bearer = HTTPBearer(auto_error=False)

    async def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
        token = settings.api_token.get_secret_value()
        if not token:
            raise HTTPException(503, "API token is not configured")
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        return settings.owner_id

    def idempotency(key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]) -> str:
        return key

    @app.exception_handler(NotFound)
    async def not_found(req: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(req: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Unavailable)
    async def unavailable(req: Request, exc: Unavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_value(req: Request, exc: ValueError) -> JSONResponse:
        detail = "Local configuration failed schema validation" if isinstance(exc, ValidationError) else str(exc)
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(FileNotFoundError)
    async def missing_local_file(req: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Configured local repository or configuration was not found"})

    @app.exception_handler(ArtifactError)
    async def artifact_error(req: Request, exc: ArtifactError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "Artifact is unavailable or failed integrity verification"})

    @app.post("/v1/projects", status_code=201)
    async def create_project(body: ProjectCreate, owner: Annotated[str, Depends(authorize)]) -> Any:
        return await service.register_project(**body.model_dump())

    @app.post("/v1/runs", status_code=202)
    async def create_run(body: RunCreate, owner: Annotated[str, Depends(authorize)],
                         key: Annotated[str, Depends(idempotency)]) -> Any:
        run = await service.submit_run(**body.model_dump(), idempotency_key=key)
        return public_payload(await service.public_run(run))

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, owner: Annotated[str, Depends(authorize)]) -> Any:
        return public_payload(await service.public_run(await repo.get_run(run_id, owner)))

    @app.post("/v1/runs/{run_id}/messages", status_code=202)
    async def add_message(run_id: str, body: MessageCreate, owner: Annotated[str, Depends(authorize)],
                          key: Annotated[str, Depends(idempotency)]) -> Any:
        return await repo.add_message(run_id, owner, key, body.text)

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str, owner: Annotated[str, Depends(authorize)]) -> Any:
        return public_payload(await service.public_run(await repo.cancel(run_id, owner)))

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, body: Resume, owner: Annotated[str, Depends(authorize)]) -> Any:
        return public_payload(await service.public_run(await repo.resume(run_id, owner, body.version)))

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: Decision, owner: Annotated[str, Depends(authorize)]) -> Any:
        return await repo.decide(approval_id, owner, body.decision, body.version)

    @app.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, request: Request, owner: Annotated[str, Depends(authorize)],
                     last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
                     after: Annotated[int, Query(ge=0)] = 0, follow: bool = True) -> StreamingResponse:
        run = await repo.get_run(run_id, owner)
        if last_event_id is not None:
            if not re.fullmatch(r"[0-9]{1,18}", last_event_id):
                raise HTTPException(422, "Last-Event-ID must be a nonnegative event sequence")
            after = int(last_event_id)
        if after > run["event_seq"]:
            raise HTTPException(409, "Event cursor is ahead of the run")

        async def stream() -> AsyncIterator[bytes]:
            cursor = after
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                page = await repo.list_events(run_id, owner, cursor, limit=100)
                for item in page:
                    yield event_frame(item)
                    cursor = item["seq"]
                if page:
                    continue
                current = await repo.get_run(run_id, owner)
                if not follow or (current["status"] in TERMINAL and cursor >= current["event_seq"]):
                    return
                if time.monotonic() - heartbeat >= 15:
                    yield b": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(settings.event_poll_seconds)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/runs/{run_id}/artifacts")
    async def list_artifacts(run_id: str, owner: Annotated[str, Depends(authorize)]) -> Any:
        rows = await repo.list_artifacts(run_id, owner)
        return [{"id": row["id"], "run_id": row["run_id"], "kind": row["kind"],
                 "details": public_payload(row["details"])} for row in rows if row["kind"] in PUBLIC_ARTIFACT_KINDS]

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, owner: Annotated[str, Depends(authorize)]) -> Response:
        row = await repo.get_artifact(artifact_id, owner)
        if row["kind"] not in PUBLIC_ARTIFACT_KINDS:
            raise NotFound("Public artifact not found")
        metadata = artifacts.metadata(artifact_id)
        if metadata["run_id"] != row["run_id"] or metadata["kind"] != row["kind"]:
            raise ArtifactError("Artifact identity mismatch")
        content = artifacts.read_bytes(artifact_id)
        return Response(content=content, media_type=metadata["content_type"], headers={
            "ETag": f'"{metadata["sha256"]}"', "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{artifact_id}"'})

    @app.post("/v1/evaluations", status_code=202)
    async def create_evaluation(body: EvaluationCreate, owner: Annotated[str, Depends(authorize)]) -> Any:
        if len(set(body.task_ids)) != len(body.task_ids) or len(set(body.conditions)) != len(body.conditions) or not body.conditions:
            raise HTTPException(422, "Tasks and conditions must be nonempty and unique")
        return await service.enqueue_evaluation(body.model_dump(mode="json"))

    @app.get("/v1/evaluations/{evaluation_id}")
    async def get_evaluation(evaluation_id: str, owner: Annotated[str, Depends(authorize)]) -> Any:
        return public_payload(await service.get_evaluation(evaluation_id))

    return app
