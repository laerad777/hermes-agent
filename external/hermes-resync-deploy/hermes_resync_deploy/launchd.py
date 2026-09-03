"""Bounded launchd artifacts for disposable slot identities."""

from __future__ import annotations

import json
import hashlib
import os
import plistlib
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .model import SlotIdentity

_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PREFIX = "org.hermes.resync.slot."
_PROTECTED = ("/Users/jerome/.hermes", "/current")
_MAX_PLIST_BYTES = 64 * 1024


SlotResolver = Callable[[SlotIdentity], SlotIdentity]


@dataclass(frozen=True, slots=True)
class SlotProfile:
    """Complete, disposable launch profile bound to exactly one slot."""

    slot: SlotIdentity
    label: str
    program_arguments: tuple[str, ...]
    home: Path
    hermes_home: Path
    selector_path: Path
    selector_owner: str
    port: int
    health_nonce: str = ""


def _reject_live_path(path: Path) -> Path:
    resolved = path.absolute()
    raw = str(resolved)
    if any(raw == prefix or raw.startswith(prefix + "/") for prefix in _PROTECTED):
        raise ValueError("live runtime paths are outside the disposable-slot contract")
    return resolved


def _checked(slot: SlotIdentity) -> None:
    if not _SLOT.fullmatch(slot.name) or not _SLOT.fullmatch(slot.revision):
        raise ValueError("slot name and revision must be bounded identities")
    _reject_live_path(slot.root)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_runtime(slot: SlotIdentity) -> SlotIdentity:
    """Reject an unresolved or substituted per-slot runtime identity."""
    if slot.executable is None or slot.interpreter is None:
        raise ValueError("slot lacks a verified executable and interpreter identity")
    root = Path(slot.root).resolve()
    raw_executable, raw_interpreter = Path(slot.executable), Path(slot.interpreter)
    if not raw_executable.is_absolute() or not raw_interpreter.is_absolute():
        raise ValueError("slot runtime identities must be absolute")
    if raw_executable.is_symlink() or raw_interpreter.is_symlink():
        raise ValueError("slot runtime identities must be regular files")
    executable, interpreter = raw_executable.resolve(), raw_interpreter.resolve()
    if not executable.is_absolute() or not interpreter.is_absolute():
        raise ValueError("slot runtime identities must be absolute")
    try:
        executable.relative_to(root)
        interpreter.relative_to(root)
    except ValueError as exc:
        raise ValueError("slot runtime identities must be under the slot root") from exc
    if not executable.is_file() or executable.is_symlink() or not interpreter.is_file() or interpreter.is_symlink():
        raise ValueError("slot runtime identities must be regular files")
    if not slot.executable_digest or not slot.interpreter_digest:
        raise ValueError("slot runtime identities require digests")
    if _digest(executable) != slot.executable_digest or _digest(interpreter) != slot.interpreter_digest:
        raise ValueError("slot runtime identity digest mismatch")
    return SlotIdentity(slot.name, root, slot.revision, slot.candidate_digest, slot.pid,
                        executable, interpreter, slot.executable_digest, slot.interpreter_digest)


def slot_service_label(slot: SlotIdentity) -> str:
    """Return the only launchd label permitted for this disposable slot."""
    _checked(slot)
    return _PREFIX + slot.name


def _checked_profile(profile: SlotProfile) -> None:
    _checked(profile.slot)
    if not _LABEL.fullmatch(profile.label):
        raise ValueError("launchd label must be bounded")
    if not _OWNER.fullmatch(profile.selector_owner):
        raise ValueError("selector owner must be bounded")
    if not profile.program_arguments or any(not isinstance(arg, str) or not arg or len(arg) > 1024 for arg in profile.program_arguments):
        raise ValueError("program arguments must be a bounded non-empty sequence")
    program = Path(profile.program_arguments[0])
    if not program.is_absolute():
        raise ValueError("program must be an absolute path")
    for path in (profile.home, profile.hermes_home, profile.selector_path):
        _reject_live_path(path)
    if profile.home == profile.hermes_home:
        raise ValueError("HOME and HERMES_HOME must be isolated")
    if not 1 <= profile.port <= 65535:
        raise ValueError("port must be in the TCP range")


