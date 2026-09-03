from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from hermes_resync.protocol import Cancelled, Envelope, MAX_PROGRESS_MESSAGE_BYTES, MAX_SOCKET_FRAME_BYTES, ProducerHighWater, ProtocolError, RetryQueue, SequenceGap, StreamGuard, UnixSocketClient, occurred_now


def event(sequence: int, event_id: str | None = None) -> Envelope:
    return Envelope(run_id="run-1", task_id="task-1", candidate_digest="a" * 64, sequence=sequence, kind="progress", occurred_at=occurred_now(), event_id=event_id or f"event-{sequence}", payload={"message": "working"})


class ProtocolTests(unittest.TestCase):
    def test_exact_envelope_and_forbidden_authority(self) -> None:
        self.assertEqual(set(event(1).as_dict()), {"version", "event_id", "run_id", "task_id", "candidate_digest", "sequence", "kind", "occurred_at", "payload"})
        with self.assertRaises(ProtocolError):
            Envelope(run_id="r", task_id="t", candidate_digest="d", sequence=1, kind="complete", occurred_at=occurred_now(), payload={})
        with self.assertRaises(ProtocolError):
            Envelope.from_dict({**event(1).as_dict(), "authority": "controller"})

    def test_per_kind_payload_schemas_reject_unsafe_and_secret_fields(self) -> None:
        base = event(1).as_dict()
        invalid_payloads = (
            {"message": "working", "secret": "token"},
            {"message": "line\nbreak"},
            {"message": "x" * (MAX_PROGRESS_MESSAGE_BYTES + 1)},
            {"relative_path": "../secret", "sha256": "a" * 64},
            {"relative_path": "/secret", "sha256": "a" * 64},
            {"relative_path": "result", "sha256": "A" * 64},
            {"relative_path": "result", "sha256": "a" * 64, "uri": "file:///secret"},
        )
        for payload in invalid_payloads:
            value = {**base, "kind": "artifact_declared" if "relative_path" in payload else "progress", "payload": payload}
            with self.assertRaises(ProtocolError):
                Envelope.from_dict(value)

    def test_socket_response_frame_is_bounded_before_newline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw); token, socket_path = directory / "token", directory / "controller.sock"
            token.write_text("local-token", encoding="utf-8"); os.chmod(token, 0o600)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(os.fspath(socket_path)); server.listen(1)
                def oversized_response() -> None:
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096); connection.sendall(b"x" * (MAX_SOCKET_FRAME_BYTES + 1))
                thread = threading.Thread(target=oversized_response); thread.start()
                with self.assertRaises(ProtocolError): UnixSocketClient(socket_path=socket_path, token_path=token).expected_sequence(("run", "task", "digest"))
                thread.join()

    def test_duplicate_gap_and_cancel_fail_closed(self) -> None:
        guard = StreamGuard()
        self.assertTrue(guard.accept(event(1)))
        self.assertFalse(guard.accept(event(1)))
        with self.assertRaises(SequenceGap):
            guard.accept(event(3))
        guard.cancel(event(1))
        with self.assertRaises(Cancelled):
            guard.accept(event(2))

    def test_ack_loss_queues_and_replay_uses_real_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            token, queue_path, socket_path = directory / "token", directory / "queue", directory / "controller.sock"
            token.write_text("local-token", encoding="utf-8")
            os.chmod(token, 0o600)
            queue = RetryQueue(queue_path)
            client = UnixSocketClient(socket_path=socket_path, token_path=token, queue=queue)
            first = event(1)
            with self.assertRaises(ProtocolError):
                client.send(first)
            self.assertEqual(queue.read(), [first])
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(os.fspath(socket_path)); server.listen(1)
                def acknowledge() -> None:
                    connection, _ = server.accept()
                    with connection:
                        request = connection.recv(4096)
                        self.assertIn(b"local-token", request)
                        connection.sendall(b'{"event_id":"event-1","status":"accepted","expected_sequence":2}\n')
                thread = threading.Thread(target=acknowledge); thread.start()
                client.replay(); thread.join()
            self.assertEqual(queue.read(), [])

    def test_high_water_reserves_sequences_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sequence"
            first = ProducerHighWater(path)
            self.assertEqual(first.reserve(), 1)
            self.assertEqual(first.reserve(), 2)
            recovered = ProducerHighWater(path)
            self.assertEqual(recovered.read(), 2)
            self.assertEqual(recovered.reserve(), 3)

    def test_retry_queue_refuses_to_discard_an_ordered_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            queue = RetryQueue(Path(raw) / "queue", maximum=1)
            first = event(1)
            queue.append(first)
            with self.assertRaises(ProtocolError):
                queue.append(event(2))
            self.assertEqual(queue.read(), [first])
