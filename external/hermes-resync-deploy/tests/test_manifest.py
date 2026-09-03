from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_resync_deploy.manifest import build_composition, candidate_digest, verify_composition
from hermes_resync_deploy.model import CandidateComposition


def composition() -> CandidateComposition:
    return CandidateComposition(
        candidate_source_sha="source", candidate_tree_digest="source-tree",
        plugin_wheel_digest="wheel", plugin_version="1.2.3", plugin_sbom_digest="sbom",
        plugin_lock_digest="plugin-lock", skill_package_digest="skills", skill_mode_manifest_digest="modes",
        controller_digest="controller", controller_lock_digest="controller-lock", dependency_closure_digest="dependencies",
        test_wrapper_version="test-wrapper", artifact_tree_digest="artifact-tree", final_manifest_digest="final-manifest",
        gate_a_receipt_digest="receipt-a", gate_b_receipt_digest="receipt-b",
        gates={"A": "approved", "B": "approved"},
    )


def test_canonical_composition_round_trip_has_one_versioned_json_shape(tmp_path: Path) -> None:
    candidate = composition()
    path = tmp_path / "composition.json"

    digest = build_composition(candidate, path)

    assert verify_composition(path, digest) == candidate
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "schemaVersion": 1, "sourceHash": "source", "sourceTreeDigest": "source-tree",
        "plugin": {"wheelDigest": "wheel", "version": "1.2.3", "sbomDigest": "sbom", "buildLockDigest": "plugin-lock"},
        "skills": {"packageDigest": "skills", "modeManifestDigest": "modes"},
        "controller": {"digest": "controller", "buildLockDigest": "controller-lock"},
        "dependencyDigest": "dependencies", "testWrapper": {"digest": "test-wrapper"},
        "artifactTreeDigest": "artifact-tree", "finalManifestDigest": "final-manifest",
        "receipts": {"gateA": "receipt-a", "gateB": "receipt-b"}, "gates": {"A": "approved", "B": "approved"},
    }
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest() == candidate_digest(candidate)


@pytest.mark.parametrize("field", ["sourceHash", "finalManifestDigest"])
def test_byte_mutation_invalidates_canonical_and_digest_binding(tmp_path: Path, field: str) -> None:
    path = tmp_path / "composition.json"
    digest = build_composition(composition(), path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = "mutated"
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_composition(path, digest)


def test_receipt_source_and_final_manifest_values_are_digest_bound(tmp_path: Path) -> None:
    base = composition()
    base_digest = candidate_digest(base)
    for changed in (
        {"candidate_source_sha": "other-source"},
        {"final_manifest_digest": "other-final"},
        {"gate_a_receipt_digest": "other-receipt"},
        {"gate_b_receipt_digest": "other-receipt"},
    ):
        assert candidate_digest(replace(base, **changed)) != base_digest

    # Extra JSON aliases cannot introduce an independent flat source, receipt, or final truth.
    path = tmp_path / "composition.json"
    build_composition(base, path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["candidateSourceSha"] = "other-source"
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid composition manifest"):
        verify_composition(path)
