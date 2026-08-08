from __future__ import annotations

import io
import json
import os
import struct
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_activation(monkeypatch):
    from tools import reviewer_authority

    monkeypatch.setattr(reviewer_authority, "_state", "UNSET")
    monkeypatch.setattr(reviewer_authority, "_activation", None)


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def test_bootstrap_frame_round_trip_is_canonical():
    from tools.reviewer_authority import read_bootstrap_frame, write_bootstrap_frame

    stream = io.BytesIO()
    write_bootstrap_frame(stream, {"v": 1, "pid": 7}, max_bytes=1024)
    encoded = stream.getvalue()

    assert encoded == _frame(b'{"pid":7,"v":1}')
    assert read_bootstrap_frame(
        io.BytesIO(encoded), max_bytes=1024, fields={"v": int, "pid": int}
    ) == {"pid": 7, "v": 1}


def test_write_bootstrap_frame_times_out_on_saturated_pipe():
    from tools.reviewer_authority import (
        BootstrapHandshakeTimeoutError,
        write_bootstrap_frame,
    )

    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write_fd, True)
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as stream:
            started = time.monotonic()
            with pytest.raises(
                BootstrapHandshakeTimeoutError,
                match="bootstrap_handshake_timeout",
            ):
                write_bootstrap_frame(
                    stream,
                    {"payload": "y" * 1024},
                    max_bytes=2048,
                    deadline=started + 0.1,
                )
            assert time.monotonic() - started < 1
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_write_bootstrap_frame_rechecks_deadline_after_select_before_write(monkeypatch):
    from tools import reviewer_authority

    read_fd, write_fd = os.pipe()
    writes = []
    real_write = reviewer_authority.os.write
    deadline = time.monotonic() + 0.05

    def delayed_select(*_args):
        time.sleep(0.06)
        return [], [write_fd], []

    def tracked_write(fd, data):
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(reviewer_authority.select, "select", delayed_select)
    monkeypatch.setattr(reviewer_authority.os, "write", tracked_write)
    try:
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as stream:
            with pytest.raises(
                reviewer_authority.BootstrapHandshakeTimeoutError,
                match="bootstrap_handshake_timeout",
            ):
                reviewer_authority.write_bootstrap_frame(
                    stream,
                    {"payload": "y" * 8192},
                    max_bytes=16384,
                    deadline=deadline,
                )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert writes == []


def test_write_bootstrap_frame_rechecks_deadline_before_repeated_partial_write(monkeypatch):
    from tools import reviewer_authority

    read_fd, write_fd = os.pipe()
    writes = []
    real_write = reviewer_authority.os.write
    deadline = time.monotonic() + 0.05

    def always_writable(*_args):
        return [], [write_fd], []

    def partial_write(fd, data):
        writes.append(bytes(data[:1024]))
        written = real_write(fd, data[:1024])
        time.sleep(0.06)
        return written

    monkeypatch.setattr(reviewer_authority.select, "select", always_writable)
    monkeypatch.setattr(reviewer_authority.os, "write", partial_write)
    try:
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as stream:
            with pytest.raises(
                reviewer_authority.BootstrapHandshakeTimeoutError,
                match="bootstrap_handshake_timeout",
            ):
                reviewer_authority.write_bootstrap_frame(
                    stream,
                    {"payload": "z" * 8192},
                    max_bytes=16384,
                    deadline=deadline,
                )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert len(writes) == 1


def test_write_bootstrap_frame_slow_reader_gets_complete_large_frame_within_deadline():
    from tools.reviewer_authority import write_bootstrap_frame

    read_fd, write_fd = os.pipe()
    payload = {"payload": "q" * 65536}
    received = bytearray()

    def slow_reader():
        while True:
            chunk = os.read(read_fd, 2048)
            if not chunk:
                return
            received.extend(chunk)
            time.sleep(0.001)

    reader = threading.Thread(target=slow_reader)
    reader.start()
    try:
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as stream:
            write_bootstrap_frame(
                stream,
                payload,
                max_bytes=131072,
                deadline=time.monotonic() + 2,
            )
    finally:
        os.close(write_fd)
        reader.join(timeout=2)
        os.close(read_fd)

    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert bytes(received) == _frame(raw)