def _make_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def render_launchd_plist(profile: SlotProfile) -> bytes:
    """Render a profile and materialize only its disposable 0700 homes."""
    _checked_profile(profile)
    home, hermes_home = Path(profile.home), Path(profile.hermes_home)
    _make_private_directory(home)
    _make_private_directory(hermes_home)
    environment = {
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_RESYNC_SELECTOR": str(profile.selector_path),
        "HERMES_RESYNC_SELECTOR_OWNER": profile.selector_owner,
        "HERMES_RESYNC_SLOT": profile.slot.name,
        "HERMES_RESYNC_REVISION": profile.slot.revision,
        "HERMES_RESYNC_CANDIDATE_DIGEST": profile.slot.candidate_digest,
        "HERMES_RESYNC_SLOT_ROOT": str(profile.slot.root),
        "HERMES_RESYNC_PORT": str(profile.port),
    }
    if profile.health_nonce:
        environment["HERMES_RESYNC_HEALTH_NONCE"] = profile.health_nonce
    if profile.slot.pid is not None:
        environment["HERMES_RESYNC_PID"] = str(profile.slot.pid)
    if profile.slot.executable is not None:
        environment["HERMES_RESYNC_EXECUTABLE"] = str(profile.slot.executable)
        environment["HERMES_RESYNC_EXECUTABLE_DIGEST"] = profile.slot.executable_digest
    if profile.slot.interpreter is not None:
        environment["HERMES_RESYNC_INTERPRETER"] = str(profile.slot.interpreter)
        environment["HERMES_RESYNC_INTERPRETER_DIGEST"] = profile.slot.interpreter_digest
    payload = {
        "Label": profile.label,
        "ProgramArguments": list(profile.program_arguments),
        "WorkingDirectory": str(profile.slot.root),
        "EnvironmentVariables": environment,
        "RunAtLoad": False,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _read_plist(plist: bytes | str | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(plist, Mapping):
        return plist
    raw = plist.encode() if isinstance(plist, str) else plist
    if len(raw) > _MAX_PLIST_BYTES:
        raise ValueError("plist exceeds bounded size")
    decoded = plistlib.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("plist must be a dictionary")
    return decoded


def parse_launchd_plist(plist: bytes | str | Mapping[str, object]) -> SlotProfile:
    """Parse and validate a full disposable profile without operating launchd."""
    decoded = _read_plist(plist)
    environment = decoded.get("EnvironmentVariables")
    arguments = decoded.get("ProgramArguments")
    if not isinstance(environment, dict) or not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError("launchd profile has invalid environment or program arguments")
    required = ("HOME", "HERMES_HOME", "HERMES_RESYNC_SELECTOR", "HERMES_RESYNC_SELECTOR_OWNER", "HERMES_RESYNC_SLOT", "HERMES_RESYNC_REVISION", "HERMES_RESYNC_CANDIDATE_DIGEST", "HERMES_RESYNC_SLOT_ROOT", "HERMES_RESYNC_PORT")
    if any(not isinstance(environment.get(key), str) for key in required):
        raise ValueError("launchd profile lacks required bounded identity")
    pid_text = environment.get("HERMES_RESYNC_PID")
    try:
        pid = int(pid_text) if isinstance(pid_text, str) else None
        port = int(environment["HERMES_RESYNC_PORT"])
    except ValueError as exc:
        raise ValueError("launchd profile contains an invalid numeric identity") from exc
    runtime = tuple(environment.get(key) for key in ("HERMES_RESYNC_EXECUTABLE", "HERMES_RESYNC_INTERPRETER", "HERMES_RESYNC_EXECUTABLE_DIGEST", "HERMES_RESYNC_INTERPRETER_DIGEST"))
    if any(value is not None and not isinstance(value, str) for value in runtime) or any(value is None for value in runtime) and any(value is not None for value in runtime):
        raise ValueError("launchd profile has incomplete runtime identity")
    slot = SlotIdentity(environment["HERMES_RESYNC_SLOT"], Path(environment["HERMES_RESYNC_SLOT_ROOT"]), environment["HERMES_RESYNC_REVISION"], environment["HERMES_RESYNC_CANDIDATE_DIGEST"], pid,
                        Path(runtime[0]) if runtime[0] is not None else None,
                        Path(runtime[1]) if runtime[1] is not None else None,
                        runtime[2] or "", runtime[3] or "")
    nonce = environment.get("HERMES_RESYNC_HEALTH_NONCE", "")
    if not isinstance(nonce, str) or len(nonce) > 128:
        raise ValueError("launchd profile contains an invalid health nonce")
    profile = SlotProfile(slot, decoded.get("Label"), tuple(arguments), Path(environment["HOME"]), Path(environment["HERMES_HOME"]), Path(environment["HERMES_RESYNC_SELECTOR"]), environment["HERMES_RESYNC_SELECTOR_OWNER"], port, nonce)
    if decoded.get("WorkingDirectory") != str(profile.slot.root):
        raise ValueError("launchd working directory does not match slot root")
    _checked_profile(profile)
    return profile


def render_slot_plist(slot: SlotIdentity, program: str) -> bytes:
    """Render the legacy bounded slot-only contract using a full profile."""
    _checked(slot)
    root = Path(slot.root)
    return render_launchd_plist(SlotProfile(
        slot, _PREFIX + slot.name, (program, "--slot", slot.name, "--revision", slot.revision),
        root / "home", root / "hermes-home", root / "selector", "resync-controller", 1,
    ))


def parse_slot_service_identity(plist: bytes | str | Mapping[str, object]) -> str:
    """Return a bounded service identity from either supported plist contract."""
    decoded = _read_plist(plist)
    label = decoded.get("Label")
    if not isinstance(label, str):
        raise ValueError("not a resync slot service")
    if label.startswith(_PREFIX):
        name = label.removeprefix(_PREFIX)
        if not _SLOT.fullmatch(name):
            raise ValueError("invalid slot service identity")
        return name
    return parse_launchd_plist(decoded).slot.name


def fence_slot_service(slot: SlotIdentity, executor: Callable[[str], None]) -> None:
    """Fence only through an explicitly injected disposable-service executor."""
    if not callable(executor):
        raise TypeError("fence executor must be injected")
    executor(slot_service_label(slot))


class LongRunningLaunchdAdapter:
    """Operate a launchd slot only through an injected command seam.

    This adapter intentionally has no Gate B policy: callers must place it
    behind the controller's candidate-bound Gate B verifier.
    """

    def __init__(self, *, selector: Path, ports: Mapping[str, int],
                 executor: Callable[[tuple[str, ...]], object], resolver: SlotResolver,
                 poll_attempts: int = 10, poll_interval: float = 0.01,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        if not callable(executor):
            raise TypeError("launchd executor must be injected")
        if not callable(resolver):
            raise TypeError("slot resolver must be injected")
        if poll_attempts < 1 or poll_attempts > 100 or poll_interval < 0 or not callable(sleeper):
            raise ValueError("launchd poll contract is invalid")
        self._selector = selector
        self._ports = dict(ports)
        self._executor = executor
        self._resolver = resolver
        self._poll_attempts, self._poll_interval, self._sleeper = poll_attempts, poll_interval, sleeper
        self._launches: dict[str, tuple[str, int, int]] = {}

    def _profile(self, slot: SlotIdentity) -> SlotProfile:
        resolved = self._resolver(slot)
        if (resolved.name, resolved.root, resolved.revision, resolved.candidate_digest) != (slot.name, slot.root, slot.revision, slot.candidate_digest):
            raise ValueError("slot resolver changed the requested slot identity")
        slot = _verified_runtime(resolved)
        port = self._ports.get(slot.name)
        if port is None and slot.name.startswith("blue-rollback-"):
            port = self._ports.get("blue")
        if port is None:
            raise ValueError("slot has no configured launchd port")
        root = _reject_live_path(slot.root)
        slot = SlotIdentity(slot.name, root, slot.revision, slot.candidate_digest, slot.pid,
                            slot.executable, slot.interpreter, slot.executable_digest, slot.interpreter_digest)
        arguments = (str(slot.interpreter), str(slot.executable))
        return SlotProfile(
            slot, slot_service_label(slot), arguments, root / "home",
            root / "hermes-home", self._selector, "hermes-resync-deploy", port,
        )

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    def _bootout(self, slot: SlotIdentity) -> None:
        self._executor(("/bin/launchctl", "bootout", f"{self._domain()}/{slot_service_label(slot)}"))

    def fence(self, slot: SlotIdentity) -> None:
        self._bootout(slot)

    def retire(self, slot: SlotIdentity) -> None:
        self._bootout(slot)

    def start(self, slot: SlotIdentity) -> SlotIdentity:
        health_path = _reject_live_path(slot.root) / "health.json"
        if health_path.exists() or health_path.is_symlink():
            if health_path.is_dir() or health_path.is_symlink():
                raise ValueError("pre-existing health evidence is not a regular file")
            health_path.unlink()
        nonce = secrets.token_urlsafe(24)
        started_ns = time.time_ns()
        base = self._profile(slot)
        profile = SlotProfile(base.slot, base.label, base.program_arguments, base.home,
                              base.hermes_home, base.selector_path, base.selector_owner,
                              base.port, nonce)
        plist_path = profile.slot.root / "launchd.plist"
        plist_path.write_bytes(render_launchd_plist(profile))
        os.chmod(plist_path, 0o600)
        self._executor(("/bin/launchctl", "bootstrap", self._domain(), str(plist_path)))
        self._executor(("/bin/launchctl", "kickstart", "-k", f"{self._domain()}/{profile.label}"))
        launched = SlotIdentity(slot.name, slot.root, slot.revision, slot.candidate_digest, None,
                                profile.slot.executable, profile.slot.interpreter,
                                profile.slot.executable_digest, profile.slot.interpreter_digest)
        try:
            for attempt in range(self._poll_attempts):
                if health_path.is_file() and not health_path.is_symlink() and health_path.stat().st_mtime_ns >= started_ns:
                    self._launches[slot.name] = (nonce, started_ns, health_path.stat().st_mtime_ns)
                    evidence = self.evidence(launched)
                    status = self._executor(("/bin/launchctl", "print", f"{self._domain()}/{profile.label}"))
                    live_pid = status.get("pid") if isinstance(status, Mapping) else None
                    if (not isinstance(status, Mapping) or status.get("alive") is not True or
                            not isinstance(live_pid, int) or live_pid <= 0):
                        raise ValueError("launchd service exited before readiness")
                    if evidence["pid"] == live_pid:
                        return SlotIdentity(slot.name, slot.root, slot.revision, slot.candidate_digest, evidence["pid"],
                                            profile.slot.executable, profile.slot.interpreter,
                                            profile.slot.executable_digest, profile.slot.interpreter_digest)
                    raise ValueError("launchd health PID differs from launched process")
                if attempt + 1 < self._poll_attempts:
                    self._sleeper(self._poll_interval)
            raise ValueError("launchd service startup timed out waiting for fresh health evidence")
        except BaseException:
            self._launches.pop(slot.name, None)
            self._bootout(slot)
            raise

    def evidence(self, slot: SlotIdentity) -> Mapping[str, object]:
        path = _reject_live_path(slot.root) / "health.json"
        launch = self._launches.get(slot.name)
        if launch is None or not path.is_file() or path.is_symlink():
            raise ValueError("launchd slot has no regular health evidence")
        raw = path.read_bytes()
        if len(raw) > _MAX_PLIST_BYTES:
            raise ValueError("launchd health evidence exceeds bounded size")
        evidence = json.loads(raw)
        if not isinstance(evidence, dict):
            raise ValueError("launchd health evidence must be an object")
        nonce, started_ns, mtime = launch
        port = self._profile(slot).port
        required = {"pid": int, "slot": str, "revision": str, "candidate_digest": str,
                    "socket": str, "port": int, "port_owner": str, "health_nonce": str,
                    "timestamp_ns": int}
        if any(not isinstance(evidence.get(key), kind) for key, kind in required.items()):
            raise ValueError("launchd health evidence lacks process identity")
        if (path.stat().st_mtime_ns != mtime or evidence["pid"] <= 0 or
                (slot.pid is not None and evidence["pid"] != slot.pid) or
                evidence["slot"] != slot.name or evidence["revision"] != slot.revision or
                evidence["candidate_digest"] != slot.candidate_digest or
                evidence["socket"] != str(slot.root / "service.sock") or evidence["port"] != port or
                evidence["port_owner"] != slot.name or evidence["health_nonce"] != nonce or
                evidence["timestamp_ns"] < started_ns):
            raise ValueError("launchd health evidence is not owned by the started slot")
        return evidence
