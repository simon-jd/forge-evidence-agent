from pathlib import Path

import pytest

from forge.context import EvidenceLedger
from forge.retrieval import RetrievalError


def test_evidence_detects_external_edit_missing_and_revert(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("answer = 1\nnext = 2\n")
    ledger = EvidenceLedger(tmp_path)
    old = ledger.register("a.py", 1, 1, "read-1")
    assert old.excerpt == "answer = 1\n"
    source.write_text("answer = 3\nnext = 2\n")
    refreshed = ledger.refresh()
    assert refreshed[0].status == "stale"
    assert refreshed[0].excerpt == old.excerpt  # Historical evidence is immutable.
    new = ledger.register("a.py", 1, 1, "read-2")
    assert new.evidence_id != old.evidence_id
    source.unlink()
    assert {item.status for item in ledger.refresh()} == {"missing"}
    source.write_text("answer = 1\nnext = 2\n")
    assert [item.status for item in ledger.refresh()] == ["current", "stale"]


def test_matching_hash_does_not_make_forged_excerpt_current(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("secret = False\n")
    ledger = EvidenceLedger(tmp_path)
    anchor = ledger.register("a.py")
    forged = anchor.model_copy(update={"excerpt": "secret = True\n"})
    assert EvidenceLedger(tmp_path, [forged]).refresh()[0].status == "stale"
    with pytest.raises(ValueError, match="rebound"):
        ledger.add(forged)


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", ".git/config", "a/../b", "a\\b"])
def test_path_escape_and_protected_paths_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(RetrievalError):
        EvidenceLedger(tmp_path).register(path)


def test_symlink_parent_cannot_supply_evidence(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("/etc", target_is_directory=True)
    with pytest.raises(RetrievalError):
        EvidenceLedger(tmp_path).register("link/passwd")


def test_validation_pass_expires_after_any_snapshot_or_profile_change(tmp_path: Path) -> None:
    ledger = EvidenceLedger(tmp_path)
    observation = ledger.register_validation("v1", True, 3, "snapshot1", "profile1", "artifact1")
    assert observation.status == "current"
    assert ledger.refresh_validations(3, "snapshot1", "profile1")[0].status == "current"
    assert ledger.refresh_validations(4, "snapshot1", "profile1")[0].status == "historical"
    assert ledger.refresh_validations(3, "snapshot2", "profile1")[0].status == "historical"
    assert ledger.refresh_validations(3, "snapshot1", "profile2")[0].status == "historical"
    assert observation.refresh(None, None, None).status == "historical"
