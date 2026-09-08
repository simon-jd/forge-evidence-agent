"""Content-addressed observations survive edits without becoming current facts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from forge.domain.types import Contract, EvidenceAnchor
from forge.retrieval.code import FileSnapshot, RetrievalError, read_snapshot


class ValidationEvidence(Contract):
    validation_id: str
    passed: bool
    workspace_revision: int
    snapshot_sha256: str
    profile_sha256: str
    artifact_ref: str
    status: Literal["current", "historical"] = "current"

    def refresh(
        self, workspace_revision: int | None, snapshot_sha256: str | None,
        profile_sha256: str | None,
    ) -> ValidationEvidence:
        current = (workspace_revision == self.workspace_revision
                   and bool(snapshot_sha256) and snapshot_sha256 == self.snapshot_sha256
                   and bool(profile_sha256) and profile_sha256 == self.profile_sha256)
        return self.model_copy(update={"status": "current" if current else "historical"})


class EvidenceLedger:
    def __init__(self, workspace: Path, anchors: Iterable[EvidenceAnchor] = ()) -> None:
        self.workspace = Path(workspace)
        self._anchors: dict[str, EvidenceAnchor] = {}
        for anchor in anchors:
            self.add(anchor)
        self._validations: dict[str, ValidationEvidence] = {}

    @property
    def validations(self) -> list[ValidationEvidence]:
        return [item.model_copy(deep=True) for item in self._validations.values()]

    def register_validation(
        self, validation_id: str, passed: bool, workspace_revision: int,
        snapshot_sha256: str, profile_sha256: str, artifact_ref: str,
    ) -> ValidationEvidence:
        if not snapshot_sha256 or not profile_sha256 or not artifact_ref:
            raise ValueError("validation evidence requires snapshot, profile and artifact provenance")
        item = ValidationEvidence(
            validation_id=validation_id, passed=passed, workspace_revision=workspace_revision,
            snapshot_sha256=snapshot_sha256, profile_sha256=profile_sha256, artifact_ref=artifact_ref,
        )
        if validation_id in self._validations and self._validations[validation_id].model_dump(exclude={"status"}) != item.model_dump(exclude={"status"}):
            raise ValueError("validation ID cannot be rebound")
        self._validations[validation_id] = item
        return item.model_copy(deep=True)

    def refresh_validations(
        self, workspace_revision: int, snapshot_sha256: str, profile_sha256: str,
    ) -> list[ValidationEvidence]:
        self._validations = {
            key: item.refresh(workspace_revision, snapshot_sha256, profile_sha256)
            for key, item in self._validations.items()
        }
        return self.validations

    @property
    def anchors(self) -> list[EvidenceAnchor]:
        return [anchor.model_copy(deep=True) for anchor in self._anchors.values()]

    def register(
        self, path: str, start_line: int = 1, end_line: int | None = None,
        origin_call_id: str | None = None,
    ) -> EvidenceAnchor:
        anchor = read_snapshot(self.workspace, path).anchor(start_line, end_line, origin_call_id)
        self._anchors[anchor.evidence_id] = anchor
        return anchor.model_copy(deep=True)

    def add(self, anchor: EvidenceAnchor) -> None:
        previous = self._anchors.get(anchor.evidence_id)
        if previous is not None and previous.model_dump(exclude={"status"}) != anchor.model_dump(exclude={"status"}):
            raise ValueError("an evidence ID cannot be rebound to different content")
        self._anchors[anchor.evidence_id] = anchor.model_copy(deep=True)

    def refresh(self) -> list[EvidenceAnchor]:
        snapshots: dict[str, FileSnapshot | None] = {}
        for anchor in self._anchors.values():
            if anchor.path not in snapshots:
                try:
                    snapshots[anchor.path] = read_snapshot(self.workspace, anchor.path)
                except RetrievalError:
                    snapshots[anchor.path] = None
            snapshot = snapshots[anchor.path]
            if snapshot is None:
                anchor.status = "missing"
                continue
            # Hash alone is insufficient: a caller could attach a forged excerpt
            # to an otherwise genuine hash. Validate the claimed line range too.
            try:
                actual = snapshot.anchor(anchor.start_line, anchor.end_line)
                same_excerpt = actual.end_line == anchor.end_line and actual.excerpt == anchor.excerpt
            except RetrievalError:
                same_excerpt = False
            anchor.status = "current" if snapshot.sha256 == anchor.sha256 and same_excerpt else "stale"
        return self.anchors
