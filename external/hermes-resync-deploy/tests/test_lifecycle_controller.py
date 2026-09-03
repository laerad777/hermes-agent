from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2] / "hermes-resync-plugin"))
from hermes_resync.protocol import Envelope, UnixSocketClient, occurred_now
from hermes_resync.skills import LifecycleProducer
from hermes_resync_deploy.lifecycle_controller import CancelRequest, LifecycleController, LifecycleControllerError, MAX_SOCKET_FRAME_BYTES


def envelope(sequence: int, event_id: str | None = None) -> Envelope:
    return Envelope(run_id="run", task_id="task", candidate_digest="digest", sequence=sequence, kind="progress", occurred_at=occurred_now(), event_id=event_id or f"id-{sequence}", payload={"message": "working"})


class LifecycleControllerTests(unittest.TestCase):
    def test_socket_duplicate_gap_restart_replay_and_forbidden_kind(self) -> None:
        with self._materials() as paths:
            controller = self._controller(paths)
            self.assertEqual(controller.claim("run", "task", "digest"), {"status": "claimed"})
            with self._server(controller):
                client = UnixSocketClient(socket_path=paths["socket"], token_path=paths["token"])
                producer = LifecycleProducer.connect(run_id="run", task_id="task", candidate_digest="digest", socket_path=paths["socket"], token_path=paths["token"])
                first = producer.progress(message="working")
                client.send(first)
                with self.assertRaises(Exception): client.send(envelope(3))
                forbidden = envelope(2).as_dict(); forbidden["kind"] = "complete"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
                    raw.connect(os.fspath(paths["socket"])); raw.sendall((json.dumps({"token": "private-key", "event": forbidden}) + "\n").encode())
                    self.assertIn(b'"status":"rejected"', raw.recv(4096))
            recovered = self._controller(paths)
            self.assertEqual(recovered.accept(envelope(2).as_dict())["status"], "accepted")

    def test_artifact_schema_containment_and_log_recovery(self) -> None:
        with self._materials() as paths:
            artifact = paths["artifacts"] / "result.txt"; artifact.write_text("result", encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            controller = self._controller(paths); controller.claim("run", "task", "digest")
            accepted = envelope(1).as_dict(); accepted["kind"] = "artifact_declared"; accepted["payload"] = {"relative_path": "result.txt", "sha256": digest}
            self.assertEqual(controller.accept(accepted)["status"], "accepted")
            self.assertEqual(self._controller(paths).handshake(("run", "task", "digest"))["expected_sequence"], 2)
            wrong_digest = envelope(2).as_dict(); wrong_digest["kind"] = "artifact_declared"; wrong_digest["payload"] = {"relative_path": "result.txt", "sha256": "0" * 64}
            with self.assertRaises(LifecycleControllerError): controller.accept(wrong_digest)
            self.assertEqual(controller.handshake(("run", "task", "digest"))["expected_sequence"], 2)
            for path in ("../secret", "/tmp/secret", "nested/../secret"):
                rejected = envelope(2).as_dict(); rejected["kind"] = "artifact_declared"; rejected["payload"] = {"relative_path": path, "sha256": digest}
                with self.assertRaises(LifecycleControllerError): controller.accept(rejected)
            invalid = envelope(2).as_dict(); invalid["kind"] = "artifact_declared"; invalid["payload"] = {"relative_path": "result.txt", "sha256": digest, "uri": "file:///secret"}
            with self.assertRaises(LifecycleControllerError): controller.accept(invalid)
            for payload in ({"relative_path": "missing.txt", "sha256": digest}, {"relative_path": "result.txt", "sha256": digest.upper()}):
                invalid = envelope(2).as_dict(); invalid["kind"] = "artifact_declared"; invalid["payload"] = payload
                with self.assertRaises(LifecycleControllerError): controller.accept(invalid)
            outside = paths["artifacts"].parent / "outside.txt"; outside.write_text("outside", encoding="utf-8")
            os.symlink(outside, paths["artifacts"] / "linked.txt")
            linked = envelope(2).as_dict(); linked["kind"] = "artifact_declared"; linked["payload"] = {"relative_path": "linked.txt", "sha256": digest}
            with self.assertRaises(LifecycleControllerError): controller.accept(linked)

    def test_socket_rejects_oversized_frame_before_newline(self) -> None:
        with self._materials() as paths:
            controller = self._controller(paths)
            with self._server(controller):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
                    raw.connect(os.fspath(paths["socket"])); raw.sendall(b"x" * (MAX_SOCKET_FRAME_BYTES + 1))
                    self.assertIn(b'"status":"rejected"', raw.recv(4096))

    def test_signed_cancel_is_durable_and_invalid_signature_is_rejected(self) -> None:
        with self._materials() as paths:
            controller = self._controller(paths); controller.claim("run", "task", "digest")
            request = CancelRequest("run", "task", "digest", "bad")
            self.assertEqual(controller.cancel(request), {"status": "rejected"})
            signature = hmac.new(paths["key"].read_bytes(), request.signed_body(), hashlib.sha256).hexdigest()
            self.assertEqual(controller.cancel(CancelRequest("run", "task", "digest", signature)), {"status": "cancel_requested"})
            self.assertEqual(controller.acknowledge_cancel(("run", "task", "digest")), {"status": "cancelled"})

    @staticmethod
    def _controller(paths: dict[str, Path]) -> LifecycleController:
        return LifecycleController(event_log=paths["log"], token_path=paths["token"], cancel_key_path=paths["key"], artifact_root=paths["artifacts"], socket_path=paths["socket"])

    class _materials:
        def __enter__(self) -> dict[str, Path]:
            self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.__enter__())
            paths = {"log": root / "events.jsonl", "token": root / "token", "key": root / "cancel", "socket": root / "controller.sock", "artifacts": root / "artifacts"}
            paths["artifacts"].mkdir()
            for name in ("token", "key"): paths[name].write_bytes(b"private-key"); os.chmod(paths[name], 0o600)
            return paths
        def __exit__(self, *args: object) -> None: self.temp.__exit__(*args)

    class _server:
        def __init__(self, controller: LifecycleController) -> None: self.controller = controller
        def __enter__(self) -> None: self.controller.bind(); self.thread = threading.Thread(target=self.controller.serve); self.thread.start()
        def __exit__(self, *args: object) -> None:
            self.controller.close(); self.thread.join(timeout=2)
            if self.thread.is_alive(): raise RuntimeError("controller server did not terminate")
