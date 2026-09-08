from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env", extra="ignore")
    api_token: SecretStr = SecretStr("")
    api_url: str = "http://127.0.0.1:8765"


class ClientError(RuntimeError):
    pass


class ForgeClient:
    def __init__(self, *, base_url: str | None = None, token: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        settings = ClientSettings()
        token = token if token is not None else settings.api_token.get_secret_value()
        if not token:
            raise ClientError("Set FORGE_API_TOKEN to authenticate with the API")
        api_url = base_url if base_url else settings.api_url
        self.client = httpx.Client(base_url=api_url,
                                  headers={"Authorization": f"Bearer {token}"}, timeout=30,
                                  transport=transport)

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                key: str | None = None) -> httpx.Response:
        try:
            response = self.client.request(method, path, json=body,
                                            headers={"Idempotency-Key": key} if key else None)
            self.check(response)
            return response
        except httpx.TransportError as exc:
            raise ClientError(f"API transport failed: {type(exc).__name__}") from exc

    @staticmethod
    def check(response: httpx.Response) -> None:
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except ValueError:
                detail = response.reason_phrase
            raise ClientError(f"API {response.status_code}: {detail}")

    def watch(self, run_id: str, *, after: int = 0, reconnect_attempts: int = 5) -> Iterator[dict[str, Any]]:
        cursor, failures = after, 0
        while True:
            try:
                with self.client.stream("GET", f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)},
                                         timeout=httpx.Timeout(30, read=25)) as response:
                    if response.is_error:
                        response.read()
                    self.check(response)
                    data: list[str] = []
                    for line in response.iter_lines():
                        if not line:
                            if data:
                                event = json.loads("\n".join(data))
                                seq = int(event["seq"])
                                if seq > cursor:
                                    yield event
                                    cursor = seq
                                    failures = 0
                                data = []
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                status = self.request("GET", f"/v1/runs/{run_id}").json()["status"]
                if status in {"completed", "failed", "cancelled", "budget_exhausted"}:
                    return
                failures += 1
            except httpx.TransportError:
                failures += 1
            except (KeyError, ValueError) as exc:
                raise ClientError("API sent an invalid event frame") from exc
            if failures > reconnect_attempts:
                raise ClientError(f"Event stream disconnected; resume with --after {cursor}")
            time.sleep(min(0.25 * 2 ** (failures - 1), 4))
