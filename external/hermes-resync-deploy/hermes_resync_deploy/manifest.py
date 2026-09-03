"""Canonical, durable candidate-composition manifests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .model import CandidateComposition


_SCHEMA_VERSION = 1


def _canonical_document(composition: CandidateComposition) -> dict[str, Any]:
    """Return the one JSON shape shared by generators, artifacts, CLI, and controller."""
    return {
        "schemaVersion": composition.schema_version,
        "sourceHash": composition.candidate_source_sha,
        "sourceTreeDigest": composition.candidate_tree_digest,
        "plugin": {
            "wheelDigest": composition.plugin_wheel_digest,
            "version": composition.plugin_version,
            "sbomDigest": composition.plugin_sbom_digest,
            "buildLockDigest": composition.plugin_lock_digest,
        },
        "skills": {
            "packageDigest": composition.skill_package_digest,
            "modeManifestDigest": composition.skill_mode_manifest_digest,
        },
        "controller": {
            "digest": composition.controller_digest,
            "buildLockDigest": composition.controller_lock_digest,
        },
        "dependencyDigest": composition.dependency_closure_digest,
        "testWrapper": {"digest": composition.test_wrapper_version},
        "artifactTreeDigest": composition.artifact_tree_digest,
        "finalManifestDigest": composition.final_manifest_digest,
        "receipts": {
            "gateA": composition.gate_a_receipt_digest,
            "gateB": composition.gate_b_receipt_digest,
        },
        "gates": dict(composition.gates),
    }


def canonical_composition_bytes(composition: CandidateComposition) -> bytes:
    """Serialize a composition using the schema's sole canonical byte form."""
    return (json.dumps(
        _canonical_document(composition), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ) + "\n").encode("utf-8")


def _object(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"invalid {name} in composition manifest")
    return value


def _composition_from_document(document: Any) -> CandidateComposition:
    root = _object(document, {
        "schemaVersion", "sourceHash", "sourceTreeDigest", "plugin", "skills", "controller",
        "dependencyDigest", "testWrapper", "artifactTreeDigest", "finalManifestDigest", "receipts", "gates",
    }, "root")
    if root["schemaVersion"] != _SCHEMA_VERSION:
        raise ValueError("unsupported composition manifest schema version")
    plugin = _object(root["plugin"], {"wheelDigest", "version", "sbomDigest", "buildLockDigest"}, "plugin")
    skills = _object(root["skills"], {"packageDigest", "modeManifestDigest"}, "skills")
    controller = _object(root["controller"], {"digest", "buildLockDigest"}, "controller")
    wrapper = _object(root["testWrapper"], {"digest"}, "test wrapper")
    receipts = _object(root["receipts"], {"gateA", "gateB"}, "receipts")
    gates = root["gates"]
    if not isinstance(gates, dict):
        raise ValueError("invalid gates in composition manifest")
    try:
        return CandidateComposition(
            candidate_source_sha=root["sourceHash"], candidate_tree_digest=root["sourceTreeDigest"],
            plugin_wheel_digest=plugin["wheelDigest"], plugin_version=plugin["version"],
            plugin_sbom_digest=plugin["sbomDigest"], plugin_lock_digest=plugin["buildLockDigest"],
            skill_package_digest=skills["packageDigest"], skill_mode_manifest_digest=skills["modeManifestDigest"],
            controller_digest=controller["digest"], controller_lock_digest=controller["buildLockDigest"],
            dependency_closure_digest=root["dependencyDigest"], test_wrapper_version=wrapper["digest"],
            artifact_tree_digest=root["artifactTreeDigest"], schema_version=root["schemaVersion"],
            final_manifest_digest=root["finalManifestDigest"], gate_a_receipt_digest=receipts["gateA"],
            gate_b_receipt_digest=receipts["gateB"], gates=gates,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid composition manifest") from exc


def candidate_digest(composition: CandidateComposition) -> str:
    """Return the SHA-256 of the exact canonical composition bytes."""
    return hashlib.sha256(canonical_composition_bytes(composition)).hexdigest()


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def build_composition(composition: CandidateComposition, manifest_path: Path) -> str:
    """Atomically persist a composition and prove the persisted bytes match.

    The caller selects an external manifest path.  This function deliberately
    has no notion of HOME, HERMES_HOME, a selector, or a live release.
    """
    manifest_path = Path(manifest_path)
    parent = manifest_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = canonical_composition_bytes(composition)
    digest = hashlib.sha256(payload).hexdigest()
    temporary = parent / "manifest.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    _fsync_directory(parent)
    os.replace(temporary, manifest_path)
    _fsync_directory(parent)
    persisted = manifest_path.read_bytes()
    if persisted != payload or hashlib.sha256(persisted).hexdigest() != digest:
        raise RuntimeError("composition manifest readback verification failed")
    return digest


def verify_composition(
    manifest_path: Path,
    expected_digest: str | None = None,
) -> CandidateComposition:
    """Read only canonical manifests and optionally bind them to a digest."""
    raw = Path(manifest_path).read_bytes()
    if not raw.endswith(b"\n") or raw != raw.strip() + b"\n":
        raise ValueError("composition manifest is not canonical")
    try:
        composition = _composition_from_document(json.loads(raw))
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid composition manifest") from exc
    if raw != canonical_composition_bytes(composition):
        raise ValueError("composition manifest is not canonical")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("composition digest mismatch")
    return composition
