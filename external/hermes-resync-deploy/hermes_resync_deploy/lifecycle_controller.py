"""Disposable local lifecycle controller with durable claimed-task authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import socket
import stat
from typing import Any

VERSION = "gjc.clawhip.lifecycle/v1"
ALLOWED_KINDS = frozenset({"progress", "artifact_declared"})
_FIELDS = frozenset({"version", "event_id", "run_id", "task_id", "candidate_digest", "sequence", "kind", "occurred_at", "payload"})
MAX_PROGRESS_MESSAGE_BYTES = 1024
MAX_SOCKET_FRAME_BYTES = 65536
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class LifecycleControllerError(ValueError):
    pass


def _private(path: Path) -> None:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise LifecycleControllerError("private controller material must be a 0600 regular file")


def _stream_value(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, (tuple, list)) or len(value) != 3 or not all(isinstance(item, str) and item for item in value):
        raise LifecycleControllerError("invalid claimed task authority")
    return tuple(value)  # type: ignore[return-value]


@dataclass(frozen=True)
class CancelRequest:
    run_id: str
    task_id: str
    candidate_digest: str
    signature: str

    def signed_body(self) -> bytes:
        return json.dumps({"candidate_digest": self.candidate_digest, "run_id": self.run_id, "task_id": self.task_id}, separators=(",", ":"), sort_keys=True).encode()


class LifecycleController:
    """Owns a private Unix socket and durable lifecycle task authority."""

    def __init__(self, *, event_log: Path, token_path: Path, cancel_key_path: Path, artifact_root: Path, socket_path: Path | None = None) -> None:
        self.event_log, self.token_path, self.cancel_key_path = Path(event_log), Path(token_path), Path(cancel_key_path)
        self.socket_path = Path(socket_path) if socket_path is not None else self.event_log.with_suffix(".sock")
        supplied_artifact_root = Path(artifact_root)
        if supplied_artifact_root.is_symlink() or not supplied_artifact_root.is_dir():
            raise LifecycleControllerError("artifact root must be a non-symlink directory")
        self.artifact_root = supplied_artifact_root.resolve(strict=True)
        self.event_log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.event_log.exists():
            os.close(os.open(self.event_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        for path in (self.event_log, self.token_path, self.cancel_key_path):
            _private(path)
        self._ids: set[str] = set()
        self._next: dict[tuple[str, str, str], int] = {}
        self._claimed: set[tuple[str, str, str]] = set()
        self._cancel_requested: set[tuple[str, str, str]] = set()
        self._cancelled: set[tuple[str, str, str]] = set()
        self._listener: socket.socket | None = None
        self._recover()

    @staticmethod
    def _stream(event: dict[str, Any]) -> tuple[str, str, str]:
        return event["run_id"], event["task_id"], event["candidate_digest"]

    def _recover(self) -> None:
        for line in self.event_log.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            record_type = record.get("type")
            if record_type == "claim":
                self._claimed.add(_stream_value(record.get("stream")))
            elif record_type == "event":
                event = record.get("event")
                self._validate_event(event)
                stream = self._stream(event)
                if stream not in self._claimed or event["event_id"] in self._ids or event["sequence"] != self._next.get(stream, 1):
                    raise LifecycleControllerError("durable event log is corrupt")
                self._ids.add(event["event_id"])
                self._next[stream] = event["sequence"] + 1
            elif record_type == "cancel_requested":
                self._cancel_requested.add(_stream_value(record.get("stream")))
            elif record_type == "cancelled":
                stream = _stream_value(record.get("stream"))
                self._cancel_requested.discard(stream)
                self._cancelled.add(stream)
            else:
                raise LifecycleControllerError("durable event log is corrupt")

    def claim(self, run_id: str, task_id: str, candidate_digest: str) -> dict[str, Any]:
        """Create durable task authority from the trusted controller control plane."""
        stream = _stream_value((run_id, task_id, candidate_digest))
        if stream not in self._claimed:
            self._append({"type": "claim", "stream": list(stream)})
            self._claimed.add(stream)
        return {"status": "claimed"}

    def accept(self, event: dict[str, Any]) -> dict[str, Any]:
        self._validate_event(event)
        stream, event_id = self._stream(event), event["event_id"]
        expected = self._next.get(stream, 1)
        if stream not in self._claimed:
            return self._ack(event_id, "rejected", expected)
        if stream in self._cancelled:
            return self._ack(event_id, "cancelled", expected)
        if stream in self._cancel_requested:
            return self._ack(event_id, "cancel_requested", expected)
        if event_id in self._ids:
            return self._ack(event_id, "duplicate", expected)
        if event["sequence"] != expected:
            return self._ack(event_id, "gap", expected)
        self._append({"type": "event", "event": event})
        self._ids.add(event_id)
        self._next[stream] = expected + 1
        return self._ack(event_id, "accepted", expected + 1)

    def handshake(self, stream: tuple[str, str, str]) -> dict[str, Any]:
        """Return the durable next sequence before a producer resumes delivery."""
        if stream not in self._claimed or stream in self._cancelled or stream in self._cancel_requested:
            return {"status": "rejected", "expected_sequence": 0}
        return {"status": "ready", "expected_sequence": self._next.get(stream, 1)}

    def cancel(self, request: CancelRequest) -> dict[str, Any]:
        actual = hmac.new(self.cancel_key_path.read_bytes(), request.signed_body(), hashlib.sha256).hexdigest()
        stream = (request.run_id, request.task_id, request.candidate_digest)
        if not hmac.compare_digest(actual, request.signature) or stream not in self._claimed:
            return {"status": "rejected"}
        if stream not in self._cancelled and stream not in self._cancel_requested:
            self._append({"type": "cancel_requested", "stream": list(stream)})
            self._cancel_requested.add(stream)
        return {"status": "cancel_requested"}

    def acknowledge_cancel(self, stream: tuple[str, str, str]) -> dict[str, Any]:
        if stream not in self._cancel_requested:
            return {"status": "rejected"}
        self._append({"type": "cancelled", "stream": list(stream)})
        self._cancel_requested.remove(stream)
        self._cancelled.add(stream)
        return {"status": "cancelled"}

    def bind(self) -> socket.socket:
        if self._listener is not None:
            raise LifecycleControllerError("controller socket is already bound")
        directory = self.socket_path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise LifecycleControllerError("controller socket directory must be mode 0700")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise LifecycleControllerError("refusing to remove non-socket controller path")
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(16)
            listener.settimeout(0.1)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        return listener

    def close(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        if self.socket_path.exists() and stat.S_ISSOCK(self.socket_path.lstat().st_mode):
            self.socket_path.unlink()

    def serve(self) -> None:
        """Serve until close() releases this controller-owned listener."""
        if self._listener is None:
            raise LifecycleControllerError("controller socket is not bound")
        while self._listener is not None:
            try:
                self.serve_once()
            except socket.timeout:
                continue
            except OSError:
                if self._listener is None:
                    return
                raise

    def serve_once(self, listener: socket.socket | None = None) -> None:
        server = listener or self._listener
        if server is None:
            raise LifecycleControllerError("controller socket is not bound")
        connection, _ = server.accept()
        with connection:
            try:
                request = self._receive(connection)
            except (json.JSONDecodeError, UnicodeDecodeError, LifecycleControllerError):
                request = None
            event = request.get("event") if isinstance(request, dict) else None
            event_id = event.get("event_id", "") if isinstance(event, dict) else ""
            if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("token", "")).encode(), self.token_path.read_bytes().strip()):
                response = self._ack(event_id, "rejected", 0)
            else:
                try:
                    if "claim" in request:
                        response = self._ack(event_id, "rejected", 0)
                    elif "cancel_ack" in request:
                        response = self.acknowledge_cancel(_stream_value(request["cancel_ack"]))
                    elif "handshake" in request:
                        response = self.handshake(_stream_value(request["handshake"]))
                    else:
                        response = self.accept(request["event"])
                except (KeyError, TypeError, LifecycleControllerError):
                    response = self._ack(event_id, "rejected", 0)
            connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")

    @staticmethod
    def _receive(connection: socket.socket) -> dict[str, Any]:
        data = b""
        while not data.endswith(b"\n"):
            block = connection.recv(4096)
            if not block:
                break
            data += block
            if len(data) > MAX_SOCKET_FRAME_BYTES:
                raise LifecycleControllerError("lifecycle socket frame exceeds maximum size")
        if len(data) > MAX_SOCKET_FRAME_BYTES:
            raise LifecycleControllerError("lifecycle socket frame exceeds maximum size")
        return json.loads(data.decode("utf-8"))

    @staticmethod
    def _ack(event_id: str, status: str, expected: int) -> dict[str, Any]:
        return {"event_id": event_id, "status": status, "expected_sequence": expected}

    def _validate_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict) or set(event) != _FIELDS:
            raise LifecycleControllerError("invalid lifecycle envelope")
        if event["version"] != VERSION or event["kind"] not in ALLOWED_KINDS:
            raise LifecycleControllerError("forbidden lifecycle authority")
        if not all(isinstance(event[key], str) and event[key] for key in ("event_id", "run_id", "task_id", "candidate_digest", "occurred_at")):
            raise LifecycleControllerError("invalid lifecycle identifiers")
        if not isinstance(event["sequence"], int) or isinstance(event["sequence"], bool) or event["sequence"] < 1:
            raise LifecycleControllerError("invalid lifecycle event")
        self._validate_payload(event["kind"], event["payload"])

    def _validate_payload(self, kind: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise LifecycleControllerError("invalid lifecycle payload")
        if kind == "progress":
            if set(payload) not in ({"message"}, {"message", "percent"}):
                raise LifecycleControllerError("invalid progress payload fields")
            message = payload.get("message")
            if not isinstance(message, str) or not message or len(message.encode("utf-8")) > MAX_PROGRESS_MESSAGE_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in message):
                raise LifecycleControllerError("progress message must be bounded safe text")
            if "percent" in payload and (not isinstance(payload["percent"], int) or isinstance(payload["percent"], bool) or not 0 <= payload["percent"] <= 100):
                raise LifecycleControllerError("progress percent must be between 0 and 100")
            return
        if set(payload) != {"relative_path", "sha256"}:
            raise LifecycleControllerError("invalid artifact payload fields")
        relative_path, digest = payload["relative_path"], payload["sha256"]
        if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
            raise LifecycleControllerError("artifact path must be a relative POSIX path")
        path = PurePosixPath(relative_path)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise LifecycleControllerError("artifact path must not escape its allowed root")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise LifecycleControllerError("artifact sha256 must be lowercase hexadecimal")
        actual_digest = self._artifact_sha256(path.parts)
        if not hmac.compare_digest(digest, actual_digest):
            raise LifecycleControllerError("artifact sha256 does not match declared content")

    def _artifact_sha256(self, parts: tuple[str, ...]) -> str:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors: list[int] = []
        try:
            directory_fd = os.open(os.fspath(self.artifact_root), directory_flags)
            descriptors.append(directory_fd)
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise LifecycleControllerError("artifact root is not a directory")
            for part in parts[:-1]:
                directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                descriptors.append(directory_fd)
            artifact_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            descriptors.append(artifact_fd)
            if not stat.S_ISREG(os.fstat(artifact_fd).st_mode):
                raise LifecycleControllerError("artifact must be a regular file")
            digest = hashlib.sha256()
            while block := os.read(artifact_fd, 65536):
                digest.update(block)
            return digest.hexdigest()
        except OSError as error:
            raise LifecycleControllerError("artifact path must be an accessible contained regular file") from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _append(self, record: dict[str, Any]) -> None:
        _private(self.event_log)
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
