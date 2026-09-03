from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_resync_deploy.backup import create_active_release_snapshot
from hermes_resync_deploy.controller import DeploymentBlocked, DeploymentController
from hermes_resync_deploy.health import HealthVerifier
from hermes_resync_deploy.manifest import candidate_digest
from hermes_resync_deploy.model import CandidateComposition, SlotIdentity, TransactionRecord, TransactionState


def composition(marker: str = "a") -> CandidateComposition:
    return CandidateComposition(
        candidate_source_sha=marker * 40, candidate_tree_digest=marker * 64,
        plugin_wheel_digest=marker * 64, plugin_version="1.0.0", plugin_sbom_digest=marker * 64,
        plugin_lock_digest=marker * 64, skill_package_digest=marker * 64,
        skill_mode_manifest_digest=marker * 64, controller_digest=marker * 64,
        controller_lock_digest=marker * 64, dependency_closure_digest=marker * 64,
        test_wrapper_version="test-wrapper", artifact_tree_digest=marker * 64,
    )


class FakeSeams:
    """Hermetic callables; no process, service, or selector is real."""

    def __init__(self, selector: str | None, *, cas_result: bool = True) -> None:
        self.selector = selector
        self.cas_result = cas_result
        self.calls: list[tuple[str, str | None]] = []

    def selector_read(self) -> str | None:
        self.calls.append(("read_selector", None))
        return self.selector

    def selector_cas(self, expected: str | None, replacement: str) -> bool:
        self.calls.append(("cas_selector", f"{expected}->{replacement}"))
        if self.cas_result:
            self.selector = replacement
        return self.cas_result

    def fence(self, slot: SlotIdentity) -> None:
        self.calls.append(("fence", slot.name))

    def start(self, slot: SlotIdentity) -> SlotIdentity:
        self.calls.append(("start", slot.name))
        return replace(slot, pid=slot.pid or 99)

    def retire(self, slot: SlotIdentity) -> None:
        self.calls.append(("retire", slot.name))


def controller(seams: FakeSeams, candidate: CandidateComposition, prior: CandidateComposition, root: Path) -> DeploymentController:
    health = HealthVerifier(["hermes-resync"], evidence_reader=lambda slot: {
        "ready": True, "slot": slot.name, "candidate_digest": slot.candidate_digest,
        "revision": slot.revision,
        "plugins": ["hermes-resync"],
        "sidecar_ready": True, "writer_count": 1, "kanban": False,
        "kanban_processes": [], "forbidden_writes": [], "pid": slot.pid,
    })
    return DeploymentController(
        candidate, health, selector_read=seams.selector_read, selector_cas=seams.selector_cas,
        fence=seams.fence, start=seams.start, retire=seams.retire,
        gate_b_validator=lambda receipt: receipt == "gate-b-receipt",
        prior_composition=prior,
        journal_dir=root / "journal",
    )


def record(root: Path, candidate: CandidateComposition, prior: CandidateComposition, phase: str, observed: str | None) -> TransactionRecord:
    source = root / "source"
    source.mkdir()
    (source / "release.py").write_text("release code\n", encoding="utf-8")
    digest, prior_digest = candidate_digest(candidate), candidate_digest(prior)
    snapshot = create_active_release_snapshot(source, root / "cas", ["release.py"], composition_digest=prior_digest)
    blue = SlotIdentity("blue", root / "blue", "prior", prior_digest, pid=11)
    green = SlotIdentity("green", root / "green", "candidate", digest, pid=22)
    return TransactionRecord(
        transaction_id="transaction", state=TransactionState.HEALTHY, candidate_digest=digest,
        blue=blue, green=green, prior_snapshot=snapshot, selector_expected="blue",
        selector_observed=observed, detail={"phase": phase},
    )


class ControllerRecoveryTests(unittest.TestCase):
    def test_prepared_recovery_fences_green_residue_without_replacing_running_blue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = composition()
            prior = composition("b")
            seams = FakeSeams("blue")
            recovered = controller(seams, candidate, prior, root).recover(
                record(root, candidate, prior, "prepared", "blue"), "gate-b-receipt"
            )

        self.assertEqual(recovered.state, TransactionState.ROLLED_BACK)
        self.assertEqual(recovered.detail["phase"], "prepared_green_fenced")
        self.assertEqual(seams.calls, [("read_selector", None), ("fence", "green")])

    def test_interrupted_uncommitted_transaction_rolls_back_green_and_retains_blue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = composition()
            prior = composition("b")
            seams = FakeSeams("blue")
            recovered = controller(seams, candidate, prior, root).recover(
                record(root, candidate, prior, "green_healthy", "blue"), "gate-b-receipt"
            )

        self.assertEqual(recovered.state, TransactionState.ROLLED_BACK)
        self.assertEqual(recovered.detail["phase"], "rollback_verified")
        self.assertIn(("fence", "green"), seams.calls)
        rollback_blue = next(value for name, value in seams.calls if name == "start" and value.startswith("blue-rollback-"))
        self.assertIn(("cas_selector", f"blue->{rollback_blue}"), seams.calls)
        self.assertEqual(seams.selector, rollback_blue)
        self.assertEqual(recovered.blue.pid, 99)
        self.assertNotEqual(recovered.blue.pid, 11)

    def test_recovery_cas_failure_does_not_commit_candidate_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = composition()
            prior = composition("b")
            seams = FakeSeams("blue", cas_result=False)
            with self.assertRaises(DeploymentBlocked):
                controller(seams, candidate, prior, root).recover(
                record(root, candidate, prior, "green_healthy", "blue"), "gate-b-receipt"
                )

        self.assertEqual(seams.selector, "blue")
        self.assertNotIn(("cas_selector", "blue->green"), seams.calls)
        self.assertTrue(any(name == "cas_selector" and value.startswith("blue->blue-rollback-") for name, value in seams.calls))

    def test_ambiguous_selector_is_manual_blocked_without_gate_b_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = composition()
            prior = composition("b")
            seams = FakeSeams("unknown-slot")
            recovered = controller(seams, candidate, prior, root).recover(
                record(root, candidate, prior, "selector_committed", "green")
            )

        self.assertEqual(recovered.state, TransactionState.BLOCKED)
        self.assertEqual(recovered.detail["phase"], "manual_blocked")
        self.assertEqual(recovered.detail["reason"], "ambiguous selector or transaction evidence")
        self.assertEqual(seams.calls, [("read_selector", None)])


if __name__ == "__main__":
    unittest.main()
