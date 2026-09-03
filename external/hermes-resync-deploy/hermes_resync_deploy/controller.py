"""Fail-closed blue/green transaction controller for disposable slots only."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from .backup import restore_snapshot_to_slot, verify_snapshot
from .health import HealthVerifier
from .manifest import candidate_digest
from .model import CandidateComposition, PriorRuntimeSnapshot, SlotIdentity, TransactionRecord, TransactionState


class DeploymentBlocked(RuntimeError):
    """The transaction is unsafe to continue without an operator decision."""


class GateBRequired(PermissionError):
    """A selector or service action was attempted without Gate B evidence."""


class GateBReceiptVerifier:
    """Verify a versioned HMAC receipt bound to this exact candidate digest.

    Receipt format is ``v1.<candidate-sha256>.<hex-hmac-sha256>``.  The signing
    key is an injected disposable seam; the controller never reads credentials.
    """

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("Gate B verification key must not be empty")
        self._key = bytes(key)

    def verify(self, receipt: str, digest: str) -> bool:
        parts = receipt.split(".")
        if len(parts) != 3 or parts[0] != "v1" or not hmac.compare_digest(parts[1], digest):
            return False
        expected = hmac.new(self._key, f"v1.{digest}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(parts[2], expected)

    def issue_for_test(self, digest: str) -> str:
        """Create deterministic disposable-harness evidence; never use in production."""
        return f"v1.{digest}.{hmac.new(self._key, f'v1.{digest}'.encode(), hashlib.sha256).hexdigest()}"


RecordSink = Callable[[TransactionRecord], None]


class DeploymentController:
    """Own one durable transaction through injected disposable seams only."""

    def __init__(
        self, composition: CandidateComposition, health: HealthVerifier, *,
        selector_read: Callable[[], str | None], selector_cas: Callable[[str | None, str], bool],
        fence: Callable[[SlotIdentity], None], start: Callable[[SlotIdentity], SlotIdentity],
        retire: Callable[[SlotIdentity], None],
        restore: Callable[[PriorRuntimeSnapshot, SlotIdentity], SlotIdentity] | None = None,
        record_sink: RecordSink | None = None, gate_b_validator: Callable[[str], bool] | None = None,
        gate_b_verifier: GateBReceiptVerifier | None = None,
        prior_composition: CandidateComposition | None = None, journal_dir: Path | None = None,
        journal_path: Path | None = None, lock_path: Path | None = None,
    ) -> None:
        self.composition, self.health = composition, health
        self._selector_read, self._selector_cas = selector_read, selector_cas
        self._fence, self._start, self._retire = fence, start, retire
        self._restore = restore or self._default_restore
        self._record_sink = record_sink or (lambda _: None)
        self._legacy_gate_b_validator = gate_b_validator
        self._gate_b_verifier = gate_b_verifier
        self._prior_composition = prior_composition
        self._journal_dir = Path(journal_dir) if journal_dir is not None else None
        self._journal_path = Path(journal_path) if journal_path is not None else None
        self._lock_path = Path(lock_path) if lock_path is not None else None
        self._lock_fd: int | None = None
        self.record: TransactionRecord | None = None

    @staticmethod
    def _default_restore(snapshot: PriorRuntimeSnapshot, slot: SlotIdentity) -> SlotIdentity:
        restore_snapshot_to_slot(snapshot, slot.root)
        return slot

    def _journal_paths(self) -> tuple[Path, Path]:
        if self._journal_dir is None:
            raise DeploymentBlocked("durable transaction journal is required")
        journal = self._journal_path or self._journal_dir / "transaction.json"
        lock = self._lock_path or self._journal_dir / "transaction.lock"
        if journal.parent != self._journal_dir or lock.parent != self._journal_dir:
            raise DeploymentBlocked("journal and lock must be inside the transaction directory")
        return journal, lock

    def _acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        if self._journal_dir is None:
            raise DeploymentBlocked("exclusive transaction lock is required")
        self._journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._journal_dir, 0o700)
        _, lock = self._journal_paths()
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DeploymentBlocked("another transaction owns the exclusive lock") from exc
        self._lock_fd = fd

    def _persist(self, record: TransactionRecord) -> None:
        journal, _ = self._journal_paths()
        payload = asdict(record)
        payload["state"] = record.state.value
        for name in ("blue", "green"):
            if payload[name] is not None:
                payload[name]["root"] = str(payload[name]["root"])
                for runtime_path in ("executable", "interpreter"):
                    if payload[name][runtime_path] is not None:
                        payload[name][runtime_path] = str(payload[name][runtime_path])
        if payload["prior_snapshot"] is not None:
            payload["prior_snapshot"]["cas_path"] = str(payload["prior_snapshot"]["cas_path"])
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = journal.with_name(f".{journal.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, journal)
        os.chmod(journal, 0o600)
        directory_fd = os.open(self._journal_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _require_gate_b(self, receipt: str | None) -> None:
        if not receipt:
            raise GateBRequired("candidate-bound Gate B receipt is required")
        digest = candidate_digest(self.composition)
        if self._gate_b_verifier is not None:
            valid = self._gate_b_verifier.verify(receipt, digest)
        elif self._legacy_gate_b_validator is not None:
            valid = self._legacy_gate_b_validator(receipt)
        else:
            valid = False
        if not valid:
            raise GateBRequired("invalid candidate-bound Gate B receipt")

    def _transition(self, state: TransactionState, phase: str, **detail: str) -> TransactionRecord:
        if self.record is None:
            raise DeploymentBlocked("no prepared transaction")
        merged = dict(self.record.detail); merged.update(detail); merged["phase"] = phase
        self.record = replace(self.record, state=state, detail=merged)
        self._persist(self.record)
        self._record_sink(self.record)
        return self.record

    def prepare(self, transaction_id: str, blue: SlotIdentity, green: SlotIdentity,
                prior_snapshot: PriorRuntimeSnapshot, expected_selector: str | None) -> TransactionRecord:
        if not transaction_id or self.record is not None:
            raise DeploymentBlocked("transaction id must be unique")
        if candidate_digest(self.composition) != green.candidate_digest:
            raise DeploymentBlocked("green slot is not bound to the candidate composition")
        if self._prior_composition is None or candidate_digest(self._prior_composition) != blue.candidate_digest:
            raise DeploymentBlocked("blue slot must be bound to its prior composition")
        if prior_snapshot.composition_digest != blue.candidate_digest:
            raise DeploymentBlocked("prior snapshot is not bound to blue composition")
        self._journal_dir = self._journal_dir or green.root.parent / ".transaction-journal"
        self._acquire_lock(); verify_snapshot(prior_snapshot)
        observed = self._selector_read()
        if observed != expected_selector:
            raise DeploymentBlocked("selector compare-and-swap precondition failed")
        self.record = TransactionRecord(transaction_id, TransactionState.PREPARED, green.candidate_digest,
            blue, green, prior_snapshot, expected_selector, observed, {"phase": "prepared"})
        self._persist(self.record); self._record_sink(self.record)
        return self.record

    def fence_blue(self) -> TransactionRecord:
        if self.record is None or self.record.state is not TransactionState.PREPARED or not self.record.blue:
            raise DeploymentBlocked("blue may only be fenced from prepared")
        self._fence(self.record.blue); return self._transition(TransactionState.FENCED, "blue_fenced")

    def start_green(self) -> TransactionRecord:
        if self.record is None or self.record.detail.get("phase") != "blue_fenced" or not self.record.green:
            raise DeploymentBlocked("green may only start after blue is fenced")
        self.record = replace(self.record, green=self._start(self.record.green))
        return self._transition(TransactionState.FENCED, "green_started")

    def verify_green(self) -> TransactionRecord:
        if self.record is None or self.record.detail.get("phase") != "green_started" or not self.record.green:
            raise DeploymentBlocked("green health requires a started green slot")
        report = self.health.verify_slot(self.record.green, self.composition)
        if not report.healthy:
            raise DeploymentBlocked("green health verification failed: " + ", ".join(report.failures))
        return self._transition(TransactionState.HEALTHY, "green_healthy")

    def publish_selector(self) -> TransactionRecord:
        if self.record is None or self.record.state is not TransactionState.HEALTHY or not self.record.green:
            raise DeploymentBlocked("selector may only publish verified green")
        if not self._selector_cas(self.record.selector_expected, self.record.green.name):
            raise DeploymentBlocked("selector compare-and-swap failed")
        return self._transition(TransactionState.PUBLISHED, "selector_committed", selector_observed=self.record.green.name)

    def activate(self, receipt: str | None) -> TransactionRecord:
        self._acquire_lock()
        self._require_gate_b(receipt)
        try:
            self.fence_blue(); self.start_green(); self.verify_green(); self.publish_selector()
            if self.record is None or not self.record.blue: raise DeploymentBlocked("blue identity is unavailable")
            self._retire(self.record.blue); return self._transition(TransactionState.PUBLISHED, "blue_retired")
        except BaseException as exc:
            if self.record is None: raise
            try: self.rollback(receipt)
            except BaseException as rollback_exc:
                self._transition(TransactionState.BLOCKED, "manual_blocked", reason=str(rollback_exc))
                raise DeploymentBlocked("activation and rollback both failed") from rollback_exc
            raise DeploymentBlocked("activation failed and rollback completed") from exc

    def rollback(self, receipt: str | None) -> TransactionRecord:
        self._acquire_lock()
        self._require_gate_b(receipt)
        if self.record is None or not self.record.green or not self.record.blue or not self.record.prior_snapshot or self._prior_composition is None:
            raise DeploymentBlocked("rollback lacks verified transaction evidence")
        self._transition(TransactionState.FENCED, "rollback_prepared"); self._fence(self.record.green)
        verify_snapshot(self.record.prior_snapshot)
        blue = self._new_rollback_blue(self.record.blue)
        restored_contract = blue
        blue = self._restore(self.record.prior_snapshot, blue)
        prior_runtime = self.record.blue
        if prior_runtime.executable is not None and (
            blue.executable is None or blue.interpreter is None or
            blue.executable != restored_contract.executable or blue.interpreter != restored_contract.interpreter or
            blue.executable_digest != prior_runtime.executable_digest or
            blue.interpreter_digest != prior_runtime.interpreter_digest or
            not Path(blue.executable).is_relative_to(blue.root) or not Path(blue.interpreter).is_relative_to(blue.root)
        ):
            raise DeploymentBlocked("restored blue runtime differs from the verified prior executable")
        blue = self._start(blue)
        if blue.candidate_digest != candidate_digest(self._prior_composition):
            raise DeploymentBlocked("restored blue identity differs from prior composition")
        self.record = replace(self.record, blue=blue)
        report = self.health.verify_slot(blue, self._prior_composition)
        if not report.healthy: raise DeploymentBlocked("restored blue health verification failed")
        if not self._selector_cas(self._selector_read(), blue.name): raise DeploymentBlocked("rollback selector compare-and-swap failed")
        return self._transition(TransactionState.ROLLED_BACK, "rollback_verified", selector_observed=blue.name)

    def _new_rollback_blue(self, prior_blue: SlotIdentity) -> SlotIdentity:
        """Allocate a fresh immutable restore slot; never overwrite prior blue."""
        if self.record is None:
            raise DeploymentBlocked("rollback lacks transaction identity")
        stem = "".join(char if char.isalnum() or char in "_.-" else "-" for char in self.record.transaction_id)
        stem = stem[:40] or "transaction"
        for sequence in range(1, 1000):
            name = f"blue-rollback-{stem}-{sequence}"
            root = prior_blue.root.parent / name
            if not root.exists():
                # A restored release must acquire a new process identity.  Carry only
                # relative runtime locations and digests, never stale absolute paths.
                executable = interpreter = None
                if prior_blue.executable is not None or prior_blue.interpreter is not None:
                    if prior_blue.executable is None or prior_blue.interpreter is None:
                        raise DeploymentBlocked("prior runtime contract is incomplete")
                    try:
                        executable = root / Path(prior_blue.executable).relative_to(prior_blue.root)
                        interpreter = root / Path(prior_blue.interpreter).relative_to(prior_blue.root)
                    except ValueError as exc:
                        raise DeploymentBlocked("prior runtime is outside its blue slot") from exc
                return SlotIdentity(
                    name, root, prior_blue.revision, prior_blue.candidate_digest,
                    executable=executable, interpreter=interpreter,
                    executable_digest=prior_blue.executable_digest,
                    interpreter_digest=prior_blue.interpreter_digest,
                )
        raise DeploymentBlocked("unable to allocate a fresh rollback blue slot")

    def recover(self, record: TransactionRecord, receipt: str | None = None) -> TransactionRecord:
        self._journal_dir = self._journal_dir or (record.green.root.parent / ".transaction-journal" if record.green else None)
        self._acquire_lock(); self.record = record
        observed, phase = self._selector_read(), record.detail.get("phase")
        if phase == "prepared" and record.green and observed == record.selector_expected:
            self._require_gate_b(receipt)
            self._fence(record.green)
            return self._transition(TransactionState.ROLLED_BACK, "prepared_green_fenced")
        if phase in {"blue_fenced", "green_started", "green_healthy"} and observed == record.selector_expected:
            self._require_gate_b(receipt); return self.rollback(receipt)
        if phase in {"selector_committed", "blue_retired"} and record.green and observed == record.green.name:
            self._require_gate_b(receipt); return self.rollback(receipt)
        return self._transition(TransactionState.BLOCKED, "manual_blocked", reason="ambiguous selector or transaction evidence")
