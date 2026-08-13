"""Persistent, display-only Discord native interaction support."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.discord_native import (
    DiscordNativePayloadV1,
    discord_native_to_mapping,
    validate_discord_native_payload,
)
from plugins.platforms.discord.product_details import (
    SecureStoreUnavailable,
    _PosixSecureStorePrimitives,
    secure_store_capability,
)

_COMPONENT_KINDS = frozenset({
    "modal", "string_select", "user_select", "role_select",
    "channel_select", "mentionable_select",
})
_DM_KINDS = frozenset({"modal", "string_select", "user_select"})
_EDIT_ROUTES = frozenset({"nonstream_edit", "stream_edit", "overflow_final"})
_MARKDOWN = re.compile(r"([\\*_~|>\[\]()#!])")
_BIDI = frozenset({
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
})


@dataclass(frozen=True, slots=True)
class NativeInteractionDelivery:
    logical_id: str
    delivery_id: str
    custom_ids: tuple[str, ...]
    expires_at: int


def native_route_allows(
    kind: str,
    *,
    route: str,
    chat_type: str,
    owner_user_id: str | None,
) -> bool:
    """Apply the fixed route matrix before allocating persistent state."""
    if chat_type == "forum" or route == "standalone":
        return False
    if kind == "poll":
        return route in {"normal", "cron_live"}
    if kind not in _COMPONENT_KINDS or owner_user_id is None:
        return False
    if chat_type == "dm" and kind not in _DM_KINDS:
        return False
    return route in {"normal", "cron_live"} | _EDIT_ROUTES


def sanitize_ephemeral_items(items: list[Any], *, limit: int = 1900) -> str:
    """Render selected/submitted values without mentions or Markdown control."""
    rendered: list[str] = []
    used = 0
    for raw in items:
        value = unicodedata.normalize("NFKC", str(raw))
        value = "".join(
            char for char in value
            if char not in _BIDI and char != "`" and not unicodedata.category(char).startswith("C")
        )
        value = _MARKDOWN.sub(r"\\\1", value.replace("@", "＠"))
        line = f"- {value}"
        if used + len(line.encode("utf-16-le")) // 2 + 1 > limit:
            break
        rendered.append(line)
        used += len(line.encode("utf-16-le")) // 2 + 1
    return "\n".join(rendered) if rendered else "No values selected."


def build_native_view(discord: Any, adapter: Any, delivery: Any, envelope: DiscordNativePayloadV1) -> Any:
    """Build a persistent display-only View for one validated component spec."""
    custom_id = delivery.custom_ids[0]
    spec = envelope.payload

    async def deny(interaction: Any) -> None:
        sender = interaction.response if not interaction.response.is_done() else interaction.followup
        await sender.send_message(
            "This interaction is unavailable.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def resolve(interaction: Any) -> DiscordNativePayloadV1 | None:
        store = getattr(adapter, "_native_interaction_store", None)
        return store.resolve(
            custom_id,
            owner_user_id=str(getattr(getattr(interaction, "user", None), "id", "")),
            guild_id=str(getattr(getattr(interaction, "guild", None), "id", "")) or None,
            channel_id=str(getattr(getattr(interaction, "channel", None), "id", "")),
            message_id=str(getattr(getattr(interaction, "message", None), "id", "")),
        ) if store is not None else None

    def interaction_binding(interaction: Any) -> tuple[str, str | None, str, str]:
        return (
            str(getattr(getattr(interaction, "user", None), "id", "")),
            str(getattr(getattr(interaction, "guild", None), "id", "")) or None,
            str(getattr(getattr(interaction, "channel", None), "id", "")),
            str(getattr(getattr(interaction, "message", None), "id", "")),
        )

    class NativeModal(discord.ui.Modal):
        def __init__(self, *, original_binding: tuple[str, str | None, str, str]) -> None:
            super().__init__(title=spec["title"])
            self.original_binding = original_binding
            self.inputs = []
            for item in spec["inputs"]:
                text_input = discord.ui.TextInput(
                    custom_id=item["id"],
                    label=item["label"],
                    style=(
                        discord.TextStyle.paragraph
                        if item["style"] == "paragraph"
                        else discord.TextStyle.short
                    ),
                    required=item["required"],
                    min_length=item["min_length"],
                    max_length=item["max_length"],
                    placeholder=item.get("placeholder"),
                )
                self.inputs.append(text_input)
                self.add_item(text_input)

        async def on_submit(self, interaction: Any) -> None:
            owner_user_id, guild_id, channel_id, _ = interaction_binding(interaction)
            _, original_guild_id, original_channel_id, original_message_id = self.original_binding
            store = getattr(adapter, "_native_interaction_store", None)
            resolved = store.resolve(
                custom_id,
                owner_user_id=owner_user_id,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=original_message_id,
            ) if store is not None else None
            if (
                resolved is None
                or guild_id != original_guild_id
                or channel_id != original_channel_id
            ):
                await deny(interaction)
                return
            await interaction.response.send_message(
                sanitize_ephemeral_items([item.value for item in self.inputs]),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    class ModalButton(discord.ui.Button):
        async def callback(self, interaction: Any) -> None:
            if resolve(interaction) is None:
                await deny(interaction)
                return
            await interaction.response.send_modal(NativeModal(
                original_binding=interaction_binding(interaction),
            ))

    class DisplaySelectMixin:
        async def callback(self, interaction: Any) -> None:
            if resolve(interaction) is None:
                await deny(interaction)
                return
            values = []
            for item in self.values:
                identifier = getattr(item, "id", item)
                label = getattr(item, "display_name", None) or getattr(item, "name", None)
                values.append(f"{label} ({identifier})" if label else str(identifier))
            await interaction.response.send_message(
                sanitize_ephemeral_items(values),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    view = discord.ui.View(timeout=None)
    if envelope.kind == "modal":
        view.add_item(ModalButton(
            label=spec["trigger_label"],
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id,
        ))
        return view
    kwargs = {
        "custom_id": custom_id,
        "placeholder": spec.get("placeholder"),
        "min_values": spec["min_values"],
        "max_values": spec["max_values"],
        "disabled": spec["disabled"],
    }
    if envelope.kind == "string_select":
        def option_emoji(item: dict[str, Any]) -> Any:
            emoji = item.get("emoji")
            if emoji is None:
                return None
            if "id" in emoji:
                return discord.PartialEmoji(id=int(emoji["id"]))
            return discord.PartialEmoji(name=emoji["name"])

        options = [
            discord.SelectOption(
                label=item["label"], value=item["value"],
                description=item.get("description"), default=item["default"],
                emoji=option_emoji(item),
            )
            for item in spec["options"]
        ]
        select_type = type("NativeStringSelect", (DisplaySelectMixin, discord.ui.Select), {})
        view.add_item(select_type(options=options, **kwargs))
    else:
        classes = {
            "user_select": discord.ui.UserSelect,
            "role_select": discord.ui.RoleSelect,
            "channel_select": discord.ui.ChannelSelect,
            "mentionable_select": discord.ui.MentionableSelect,
        }
        if envelope.kind == "channel_select" and "channel_types" in spec:
            kwargs["channel_types"] = [discord.ChannelType(value) for value in spec["channel_types"]]
        select_type = type(f"Native{envelope.kind.title()}", (DisplaySelectMixin, classes[envelope.kind]), {})
        view.add_item(select_type(**kwargs))
    return view


class DiscordNativeInteractionStore:
    """Small spec-only store; callback values are never accepted by this API."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._directory_fd: int | None = None
        self.capability = secure_store_capability()
        if not self.capability.available:
            raise SecureStoreUnavailable(self.capability.reason)
        self._secure = _PosixSecureStorePrimitives(self.capability)
        self._directory_fd = self._secure.open_directory(self.state_dir, create=True)
        self._euid = self._secure._euid
        self.key_path = self.state_dir / "signing-key-v1"
        self.db_path = self.state_dir / "native-v1.sqlite3"
        self.recovery_path = self.state_dir / ".native-v1.sqlite3.recovery"
        try:
            os.stat(
                self.recovery_path.name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SecureStoreUnavailable("native_snapshot_recovery_required")
        self.key = self._load_key()
        self.connection = self._connect_database()
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS deliveries(
                logical_id TEXT UNIQUE NOT NULL,
                delivery_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS poll_deliveries(
                logical_id TEXT PRIMARY KEY,
                target_hash TEXT NOT NULL,
                obligation_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                message_id TEXT,
                error_class TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(target_hash, obligation_hash)
            )
        """)
        poll_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(poll_deliveries)")
        }
        if "target_hash" not in poll_columns:
            # The feature-local v1 ledger had global obligation uniqueness and
            # cannot safely infer historical target identity. Recreate only this
            # no-vote Poll claim table; component/product-detail state is intact.
            self.connection.execute("DROP TABLE poll_deliveries")
            self.connection.execute("""
                CREATE TABLE poll_deliveries(
                    logical_id TEXT PRIMARY KEY,
                    target_hash TEXT NOT NULL,
                    obligation_hash TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    message_id TEXT,
                    error_class TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(target_hash, obligation_hash)
                )
            """)
        self._commit()
        self.maintain()

    def _connect_database(self) -> sqlite3.Connection:
        """Open a proc-fd database or a verified macOS snapshot."""
        if self._directory_fd is None:
            raise SecureStoreUnavailable("native interaction state store is closed")
        if self.capability.backend == "darwin-snapshot":
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                fd = os.open(
                    self.db_path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._directory_fd,
                )
            except FileNotFoundError:
                return connection
            try:
                info = self._secure.validate_owned_regular_fd(
                    fd, label="Discord native interaction snapshot",
                )
                if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8 * 1024 * 1024:
                    raise OSError("native interaction snapshot has invalid metadata")
                data = bytearray()
                while len(data) < info.st_size:
                    chunk = os.read(fd, min(1024 * 1024, info.st_size - len(data)))
                    if not chunk:
                        raise OSError("native interaction snapshot was truncated")
                    data.extend(chunk)
                if data:
                    connection.deserialize(bytes(data))
                return connection
            except BaseException:
                connection.close()
                raise
            finally:
                os.close(fd)
        if self.capability.backend != "linux-procfd":
            raise SecureStoreUnavailable("secure native interaction backend unavailable")
        anchored = f"/proc/self/fd/{self._directory_fd}/{self.db_path.name}"
        connection = sqlite3.connect(f"file:{anchored}?nofollow=1", uri=True)
        file_info = os.stat(
            self.db_path.name,
            dir_fd=self._directory_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != self._euid:
            connection.close()
            raise OSError("native interaction database has invalid metadata")
        os.chmod(
            self.db_path.name, 0o600,
            dir_fd=self._directory_fd, follow_symlinks=False,
        )
        return connection

    def _persist_snapshot_bytes(self, data: bytes) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("native interaction state store is closed")
        temporary_name = f".{self.db_path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            self._secure.validate_owned_regular_fd(
                fd, label="Discord native interaction temporary snapshot",
            )
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("failed to write native interaction snapshot")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        backup_name = f".{self.db_path.name}.rollback"
        recovery_name = self.recovery_path.name
        had_snapshot = False
        published = False
        try:
            marker_fd = os.open(
                recovery_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._directory_fd,
            )
            try:
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(self._directory_fd)
            try:
                os.rename(
                    self.db_path.name, backup_name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
            except FileNotFoundError:
                pass
            else:
                had_snapshot = True
            os.replace(
                temporary_name, self.db_path.name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            published = True
            os.fsync(self._directory_fd)
            if had_snapshot:
                os.unlink(backup_name, dir_fd=self._directory_fd)
            os.unlink(recovery_name, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except BaseException as publication_error:
            try:
                if published:
                    if had_snapshot:
                        os.replace(
                            backup_name, self.db_path.name,
                            src_dir_fd=self._directory_fd,
                            dst_dir_fd=self._directory_fd,
                        )
                    else:
                        os.unlink(self.db_path.name, dir_fd=self._directory_fd)
                elif had_snapshot:
                    os.replace(
                        backup_name, self.db_path.name,
                        src_dir_fd=self._directory_fd,
                        dst_dir_fd=self._directory_fd,
                    )
                os.fsync(self._directory_fd)
                os.unlink(recovery_name, dir_fd=self._directory_fd)
                os.fsync(self._directory_fd)
            except BaseException as rollback_error:
                raise SecureStoreUnavailable("native_snapshot_recovery_required") from ExceptionGroup(
                    "native snapshot publication and rollback failed",
                    [publication_error, rollback_error],
                )
            raise
        finally:
            try:
                os.unlink(temporary_name, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass

    def _commit(self) -> None:
        if self.capability.backend != "darwin-snapshot":
            self.connection.commit()
            return
        try:
            # Serialize and durably publish while the transaction is still
            # rollback-capable.  Only then make the in-memory state visible.
            self._persist_snapshot_bytes(self.connection.serialize())
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _load_key(self) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if self._directory_fd is None:
            raise SecureStoreUnavailable("native interaction state store is closed")
        try:
            fd = os.open(self.key_path.name, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            fd = os.open(
                self.key_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._directory_fd,
            )
            try:
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            return key
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("signing key is not a regular file")
            if self._euid is not None and info.st_uid != self._euid:
                raise OSError("signing key has an unexpected owner")
            key = os.read(fd, 33)
            if len(key) != 32:
                raise OSError("signing key has an invalid length")
            return key
        finally:
            os.close(fd)

    def _custom_id(self, delivery_id: str, expires_at: int) -> str:
        body = f"hni1.{delivery_id}.{expires_at}"
        signature = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{body}.{signature}"

    def prepare_delivery(
        self,
        *,
        logical_id: str,
        envelope: DiscordNativePayloadV1,
        owner_user_id: str | None,
        guild_id: str | None,
        channel_id: str,
        now: float | None = None,
    ) -> NativeInteractionDelivery:
        if envelope.kind not in _COMPONENT_KINDS or not owner_user_id:
            raise ValueError("component owner is required")
        fresh = validate_discord_native_payload(envelope.kind, envelope.payload)
        current = int(time.time() if now is None else now)
        expires_at = current + int(fresh.payload["ttl_seconds"])
        existing = self.connection.execute(
            "SELECT delivery_id, expires_at FROM deliveries WHERE logical_id=?",
            (str(logical_id),),
        ).fetchone()
        if existing:
            return NativeInteractionDelivery(
                str(logical_id), existing[0], (self._custom_id(existing[0], existing[1]),), existing[1]
            )
        delivery_id = secrets.token_urlsafe(12)
        mapping = discord_native_to_mapping(fresh)
        self.connection.execute(
            "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(logical_id), delivery_id, fresh.kind,
                json.dumps(mapping["payload"], sort_keys=True, separators=(",", ":")),
                str(owner_user_id), str(guild_id) if guild_id is not None else None,
                str(channel_id), None, expires_at, "pending",
            ),
        )
        self._commit()
        return NativeInteractionDelivery(
            str(logical_id), delivery_id, (self._custom_id(delivery_id, expires_at),), expires_at
        )

    def bind_delivery(self, delivery: NativeInteractionDelivery, message_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE deliveries SET message_id=?, state='bound' "
            "WHERE delivery_id=? AND state='pending' AND expires_at>?",
            (str(message_id), delivery.delivery_id, int(time.time())),
        )
        self._commit()
        return cursor.rowcount == 1

    def resolve(
        self,
        custom_id: str,
        *,
        owner_user_id: str,
        guild_id: str | None,
        channel_id: str,
        message_id: str,
        now: float | None = None,
    ) -> DiscordNativePayloadV1 | None:
        try:
            prefix, delivery_id, expires, signature = custom_id.split(".")
            if prefix != "hni1":
                return None
            expires_at = int(expires)
            body = f"{prefix}.{delivery_id}.{expires_at}"
            expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:24]
            if not hmac.compare_digest(signature, expected):
                return None
        except (TypeError, ValueError):
            return None
        current = int(time.time() if now is None else now)
        if expires_at <= current:
            return None
        row = self.connection.execute(
            "SELECT kind,spec_json,owner_user_id,guild_id,channel_id,message_id,expires_at,state "
            "FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        if not row:
            return None
        kind, spec_json, owner, guild, channel, message, stored_expiry, state = row
        if (
            state != "bound" or stored_expiry != expires_at or owner != str(owner_user_id)
            or guild != (str(guild_id) if guild_id is not None else None)
            or channel != str(channel_id) or message != str(message_id)
        ):
            return None
        return validate_discord_native_payload(kind, json.loads(spec_json))

    def restore_active_deliveries(self) -> list[tuple[NativeInteractionDelivery, DiscordNativePayloadV1, str]]:
        now = int(time.time())
        rows = self.connection.execute(
            "SELECT logical_id,delivery_id,kind,spec_json,expires_at,message_id "
            "FROM deliveries WHERE state='bound' AND expires_at>?",
            (now,),
        ).fetchall()
        return [
            (
                NativeInteractionDelivery(logical, delivery, (self._custom_id(delivery, expiry),), expiry),
                validate_discord_native_payload(kind, json.loads(spec)),
                message,
            )
            for logical, delivery, kind, spec, expiry, message in rows
        ]

    def discard_pending(self, delivery: NativeInteractionDelivery) -> None:
        self.connection.execute(
            "DELETE FROM deliveries WHERE delivery_id=? AND state='pending'", (delivery.delivery_id,)
        )
        self._commit()

    def discard_delivery(self, delivery: NativeInteractionDelivery) -> None:
        self.connection.execute(
            "DELETE FROM deliveries WHERE delivery_id=?", (delivery.delivery_id,)
        )
        self._commit()

    def claim_poll(
        self, logical_id: str, target_hash: str, obligation_hash: str, payload_hash: str,
        *, now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        existing = self.connection.execute(
            "SELECT payload_hash FROM poll_deliveries "
            "WHERE target_hash=? AND obligation_hash=?",
            (str(target_hash), str(obligation_hash)),
        ).fetchone()
        if existing is not None and existing[0] != str(payload_hash):
            raise ValueError("poll obligation payload mismatch")
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO poll_deliveries "
            "(logical_id,target_hash,obligation_hash,payload_hash,state,message_id,error_class,created_at,updated_at) "
            "VALUES(?,?,?,?,'claimed',NULL,NULL,?,?)",
            (
                str(logical_id), str(target_hash), str(obligation_hash),
                str(payload_hash), current, current,
            ),
        )
        self._commit()
        return cursor.rowcount == 1

    def finish_poll(
        self,
        logical_id: str,
        state: str,
        *,
        message_id: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if state not in {"sent", "definitely_not_sent", "unknown"}:
            raise ValueError("invalid poll terminal state")
        self.connection.execute(
            "UPDATE poll_deliveries SET state=?,message_id=?,error_class=?,updated_at=? "
            "WHERE logical_id=? AND state='claimed'",
            (state, message_id, error_class, time.time(), str(logical_id)),
        )
        self._commit()

    def poll_delivery(self, logical_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT logical_id,state,message_id,error_class FROM poll_deliveries "
            "WHERE logical_id=?",
            (str(logical_id),),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("logical_id", "state", "message_id", "error_class"), row))

    def maintain(self) -> None:
        self.connection.execute("DELETE FROM deliveries WHERE expires_at<=?", (int(time.time()),))
        self._commit()

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
            self.connection = None
        directory_fd = getattr(self, "_directory_fd", None)
        if directory_fd is not None:
            os.close(directory_fd)
            self._directory_fd = None
