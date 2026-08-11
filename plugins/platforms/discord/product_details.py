"""Profile-local persistent state for Discord private product details."""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gateway.discord_product_details import (
    DiscordProductDetailItemV1,
    DiscordProductDetailsEnvelopeV1,
    discord_product_details_to_canonical_mapping,
)

KEY_ROTATION_SECONDS = 30 * 24 * 60 * 60
MAX_ACTIVE_DELIVERIES = 500
MAX_ACTIVE_BODY_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
_LEGACY_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_LEGACY_STORE_NAMES = ("details-v1.sqlite3",) + tuple(
    "details-v1.sqlite3" + suffix for suffix in _LEGACY_SIDECAR_SUFFIXES
)


class SecureStoreUnavailable(OSError):
    """Raised when this runtime cannot enforce the secure-store contract."""


@dataclass(frozen=True, slots=True)
class SecureStoreCapability:
    available: bool
    backend: str
    reason: str


def secure_store_capability(
    *, platform: str | None = None, win32_api: object | None = None,
) -> SecureStoreCapability:
    """Describe the selected primitive set without probing user state paths."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        # SQLite's stdlib Windows VFS does not expose an OPEN_REPARSE_POINT
        # handle or a way to verify its owner/DACL on the same handle used by
        # SQLite.  pywin32 can secure ordinary files, but cannot close that
        # database-open race.  Keep an explicit Windows backend result so the
        # adapter can degrade to summary-only rather than crash or weaken the
        # guarantee silently.
        return SecureStoreCapability(
            False,
            "windows",
            "secure Windows SQLite non-reparse-point handle guarantee is unavailable",
        )

    required_dir_fd = (os.open, os.mkdir, os.unlink, os.rename, os.stat)
    posix_primitives = (
        hasattr(os, "geteuid")
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and all(function in os.supports_dir_fd for function in required_dir_fd)
    )
    if platform == "darwin":
        if (
            posix_primitives
            and hasattr(sqlite3.Connection, "serialize")
            and hasattr(sqlite3.Connection, "deserialize")
            and hasattr(fcntl, "flock")
        ):
            return SecureStoreCapability(
                True, "darwin-snapshot", "secure primitives available",
            )
        return SecureStoreCapability(
            False,
            "unsupported",
            "secure macOS SQLite snapshot primitives are unavailable",
        )

    if platform != "linux" or not Path("/proc/self/fd").is_dir():
        return SecureStoreCapability(
            False,
            "unsupported",
            "secure SQLite directory-fd path binding is unavailable",
        )

    # CPython exposes replace() with src/dst dir-fd support through the same
    # platform primitive reported for rename(); replace itself is not listed
    # in os.supports_dir_fd on POSIX.
    if not posix_primitives:
        return SecureStoreCapability(
            False,
            "unsupported",
            "secure POSIX dir-fd and owner primitives are unavailable",
        )
    return SecureStoreCapability(True, "linux-procfd", "secure primitives available")


class _PosixSecureStorePrimitives:
    def __init__(self, capability: SecureStoreCapability) -> None:
        self.capability = capability
        self._euid = os.geteuid()

    def open_directory(self, path: Path, *, create: bool) -> int:
        """Walk from root using same-directory handles; optionally create leaf."""
        absolute = Path(path).absolute()
        parts = absolute.parts
        directory_fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in parts[1:]:
                try:
                    next_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except NotADirectoryError as exc:
                    raise OSError(
                        f"symlink is not allowed in Discord details state path: {absolute}"
                    ) from exc
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(name, 0o700, dir_fd=directory_fd)
                    next_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                os.close(directory_fd)
                directory_fd = next_fd
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self._euid:
                raise OSError("Discord details state directory has an unexpected owner or type")
            if create:
                os.fchmod(directory_fd, 0o700)
            if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
                raise OSError("Discord details state directory must have mode 0700")
            return directory_fd
        except Exception:
            os.close(directory_fd)
            raise

    def validate_owned_regular_fd(self, fd: int, *, label: str) -> os.stat_result:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{label} is not a regular file")
        if info.st_uid != self._euid:
            raise OSError(f"{label} has an unexpected owner")
        return info

    def chmod_regular_at(self, directory_fd: int, name: str, *, missing_ok: bool = False) -> None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        try:
            self.validate_owned_regular_fd(fd, label=f"Discord details state file {name}")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class ProductDetailDelivery:
    logical_id: str
    delivery_id: str
    custom_ids: tuple[str, ...]
    expires_at: int


class DiscordProductDetailStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        capability: SecureStoreCapability | None = None,
    ) -> None:
        self._directory_fd: int | None = None
        self._writer_lock_fd: int | None = None
        self._snapshot_connection: sqlite3.Connection | None = None
        self._recovery_required = False
        self._transaction_lock = threading.RLock()
        self.capability = capability or secure_store_capability()
        if not self.capability.available:
            raise SecureStoreUnavailable(self.capability.reason)
        self._secure = _PosixSecureStorePrimitives(self.capability)
        self.state_dir = Path(state_dir)
        try:
            self._directory_fd = self._secure.open_directory(self.state_dir, create=True)
            directory_info = os.fstat(self._directory_fd)
            self._directory_identity = (directory_info.st_dev, directory_info.st_ino)
            self.key_path = self.state_dir / "signing-key-v1"
            self.db_path = self.state_dir / "details-v1.sqlite3"
            if self.capability.backend == "darwin-snapshot":
                self.db_path = self.state_dir / "details-v2-snapshot.sqlite3"
                self._reject_legacy_store()
                self._reject_recovery_required()
                self._acquire_writer_lock()
                self._reject_legacy_store()
                self._snapshot_connection = self._load_snapshot_connection()
            self.key = self._load_key()
            self._initialize()
            self.maintain()
        except BaseException:
            self.close()
            raise

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        """Release retained secure-store resources; safe to call repeatedly."""
        snapshot_connection = getattr(self, "_snapshot_connection", None)
        self._snapshot_connection = None
        if snapshot_connection is not None:
            try:
                snapshot_connection.close()
            except sqlite3.Error:
                pass
        writer_lock_fd = getattr(self, "_writer_lock_fd", None)
        self._writer_lock_fd = None
        if writer_lock_fd is not None:
            try:
                fcntl.flock(writer_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(writer_lock_fd)
            except OSError:
                pass
        directory_fd = getattr(self, "_directory_fd", None)
        self._directory_fd = None
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path).absolute()
        for component in (current, *current.parents):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise OSError(f"symlink is not allowed in Discord details state path: {component}")

    @staticmethod
    def _reject_symlink_file(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise OSError(f"symlink is not allowed for Discord details state file: {path}")
        if not stat.S_ISREG(mode):
            raise OSError(f"Discord details state file is not regular: {path}")

    def _open_state_dir(self) -> int:
        self._validate_state_dir_identity()
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        return os.dup(self._directory_fd)

    def _validate_state_dir_identity(self) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        held = os.fstat(self._directory_fd)
        if (held.st_dev, held.st_ino) != self._directory_identity:
            raise SecureStoreUnavailable("Discord details state directory identity changed")
        current_fd = self._secure.open_directory(self.state_dir, create=False)
        try:
            current = os.fstat(current_fd)
        finally:
            os.close(current_fd)
        if (current.st_dev, current.st_ino) != self._directory_identity:
            raise SecureStoreUnavailable("Discord details state directory identity changed")

    def _validate_key_fd(self, fd: int) -> os.stat_result:
        info = self._secure.validate_owned_regular_fd(
            fd, label="Discord product-details signing key",
        )
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("Discord product-details signing key must have mode 0600")
        return info

    def _reject_legacy_store(self) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        for name in _LEGACY_STORE_NAMES:
            try:
                os.stat(
                    name,
                    dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            raise SecureStoreUnavailable("legacy_store_present")

    def _reject_recovery_required(self) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        if self._recovery_required:
            raise SecureStoreUnavailable("recovery_required")
        try:
            os.stat(
                ".details-v2-snapshot.sqlite3.recovery",
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise SecureStoreUnavailable("recovery_required")

    def _acquire_writer_lock(self) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        fd = os.open(
            ".details-v2-snapshot.sqlite3.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            info = self._secure.validate_owned_regular_fd(
                fd, label="Discord product-details writer lock",
            )
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("Discord product-details writer lock must have mode 0600")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SecureStoreUnavailable(
                    "Discord product-details writer lock is already held"
                ) from exc
        except BaseException:
            os.close(fd)
            raise
        self._writer_lock_fd = fd

    def _load_snapshot_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self._directory_fd is None:
            conn.close()
            raise SecureStoreUnavailable("Discord details state store is closed")
        try:
            fd = os.open(
                self.db_path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._directory_fd,
            )
        except FileNotFoundError:
            return conn
        except OSError as exc:
            conn.close()
            if exc.errno == errno.ELOOP:
                raise OSError(
                    "symlink is not allowed for Discord product-details snapshot"
                ) from exc
            raise
        try:
            info = self._secure.validate_owned_regular_fd(
                fd, label="Discord product-details snapshot",
            )
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("Discord product-details snapshot must have mode 0600")
            if info.st_size <= 0 or info.st_size > MAX_SNAPSHOT_BYTES:
                raise OSError("Discord product-details snapshot has invalid size")
            chunks = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    raise OSError("Discord product-details snapshot was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            conn.deserialize(b"".join(chunks))
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Discord product-details snapshot is corrupt")
            return conn
        except BaseException:
            conn.close()
            raise
        finally:
            os.close(fd)

    def _persist_snapshot(self, conn: sqlite3.Connection) -> None:
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        self._reject_recovery_required()
        self._reject_legacy_store()
        data = conn.serialize()
        if not data or len(data) > MAX_SNAPSHOT_BYTES:
            raise OSError("Discord product-details snapshot has invalid size")
        temporary_name = f".{self.db_path.name}.{secrets.token_hex(8)}.tmp"
        backup_name = f".{self.db_path.name}.rollback"
        recovery_name = f".{self.db_path.name}.recovery"
        fd: int | None = None
        had_snapshot = False
        published = False
        cleanup_started = False
        try:
            self._reject_legacy_store()
            fd = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            self._secure.validate_owned_regular_fd(
                fd, label="Discord product-details temporary snapshot",
            )
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("failed to write Discord product-details snapshot")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            self._reject_legacy_store()
            marker_fd = os.open(
                recovery_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
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
                    self.db_path.name,
                    backup_name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
            except FileNotFoundError:
                pass
            else:
                had_snapshot = True
                os.fsync(self._directory_fd)
            self._reject_legacy_store()
            os.replace(
                temporary_name,
                self.db_path.name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            published = True
            self._reject_legacy_store()
            os.fsync(self._directory_fd)
            cleanup_started = True
            if had_snapshot:
                os.unlink(backup_name, dir_fd=self._directory_fd)
            os.unlink(recovery_name, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except Exception as publication_error:
            if cleanup_started:
                self._recovery_required = True
                try:
                    marker_fd = os.open(
                        recovery_name,
                        os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=self._directory_fd,
                    )
                    try:
                        os.fsync(marker_fd)
                    finally:
                        os.close(marker_fd)
                    os.fsync(self._directory_fd)
                except Exception as marker_error:
                    raise SecureStoreUnavailable("recovery_required") from ExceptionGroup(
                        "snapshot cleanup durability and recovery marker failed",
                        [publication_error, marker_error],
                    )
                raise SecureStoreUnavailable("recovery_required") from publication_error
            try:
                if published:
                    if had_snapshot:
                        os.replace(
                            backup_name,
                            self.db_path.name,
                            src_dir_fd=self._directory_fd,
                            dst_dir_fd=self._directory_fd,
                        )
                    else:
                        os.unlink(self.db_path.name, dir_fd=self._directory_fd)
                    os.fsync(self._directory_fd)
                elif had_snapshot:
                    os.replace(
                        backup_name,
                        self.db_path.name,
                        src_dir_fd=self._directory_fd,
                        dst_dir_fd=self._directory_fd,
                    )
                    os.fsync(self._directory_fd)
                try:
                    os.unlink(recovery_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass
                os.fsync(self._directory_fd)
            except Exception as rollback_error:
                self._recovery_required = True
                raise SecureStoreUnavailable("recovery_required") from ExceptionGroup(
                    "snapshot publication and rollback durability failed",
                    [publication_error, rollback_error],
                )
            raise
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary_name, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass

    def _load_key(self) -> bytes:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = self._open_state_dir()
        try:
            try:
                fd = os.open(
                    self.key_path.name,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                try:
                    fd = os.open(
                        self.key_path.name,
                        os.O_RDONLY | nofollow,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise OSError(
                            "symlink is not allowed for Discord details signing key"
                        ) from exc
                    raise
                created = False
            else:
                created = True
            with os.fdopen(fd, "r+b" if created else "rb") as file:
                self._validate_key_fd(file.fileno())
                if created:
                    file.write(secrets.token_bytes(32))
                    file.flush()
                    os.fsync(file.fileno())
                    file.seek(0)
                key = file.read()
        finally:
            os.close(directory_fd)
        if len(key) != 32:
            raise ValueError("invalid Discord product-details signing key")
        return key

    def _rotate_key_if_due(self, now: float) -> bool:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = self._open_state_dir()
        temporary_name = f".{self.key_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            key_fd = os.open(
                self.key_path.name,
                os.O_RDONLY | nofollow,
                dir_fd=directory_fd,
            )
            try:
                key_info = self._validate_key_fd(key_fd)
            finally:
                os.close(key_fd)
            if now - key_info.st_mtime < KEY_ROTATION_SECONDS:
                return False

            new_key = secrets.token_bytes(32)
            fd = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "wb") as file:
                self._validate_key_fd(file.fileno())
                file.write(new_key)
                file.flush()
                os.fsync(file.fileno())
            os.replace(
                temporary_name,
                self.key_path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            self.key = new_key
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
        return True

    def _connect(self) -> sqlite3.Connection:
        if getattr(getattr(self, "capability", None), "backend", None) == "darwin-snapshot":
            if self._snapshot_connection is None:
                raise SecureStoreUnavailable("Discord details state store is closed")
            return self._snapshot_connection
        self._validate_state_dir_identity()
        if self._directory_fd is None:
            raise SecureStoreUnavailable("Discord details state store is closed")
        # /proc/self/fd/<n> stays bound to the opened directory after rename,
        # so SQLite derives the main DB, WAL, and SHM under one verified root.
        anchored_path = f"/proc/self/fd/{self._directory_fd}/{self.db_path.name}"
        uri = f"file:{anchored_path}?nofollow=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """Commit or roll back the transaction, then always close its handle."""
        if getattr(getattr(self, "capability", None), "backend", None) == "darwin-snapshot":
            with self._transaction_lock:
                self._reject_recovery_required()
                self._reject_legacy_store()
                conn = self._connect()
                before = sqlite3.connect(":memory:", check_same_thread=False)
                before.row_factory = sqlite3.Row
                conn.backup(before)
                committed = False
                try:
                    with conn:
                        yield conn
                    committed = True
                    self._persist_snapshot(conn)
                except BaseException:
                    if committed:
                        # Publication is part of the mutation contract.  A
                        # committed in-memory change is not visible to callers
                        # unless its durable snapshot was published too.
                        self._snapshot_connection = before
                        before = None
                        conn.close()
                    raise
                finally:
                    if before is not None:
                        before.close()
            return
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            if self.capability.backend != "darwin-snapshot":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    logical_id TEXT PRIMARY KEY, delivery_id TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL, guild_id TEXT, channel_id TEXT NOT NULL,
                    message_id TEXT, owner_user_id TEXT, expires_at INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    delivery_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    label TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(delivery_id, item_id)
                );
            """)
        if self.capability.backend == "darwin-snapshot":
            return
        directory_fd = self._open_state_dir()
        try:
            self._secure.chmod_regular_at(directory_fd, self.db_path.name)
            for suffix in ("-wal", "-shm"):
                self._secure.chmod_regular_at(
                    directory_fd, self.db_path.name + suffix, missing_ok=True,
                )
        finally:
            os.close(directory_fd)

    @staticmethod
    def _purge_expired_in_transaction(conn: sqlite3.Connection, now: float) -> None:
        expired = conn.execute(
            "SELECT delivery_id FROM deliveries WHERE expires_at<=?", (now,)
        ).fetchall()
        if not expired:
            return
        conn.executemany(
            "DELETE FROM items WHERE delivery_id=?",
            ((row["delivery_id"],) for row in expired),
        )
        conn.execute("DELETE FROM deliveries WHERE expires_at<=?", (now,))

    def maintain(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rotated = self._rotate_key_if_due(now)
        with self._connection() as conn:
            if rotated:
                conn.execute("DELETE FROM items")
                conn.execute("DELETE FROM deliveries")
            else:
                self._purge_expired_in_transaction(conn, now)

    def purge_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._connection() as conn:
            self._purge_expired_in_transaction(conn, now)

    def _signature(self, value: str) -> str:
        return base64.urlsafe_b64encode(
            hmac.new(self.key, ("hermes-product-details-v1\0" + value).encode(), hashlib.sha256).digest()[:12]
        ).decode().rstrip("=")

    def _custom_id(self, delivery_id: str, item_id: str, expires_at: int) -> str:
        core = f"hpd:v1:{delivery_id}:{item_id}:{expires_at}"
        return f"{core}:{self._signature(core)}"

    def prepare_delivery(self, *, logical_id: str, envelope: DiscordProductDetailsEnvelopeV1,
                         guild_id: str | None, channel_id: str, owner_user_id: str | None,
                         now: float | None = None) -> ProductDetailDelivery:
        now = time.time() if now is None else now
        expires_at = int(now + envelope.ttl_seconds)
        canonical = json.dumps(
            discord_product_details_to_canonical_mapping(envelope),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        with self._connection() as conn:
            self._purge_expired_in_transaction(conn, now)
            row = conn.execute("SELECT * FROM deliveries WHERE logical_id=?", (logical_id,)).fetchone()
            if row:
                if row["payload_digest"] != digest or row["channel_id"] != channel_id:
                    raise ValueError("logical delivery identity mismatch")
                item_rows = conn.execute("SELECT item_id FROM items WHERE delivery_id=? ORDER BY rowid", (row["delivery_id"],)).fetchall()
                return ProductDetailDelivery(logical_id, row["delivery_id"], tuple(
                    self._custom_id(row["delivery_id"], item["item_id"], row["expires_at"]) for item in item_rows
                ), row["expires_at"])
            active_count = conn.execute(
                "SELECT count(*) FROM deliveries "
                "WHERE state IN ('pending','bound','uncertain') AND expires_at>?",
                (now,),
            ).fetchone()[0]
            active_bodies = conn.execute(
                "SELECT i.body FROM deliveries d JOIN items i ON i.delivery_id=d.delivery_id "
                "WHERE d.state IN ('pending','bound','uncertain') AND d.expires_at>?",
                (now,),
            ).fetchall()
            active_body_bytes = sum(
                len(row["body"].encode("utf-8")) for row in active_bodies
            )
            requested_body_bytes = sum(len(item.body.encode("utf-8")) for item in envelope.items)
            if (
                active_count >= MAX_ACTIVE_DELIVERIES
                or active_body_bytes + requested_body_bytes > MAX_ACTIVE_BODY_BYTES
            ):
                raise ValueError("Discord product-details capacity exceeded")
            delivery_id = secrets.token_urlsafe(12)
            conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?)", (
                logical_id, delivery_id, "pending", guild_id, channel_id, None,
                owner_user_id, expires_at, digest,
            ))
            ids = []
            for index, item in enumerate(envelope.items):
                item_id = str(index)
                conn.execute("INSERT INTO items VALUES (?,?,?,?,?)", (
                    delivery_id, item_id, item.label, item.title, item.body,
                ))
                ids.append(self._custom_id(delivery_id, item_id, expires_at))
        return ProductDetailDelivery(logical_id, delivery_id, tuple(ids), expires_at)

    def bind_delivery(self, delivery: ProductDetailDelivery, message_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET state='bound', message_id=? WHERE logical_id=? AND delivery_id=? AND state='pending' AND message_id IS NULL",
                (message_id, delivery.logical_id, delivery.delivery_id),
            )
            return cursor.rowcount == 1

    def mark_uncertain(self, delivery: ProductDetailDelivery) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET state='uncertain' WHERE logical_id=? AND delivery_id=? AND state='pending'",
                (delivery.logical_id, delivery.delivery_id),
            )
            return cursor.rowcount == 1

    def discard_pending(self, delivery: ProductDetailDelivery) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT state FROM deliveries WHERE logical_id=?", (delivery.logical_id,)).fetchone()
            if not row or row["state"] != "pending":
                return False
            conn.execute("DELETE FROM items WHERE delivery_id=?", (delivery.delivery_id,))
            conn.execute("DELETE FROM deliveries WHERE logical_id=?", (delivery.logical_id,))
            return True

    def discard_delivery(self, delivery: ProductDetailDelivery) -> bool:
        """Remove a prepared or bound delivery after adapter-side failure."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE logical_id=? AND delivery_id=?",
                (delivery.logical_id, delivery.delivery_id),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM items WHERE delivery_id=?", (delivery.delivery_id,))
            conn.execute(
                "DELETE FROM deliveries WHERE logical_id=? AND delivery_id=?",
                (delivery.logical_id, delivery.delivery_id),
            )
            return True

    def lookup(self, custom_id: str, *, guild_id: str | None, channel_id: str,
               message_id: str, user_id: str, now: float | None = None) -> DiscordProductDetailItemV1 | None:
        now = time.time() if now is None else now
        try:
            prefix, version, delivery_id, item_id, exp, signature = custom_id.split(":")
            core = ":".join((prefix, version, delivery_id, item_id, exp))
            if prefix != "hpd" or version != "v1" or not hmac.compare_digest(signature, self._signature(core)):
                return None
            if int(exp) <= now:
                return None
        except (ValueError, TypeError):
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if not row or row["state"] != "bound" or row["expires_at"] <= now:
                return None
            if row["guild_id"] != guild_id or row["channel_id"] != channel_id or row["message_id"] != message_id:
                return None
            if row["owner_user_id"] is not None and row["owner_user_id"] != user_id:
                return None
            item = conn.execute("SELECT * FROM items WHERE delivery_id=? AND item_id=?", (delivery_id, item_id)).fetchone()
            if not item:
                return None
            return DiscordProductDetailItemV1(item["label"], item["title"], item["body"])

    def active_bound_deliveries(self, now: float | None = None) -> list[sqlite3.Row]:
        now = time.time() if now is None else now
        with self._connection() as conn:
            return conn.execute(
                "SELECT * FROM deliveries WHERE state='bound' AND expires_at>? ORDER BY expires_at LIMIT 500", (now,)
            ).fetchall()

    def restore_active_deliveries(self, now: float | None = None):
        """Return immutable delivery/envelope pairs for persistent View restart."""
        restored = []
        for row in self.active_bound_deliveries(now):
            with self._connection() as conn:
                items = conn.execute(
                    "SELECT item_id,label,title,body FROM items WHERE delivery_id=? ORDER BY rowid",
                    (row["delivery_id"],),
                ).fetchall()
            envelope = DiscordProductDetailsEnvelopeV1(
                tuple(
                    DiscordProductDetailItemV1(item["label"], item["title"], item["body"])
                    for item in items
                ),
                max(30, min(900, int(row["expires_at"] - (now or time.time())))),
                row["owner_user_id"],
            )
            delivery = ProductDetailDelivery(
                row["logical_id"], row["delivery_id"],
                tuple(self._custom_id(row["delivery_id"], item["item_id"], row["expires_at"]) for item in items),
                row["expires_at"],
            )
            restored.append((delivery, envelope, row["message_id"]))
        return restored
