"""Immutable data contracts for the isolated deployment controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class CandidateComposition:
    """The single versioned, digest-bound candidate composition contract."""

    candidate_source_sha: str
    candidate_tree_digest: str
    plugin_wheel_digest: str
    plugin_version: str
    plugin_sbom_digest: str
    plugin_lock_digest: str
    skill_package_digest: str
    skill_mode_manifest_digest: str
    controller_digest: str
    controller_lock_digest: str
    dependency_closure_digest: str
    test_wrapper_version: str
    artifact_tree_digest: str
    schema_version: int = 1
    final_manifest_digest: str = ""
    gate_a_receipt_digest: str = ""
    gate_b_receipt_digest: str = ""
    gates: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported candidate composition schema version")
        if not all(isinstance(value, str) for value in (
            self.candidate_source_sha, self.candidate_tree_digest,
            self.plugin_wheel_digest, self.plugin_version, self.plugin_sbom_digest,
            self.plugin_lock_digest, self.skill_package_digest,
            self.skill_mode_manifest_digest, self.controller_digest,
            self.controller_lock_digest, self.dependency_closure_digest,
            self.test_wrapper_version, self.artifact_tree_digest,
            self.final_manifest_digest, self.gate_a_receipt_digest,
            self.gate_b_receipt_digest,
        )):
            raise TypeError("candidate composition values must be strings")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.gates.items()):
            raise TypeError("candidate composition gates must be string mappings")
        object.__setattr__(self, "gates", MappingProxyType(dict(self.gates)))


@dataclass(frozen=True, slots=True)
class PriorRuntimeSnapshot:
    """A verified code-only snapshot stored in the controller's CAS."""

    manifest_digest: str
    tree_digest: str
    cas_path: Path
    kind: str
    entry_count: int
    composition_digest: str = ""


@dataclass(frozen=True, slots=True)
class SlotIdentity:
    """Identity and isolated filesystem root of a disposable slot."""

    name: str
    root: Path
    revision: str
    candidate_digest: str
    pid: int | None = None
    executable: Path | None = None
    interpreter: Path | None = None
    executable_digest: str = ""
    interpreter_digest: str = ""


class TransactionState(str, Enum):
    PREPARED = "prepared"
    FENCED = "fenced"
    HEALTHY = "healthy"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """Append-only controller state; transitions are owned by the controller."""

    transaction_id: str
    state: TransactionState
    candidate_digest: str
    blue: SlotIdentity | None
    green: SlotIdentity | None
    prior_snapshot: PriorRuntimeSnapshot | None
    selector_expected: str | None
    selector_observed: str | None
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Finite health evidence collected from a single isolated slot."""

    healthy: bool
    checks: Mapping[str, bool]
    failures: tuple[str, ...]
    evidence: Mapping[str, str]
