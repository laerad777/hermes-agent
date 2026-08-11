"""Process-local reviewer activation and framed bootstrap primitives."""

from __future__ import annotations

import json
import os
import select
import struct
import threading
import time
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping


class BootstrapProtocolError(ValueError):
    pass


class ActivationError(RuntimeError):
    pass


class BootstrapHandshakeTimeoutError(TimeoutError):
    pass


_lock = threading.Lock()
_state = "UNSET"
_activation: Mapping[str, Any] | None = None


def _protocol_error(detail: str) -> BootstrapProtocolError:
    return BootstrapProtocolError(f"bootstrap_protocol_violation: {detail}")


def _read_once(stream: BinaryIO, size: int, deadline: float | None) -> bytes:
    if deadline is None:
        return stream.read(size)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BootstrapHandshakeTimeoutError("bootstrap_handshake_timeout")
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        return stream.read(size)
    readable, _, _ = select.select([fd], [], [], remaining)
    if not readable:
        raise BootstrapHandshakeTimeoutError("bootstrap_handshake_timeout")
    return os.read(fd, size)


def _read_exact(stream: BinaryIO, size: int, deadline: float | None = None) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = _read_once(stream, size - len(chunks), deadline)
        if not chunk:
            raise _protocol_error("unexpected EOF")
        chunks.extend(chunk)
    return bytes(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _protocol_error(f"duplicate field {key!r}")
        result[key] = value
    return result


def read_bootstrap_frame(
    stream: BinaryIO,
    *,
    max_bytes: int,
    fields: Mapping[str, type],
    deadline: float | None = None,
) -> dict[str, Any]:
    try:
        size = struct.unpack(">I", _read_exact(stream, 4, deadline))[0]
        if size == 0 or size > max_bytes:
            raise _protocol_error("invalid frame length")
        raw = _read_exact(stream, size, deadline)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except BootstrapProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, struct.error) as exc:
        raise _protocol_error("malformed frame") from exc
    if not isinstance(value, dict):
        raise _protocol_error("frame payload must be an object")
    if set(value) != set(fields):
        raise _protocol_error("unknown or missing field")
    for name, expected in fields.items():
        actual = value[name]
        if expected is int and (not isinstance(actual, int) or isinstance(actual, bool)):
            raise _protocol_error(f"invalid type for {name}")
        if expected is not int and not isinstance(actual, expected):
            raise _protocol_error(f"invalid type for {name}")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if canonical != raw:
        raise _protocol_error("noncanonical JSON")
    return value


def write_bootstrap_frame(
    stream: BinaryIO,
    value: Mapping[str, Any],
    *,
    max_bytes: int,
    deadline: float | None = None,
) -> None:
    raw = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not raw or len(raw) > max_bytes:
        raise _protocol_error("invalid frame length")
    wire = struct.pack(">I", len(raw)) + raw
    if deadline is None:
        stream.write(wire)
        stream.flush()
        return
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        stream.write(wire)
        stream.flush()
        return

    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    offset = 0
    try:
        while offset < len(wire):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BootstrapHandshakeTimeoutError("bootstrap_handshake_timeout")
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                raise BootstrapHandshakeTimeoutError("bootstrap_handshake_timeout")
            if deadline - time.monotonic() <= 0:
                raise BootstrapHandshakeTimeoutError("bootstrap_handshake_timeout")
            try:
                written = os.write(fd, wire[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise _protocol_error("unexpected zero-length write")
            offset += written
    finally:
        os.set_blocking(fd, was_blocking)


def require_bootstrap_eof(stream: BinaryIO, *, deadline: float | None = None) -> None:
    if _read_once(stream, 1, deadline) != b"":
        raise _protocol_error("trailing data")


def invalidate_activation() -> None:
    """Reset only an unconsumed activation slot."""
    global _state, _activation
    with _lock:
        if _state == "ACTIVE":
            raise ActivationError("activation_is_immutable")
        _state = "UNSET"
        _activation = None


def activate_reviewer(grant: Mapping[str, Any]) -> None:
    global _state, _activation
    with _lock:
        if _state != "UNSET":
            raise ActivationError("activation_already_consumed")
        _state = "CONSUMING"
        try:
            required = {"role": str, "profile": str, "pid": int, "grant_id": str}
            if not set(required).issubset(grant):
                raise ActivationError("grant_binding_mismatch")
            if any(not isinstance(grant[key], expected) for key, expected in required.items()):
                raise ActivationError("grant_binding_mismatch")
            if grant["pid"] != os.getpid():
                raise ActivationError("grant_binding_mismatch")
            _activation = MappingProxyType(dict(grant))
            _state = "ACTIVE"
        except BaseException:
            _activation = None
            _state = "UNSET"
            raise


def require_activation() -> Mapping[str, Any]:
    with _lock:
        if _state != "ACTIVE" or _activation is None:
            raise ActivationError("missing_activation")
        if _activation["pid"] != os.getpid():
            raise ActivationError("activation_pid_mismatch")
        return _activation


def reviewer_active() -> bool:
    try:
        require_activation()
    except ActivationError:
        return False
    return True


if hasattr(os, "register_at_fork"):
    def _make_fork_child_invalidator():
        def invalidate_fork_child() -> None:
            global _state, _activation
            _state = "UNSET"
            _activation = None

        return invalidate_fork_child

    os.register_at_fork(after_in_child=_make_fork_child_invalidator())
    del _make_fork_child_invalidator