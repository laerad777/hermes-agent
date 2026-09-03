"""Durable, fail-closed continuity state for generic Hermes lifecycle events.

Event de-duplication is at-most-once only within the configured retention window:
expired records and their identifiers are deliberately compacted.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

_MAX_IDS = 64
_MAX_RETRIES = 3
_DEFAULT_TTL_SECONDS = 60 * 60
_DEFAULT_TOMBSTONE_TTL_SECONDS = 10 * 60
_EXACT_RECALL_FIELDS = (
    "profile", "platform", "chat_id", "chat_type", "thread_id", "parent_chat_id",
    "previous_session_id", "current_session_id",
)


@dataclass
class ContinuityRecord:
    route: str
    previous_session_id: str
    current_session_id: str
    state: str = "prior_recorded"
    accepted_generation_high_water: int = 0
    event_ids: list[str] = field(default_factory=list)
    delivery_ids: list[str] = field(default_factory=list)
    active_turn_id: str = ""
    retries: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0


class ContinuityStore:
    """Owns bounded, fail-closed continuity state for one exact session edge."""

    def __init__(
        self,
        state_path: Path,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        tombstone_ttl_seconds: float = _DEFAULT_TOMBSTONE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0 or tombstone_ttl_seconds <= 0:
            raise ValueError("continuity TTLs must be positive")
        self.state_path = state_path
        self.records: dict[str, ContinuityRecord] = {}
        self.tombstones: dict[str, float] = {}
        self.available = True
        self.ttl_seconds = ttl_seconds
        self.tombstone_ttl_seconds = tombstone_ttl_seconds
        self._clock = clock
        self._load()

    @staticmethod
    def route_key(event: dict[str, Any]) -> str:
        fields = ("profile", "platform", "chat_id", "chat_type", "thread_id", "parent_chat_id")
        return "\x1f".join(str(event.get(field) or "") for field in fields)

    @staticmethod
    def previous_session_id(event: dict[str, Any]) -> str:
        return str(event.get("lineage_parent_session_id") or event.get("previous_session_id") or event.get("old_session_id") or "")

    @staticmethod
    def current_session_id(event: dict[str, Any]) -> str:
        return str(event.get("session_id") or event.get("new_session_id") or "")

    @classmethod
    def _key(cls, route: str, previous_session_id: str, current_session_id: str) -> str:
        return "\x1e".join((route, previous_session_id, current_session_id))

    def _load(self) -> None:
        try:
            if not self.state_path.exists():
                return
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if raw.get("version") != 2 or not isinstance(raw.get("records"), dict) or not isinstance(raw.get("tombstones"), dict):
                raise ValueError("invalid continuity state")
            self.records = {key: ContinuityRecord(**value) for key, value in raw["records"].items()}
            self.tombstones = {str(key): float(value) for key, value in raw["tombstones"].items()}
            if self._compact(self._clock()):
                self._save()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.available = False
            self.records = {}
            self.tombstones = {}

    def _compact(self, now: float) -> bool:
        changed = False
        for key, record in list(self.records.items()):
            if record.expires_at <= now:
                del self.records[key]
                self.tombstones[key] = now + self.tombstone_ttl_seconds
                changed = True
        for key, expires_at in list(self.tombstones.items()):
            if expires_at <= now:
                del self.tombstones[key]
                changed = True
        return changed

    def _save(self) -> bool:
        if not self.available:
            return False
        try:
            now = self._clock()
            self._compact(now)
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": 2, "records": {k: asdict(v) for k, v in self.records.items()}, "tombstones": self.tombstones},
                sort_keys=True,
            )
            fd, temporary = tempfile.mkstemp(prefix=".continuity-", dir=self.state_path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.state_path)
                os.chmod(self.state_path, 0o600)
                directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                if json.loads(self.state_path.read_text(encoding="utf-8")) != json.loads(payload):
                    raise OSError("continuity state readback mismatch")
                return True
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            self.available = False
            return False

    @staticmethod
    def _remember(items: list[str], value: str) -> None:
        if value and value not in items:
            items.append(value)
            del items[:-_MAX_IDS]

    def _identity(self, event: dict[str, Any]) -> tuple[str, str, str] | None:
        profile = str(event.get("profile") or "")
        route_profile = event.get("route_profile")
        if route_profile is not None and str(route_profile) != profile:
            return None
        route = self.route_key(event)
        previous = self.previous_session_id(event)
        current = self.current_session_id(event)
        if not route or not previous or not current:
            return None
        return route, previous, current

    def record_boundary(self, event: dict[str, Any]) -> None:
        identity = self._identity(event)
        if identity is None or not self.available:
            return
        route, previous, current = identity
        key = self._key(route, previous, current)
        now = self._clock()
        self._compact(now)
        if key in self.tombstones:
            return
        generation = int(event.get("run_generation") or 0)
        existing = self.records.get(key)
        if existing and generation < existing.accepted_generation_high_water:
            return
        event_id = str(event.get("event_id") or "")
        if existing and event_id in existing.event_ids:
            return
        record = existing or ContinuityRecord(route=route, previous_session_id=previous, current_session_id=current, created_at=now)
        record.accepted_generation_high_water = max(record.accepted_generation_high_water, generation)
        record.state = "prior_recorded"
        record.updated_at = now
        record.expires_at = now + self.ttl_seconds
        self._remember(record.event_ids, event_id)
        self.records[key] = record
        self._save()

    def exact_recall(self, event: dict[str, Any]) -> bool:
        """Accept only a lossless route/profile and previous-to-current session match."""
        identity = self._identity(event)
        if identity is None or not self.available:
            return False
        route, previous, current = identity
        key = self._key(route, previous, current)
        now = self._clock()
        if self._compact(now):
            self._save()
        record = self.records.get(key)
        return bool(
            record
            and record.route == route
            and record.previous_session_id == previous
            and record.current_session_id == current
            and record.state in {"recall_required", "clarification_pending"}
        )

    def exact_recall_anchor(self, anchor: dict[str, Any]) -> bool:
        """Validate and resolve the tool API's complete, canonical session anchor."""
        if set(anchor) != set(_EXACT_RECALL_FIELDS):
            return False
        if any(not isinstance(anchor[field], str) or not anchor[field] for field in _EXACT_RECALL_FIELDS):
            return False
        event = {
            "profile": anchor["profile"],
            "platform": anchor["platform"],
            "chat_id": anchor["chat_id"],
            "chat_type": anchor["chat_type"],
            "thread_id": anchor["thread_id"],
            "parent_chat_id": anchor["parent_chat_id"],
            "previous_session_id": anchor["previous_session_id"],
            "new_session_id": anchor["current_session_id"],
            "route_profile": anchor["profile"],
        }
        return self.exact_recall(event)

    def apply_origin(self, event: dict[str, Any]) -> str | None:
        # A missing lineage is an ordinary turn, regardless of origin kind.
        # Fail closed only when an explicit old→new lineage is present but its
        # persisted record is missing or corrupt.
        if not event.get("lineage_parent_session_id"):
            return None
        identity = self._identity(event)
        if identity is None:
            return self._unavailable("", "", "", int(event.get("run_generation") or 0), event)
        route, previous, current = identity
        key = self._key(route, previous, current)
        generation = int(event.get("run_generation") or 0)
        now = self._clock()
        if self._compact(now):
            self._save()
        record = self.records.get(key)
        if not self.available or record is None:
            return self._unavailable(route, previous, current, generation, event)
        event_id = str(event.get("event_id") or "")
        if event_id in record.event_ids or generation < record.accepted_generation_high_water:
            return None
        self._remember(record.event_ids, event_id)
        record.active_turn_id = str(event.get("turn_id") or "")
        record.accepted_generation_high_water = max(record.accepted_generation_high_water, generation)
        record.updated_at = now
        record.expires_at = now + self.ttl_seconds
        if record.state == "prior_recorded":
            record.state = "recall_required"
        self._save()
        return self._instruction(record) if record.state in {"recall_required", "clarification_pending", "unavailable"} else None

    def apply_delivery(self, event: dict[str, Any]) -> None:
        identity = self._identity(event)
        if identity is None or not self.available:
            return
        route, previous, current = identity
        key = self._key(route, previous, current)
        now = self._clock()
        if self._compact(now):
            self._save()
        record = self.records.get(key)
        if record is None:
            return
        generation = int(event.get("run_generation") or 0)
        delivery_id = str(event.get("delivery_id") or "")
        if not delivery_id or (record.active_turn_id and str(event.get("turn_id") or "") != record.active_turn_id) or delivery_id in record.delivery_ids or generation < record.accepted_generation_high_water:
            return
        if record.state not in {"recall_required", "clarification_pending"}:
            return
        self._remember(record.delivery_ids, delivery_id)
        record.accepted_generation_high_water = max(record.accepted_generation_high_water, generation)
        record.updated_at = now
        record.expires_at = now + self.ttl_seconds
        if event.get("success") is True and event.get("certainty") == "certain":
            record.state = "delivered"
        else:
            record.retries += 1
            record.state = "clarification_pending" if record.retries >= _MAX_RETRIES else "recall_required"
        self._save()

    def _unavailable(self, route: str, previous: str, current: str, generation: int, event: dict[str, Any]) -> str:
        if self.available and route and previous and current:
            now = self._clock()
            key = self._key(route, previous, current)
            if key not in self.tombstones:
                record = ContinuityRecord(route=route, previous_session_id=previous, current_session_id=current, state="unavailable", accepted_generation_high_water=generation, created_at=now, updated_at=now, expires_at=now + self.ttl_seconds)
                self._remember(record.event_ids, str(event.get("event_id") or ""))
                self.records[key] = record
                self._save()
        return "[Continuity unavailable: ask the user to clarify the prior task; do not search or infer prior session history.]"

    @staticmethod
    def _instruction(record: ContinuityRecord) -> str:
        if record.state == "unavailable":
            return "[Continuity unavailable: ask the user to clarify the prior task; do not search or infer prior session history.]"
        return f"[Continuity recall required: use only the exact prior session {record.previous_session_id!r}; ask for clarification when it is unavailable.]"
