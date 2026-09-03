"""Narrow producer helpers for the resync plugin's lifecycle observations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import stat
from typing import Any, Callable, Iterable, Protocol

from .protocol import Cancelled, Envelope, ProducerHighWater, ProtocolError, RetryQueue, UnixSocketClient, occurred_now


@dataclass(frozen=True)
class MailMessage:
    """The bounded mail metadata needed for duplicate-send preflight."""

    recipients: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[str, ...] = ()


class MailAdapter(Protocol):
    """Read-only synthetic or real mailbox view; this package never sends mail."""

    def messages(self, mailbox: str) -> Iterable[MailMessage] | None: ...


@dataclass(frozen=True)
class HimalayaPreflight:
    """Parsed, read-only duplicate-send procedure shipped as a plugin asset."""

    steps: tuple[str, ...]

    @classmethod
    def load(cls) -> "HimalayaPreflight":
        asset = _asset_path("himalaya_preflight.md")
        steps = tuple(
            line.split(". ", 1)[1] for line in asset.read_text(encoding="utf-8").splitlines()
            if line[:1].isdigit() and ". " in line
        )
        if len(steps) != 5:
            raise ValueError("invalid Himalaya preflight asset")
        return cls(steps)

    def evaluate(self, draft: MailMessage, mail: MailAdapter) -> str:
        """Return a fail-closed preflight disposition without mutating a mailbox."""
        sent = mail.messages("Sent")
        outbox = mail.messages("Outbox")
        if sent is None or outbox is None:
            return "uncertain"
        if any(_same_message(draft, message) for message in sent):
            return "duplicate"
        if any(_same_message(draft, message) for message in outbox):
            return "outbox"
        return "sent"


def _same_message(left: MailMessage, right: MailMessage) -> bool:
    return (
        tuple(sorted(recipient.casefold().strip() for recipient in left.recipients))
        == tuple(sorted(recipient.casefold().strip() for recipient in right.recipients))
        and left.subject.casefold().strip() == right.subject.casefold().strip()
        and left.body == right.body
        and tuple(sorted(left.attachments)) == tuple(sorted(right.attachments))
    )


def _asset_path(name: str) -> Path:
    path = files("hermes_resync").joinpath("assets", name)
    if isinstance(path, Path) and path.is_file():
        return path
    source_asset = Path(__file__).parents[1] / "assets" / name
    if source_asset.is_file():
        return source_asset
    raise FileNotFoundError(f"packaged resync asset is unavailable: {name}")


def _validate_asset_manifest() -> None:
    manifest = json.loads(_asset_path("mode-manifest.json").read_text(encoding="utf-8"))
    for entry in manifest.get("assets", ()):
        path = _asset_path(entry["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"packaged resync asset digest mismatch: {path.name}")
        if stat.S_IMODE(path.stat().st_mode) != int(entry["mode"], 8):
            raise ValueError(f"packaged resync asset mode mismatch: {path.name}")


def register_assets(ctx: Any) -> None:
    """Expose bundled, read-only assets through the generic plugin skill API."""
    _validate_asset_manifest()
    ctx.register_skill("comfyui-setup", _asset_path("comfyui_setup.sh"), "ComfyUI workspace setup helper")
    ctx.register_skill("himalaya-preflight", _asset_path("himalaya_preflight.md"), "Himalaya duplicate-send preflight")


@dataclass
class LifecycleProducer:
    """Emit observer-safe events with durable, non-reusable sequence claims."""

    run_id: str
    task_id: str
    candidate_digest: str
    deliver: Callable[[Envelope], None]
    _sequence: int = 0
    _cancelled: bool = False
    _high_water: ProducerHighWater | None = None

    @classmethod
    def connect(
        cls, *, run_id: str, task_id: str, candidate_digest: str,
        socket_path: Path, token_path: Path,
    ) -> "LifecycleProducer":
        """Connect to a controller that has already claimed this producer stream."""
        identity = hashlib.sha256(f"{run_id}\0{task_id}\0{candidate_digest}".encode()).hexdigest()
        directory = Path(socket_path).parent
        queue = RetryQueue(directory / f".lifecycle-retry-{identity}")
        high_water = ProducerHighWater(directory / f".lifecycle-sequence-{identity}")
        client = UnixSocketClient(socket_path=socket_path, token_path=token_path, queue=queue)
        queued = queue.read()
        if any((event.run_id, event.task_id, event.candidate_digest) != (run_id, task_id, candidate_digest) for event in queued):
            raise ProtocolError("retry queue identity does not match lifecycle producer")
        expected = client.expected_sequence((run_id, task_id, candidate_digest))
        sequence = high_water.read()
        if any(event.sequence > sequence for event in queued):
            raise ProtocolError("retry queue exceeds producer sequence high-water")
        pending = [event.sequence for event in queued if event.sequence >= expected]
        if pending != list(range(expected, sequence + 1)):
            raise ProtocolError("producer sequence boundary has an unrecoverable lifecycle gap")
        client.replay()
        sequence = high_water.advance_to(client.expected_sequence((run_id, task_id, candidate_digest)) - 1)
        return cls(run_id, task_id, candidate_digest, client.send, _sequence=sequence, _high_water=high_water)

    def cancel(self) -> None:
        self._cancelled = True

    def progress(self, *, message: str, percent: int | None = None) -> Envelope:
        payload: dict[str, Any] = {"message": message}
        if percent is not None:
            if not 0 <= percent <= 100:
                raise ProtocolError("progress percent must be between 0 and 100")
            payload["percent"] = percent
        return self._emit("progress", payload)

    def artifact_declared(self, *, relative_path: str, sha256: str) -> Envelope:
        return self._emit("artifact_declared", {"relative_path": relative_path, "sha256": sha256})

    def _emit(self, kind: str, payload: dict[str, Any]) -> Envelope:
        if self._cancelled:
            raise Cancelled("producer is cancelled")
        self._sequence = self._high_water.reserve() if self._high_water is not None else self._sequence + 1
        event = Envelope(
            run_id=self.run_id, task_id=self.task_id, candidate_digest=self.candidate_digest,
            sequence=self._sequence, kind=kind, occurred_at=occurred_now(), payload=payload,
        )
        try:
            self.deliver(event)
        except Cancelled:
            self._cancelled = True
            raise
        except Exception:
            raise
        return event
