"""Artifact-first command line for the isolated deployment controller."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Mapping, Sequence

from .backup import create_active_release_snapshot, create_dirty_source_snapshot
from .controller import DeploymentController, GateBReceiptVerifier, GateBRequired
from .health import HealthVerifier
from .launchd import LongRunningLaunchdAdapter, SlotProfile, fence_slot_service, parse_launchd_plist, render_launchd_plist, slot_service_label
from .manifest import candidate_digest, verify_composition
from .model import PriorRuntimeSnapshot, SlotIdentity, TransactionRecord, TransactionState

_MAX_EVIDENCE_BYTES = 64 * 1024
_LAUNCHD_PID = re.compile(r"^\s*pid\s*=\s*(\d+)\s*;?\s*$", re.MULTILINE)
_LAUNCHD_STATE = re.compile(r"^\s*state\s*=\s*([^;\n]+)\s*;?\s*$", re.MULTILINE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-resync-deploy")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--kind", choices=("dirty-source", "active-release"), required=True)
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--cas", type=Path, required=True)
    snapshot.add_argument("--allow", action="append", required=True)
    snapshot.add_argument("--composition-manifest", type=Path, required=True)
    snapshot.add_argument("--composition-digest", required=True)
    for name in ("stage", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--digest")
    health = commands.add_parser("health")
    health.add_argument("--evidence", type=Path, required=True)
    for name in ("activate", "rollback", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--adapter", choices=("production", "synthetic"), required=True)
        command.add_argument("--record", type=Path, required=True)
        command.add_argument("--disposable-root", type=Path, required=True)
        command.add_argument("--approved-root", type=Path, required=True)
        command.add_argument("--selector-path", type=Path, required=True)
        command.add_argument("--journal", type=Path, required=True)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--slot-program", type=Path)
        command.add_argument("--service-executable", type=Path)
        command.add_argument("--blue-port", type=int, required=True)
        command.add_argument("--green-port", type=int, required=True)
        command.add_argument("--current-composition", type=Path, required=True)
        command.add_argument("--current-digest", required=True)
        command.add_argument("--prior-composition", type=Path, required=True)
        command.add_argument("--prior-digest", required=True)
        command.add_argument("--gate-b-key", type=Path, required=True)
        command.add_argument("--gate-b-receipt", required=name != "recover")
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _load_slot(value: object) -> SlotIdentity | None:
    if value is None:
        return None
    fields = {
        "name", "root", "revision", "candidate_digest", "pid", "executable",
        "interpreter", "executable_digest", "interpreter_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid slot identity schema")
    if not all(isinstance(value[key], str) for key in ("name", "root", "revision", "candidate_digest", "executable_digest", "interpreter_digest")):
        raise ValueError("invalid slot identity values")
    if value["pid"] is not None and (not isinstance(value["pid"], int) or value["pid"] <= 0):
        raise ValueError("invalid slot pid")
    for key in ("candidate_digest", "executable_digest", "interpreter_digest"):
        digest = value[key]
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError(f"invalid {key}")
    runtime = (value["executable"], value["interpreter"], value["executable_digest"], value["interpreter_digest"])
    if any(runtime) and not all(runtime):
        raise ValueError("partial slot runtime identity")
    if value["executable"] is not None and not isinstance(value["executable"], str):
        raise ValueError("invalid slot executable")
    if value["interpreter"] is not None and not isinstance(value["interpreter"], str):
        raise ValueError("invalid slot interpreter")
    return SlotIdentity(
        name=value["name"], root=Path(value["root"]), revision=value["revision"],
        candidate_digest=value["candidate_digest"], pid=value["pid"],
        executable=Path(value["executable"]) if value["executable"] else None,
        interpreter=Path(value["interpreter"]) if value["interpreter"] else None,
        executable_digest=value["executable_digest"],
        interpreter_digest=value["interpreter_digest"],
    )


def _load_record(path: Path) -> TransactionRecord:
    """Load explicit transaction evidence; never discover runtime state."""
    raw = path.read_bytes()
    if len(raw) > _MAX_EVIDENCE_BYTES:
        raise ValueError("transaction record exceeds bounded size")
    try:
        value = json.loads(raw)
        snapshot = value["prior_snapshot"]
        blue = value["blue"]
        green = value["green"]
        return TransactionRecord(
            transaction_id=value["transaction_id"], state=TransactionState(value["state"]),
            candidate_digest=value["candidate_digest"],
            blue=_load_slot(blue),
            green=_load_slot(green),
            prior_snapshot=PriorRuntimeSnapshot(
                manifest_digest=snapshot["manifest_digest"], tree_digest=snapshot["tree_digest"],
                cas_path=Path(snapshot["cas_path"]), kind=snapshot["kind"], entry_count=snapshot["entry_count"],
                composition_digest=snapshot.get("composition_digest", ""),
            ) if snapshot is not None else None,
            selector_expected=value["selector_expected"], selector_observed=value["selector_observed"],
            detail=value.get("detail", {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid transaction record") from exc


def _inside(root: Path, path: Path, label: str) -> Path:
    root, path = Path(root).resolve(), Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be under its declared root") from exc
    return path


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    raw = path.read_bytes()
    if len(raw) > _MAX_EVIDENCE_BYTES:
        raise ValueError(f"{label} exceeds bounded size")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _production_slot_resolver(slot: SlotIdentity) -> SlotIdentity:
    """Resolve only the runtime identity recorded for this exact slot."""
    if slot.executable is None or slot.interpreter is None:
        raise ValueError("production slots require a per-slot executable and interpreter")
    root = Path(slot.root).resolve()
    raw_executable, raw_interpreter = Path(slot.executable), Path(slot.interpreter)
    if not raw_executable.is_absolute() or not raw_interpreter.is_absolute():
        raise ValueError("production runtime identities must be absolute")
    if raw_executable.is_symlink() or raw_interpreter.is_symlink():
        raise ValueError("production runtime must use regular files")
    executable, interpreter = raw_executable.resolve(), raw_interpreter.resolve()
    try:
        executable.relative_to(root)
        interpreter.relative_to(root)
    except ValueError as exc:
        raise ValueError("production runtime must be under its slot root") from exc
    if not slot.executable_digest or not slot.interpreter_digest:
        raise ValueError("production slots require executable and interpreter SHA-256 digests")
    for path, digest, label in ((executable, slot.executable_digest, "executable"),
                                (interpreter, slot.interpreter_digest, "interpreter")):
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"production {label} does not match its recorded SHA-256 digest")
    return SlotIdentity(slot.name, root, slot.revision, slot.candidate_digest, slot.pid,
                        executable, interpreter, slot.executable_digest, slot.interpreter_digest)


def _production_launchd_executor(command: tuple[str, ...]) -> Mapping[str, object] | None:
    completed = subprocess.run(command, check=True, timeout=5, capture_output=True, text=True)
    if len(completed.stdout.encode()) > _MAX_EVIDENCE_BYTES:
        raise ValueError("launchctl print output exceeds bounded size")
    if len(command) < 2 or command[1] != "print":
        return None
    pid_match, state_match = _LAUNCHD_PID.search(completed.stdout), _LAUNCHD_STATE.search(completed.stdout)
    if pid_match is None or state_match is None:
        return {"alive": False}
    state = state_match.group(1).strip().lower()
    return {"pid": int(pid_match.group(1)), "alive": state == "running", "state": state}


class _SyntheticSlotAdapter:
    """Execute only hermetic disposable test programs; never use this for a live service."""

    def __init__(self, disposable: Path, program: Path, service: Path, selector: Path,
                 ports: Mapping[str, int]) -> None:
        self._disposable = disposable
        self._program = _inside(disposable, program, "slot program")
        self._service = _inside(disposable, service, "service executable")
        if not self._program.is_file() or not self._service.is_file():
            raise ValueError("slot program and service executable must be regular disposable files")
        self._selector = selector
        self._ports = dict(ports)
        self._launches: dict[str, tuple[str, int, int]] = {}

    def _profile(self, slot: SlotIdentity) -> SlotProfile:
        port = self._ports.get(slot.name)
        if port is None and slot.name.startswith("blue-rollback-"):
            port = self._ports.get("blue")
        if port is None:
            raise ValueError("slot has no explicit disposable port")
        root = _inside(self._disposable, slot.root, "slot root")
        return SlotProfile(
            slot, slot_service_label(slot), (str(self._program),),
            root / "home", root / "hermes-home", self._selector,
            "hermes-resync", port,
        )

    @staticmethod
    def _environment(profile: SlotProfile, health_nonce: str = "") -> dict[str, str]:
        environment = {
            "HOME": str(profile.home), "HERMES_HOME": str(profile.hermes_home),
            "HERMES_RESYNC_SELECTOR": str(profile.selector_path),
            "HERMES_RESYNC_SELECTOR_OWNER": profile.selector_owner,
            "HERMES_RESYNC_SLOT": profile.slot.name, "HERMES_RESYNC_REVISION": profile.slot.revision,
            "HERMES_RESYNC_CANDIDATE_DIGEST": profile.slot.candidate_digest,
            "HERMES_RESYNC_SLOT_ROOT": str(profile.slot.root), "HERMES_RESYNC_PORT": str(profile.port),
        }
        if health_nonce:
            environment["HERMES_RESYNC_HEALTH_NONCE"] = health_nonce
        return environment

    def _service_action(self, action: str, slot: SlotIdentity) -> None:
        profile = parse_launchd_plist(render_launchd_plist(self._profile(slot)))
        subprocess.run((str(self._service), action, profile.label), cwd=profile.slot.root,
                       env={**os.environ, **self._environment(profile)}, check=True, timeout=5)

    def fence(self, slot: SlotIdentity) -> None:
        fence_slot_service(slot, lambda label: self._service_action("fence", slot))

    def retire(self, slot: SlotIdentity) -> None:
        self._service_action("retire", slot)

    def start(self, slot: SlotIdentity) -> SlotIdentity:
        profile = parse_launchd_plist(render_launchd_plist(self._profile(slot)))
        evidence = profile.slot.root / "health.json"
        if evidence.exists() or evidence.is_symlink():
            if evidence.is_dir() or evidence.is_symlink():
                raise ValueError("pre-existing health evidence is not a regular file")
            evidence.unlink()
        nonce = secrets.token_hex(32)
        started_ns = time.time_ns()
        subprocess.run(profile.program_arguments, cwd=profile.slot.root,
                       env={**os.environ, **self._environment(profile, nonce)}, check=True, timeout=5)
        if not evidence.is_file() or evidence.is_symlink() or evidence.stat().st_mtime_ns < started_ns:
            raise ValueError("slot program did not produce fresh health evidence")
        self._launches[slot.name] = (nonce, started_ns, evidence.stat().st_mtime_ns)
        details = self.evidence(slot)
        return replace(slot, pid=details["pid"])

    def evidence(self, slot: SlotIdentity) -> Mapping[str, object]:
        launch = self._launches.get(slot.name)
        path = _inside(self._disposable, slot.root, "slot root") / "health.json"
        if launch is None or not path.is_file() or path.is_symlink() or path.stat().st_mtime_ns != launch[2]:
            raise ValueError("health evidence is not fresh for this started slot")
        evidence = _read_json(path, "slot health evidence")
        profile = self._profile(slot)
        required = {
            "pid": int, "slot": str, "revision": str, "candidate_digest": str,
            "socket": str, "port": int, "port_owner": str, "health_nonce": str,
            "timestamp_ns": int,
        }
        if any(not isinstance(evidence.get(key), kind) for key, kind in required.items()):
            raise ValueError("health evidence is not owned by the started slot")
        nonce, started_ns, _ = launch
        if (evidence["pid"] <= 0 or (slot.pid is not None and evidence["pid"] != slot.pid) or
                evidence["slot"] != slot.name or evidence["revision"] != slot.revision or
                evidence["candidate_digest"] != slot.candidate_digest or
                evidence["socket"] != str(profile.slot.root / "service.sock") or
                evidence["port"] != profile.port or evidence["port_owner"] != slot.name or
                evidence["health_nonce"] != nonce or evidence["timestamp_ns"] < started_ns):
            raise ValueError("health evidence is not owned by the started slot")
        return evidence


def _default_controller(args: argparse.Namespace, record: TransactionRecord) -> DeploymentController:
    """Construct a controller with an explicitly selected service adapter."""
    disposable = Path(args.disposable_root).resolve()
    approved = Path(args.approved_root).resolve()
    if not disposable.is_dir() or not approved.is_dir():
        raise ValueError("disposable and approved roots must be existing directories")
    selector = _inside(disposable, args.selector_path, "selector path")
    journal = _inside(disposable, args.journal, "journal")
    lock = _inside(disposable, args.lock, "lock")
    if journal == lock:
        raise ValueError("journal and lock must be distinct paths")
    current_path = _inside(approved, args.current_composition, "current composition")
    prior_path = _inside(approved, args.prior_composition, "prior composition")
    key_path = _inside(approved, args.gate_b_key, "Gate B key")
    for slot in (record.blue, record.green):
        if slot is not None:
            _inside(disposable, slot.root, "slot root")
    if record.prior_snapshot is not None:
        _inside(approved, record.prior_snapshot.cas_path, "snapshot CAS path")
    ports = {"blue": args.blue_port, "green": args.green_port}
    if args.adapter == "synthetic":
        if args.slot_program is None or args.service_executable is None:
            raise ValueError("synthetic adapter requires disposable program and service executables")
        adapter = _SyntheticSlotAdapter(disposable, args.slot_program, args.service_executable, selector, ports)
    else:
        # Validate every recorded production slot before any controller action;
        # a production transaction cannot fall back to a global program.
        for slot in (record.blue, record.green):
            if slot is not None:
                _production_slot_resolver(slot)
        adapter = LongRunningLaunchdAdapter(
            selector=selector, ports=ports, executor=_production_launchd_executor,
            resolver=_production_slot_resolver,
        )
    current = verify_composition(current_path, args.current_digest)
    prior = verify_composition(prior_path, args.prior_digest)
    if record.candidate_digest != candidate_digest(current):
        raise ValueError("record is not bound to current composition")
    if record.prior_snapshot is not None and record.prior_snapshot.composition_digest != candidate_digest(prior):
        raise ValueError("record snapshot is not bound to prior composition")
    key = key_path.read_bytes()
    if not key or len(key) > _MAX_EVIDENCE_BYTES:
        raise ValueError("Gate B key must be non-empty bounded evidence")

    def selector_read() -> str | None:
        if not selector.exists():
            return None
        value = selector.read_text(encoding="utf-8").strip()
        return value or None

    def selector_cas(expected: str | None, replacement: str) -> bool:
        if selector_read() != expected:
            return False
        temporary = selector.with_name(f".{selector.name}.tmp")
        temporary.write_text(replacement + "\n", encoding="utf-8")
        os.replace(temporary, selector)
        return True

    def evidence_reader(slot: SlotIdentity) -> Mapping[str, object]:
        return adapter.evidence(slot)

    return DeploymentController(
        current, HealthVerifier(["hermes-resync"], evidence_reader=evidence_reader),
        selector_read=selector_read, selector_cas=selector_cas,
        fence=adapter.fence, start=adapter.start, retire=adapter.retire,
        gate_b_verifier=GateBReceiptVerifier(key), prior_composition=prior,
        journal_dir=journal.parent, journal_path=journal, lock_path=lock,
    )


def main(argv: Sequence[str] | None = None, *, controller: object | None = None) -> int:
    """Run artifact commands or explicit disposable deployment transactions."""
    args = _parser().parse_args(argv)
    if args.command == "snapshot":
        composition = verify_composition(args.composition_manifest, args.composition_digest)
        create = create_dirty_source_snapshot if args.kind == "dirty-source" else create_active_release_snapshot
        snapshot = create(args.source, args.cas, args.allow, composition_digest=candidate_digest(composition))
        _emit({"manifest_digest": snapshot.manifest_digest, "tree_digest": snapshot.tree_digest, "entries": snapshot.entry_count, "composition_digest": snapshot.composition_digest})
        return 0
    if args.command in {"stage", "preflight"}:
        composition = verify_composition(args.manifest, args.digest)
        _emit({"candidate_source_sha": composition.candidate_source_sha, "verified": True})
        return 0
    if args.command == "health":
        evidence = _read_json(args.evidence, "health evidence")
        _emit({"artifact_only": True, "ready": evidence.get("ready") is True})
        return 0
    receipt = args.gate_b_receipt
    if args.command in {"activate", "rollback"} and not receipt:
        raise GateBRequired("Gate B receipt is required for selector or service actions")
    record = _load_record(args.record)
    active_controller = controller or _default_controller(args, record)
    if args.command == "recover":
        result = active_controller.recover(record, receipt=receipt)
    else:
        active_controller.record = record
        result = getattr(active_controller, args.command)(receipt)
    _emit({"state": result.state.value, "phase": result.detail.get("phase")})
    return 0
