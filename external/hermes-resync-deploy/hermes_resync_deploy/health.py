"""Finite, file-backed health verification for disposable slots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .manifest import candidate_digest
from .model import CandidateComposition, HealthReport, SlotIdentity


class HealthVerifier:
    """Verify explicit readiness evidence without probing a live service.

    Slots write a bounded ``health.json`` in their own disposable root.  The
    verifier neither discovers processes nor reads selectors, HOME, or runtime
    state.  A test or launcher may provide ``evidence_reader`` instead.
    """

    def __init__(
        self,
        expected_plugins: Iterable[str],
        *,
        evidence_reader: Callable[[SlotIdentity], Mapping[str, object]] | None = None,
        evidence_name: str = "health.json",
    ) -> None:
        self._expected_plugins = tuple(sorted(expected_plugins))
        self._evidence_reader = evidence_reader
        self._evidence_name = evidence_name

    def _read_evidence(self, slot: SlotIdentity) -> Mapping[str, object]:
        if self._evidence_reader is not None:
            return self._evidence_reader(slot)
        evidence_path = slot.root / self._evidence_name
        root = slot.root.resolve()
        if evidence_path.resolve().parent != root:
            raise ValueError("health evidence escapes disposable slot")
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise ValueError("health evidence must be a regular slot file")
        raw = evidence_path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("health evidence exceeds bounded size")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("health evidence must be an object")
        return decoded

    def verify_slot(self, slot: SlotIdentity, composition: CandidateComposition) -> HealthReport:
        """Return a complete report for the six required finite predicates."""
        checks: dict[str, bool] = {}
        failures: list[str] = []
        evidence_text: dict[str, str] = {}
        try:
            evidence = self._read_evidence(slot)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return HealthReport(False, {"ready": False}, (f"unreadable readiness evidence: {exc}",), {})

        expected_digest = candidate_digest(composition)
        checks["ready"] = evidence.get("ready") is True
        checks["slot"] = evidence.get("slot") == slot.name
        checks["revision"] = evidence.get("revision") == slot.revision
        checks["candidate_digest"] = evidence.get("candidate_digest") == expected_digest == slot.candidate_digest
        plugins = evidence.get("plugins")
        checks["plugin_list"] = isinstance(plugins, list) and tuple(sorted(plugins)) == self._expected_plugins
        checks["sidecar"] = evidence.get("sidecar_ready") is True
        checks["one_writer"] = evidence.get("writer_count") == 1
        checks["no_kanban"] = evidence.get("kanban") is False and not bool(evidence.get("kanban_processes", ()))
        forbidden_writes = evidence.get("forbidden_writes", ())
        checks["forbidden_writes"] = isinstance(forbidden_writes, list) and not forbidden_writes
        checks["pid"] = isinstance(evidence.get("pid"), int) and evidence["pid"] > 0
        if slot.pid is not None:
            checks["pid"] = checks["pid"] and evidence["pid"] == slot.pid

        for name, passed in checks.items():
            if not passed:
                failures.append(name)
        for key in ("candidate_digest", "pid", "revision", "writer_count", "health_nonce", "timestamp_ns"):
            if key in evidence:
                evidence_text[key] = str(evidence[key])
        return HealthReport(not failures, checks, tuple(failures), evidence_text)
