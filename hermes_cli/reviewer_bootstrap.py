"""Stdlib-only typed-reviewer bootstrap entry point."""

from __future__ import annotations

import json
import os
import runpy

import secrets
import struct
import sys
import time
import types
from typing import Any, NoReturn

_child: Any
_parent: Any


def _write(value: dict[str, Any]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if not raw or len(raw) > 1024:
        raise ValueError("bootstrap_protocol_violation")
    _child.write(struct.pack(">I", len(raw)) + raw)
    _child.flush()


def _write_partial(value: dict[str, Any], phase: str) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    wire = struct.pack(">I", len(raw)) + raw
    cutoff = 2 if phase.endswith("header") else 4 + max(1, len(raw) // 2)
    _child.write(wire[:cutoff])
    _child.flush()
    time.sleep(1)
    raise SystemExit(75)


def _fail(reason: str) -> NoReturn:
    try:
        _write({"reason": reason, "v": 1})
    except Exception:
        pass
    raise SystemExit(74)


def _exact(handle: Any, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = handle.read(size - len(value))
        if not chunk:
            _fail("bootstrap_protocol_violation")
        value.extend(chunk)
    return bytes(value)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("bootstrap_protocol_violation")
        value[key] = item
    return value


def _read(max_bytes: int) -> dict[str, Any]:
    size = struct.unpack(">I", _exact(_parent, 4))[0]
    if size < 1 or size > max_bytes:
        _fail("bootstrap_protocol_violation")
    raw = _exact(_parent, size)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except Exception:
        _fail("bootstrap_protocol_violation")
    if not isinstance(value, dict):
        _fail("bootstrap_protocol_violation")
    if json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != raw:
        _fail("bootstrap_protocol_violation")
    return value


if len(sys.argv) not in {3, 6}:
    raise SystemExit(74)
_child = os.fdopen(int(sys.argv[1]), "wb", buffering=0)
_parent = os.fdopen(int(sys.argv[2]), "rb", buffering=0)
if len(sys.argv) == 6:
    bundle_fd = int(os.environ.pop("HERMES_KANBAN_VERIFIED_SNAPSHOT_FD"))
    with os.fdopen(bundle_fd, "r", encoding="utf-8") as bundle_stream:
        snapshot_bundle = json.load(bundle_stream)
    if not isinstance(snapshot_bundle, dict):
        raise SystemExit(74)
    raw_verified = snapshot_bundle.get("verified_bytes")
    if not isinstance(raw_verified, dict):
        raise SystemExit(74)
    try:
        snapshot_source = bytes.fromhex(
            str(raw_verified["hermes_cli/kanban_runtime_snapshot.py"])
        )
    except (KeyError, ValueError):
        raise SystemExit(74)
    snapshot_module = types.ModuleType("hermes_cli.kanban_runtime_snapshot")
    snapshot_module.__file__ = os.path.join(
        str(snapshot_bundle["object_root"]),
        "payload", "hermes_cli", "kanban_runtime_snapshot.py",
    )
    sys.modules["hermes_cli.kanban_runtime_snapshot"] = snapshot_module
    exec(compile(snapshot_source, snapshot_module.__file__, "exec"), snapshot_module.__dict__)
    spec = snapshot_module.runtime_snapshot_from_verified_bundle(snapshot_bundle)
    sys.path[:] = snapshot_module.snapshot_runtime_sys_path(spec)
    snapshot_module.install_snapshot_bootstrap_capability(spec)
    payload_root = str(spec.payload_root)
marker = os.environ.pop("HERMES_REVIEWER_BOOTSTRAP_PID_MARKER", "")
if marker:
    with open(marker, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
if os.environ.pop("HERMES_REVIEWER_BOOTSTRAP_HANG_BEFORE_HELLO", "") == "1":
    time.sleep(60)
challenge = secrets.token_hex(32)
partial_phase = os.environ.pop("HERMES_REVIEWER_BOOTSTRAP_PARTIAL_PHASE", "")
hello = {"challenge": challenge, "pid": os.getpid(), "ppid": os.getppid(), "v": 1}
if partial_phase.startswith("hello_"):
    _write_partial(hello, partial_phase)
_write(hello)
if os.environ.pop("HERMES_REVIEWER_BOOTSTRAP_HANG_AFTER_HELLO", "") == "1":
    time.sleep(60)
grant = _read(4096)
required = {
    "v", "challenge", "task_id", "run_id", "claim_lock", "role", "profile",
    "workflow", "pid", "parent_pid", "expires_at", "grant_id",
}
if set(grant) != required or grant.get("v") != 1:
    _fail("grant_binding_mismatch")
if grant.get("challenge") != challenge or grant.get("pid") != os.getpid():
    _fail("grant_binding_mismatch")
if grant.get("parent_pid") != os.getppid():
    _fail("grant_binding_mismatch")
if not isinstance(grant.get("expires_at"), int):
    _fail("grant_binding_mismatch")
if grant["expires_at"] <= int(time.time()):
    _fail("grant_expired")

bootstrap_mode = os.environ.pop("HERMES_KANBAN_BOOTSTRAP_MODE", "reviewer")
if bootstrap_mode not in {"reviewer", "generic"}:
    _fail("bootstrap_protocol_violation")
pythonpath = os.environ.pop("HERMES_KANBAN_REVIEW_PYTHONPATH", "")
if len(sys.argv) == 3:
    for entry in reversed([part for part in pythonpath.split(os.pathsep) if part]):
        if entry not in sys.path:
            sys.path.insert(0, entry)
if bootstrap_mode == "reviewer":
    from tools.reviewer_authority import activate_reviewer

    activate_reviewer(grant)
if os.environ.pop("HERMES_REVIEWER_BOOTSTRAP_EXIT_AFTER_GRANT", "") == "1":
    raise SystemExit(75)
ready = {"grant_id": grant["grant_id"], "pid": os.getpid(), "v": 1}
if partial_phase.startswith("ready_"):
    if partial_phase == "ready_eof":
        _write(ready)
        time.sleep(1)
        raise SystemExit(75)
    _write_partial(ready, partial_phase)
_write(ready)
_child.close()
if _parent.read(1) != b"":
    _fail("bootstrap_protocol_violation")
_parent.close()

launch = json.loads(os.environ.pop("HERMES_KANBAN_REVIEW_LAUNCH_ARGV"))
if not isinstance(launch, list) or not launch or not all(isinstance(value, str) for value in launch):
    raise SystemExit(74)
payload = launch[2:] if len(launch) >= 2 and launch[0] == "-p" else launch
if payload[0] == "__run_path__" and len(payload) == 2:
    runpy.run_path(payload[1], run_name="__main__")
else:
    sys.argv = ["hermes", *launch]
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=False)