def test_write_bootstrap_frame_reports_broken_pipe_with_deadline():
    from tools.reviewer_authority import write_bootstrap_frame

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as stream:
            with pytest.raises(BrokenPipeError):
                write_bootstrap_frame(
                    stream, {"v": 1}, max_bytes=1024, deadline=time.monotonic() + 1
                )
    finally:
        os.close(write_fd)


@pytest.mark.parametrize(
    "wire",
    [
        struct.pack(">I", 0),
        struct.pack(">I", 1025),
        _frame(b"{\"v\":1"),
        _frame(b"\xff"),
        _frame(b'{"v":1,"v":1}'),
        _frame(b'{"v":1,"extra":2}'),
        _frame(b"[]"),
    ],
)
def test_bootstrap_frame_rejects_noncanonical_or_invalid_input(wire):
    from tools.reviewer_authority import BootstrapProtocolError, read_bootstrap_frame

    with pytest.raises(BootstrapProtocolError, match="bootstrap_protocol_violation"):
        read_bootstrap_frame(io.BytesIO(wire), max_bytes=1024, fields={"v": int})


def test_bootstrap_channel_requires_eof_after_final_frame():
    from tools.reviewer_authority import BootstrapProtocolError, require_bootstrap_eof

    with pytest.raises(BootstrapProtocolError, match="bootstrap_protocol_violation"):
        require_bootstrap_eof(io.BytesIO(b"trailing"))


def test_env_role_spoof_does_not_activate_reviewer(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_ROLE", "critic")

    from tools import reviewer_authority
    from tools.reviewer_surface import typed_reviewer_active

    reviewer_authority.invalidate_activation()
    assert typed_reviewer_active() is False


def test_activation_is_one_shot_and_pid_bound(monkeypatch):
    from tools import reviewer_authority

    reviewer_authority.invalidate_activation()
    reviewer_authority.activate_reviewer(
        {"role": "critic", "profile": "critic", "pid": os.getpid(), "grant_id": "g1"}
    )
    assert reviewer_authority.require_activation()["role"] == "critic"

    with pytest.raises(reviewer_authority.ActivationError, match="activation_already_consumed"):
        reviewer_authority.activate_reviewer(
            {"role": "critic", "profile": "critic", "pid": os.getpid(), "grant_id": "g2"}
        )

    monkeypatch.setattr(reviewer_authority.os, "getpid", lambda: os.getppid())
    with pytest.raises(reviewer_authority.ActivationError, match="activation_pid_mismatch"):
        reviewer_authority.require_activation()


def test_public_invalidation_cannot_downgrade_active_reviewer():
    from tools import reviewer_authority

    reviewer_authority.activate_reviewer(
        {"role": "critic", "profile": "critic", "pid": os.getpid(), "grant_id": "g-active"}
    )

    with pytest.raises(reviewer_authority.ActivationError, match="activation_is_immutable"):
        reviewer_authority.invalidate_activation()

    assert reviewer_authority.require_activation()["grant_id"] == "g-active"


def test_activation_is_invalidated_in_forked_child():
    if not hasattr(os, "fork"):
        pytest.skip("fork is POSIX-only")
    from tools import reviewer_authority

    reviewer_authority.invalidate_activation()
    reviewer_authority.activate_reviewer(
        {"role": "critic", "profile": "critic", "pid": os.getpid(), "grant_id": "g-fork"}
    )
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            reviewer_authority.require_activation()
        except reviewer_authority.ActivationError as exc:
            result = str(exc)
        else:
            result = "active"
        os.write(write_fd, result.encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1024).decode()
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert result == "missing_activation"