from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_resync_deploy.health import HealthVerifier
from hermes_resync_deploy.manifest import candidate_digest
from hermes_resync_deploy.model import CandidateComposition, SlotIdentity


def composition() -> CandidateComposition:
    return CandidateComposition(
        candidate_source_sha="a" * 40,
        candidate_tree_digest="b" * 64,
        plugin_wheel_digest="c" * 64,
        plugin_version="1.0.0",
        plugin_sbom_digest="d" * 64,
        plugin_lock_digest="e" * 64,
        skill_package_digest="f" * 64,
        skill_mode_manifest_digest="0" * 64,
        controller_digest="1" * 64,
        controller_lock_digest="2" * 64,
        dependency_closure_digest="3" * 64,
        test_wrapper_version="test-wrapper",
        artifact_tree_digest="4" * 64,
    )


class HealthTests(unittest.TestCase):
    def test_stale_candidate_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = composition()
            slot = SlotIdentity("green", Path(temporary), "rev", candidate_digest(candidate), pid=97)
            report = HealthVerifier(["hermes-resync-plugin"], evidence_reader=lambda _: {
                "ready": True, "candidate_digest": "stale", "plugins": ["hermes-resync-plugin"],
                "sidecar_ready": True, "writer_count": 1, "kanban": False,
                "kanban_processes": [], "forbidden_writes": [], "pid": 97,
            }).verify_slot(slot, candidate)

        self.assertFalse(report.healthy)
        self.assertIn("candidate_digest", report.failures)

    def test_mixed_or_unknown_plugin_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = composition()
            slot = SlotIdentity("green", Path(temporary), "rev", candidate_digest(candidate))
            verifier = HealthVerifier(["hermes-resync-plugin"])
            for plugins in (["hermes-resync-plugin", "other-plugin"], ["unknown-plugin"]):
                with self.subTest(plugins=plugins):
                    report = HealthVerifier(["hermes-resync-plugin"], evidence_reader=lambda _, plugins=plugins: {
                        "ready": True, "candidate_digest": candidate_digest(candidate), "plugins": plugins,
                        "sidecar_ready": True, "writer_count": 1, "kanban": False,
                        "kanban_processes": [], "forbidden_writes": [],
                    }).verify_slot(slot, candidate)
                    self.assertFalse(report.healthy)
                    self.assertIn("plugin_list", report.failures)

    def test_health_requires_one_writer_and_no_kanban_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = composition()
            slot = SlotIdentity("green", Path(temporary), "rev", candidate_digest(candidate))
            evidence = {
                "ready": True, "candidate_digest": candidate_digest(candidate),
                "plugins": ["hermes-resync-plugin"], "sidecar_ready": True,
                "writer_count": 2, "kanban": True, "kanban_processes": ["kanban-worker"],
                "forbidden_writes": [],
            }
            report = HealthVerifier(["hermes-resync-plugin"], evidence_reader=lambda _: evidence).verify_slot(slot, candidate)

        self.assertFalse(report.healthy)
        self.assertFalse(report.checks["one_writer"])
        self.assertFalse(report.checks["no_kanban"])


if __name__ == "__main__":
    unittest.main()
