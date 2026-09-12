"""A fixed, persistent authorization shared by smoke tests and evaluations.

Claim before enqueueing a paid run. A claimed operation is never automatically
replayed, including after a crash before its run ID became known. The operator
may reconcile the original run, but must not reset a slot to pending.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from forge.artifacts.store import atomic_write
from forge.domain.types import Contract, canonical_hash


class CampaignSlot(Contract):
    config_hash: str
    limit_usd: Decimal = Field(gt=0)
    kind: Literal["smoke", "evaluation"]


class CampaignPlan(Contract):
    campaign_id: str
    authorization_usd: Decimal = Field(gt=0)
    slots: dict[str, CampaignSlot]
    dataset_hash: str

    @model_validator(mode="after")
    def bounded(self) -> CampaignPlan:
        if not self.slots or not self.authorization_usd.is_finite():
            raise ValueError("Campaign requires a finite authorization and a fixed nonempty plan")
        if sum((slot.limit_usd for slot in self.slots.values()), Decimal(0)) > self.authorization_usd:
            raise ValueError("Fixed campaign allocation exceeds the authorized total")
        return self


class CampaignRefused(ValueError):
    pass


class CampaignLedger:
    def __init__(self, path: Path, plan: CampaignPlan) -> None:
        self.path, self.plan = Path(path), plan.model_copy(deep=True)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock():
            if self.path.exists():
                self._read()
            else:
                self._write({"version": 1, "plan": self.plan.model_dump(mode="json"),
                             "plan_hash": canonical_hash(self.plan), "halted": False,
                             "slots": {key: {"state": "pending", "reserved_usd": "0"}
                                       for key in self.plan.slots}})

    @contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text())
        if (data.get("plan_hash") != canonical_hash(self.plan)
                or data.get("plan") != self.plan.model_dump(mode="json")
                or set(data.get("slots", {})) != set(self.plan.slots)):
            raise CampaignRefused("Existing campaign does not match the frozen plan; do not reset its budget")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write(self.path, (json.dumps(data, indent=2) + "\n").encode())
        self.path.chmod(0o600)

    def snapshot(self) -> dict[str, Any]:
        with self._lock():
            data = self._read()
        committed = sum((Decimal(row["reserved_usd"]) for row in data["slots"].values()), Decimal(0))
        return {**data, "committed_upper_or_unknown_usd": str(committed),
                "unallocated_margin_usd": str(self.plan.authorization_usd - sum(
                    (slot.limit_usd for slot in self.plan.slots.values()), Decimal(0)))}

    def claim(self, slot_id: str, config_hash: str) -> None:
        with self._lock():
            data = self._read()
            definition = self.plan.slots.get(slot_id)
            if definition is None or definition.config_hash != config_hash:
                raise CampaignRefused("Unplanned run or changed configuration")
            if data["halted"] or data["slots"][slot_id]["state"] != "pending":
                raise CampaignRefused("Campaign halted or slot already claimed; reconcile the original run")
            committed = sum((Decimal(row["reserved_usd"]) for row in data["slots"].values()), Decimal(0))
            if committed + definition.limit_usd > self.plan.authorization_usd:
                raise CampaignRefused("No authorized campaign budget remains")
            data["slots"][slot_id] = {"state": "claimed", "reserved_usd": str(definition.limit_usd),
                                      "claimed_at": datetime.now(UTC).isoformat()}
            self._write(data)

    def attach_run(self, slot_id: str, run_id: str) -> None:
        with self._lock():
            data = self._read()
            row = data["slots"][slot_id]
            if row["state"] not in {"claimed", "uncertain"} or row.get("run_id", run_id) != run_id:
                raise CampaignRefused("Cannot replace the original paid run")
            row["run_id"] = run_id
            self._write(data)

    def settle(self, slot_id: str, amount: Decimal | None, cost_status: str,
               *, evidence: str | None = None) -> None:
        if amount is not None and (not amount.is_finite() or amount < 0):
            raise ValueError("Invalid reconciled cost")
        if cost_status not in {"exact", "upper_bound", "unknown", "estimated"}:
            raise ValueError("Unknown cost classification")
        with self._lock():
            data = self._read()
            row = data["slots"][slot_id]
            result = {"amount_usd": str(amount) if amount is not None else None,
                      "cost_status": cost_status, "evidence": evidence}
            if row["state"] == "finished":
                if row.get("result") != result:
                    raise CampaignRefused("Completed campaign accounting is immutable")
                return
            if row["state"] not in {"claimed", "uncertain"}:
                raise CampaignRefused("Cannot settle an unclaimed slot")
            allocation = self.plan.slots[slot_id].limit_usd
            row["reserved_usd"] = str(amount if amount is not None and cost_status in {"exact", "upper_bound"}
                                      else max(allocation, amount or Decimal(0)))
            if row.get("result") is not None:
                row.setdefault("reconciliations", []).append(row["result"])
            certain = amount is not None and cost_status in {"exact", "upper_bound"}
            row.update(state="finished" if certain else "uncertain", result=copy.deepcopy(result),
                       reconciled_at=datetime.now(UTC).isoformat())
            if amount is not None and amount > allocation:
                data["halted"] = True
                data["halt_reason"] = "Reported cost exceeded the original run allocation; no further paid work"
            self._write(data)
