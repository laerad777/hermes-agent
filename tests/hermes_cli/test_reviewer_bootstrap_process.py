from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX bootstrap contract")


def _read_frame(fd: int) -> dict:
    header = os.read(fd, 4)
    assert len(header) == 4
    size = struct.unpack(">I", header)[0]
    payload = bytearray()
    while len(payload) < size:
        payload.extend(os.read(fd, size - len(payload)))
    return json.loads(payload)


def _write_frame(fd: int, value: dict) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    os.write(fd, struct.pack(">I", len(payload)) + payload)


def test_real_bootstrap_activates_only_after_bound_grant_and_emits_ready(tmp_path):
    root = Path(__file__).resolve().parents[2]
    marker = tmp_path / "activated.json"
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    for fd in (c2p_r, c2p_w, p2c_r, p2c_w):
        os.set_inheritable(fd, False)
    payload = tmp_path / "payload.py"
    payload.write_text(
        "import json,os,pathlib\n"
        "from tools.reviewer_authority import require_activation\n"
        "pathlib.Path(os.environ['MARKER']).write_text(json.dumps(dict(require_activation())))\n"
    )
    env = dict(os.environ)
    env["HERMES_KANBAN_REVIEW_LAUNCH_ARGV"] = json.dumps(
        ["__run_path__", str(payload)]
    )
    env["PYTHONPATH"] = str(root)
    env["HERMES_KANBAN_REVIEW_PYTHONPATH"] = str(root)
    env["MARKER"] = str(marker)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(root / "hermes_cli" / "reviewer_bootstrap.py"), str(c2p_w), str(p2c_r)],
        cwd=root,
        env=env,
        pass_fds=(c2p_w, p2c_r),
        close_fds=True,
        start_new_session=True,
    )
    os.close(c2p_w)
    os.close(p2c_r)
    hello = _read_frame(c2p_r)
    assert not marker.exists()
    grant = {
        "v": 1,
        "challenge": hello["challenge"],
        "task_id": "t_review",
        "run_id": 7,
        "claim_lock": "lock",
        "role": "critic",
        "profile": "critic",
        "workflow": "jerome-kanban-v1",
        "pid": proc.pid,
        "parent_pid": os.getpid(),
        "expires_at": 4_102_444_800,
        "grant_id": "g-real",
    }
    _write_frame(p2c_w, grant)
    os.close(p2c_w)
    ready = _read_frame(c2p_r)
    os.close(c2p_r)
    assert ready == {"grant_id": "g-real", "pid": proc.pid, "v": 1}
    assert proc.wait(timeout=10) == 0
    assert json.loads(marker.read_text())["grant_id"] == "g-real"