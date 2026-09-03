from __future__ import annotations

import stat
from pathlib import Path

import pytest

from hermes_resync_deploy.backup import create_active_release_snapshot
from hermes_resync_deploy.controller import DeploymentBlocked, DeploymentController, GateBReceiptVerifier, GateBRequired
from hermes_resync_deploy.manifest import candidate_digest
from hermes_resync_deploy.model import CandidateComposition, HealthReport, PriorRuntimeSnapshot, SlotIdentity, TransactionState


class Healthy:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy, self.calls = healthy, []

    def verify_slot(self, slot: SlotIdentity, composition: CandidateComposition) -> HealthReport:
        self.calls.append(f"{slot.name}:{candidate_digest(composition)}")
        return HealthReport(self.healthy, {}, ("not_ready",) if not self.healthy else (), {})


def composition(marker: str) -> CandidateComposition:
    return CandidateComposition(*([marker] * 13))


def controller(tmp_path: Path, *, healthy: bool = True, fail_start: bool = False):
    candidate, prior = composition("a"), composition("b")
    blue = SlotIdentity("blue", tmp_path / "blue", "old", candidate_digest(prior))
    green = SlotIdentity("green", tmp_path / "green", "new", candidate_digest(candidate))
    source = tmp_path / "source"; source.mkdir(); (source / "code.py").write_text("x = 1\n")
    snapshot = create_active_release_snapshot(source, tmp_path / "cas", ["code.py"], composition_digest=blue.candidate_digest)
    selector, actions, health = {"value": "blue"}, [], Healthy(healthy)
    verifier = GateBReceiptVerifier(b"disposable-test-key")

    def cas(expected: str | None, target: str) -> bool:
        actions.append("selector")
        if selector["value"] != expected: return False
        selector["value"] = target; return True
    def start(slot: SlotIdentity) -> SlotIdentity:
        actions.append("start:" + slot.name)
        if fail_start and slot.name == "green": raise RuntimeError("green startup failed")
        return slot
    deploy = DeploymentController(candidate, health, selector_read=lambda: selector["value"], selector_cas=cas,
        fence=lambda slot: actions.append("fence:" + slot.name), start=start,
        retire=lambda slot: actions.append("retire:" + slot.name), restore=lambda _snapshot, slot: slot,
        gate_b_verifier=verifier, prior_composition=prior, journal_dir=tmp_path / "journal")
    return deploy, actions, health, blue, green, snapshot, verifier


def test_health_before_selector_and_candidate_bound_gate_b_rejection(tmp_path: Path) -> None:
    deploy, actions, _, blue, green, snapshot, verifier = controller(tmp_path, healthy=False)
    deploy.prepare("t", blue, green, snapshot, "blue")
    with pytest.raises(GateBRequired): deploy.activate(None)
    with pytest.raises(GateBRequired): deploy.activate("approved")
    with pytest.raises(GateBRequired): deploy.activate(verifier.issue_for_test("0" * 64))
    with pytest.raises(DeploymentBlocked): deploy.activate(verifier.issue_for_test(green.candidate_digest))
    assert "selector" not in actions


def test_durable_journal_lock_and_distinct_blue_rollback_identity(tmp_path: Path) -> None:
    deploy, actions, health, blue, green, snapshot, verifier = controller(tmp_path, fail_start=True)
    deploy.prepare("t", blue, green, snapshot, "blue")
    journal, lock = tmp_path / "journal" / "transaction.json", tmp_path / "journal" / "transaction.lock"
    assert journal.exists() and stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
    with pytest.raises(DeploymentBlocked):
        deploy.activate(verifier.issue_for_test(green.candidate_digest))
    assert "fence:green" in actions
    assert any(call.startswith("blue-rollback-") and call.endswith(f":{blue.candidate_digest}") for call in health.calls)
    assert deploy.record is not None and deploy.record.state is TransactionState.ROLLED_BACK


def test_loaded_record_rollback_cannot_bypass_another_writer_lock(tmp_path: Path) -> None:
    owner, _, _, blue, green, snapshot, verifier = controller(tmp_path)
    record = owner.prepare("t", blue, green, snapshot, "blue")
    contender_root = tmp_path / "contender"; contender_root.mkdir()
    contender, actions, _, _, _, _, _ = controller(contender_root)
    contender._journal_dir = tmp_path / "journal"  # loaded-record transaction uses owner's durable lock
    contender.record = record

    with pytest.raises(DeploymentBlocked, match="exclusive lock"):
        contender.rollback(verifier.issue_for_test(green.candidate_digest))

    assert actions == []


def test_recovery_with_unrelated_selector_is_manual_blocked_without_gate_b_action(tmp_path: Path) -> None:
    deploy, _, _, blue, green, snapshot, _ = controller(tmp_path)
    deploy.prepare("t", blue, green, snapshot, "blue")
    deploy._selector_read = lambda: "unknown"  # disposable seam
    record = deploy.recover(deploy.record)
    assert record.state is TransactionState.BLOCKED
    assert record.detail["phase"] == "manual_blocked"
