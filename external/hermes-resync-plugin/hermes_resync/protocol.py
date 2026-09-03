"""Fail-closed lifecycle observer protocol and local Unix-socket client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import socket
import stat
from typing import Any, Callable, Iterable
from uuid import uuid4

VERSION = "gjc.clawhip.lifecycle/v1"
ALLOWED_KINDS = frozenset({"progress", "artifact_declared"})
MAX_PROGRESS_MESSAGE_BYTES = 1024
MAX_SOCKET_FRAME_BYTES = 65536
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ProtocolError(ValueError):
    """The sender supplied an invalid or unsafe lifecycle message."""


class SequenceGap(ProtocolError):
    """The controller needs an earlier event before accepting this event."""


class Cancelled(ProtocolError):
    """A cancelled stream cannot produce further messages."""


def _private_regular_file(path: Path) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise ProtocolError("required private file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ProtocolError("required private file must be a regular file with mode 0600")


def _validate_payload(kind: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")
    if kind == "progress":
        if set(payload) not in ({"message"}, {"message", "percent"}):
            raise ProtocolError("invalid progress payload fields")
        message = payload.get("message")
        if not isinstance(message, str) or not message or len(message.encode("utf-8")) > MAX_PROGRESS_MESSAGE_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in message):
            raise ProtocolError("progress message must be bounded safe text")
        if "percent" in payload and (not isinstance(payload["percent"], int) or isinstance(payload["percent"], bool) or not 0 <= payload["percent"] <= 100):
            raise ProtocolError("progress percent must be between 0 and 100")
        return
    if set(payload) != {"relative_path", "sha256"}:
        raise ProtocolError("invalid artifact payload fields")
    relative_path, digest = payload["relative_path"], payload["sha256"]
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise ProtocolError("artifact path must be a relative POSIX path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ProtocolError("artifact path must not escape its allowed root")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ProtocolError("artifact sha256 must be lowercase hexadecimal")


@dataclass(frozen=True)
class Envelope:
    """The complete, authority-limited lifecycle envelope."""

    run_id: str
    task_id: str
    candidate_digest: str
    sequence: int
    kind: str
    occurred_at: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid4()))
    version: str = VERSION

    def __post_init__(self) -> None:
        if self.version != VERSION or self.kind not in ALLOWED_KINDS:
            raise ProtocolError("plugin event kind or version is not permitted")
        if not all(isinstance(value, str) and value for value in (self.event_id, self.run_id, self.task_id, self.candidate_digest, self.occurred_at)):
            raise ProtocolError("event, run, task, digest, and occurrence identifiers are required")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise ProtocolError("positive sequence is required")
        _validate_payload(self.kind, self.payload)
        try:
            datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProtocolError("occurred_at must be ISO-8601") from exc

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "version", "event_id", "run_id", "task_id", "candidate_digest",
            "sequence", "kind", "occurred_at", "payload",
        )}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Envelope":
        expected = {"version", "event_id", "run_id", "task_id", "candidate_digest", "sequence", "kind", "occurred_at", "payload"}
        if set(value) != expected:
            raise ProtocolError("lifecycle envelope fields are invalid")
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise ProtocolError("malformed lifecycle envelope") from exc


class StreamGuard:
    """Deduplicates ordered events by run/task/digest stream."""

    def __init__(self) -> None:
        self._event_ids: set[str] = set()
        self._next: dict[tuple[str, str, str], int] = {}
        self._cancelled: set[tuple[str, str, str]] = set()

    @staticmethod
    def _stream(event: Envelope) -> tuple[str, str, str]:
        return event.run_id, event.task_id, event.candidate_digest

    def cancel(self, event: Envelope) -> None:
        self._cancelled.add(self._stream(event))

    def accept(self, event: Envelope) -> bool:
        stream = self._stream(event)
        if stream in self._cancelled:
            raise Cancelled("stream is cancelled")
        if event.event_id in self._event_ids:
            return False
        expected = self._next.get(stream, 1)
        if event.sequence != expected:
            raise SequenceGap(f"sequence gap: expected {expected}, received {event.sequence}")
        self._event_ids.add(event.event_id)
        self._next[stream] = expected + 1
        return True

    def replay(self, events: Iterable[Envelope]) -> list[Envelope]:
        return [event for event in events if self.accept(event)]


class RetryQueue:
    """A private bounded JSON-lines queue replayed in original order."""

    def __init__(self, path: Path, *, maximum: int = 128) -> None:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self.path, self.maximum = Path(path), maximum
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            _private_regular_file(self.path)
        else:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    def append(self, event: Envelope) -> None:
        entries = self.read()
        if any(queued.event_id == event.event_id for queued in entries):
            return
        if len(entries) >= self.maximum:
            raise ProtocolError("retry queue is full; refusing to discard ordered lifecycle events")
        entries.append(event)
        self._replace(entries)

    def read(self) -> list[Envelope]:
        _private_regular_file(self.path)
        try:
            return [Envelope.from_dict(json.loads(line)) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError("retry queue is corrupt") from exc

    def drain(self, send: Callable[[Envelope], None]) -> None:
        events = self.read()
        for index, event in enumerate(events):
            send(event)
            self._replace(events[index + 1:])

    def _replace(self, events: list[Envelope]) -> None:
        _private_regular_file(self.path)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        data = "".join(json.dumps(item.as_dict(), separators=(",", ":")) + "\n" for item in events)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


class UnixSocketClient:
    """Authenticated local transport which accepts only a matching ACK."""

    def __init__(self, *, socket_path: Path, token_path: Path, queue: RetryQueue | None = None) -> None:
        self.socket_path, self.token_path, self.queue = Path(socket_path), Path(token_path), queue
        _private_regular_file(self.token_path)

    def send(self, event: Envelope) -> None:
        _private_regular_file(self.token_path)
        try:
            response = self._request({"event": event.as_dict()})
            self._validate_ack(event, response)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ProtocolError) as exc:
            if isinstance(exc, (SequenceGap, Cancelled)):
                raise
            if self.queue is not None:
                self.queue.append(event)
            raise ProtocolError("local lifecycle socket delivery was not acknowledged; event queued") from exc

    def replay(self) -> None:
        if self.queue is not None:
            self.queue.drain(self.send)

    def expected_sequence(self, stream: tuple[str, str, str]) -> int:
        response = self._request({"handshake": list(stream)})
        if set(response) != {"status", "expected_sequence"} or response["status"] != "ready":
            raise ProtocolError("lifecycle sequence handshake was rejected")
        expected = response["expected_sequence"]
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ProtocolError("lifecycle sequence handshake was invalid")
        return expected

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        _private_regular_file(self.token_path)
        token = self.token_path.read_bytes().strip()
        if not token:
            raise ProtocolError("socket token is empty")
        request = json.dumps({**body, "token": token.decode("utf-8")}, separators=(",", ":")).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(os.fspath(self.socket_path))
            client.sendall(request)
            return self._receive_line(client)

    def _acknowledge_cancel(self, event: Envelope) -> None:
        response = self._request({"cancel_ack": [event.run_id, event.task_id, event.candidate_digest]})
        if response != {"status": "cancelled"}:
            raise ProtocolError("lifecycle cancellation acknowledgement was rejected")

    @staticmethod
    def _receive_line(client: socket.socket) -> dict[str, Any]:
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_SOCKET_FRAME_BYTES:
                raise ProtocolError("lifecycle socket frame exceeds maximum size")
        if len(data) > MAX_SOCKET_FRAME_BYTES:
            raise ProtocolError("lifecycle socket frame exceeds maximum size")
        return json.loads(data.decode("utf-8"))

    def _validate_ack(self, event: Envelope, ack: dict[str, Any]) -> None:
        if set(ack) != {"event_id", "status", "expected_sequence"} or ack["event_id"] != event.event_id:
            raise ProtocolError("invalid lifecycle acknowledgement")
        if not isinstance(ack["expected_sequence"], int):
            raise ProtocolError("invalid lifecycle acknowledgement")
        if ack["status"] == "accepted" and ack["expected_sequence"] == event.sequence + 1:
            return
        if ack["status"] == "duplicate" and ack["expected_sequence"] > event.sequence:
            return
        if ack["status"] == "gap":
            raise SequenceGap(f"controller expects sequence {ack['expected_sequence']}")
        if ack["status"] == "cancel_requested":
            self._acknowledge_cancel(event)
            raise Cancelled("controller cancellation was acknowledged at a safe boundary")
        if ack["status"] == "cancelled":
            raise Cancelled("controller cancelled lifecycle stream")
        raise ProtocolError("lifecycle acknowledgement rejected")


class ProducerHighWater:
    """Durably reserve producer sequence numbers before delivery is attempted."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            _private_regular_file(self.path)
        else:
            self._replace(0)

    def read(self) -> int:
        _private_regular_file(self.path)
        try:
            value = int(self.path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError("producer sequence high-water file is corrupt") from exc
        if value < 0:
            raise ProtocolError("producer sequence high-water file is corrupt")
        return value

    def reserve(self) -> int:
        value = self.read() + 1
        self._replace(value)
        return value

    def advance_to(self, value: int) -> int:
        if value < 0:
            raise ProtocolError("invalid producer sequence high-water")
        current = self.read()
        if value > current:
            self._replace(value)
            return value
        return current

    def _replace(self, value: int) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="ascii") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


def occurred_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
