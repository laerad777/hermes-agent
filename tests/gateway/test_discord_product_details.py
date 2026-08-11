import errno
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.discord_product_details import validate_discord_product_details
from gateway.platforms.base import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord import product_details
from plugins.platforms.discord.product_details import (
    DiscordProductDetailStore,
    SecureStoreUnavailable,
    secure_store_capability,
)


@pytest.fixture(autouse=True)
def _require_secure_store_for_store_tests(request):
    capability = secure_store_capability()
    platform_contract_tests = {
        "test_darwin_snapshot_capability_uses_runtime_primitives",
        "test_windows_without_secure_native_primitives_reports_fail_closed_capability",
        "test_adapter_initializes_on_windows_without_geteuid_and_keeps_text_enabled",
        "test_database_and_wal_stay_anchored_when_state_path_is_swapped",
        "test_connect_uses_only_verified_retained_directory_fd_uri",
        "test_connection_scope_always_closes_after_commit_or_rollback",
        "test_store_close_is_idempotent_and_closes_directory_fd",
        "test_store_initialization_failure_closes_directory_fd",
        "test_repeated_adapter_disconnect_closes_store_fds",
    }
    if not capability.available and request.node.name not in platform_contract_tests:
        pytest.skip(capability.reason)


def _envelope(ttl=60):
    return validate_discord_product_details({
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": ttl,
    })


def test_store_prepare_bind_lookup_restart_and_permissions(tmp_path):
    store = DiscordProductDetailStore(tmp_path)
    delivery = store.prepare_delivery(
        logical_id="logical", envelope=_envelope(), guild_id="g", channel_id="c",
        owner_user_id="u", now=time.time(),
    )
    same = store.prepare_delivery(
        logical_id="logical", envelope=_envelope(), guild_id="g", channel_id="c",
        owner_user_id="u", now=time.time(),
    )
    assert same.delivery_id == delivery.delivery_id
    assert store.lookup(delivery.custom_ids[0], guild_id="g", channel_id="c", message_id="m", user_id="u") is None
    assert store.bind_delivery(delivery, "m") is True
    item = store.lookup(delivery.custom_ids[0], guild_id="g", channel_id="c", message_id="m", user_id="u")
    assert item.body == "secret"
    store.close()

    restarted = DiscordProductDetailStore(tmp_path)
    assert restarted.lookup(delivery.custom_ids[0], guild_id="g", channel_id="c", message_id="m", user_id="u").body == "secret"
    restored = restarted.restore_active_deliveries()
    assert len(restored) == 1
    restored_delivery, restored_envelope, restored_message_id = restored[0]
    assert restored_delivery.custom_ids == delivery.custom_ids
    assert restored_envelope.items[0].body == "secret"
    assert restored_message_id == "m"
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "signing-key-v1").st_mode) == 0o600
    database_name = (
        "details-v2-snapshot.sqlite3"
        if restarted.capability.backend == "darwin-snapshot"
        else "details-v1.sqlite3"
    )
    assert stat.S_IMODE(os.stat(tmp_path / database_name).st_mode) == 0o600


def test_windows_without_secure_native_primitives_reports_fail_closed_capability(tmp_path):
    capability = secure_store_capability(platform="win32", win32_api=None)

    assert capability.available is False
    assert capability.backend == "windows"
    assert "secure" in capability.reason.lower()
    with pytest.raises(SecureStoreUnavailable, match="secure"):
        DiscordProductDetailStore(tmp_path, capability=capability)


def test_darwin_snapshot_capability_uses_runtime_primitives():
    capability = secure_store_capability(platform="darwin")

    assert capability.available is True
    assert capability.backend == "darwin-snapshot"


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
@pytest.mark.parametrize("legacy_name", [
    "details-v1.sqlite3",
    "details-v1.sqlite3-wal",
    "details-v1.sqlite3-shm",
    "details-v1.sqlite3-journal",
])
def test_darwin_snapshot_rejects_any_legacy_namespace_without_v2_mutation(
    tmp_path, legacy_name,
):
    legacy = tmp_path / legacy_name
    legacy.write_bytes(b"legacy-bytes")
    names_before = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
        DiscordProductDetailStore(tmp_path)

    assert legacy.read_bytes() == b"legacy-bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == names_before
    assert not (tmp_path / "details-v2-snapshot.sqlite3").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_restarts_and_serializes_concurrent_threads(tmp_path):
    store = DiscordProductDetailStore(tmp_path)

    def prepare(index):
        return store.prepare_delivery(
            logical_id=f"logical-{index}", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None, now=time.time(),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        deliveries = list(pool.map(prepare, range(20)))
    for index, delivery in enumerate(deliveries):
        assert store.bind_delivery(delivery, f"message-{index}")
    store.close()

    restarted = DiscordProductDetailStore(tmp_path)
    assert len(restarted.active_bound_deliveries()) == 20
    assert (tmp_path / "details-v2-snapshot.sqlite3").is_file()
    assert not (tmp_path / "details-v1.sqlite3").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_rejects_legacy_sidecar_without_mutation(tmp_path):
    db = tmp_path / "details-v1.sqlite3"
    db.write_bytes(b"main")
    os.chmod(db, 0o600)
    wal = tmp_path / "details-v1.sqlite3-wal"
    shm = tmp_path / "details-v1.sqlite3-shm"
    wal.write_bytes(b"wal-only-committed-row")
    shm.write_bytes(b"shm")
    before = {path.name: path.read_bytes() for path in (db, wal, shm)}
    names_before = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
        DiscordProductDetailStore(tmp_path)

    assert {path.name: path.read_bytes() for path in (db, wal, shm)} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == names_before


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_rejects_real_wal_only_commit_without_mutation(tmp_path):
    db = tmp_path / "details-v1.sqlite3"
    writer = sqlite3.connect(db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_probe(value TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO wal_probe VALUES ('wal-only')")
        writer.commit()

        wal = tmp_path / "details-v1.sqlite3-wal"
        shm = tmp_path / "details-v1.sqlite3-shm"
        assert wal.stat().st_size > 0
        main_only = tmp_path / "main-only.sqlite3"
        main_only.write_bytes(db.read_bytes())
        with sqlite3.connect(main_only) as main_reader:
            assert main_reader.execute("SELECT count(*) FROM wal_probe").fetchone()[0] == 0
        main_only.unlink()

        os.chmod(db, 0o600)
        before = {path.name: path.read_bytes() for path in (db, wal, shm)}
        names_before = sorted(path.name for path in tmp_path.iterdir())

        with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
            DiscordProductDetailStore(tmp_path)

        assert {path.name: path.read_bytes() for path in (db, wal, shm)} == before
        assert sorted(path.name for path in tmp_path.iterdir()) == names_before
    finally:
        writer.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
@pytest.mark.parametrize("variant", ["symlink", "wrong-mode", "oversize"])
def test_darwin_snapshot_rejects_malformed_legacy_sidecars_without_mutation(
    tmp_path, variant,
):
    store = DiscordProductDetailStore(tmp_path)
    store.close()
    db = tmp_path / "details-v2-snapshot.sqlite3"
    before_db = db.read_bytes()
    sidecar = tmp_path / "details-v1.sqlite3-wal"
    target = tmp_path / "sidecar-target"
    if variant == "symlink":
        target.write_bytes(b"target")
        sidecar.symlink_to(target)
    elif variant == "wrong-mode":
        sidecar.write_bytes(b"wal")
        os.chmod(sidecar, 0o666)
    else:
        sidecar.write_bytes(b"x" * (product_details.MAX_SNAPSHOT_BYTES + 1))
    names_before = sorted(path.name for path in tmp_path.iterdir())
    sidecar_mode = sidecar.lstat().st_mode
    sidecar_size = sidecar.lstat().st_size
    target_before = target.read_bytes() if target.exists() else None

    with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
        DiscordProductDetailStore(tmp_path)

    assert db.read_bytes() == before_db
    assert sorted(path.name for path in tmp_path.iterdir()) == names_before
    assert sidecar.lstat().st_mode == sidecar_mode
    assert sidecar.lstat().st_size == sidecar_size
    assert (target.read_bytes() if target.exists() else None) == target_before
    assert not list(tmp_path.glob(".details-v1.sqlite3.*.tmp"))


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_subprocess_writer_lock_fails_closed(tmp_path):
    script = """
import sys
from pathlib import Path
from plugins.platforms.discord.product_details import DiscordProductDetailStore

store = DiscordProductDetailStore(Path(sys.argv[1]))
print("READY", flush=True)
sys.stdin.readline()
store.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, os.fspath(tmp_path)],
        cwd=os.fspath(Path(__file__).parents[2]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(SecureStoreUnavailable, match="writer lock"):
            DiscordProductDetailStore(tmp_path)
    finally:
        child.stdin.write("stop\n")
        child.stdin.flush()
        stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, (stdout, stderr)
    DiscordProductDetailStore(tmp_path).close()


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_persist_failure_restores_memory_and_disk_atomically(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    before = (tmp_path / "details-v2-snapshot.sqlite3").read_bytes()
    real_replace = os.replace
    failed = False

    def fail_snapshot_replace(src, dst, **kwargs):
        nonlocal failed
        if dst == "details-v2-snapshot.sqlite3" and not failed:
            failed = True
            raise OSError("crash before replace")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", fail_snapshot_replace)

    with pytest.raises(OSError, match="crash before replace"):
        store.prepare_delivery(
            logical_id="not-persisted", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    assert (tmp_path / "details-v2-snapshot.sqlite3").read_bytes() == before
    assert not list(tmp_path.glob(".details-v2-snapshot.sqlite3.*.tmp"))
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-persisted'"
    ).fetchone()[0] == 0

    monkeypatch.setattr(os, "replace", real_replace)
    persisted = store.prepare_delivery(
        logical_id="persisted", envelope=_envelope(), guild_id="g",
        channel_id="c", owner_user_id=None,
    )
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-persisted'"
    ).fetchone()[0] == 0
    store.close()

    restarted = DiscordProductDetailStore(tmp_path)
    assert restarted._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-persisted'"
    ).fetchone()[0] == 0
    assert restarted._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE delivery_id=?",
        (persisted.delivery_id,),
    ).fetchone()[0] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_post_replace_fsync_failure_restores_old_snapshot(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    snapshot = tmp_path / "details-v2-snapshot.sqlite3"
    before = snapshot.read_bytes()
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_first_publication_directory_fsync(fd):
        nonlocal directory_fsyncs
        if fd == store._directory_fd:
            directory_fsyncs += 1
            if directory_fsyncs == 3:  # marker + backup durable; publication fsync fails
                raise OSError("post-replace directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_publication_directory_fsync)
    with pytest.raises(OSError, match="post-replace directory fsync failed"):
        store.prepare_delivery(
            logical_id="not-durable", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    assert snapshot.read_bytes() == before
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-durable'"
    ).fetchone()[0] == 0
    assert not list(tmp_path.glob(".details-v2-snapshot.sqlite3.*.tmp"))
    assert not (tmp_path / ".details-v2-snapshot.sqlite3.rollback").exists()
    assert not (tmp_path / ".details-v2-snapshot.sqlite3.recovery").exists()
    monkeypatch.setattr(os, "fsync", real_fsync)
    later = store.prepare_delivery(
        logical_id="later-durable", envelope=_envelope(), guild_id="g",
        channel_id="c", owner_user_id=None,
    )
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-durable'"
    ).fetchone()[0] == 0
    store.close()
    restarted = DiscordProductDetailStore(tmp_path)
    assert restarted._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='not-durable'"
    ).fetchone()[0] == 0
    assert restarted._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE delivery_id=?", (later.delivery_id,),
    ).fetchone()[0] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_first_publication_fsync_failure_unlinks_new_snapshot(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    snapshot = tmp_path / "details-v2-snapshot.sqlite3"
    snapshot.unlink()
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_first_directory_fsync(fd):
        nonlocal directory_fsyncs
        if fd == store._directory_fd:
            directory_fsyncs += 1
            if directory_fsyncs == 2:  # marker durable; first publication fsync fails
                raise OSError("first publication directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="first publication directory fsync failed"):
        store.prepare_delivery(
            logical_id="first-failed", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    assert not snapshot.exists()
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='first-failed'"
    ).fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_rollback_durability_failure_seals_store_and_restart(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_publication_and_rollback_directory_fsync(fd):
        nonlocal directory_fsyncs
        if fd == store._directory_fd:
            directory_fsyncs += 1
            if directory_fsyncs >= 3:
                raise OSError(f"directory fsync failed {directory_fsyncs}")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_publication_and_rollback_directory_fsync)
    with pytest.raises(SecureStoreUnavailable, match="recovery_required"):
        store.prepare_delivery(
            logical_id="indeterminate", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )
    with pytest.raises(SecureStoreUnavailable, match="recovery_required"):
        store.active_bound_deliveries()

    monkeypatch.setattr(os, "fsync", real_fsync)
    store.close()
    with pytest.raises(SecureStoreUnavailable, match="recovery_required"):
        DiscordProductDetailStore(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_final_cleanup_fsync_failure_seals_restart(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_final_cleanup_directory_fsync(fd):
        nonlocal directory_fsyncs
        if fd == store._directory_fd:
            directory_fsyncs += 1
            if directory_fsyncs == 4:
                raise OSError("final cleanup directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_final_cleanup_directory_fsync)
    with pytest.raises(SecureStoreUnavailable, match="recovery_required"):
        store.prepare_delivery(
            logical_id="cleanup-indeterminate", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    monkeypatch.setattr(os, "fsync", real_fsync)
    store.close()
    with pytest.raises(SecureStoreUnavailable, match="recovery_required"):
        DiscordProductDetailStore(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_late_legacy_main_aborts_before_v2_replace(tmp_path, monkeypatch):
    store = DiscordProductDetailStore(tmp_path)
    snapshot = tmp_path / "details-v2-snapshot.sqlite3"
    before = snapshot.read_bytes()
    real_replace = os.replace

    def introduce_legacy_before_replace(src, dst, **kwargs):
        if dst == "details-v2-snapshot.sqlite3":
            (tmp_path / "details-v1.sqlite3").write_bytes(b"late-legacy")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", introduce_legacy_before_replace)
    with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
        store.prepare_delivery(
            logical_id="raced", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    assert snapshot.read_bytes() == before
    assert (tmp_path / "details-v1.sqlite3").read_bytes() == b"late-legacy"
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='raced'"
    ).fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
def test_darwin_snapshot_sidecar_race_fails_before_temp_or_output_mutation(
    tmp_path, monkeypatch,
):
    store = DiscordProductDetailStore(tmp_path)
    db = tmp_path / "details-v2-snapshot.sqlite3"
    before = db.read_bytes()
    real_reject = store._reject_legacy_store
    checks = 0

    def inject_after_first_check():
        nonlocal checks
        checks += 1
        real_reject()
        if checks == 1:
            (tmp_path / "details-v1.sqlite3-wal").write_bytes(b"raced")

    monkeypatch.setattr(store, "_reject_legacy_store", inject_after_first_check)

    with pytest.raises(SecureStoreUnavailable, match="legacy_store_present"):
        store.prepare_delivery(
            logical_id="raced", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )

    assert checks >= 2
    assert db.read_bytes() == before
    assert not list(tmp_path.glob(".details-v1.sqlite3.*.tmp"))
    assert store._snapshot_connection.execute(
        "SELECT count(*) FROM deliveries WHERE logical_id='raced'"
    ).fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS snapshot backend required")
@pytest.mark.parametrize("invalid", ["corrupt", "oversize", "mode"])
def test_darwin_snapshot_rejects_invalid_existing_database(tmp_path, invalid):
    db = tmp_path / "details-v2-snapshot.sqlite3"
    if invalid == "oversize":
        db.write_bytes(b"x" * (product_details.MAX_SNAPSHOT_BYTES + 1))
    else:
        db.write_bytes(b"not-a-sqlite-database")
    os.chmod(db, 0o644 if invalid == "mode" else 0o600)

    with pytest.raises((OSError, sqlite3.DatabaseError)):
        DiscordProductDetailStore(tmp_path)

    assert db.exists()
    assert not list(tmp_path.glob(".details-v1.sqlite3.*.tmp"))


def test_adapter_initializes_on_windows_without_geteuid_and_keeps_text_enabled(monkeypatch):
    monkeypatch.setattr(product_details.sys, "platform", "win32")
    monkeypatch.delattr(product_details.os, "geteuid", raising=False)

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))

    assert adapter._product_details_store is None
    assert adapter.format_message("normal Discord text") == "normal Discord text"


def test_posix_store_never_uses_path_based_chmod(tmp_path, monkeypatch):
    def reject_path_chmod(*args, **kwargs):
        raise AssertionError("path-based chmod is vulnerable to symlink swaps")

    monkeypatch.setattr(os, "chmod", reject_path_chmod)

    store = DiscordProductDetailStore(tmp_path)

    assert store.capability.available is True
    assert store.capability.backend in {"linux-procfd", "darwin-snapshot"}


def test_store_close_is_idempotent_and_closes_directory_fd(tmp_path):
    store = DiscordProductDetailStore.__new__(DiscordProductDetailStore)
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    store._directory_fd = directory_fd

    store.close()
    store.close()

    assert store._directory_fd is None
    with pytest.raises(OSError) as exc_info:
        os.fstat(directory_fd)
    assert exc_info.value.errno == errno.EBADF


def test_store_initialization_failure_closes_directory_fd(tmp_path, monkeypatch):
    opened_fds = []
    real_open_directory = product_details._PosixSecureStorePrimitives.open_directory

    def capture_directory_fd(self, path, *, create):
        fd = real_open_directory(self, path, create=create)
        opened_fds.append(fd)
        return fd

    monkeypatch.setattr(
        product_details._PosixSecureStorePrimitives,
        "open_directory",
        capture_directory_fd,
    )
    monkeypatch.setattr(
        DiscordProductDetailStore,
        "_initialize",
        lambda self: (_ for _ in ()).throw(RuntimeError("initialize failed")),
    )

    capability = product_details.SecureStoreCapability(
        available=True,
        backend="test-posix",
        reason="test capability",
    )
    monkeypatch.setattr(DiscordProductDetailStore, "_load_key", lambda self: b"k" * 32)

    with pytest.raises(RuntimeError, match="initialize failed"):
        DiscordProductDetailStore(tmp_path, capability=capability)

    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_connect_uses_only_verified_retained_directory_fd_uri(tmp_path, monkeypatch):
    class FakeConnection:
        row_factory = None

        def close(self):
            pass

    calls = []
    fake_connection = FakeConnection()

    def fake_connect(database, *args, **kwargs):
        calls.append((database, args, kwargs))
        return fake_connection

    store = DiscordProductDetailStore.__new__(DiscordProductDetailStore)
    store._directory_fd = 42
    store.db_path = tmp_path / "attacker-controlled" / "details-v1.sqlite3"
    store._validate_state_dir_identity = lambda: None
    monkeypatch.setattr(product_details.sqlite3, "connect", fake_connect)

    assert store._connect() is fake_connection
    assert calls == [("file:/proc/self/fd/42/details-v1.sqlite3?nofollow=1", (), {"uri": True})]
    assert os.fspath(tmp_path) not in calls[0][0]
    assert fake_connection.row_factory is sqlite3.Row


def test_connection_scope_always_closes_after_commit_or_rollback(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.closed = False
            self.exit_exception = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.exit_exception = exc

        def close(self):
            self.closed = True

    store = DiscordProductDetailStore.__new__(DiscordProductDetailStore)
    committed = FakeConnection()
    rolled_back = FakeConnection()
    connections = iter((committed, rolled_back))
    monkeypatch.setattr(store, "_connect", lambda: next(connections))

    with store._connection() as conn:
        assert conn is committed

    assert committed.exit_exception is None
    assert committed.closed is True

    with pytest.raises(RuntimeError, match="rollback"):
        with store._connection():
            raise RuntimeError("rollback")

    assert isinstance(rolled_back.exit_exception, RuntimeError)
    assert rolled_back.closed is True


@pytest.mark.parametrize("target", ["state_dir", "key", "database"])
def test_store_rejects_symlinked_state_components_and_files(tmp_path, target):
    real = tmp_path / "real"
    real.mkdir()
    state = tmp_path / "state"
    if target == "state_dir":
        state.symlink_to(real, target_is_directory=True)
    else:
        state.mkdir()
        victim = real / target
        victim.write_bytes(b"x" * 32)
        name = "signing-key-v1" if target == "key" else (
            "details-v2-snapshot.sqlite3"
            if secure_store_capability().backend == "darwin-snapshot"
            else "details-v1.sqlite3"
        )
        (state / name).symlink_to(victim)

    with pytest.raises(OSError, match="symlink"):
        DiscordProductDetailStore(state)


def test_key_read_stays_anchored_when_state_path_is_swapped(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    original_key = b"o" * 32
    (state / "signing-key-v1").write_bytes(original_key)
    os.chmod(state / "signing-key-v1", 0o600)
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    attacker_key = b"a" * 32
    (attacker / "signing-key-v1").write_bytes(attacker_key)
    os.chmod(attacker / "signing-key-v1", 0o600)

    store = DiscordProductDetailStore.__new__(DiscordProductDetailStore)
    store._secure = product_details._PosixSecureStorePrimitives(
        product_details.secure_store_capability()
    )
    store.state_dir = state
    store._directory_fd = store._secure.open_directory(state, create=False)
    info = os.fstat(store._directory_fd)
    store._directory_identity = (info.st_dev, info.st_ino)
    store.key_path = state / "signing-key-v1"
    real_open = os.open
    swapped = False

    def swap_before_key_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and os.fspath(path).endswith("signing-key-v1") and not flags & os.O_CREAT:
            swapped = True
            state.rename(tmp_path / "original-state")
            state.symlink_to(attacker, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_key_open)

    assert store._load_key() == original_key
    assert swapped is True
    assert (attacker / "signing-key-v1").read_bytes() == attacker_key


def test_key_rotation_stays_anchored_when_state_path_is_swapped(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    old_key = b"o" * 32
    key_path = state / "signing-key-v1"
    key_path.write_bytes(old_key)
    os.chmod(key_path, 0o600)
    old = time.time() - (31 * 24 * 60 * 60)
    os.utime(key_path, (old, old))
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    attacker_key = b"a" * 32
    (attacker / "signing-key-v1").write_bytes(attacker_key)
    os.chmod(attacker / "signing-key-v1", 0o600)

    store = DiscordProductDetailStore.__new__(DiscordProductDetailStore)
    store._secure = product_details._PosixSecureStorePrimitives(
        product_details.secure_store_capability()
    )
    store.state_dir = state
    store._directory_fd = store._secure.open_directory(state, create=False)
    info = os.fstat(store._directory_fd)
    store._directory_identity = (info.st_dev, info.st_ino)
    store.key_path = key_path
    store.key = old_key
    real_open = os.open
    swapped = False

    def swap_before_temporary_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        name = os.fspath(path)
        if not swapped and name.startswith(".signing-key-v1.") and flags & os.O_CREAT:
            swapped = True
            state.rename(tmp_path / "original-state")
            state.symlink_to(attacker, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_temporary_open)

    assert store._rotate_key_if_due(time.time()) is True
    assert swapped is True
    assert store.key != old_key
    assert (tmp_path / "original-state" / "signing-key-v1").read_bytes() == store.key
    assert (attacker / "signing-key-v1").read_bytes() == attacker_key


def test_database_and_wal_stay_anchored_when_state_path_is_swapped(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Linux procfd backend contract")
    state = tmp_path / "state"
    capability = secure_store_capability()
    if not capability.available:
        with pytest.raises(SecureStoreUnavailable, match="directory-fd path binding"):
            DiscordProductDetailStore(state, capability=capability)
        return
    store = DiscordProductDetailStore(state)
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    attacker_db = attacker / "details-v1.sqlite3"
    attacker_keeper = sqlite3.connect(attacker_db)
    try:
        assert attacker_keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        attacker_keeper.execute("CREATE TABLE attacker_marker(value TEXT)")
        attacker_keeper.execute("INSERT INTO attacker_marker VALUES ('untouched')")
        attacker_keeper.commit()
        attacker_paths = tuple(
            attacker / f"details-v1.sqlite3{suffix}" for suffix in ("", "-wal", "-shm")
        )
        assert all(path.is_file() for path in attacker_paths)
        attacker_before = {path.name: path.read_bytes() for path in attacker_paths}

        real_connect = sqlite3.connect
        swapped = False

        def swap_before_sqlite_open(database, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                state.rename(tmp_path / "original-state")
                state.symlink_to(attacker, target_is_directory=True)
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", swap_before_sqlite_open)

        with store._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE anchored_write(value TEXT)")
            conn.execute("INSERT INTO anchored_write VALUES ('original')")

        assert swapped is True
        assert {path.name: path.read_bytes() for path in attacker_paths} == attacker_before
        assert attacker_keeper.execute(
            "SELECT value FROM attacker_marker"
        ).fetchone()[0] == "untouched"
        for unexpected_table in ("anchored_write", "record"):
            assert attacker_keeper.execute(
                "SELECT count(*) FROM sqlite_master WHERE name=?", (unexpected_table,)
            ).fetchone()[0] == 0
        original_db = tmp_path / "original-state" / "details-v1.sqlite3"
        with real_connect(original_db) as conn:
            assert conn.execute("SELECT value FROM anchored_write").fetchone()[0] == "original"
    finally:
        attacker_keeper.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc fd accounting required")
def test_repeated_real_store_operations_and_close_do_not_leak_fds(tmp_path):
    fd_count_before = len(os.listdir("/proc/self/fd"))

    for index in range(20):
        store = DiscordProductDetailStore(tmp_path / f"state-{index}")
        delivery = store.prepare_delivery(
            logical_id=f"logical-{index}", envelope=_envelope(), guild_id="g",
            channel_id="c", owner_user_id=None,
        )
        assert store.bind_delivery(delivery, f"message-{index}") is True
        assert store.active_bound_deliveries()
        store.close()

    assert len(os.listdir("/proc/self/fd")) == fd_count_before


@pytest.mark.asyncio
async def test_repeated_adapter_disconnect_closes_store_fds(tmp_path, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    class DirectoryFdStore:
        def __init__(self, _state_dir):
            self._directory_fd = os.open(tmp_path, os.O_RDONLY)

        def close(self):
            directory_fd = self._directory_fd
            self._directory_fd = None
            if directory_fd is not None:
                os.close(directory_fd)

    monkeypatch.setattr(discord_adapter, "DiscordProductDetailStore", DirectoryFdStore)
    fd_count_before = len(os.listdir("/dev/fd"))
    directory_fds = []

    for _ in range(3):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
        store = adapter._product_details_store
        assert store is not None
        directory_fds.append(store._directory_fd)

        await adapter.disconnect()

        assert adapter._product_details_store is None

    assert len(os.listdir("/dev/fd")) == fd_count_before
    for fd in directory_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_tamper_owner_binding_expiry_and_state_transitions_fail_closed(tmp_path):
    store = DiscordProductDetailStore(tmp_path)
    delivery = store.prepare_delivery(
        logical_id="logical", envelope=_envelope(), guild_id="g", channel_id="c",
        owner_user_id="u", now=100,
    )
    assert store.mark_uncertain(delivery) is True
    assert store.lookup(delivery.custom_ids[0], guild_id="g", channel_id="c", message_id="m", user_id="u", now=101) is None

    other = store.prepare_delivery(
        logical_id="other", envelope=_envelope(), guild_id="g", channel_id="c",
        owner_user_id="u", now=100,
    )
    assert store.bind_delivery(other, "m")
    cid = other.custom_ids[0]
    assert store.lookup(cid + "x", guild_id="g", channel_id="c", message_id="m", user_id="u", now=101) is None
    assert store.lookup(cid, guild_id="x", channel_id="c", message_id="m", user_id="u", now=101) is None
    assert store.lookup(cid, guild_id="g", channel_id="c", message_id="m", user_id="x", now=101) is None
    assert store.lookup(cid, guild_id="g", channel_id="c", message_id="m", user_id="u", now=161) is None


def test_store_rotates_old_key_invalidates_rows_and_purges_expired(tmp_path):
    store = DiscordProductDetailStore(tmp_path)
    delivery = store.prepare_delivery(
        logical_id="old", envelope=_envelope(), guild_id="g", channel_id="c",
        owner_user_id=None, now=100,
    )
    assert store.bind_delivery(delivery, "m")
    old_key = store.key
    old = time.time() - (31 * 24 * 60 * 60)
    os.utime(tmp_path / "signing-key-v1", (old, old))
    store.close()

    restarted = DiscordProductDetailStore(tmp_path)

    assert restarted.key != old_key
    assert restarted.active_bound_deliveries(now=101) == []
    database = tmp_path / (
        "details-v2-snapshot.sqlite3"
        if restarted.capability.backend == "darwin-snapshot"
        else "details-v1.sqlite3"
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0

    expired = restarted.prepare_delivery(
        logical_id="expired", envelope=_envelope(ttl=30), guild_id="g", channel_id="c",
        owner_user_id=None, now=100,
    )
    assert restarted.bind_delivery(expired, "expired-message")
    restarted.purge_expired(now=131)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0


def test_store_enforces_active_delivery_cap(tmp_path, monkeypatch):
    from plugins.platforms.discord import product_details

    monkeypatch.setattr(product_details, "MAX_ACTIVE_DELIVERIES", 1)
    store = DiscordProductDetailStore(tmp_path)
    store.prepare_delivery(
        logical_id="one", envelope=_envelope(), guild_id=None, channel_id="c",
        owner_user_id=None,
    )
    with pytest.raises(ValueError, match="capacity"):
        store.prepare_delivery(
            logical_id="two", envelope=_envelope(), guild_id=None, channel_id="c",
            owner_user_id=None,
        )


@pytest.mark.asyncio
async def test_discord_lifecycle_hooks_prepare_reuse_and_finalize_store_state(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = DiscordProductDetailStore(tmp_path)
    metadata = {"discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}

    handle = await adapter._structured_delivery_begin(
        chat_id="123", content="summary", reply_to=None, metadata=metadata,
        logical_delivery_id="logical",
    )
    assert handle is not None
    first = await adapter._structured_delivery_attempt(handle=handle, attempt=0, metadata=metadata)
    second = await adapter._structured_delivery_attempt(handle=handle, attempt=1, metadata=metadata)
    assert first["_discord_structured_delivery_handle"] is handle
    assert second["_discord_structured_delivery_handle"] is handle

    await adapter._structured_delivery_finalize(
        handle=handle, outcome="not_sent_exhausted", result=None,
    )
    assert adapter._product_details_store.active_bound_deliveries() == []
    assert adapter._product_details_store.discard_pending(handle.delivery) is False


@pytest.mark.asyncio
async def test_structured_begin_binds_actual_guild_scope(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = DiscordProductDetailStore(tmp_path)
    metadata = {"discord_guild_id": "guild-1", "discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}

    handle = await adapter._structured_delivery_begin(
        chat_id="123", content="summary", reply_to=None, metadata=metadata,
        logical_delivery_id="guild-logical",
    )
    assert handle is not None
    assert adapter._product_details_store.bind_delivery(handle.delivery, "456")
    item = adapter._product_details_store.lookup(
        handle.delivery.custom_ids[0], guild_id="guild-1", channel_id="123",
        message_id="456", user_id="any",
    )
    assert item is not None
    assert item.body == "secret"


@pytest.mark.asyncio
async def test_structured_send_view_prepare_failure_discards_pending_row(tmp_path, monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = DiscordProductDetailStore(tmp_path)
    metadata = {"discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}
    handle = await adapter._structured_delivery_begin(
        chat_id="123", content="summary", reply_to=None, metadata=metadata,
        logical_delivery_id="logical",
    )
    assert handle is not None
    attempt_metadata = await adapter._structured_delivery_attempt(
        handle=handle, attempt=0, metadata=metadata,
    )
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=42)))
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(
        discord_adapter, "ProductDetailsView",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad view")),
    )

    result = await adapter.send("123", "summary", metadata=attempt_metadata)

    assert result.success is True
    assert adapter._product_details_store.discard_pending(handle.delivery) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_outcome", [False, RuntimeError("persist exploded")])
@pytest.mark.parametrize("cleanup_raises", [False, True])
async def test_bind_failure_cleanup_removes_exact_view_and_preserves_original_semantics(
    bind_outcome, cleanup_raises,
):
    message = SimpleNamespace(id=42, edit=AsyncMock())
    delivery = object()

    class Store:
        def bind_delivery(self, candidate, message_id):
            assert candidate is delivery
            assert message_id == "42"
            if isinstance(bind_outcome, Exception):
                raise bind_outcome
            return bind_outcome

        def discard_delivery(self, candidate):
            assert candidate is delivery
            if cleanup_raises:
                raise OSError("cleanup exploded")
            return True

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = Store()

    if isinstance(bind_outcome, Exception):
        with pytest.raises(RuntimeError, match="persist exploded"):
            await adapter._bind_product_delivery_or_cleanup(
                delivery, "42", message, content="summary",
            )
    else:
        assert await adapter._bind_product_delivery_or_cleanup(
            delivery, "42", message, content="summary",
        ) is False
    message.edit.assert_awaited_once_with(content="summary", view=None)


@pytest.mark.asyncio
async def test_bind_cleanup_removes_view_when_store_disappears_before_bind():
    message = SimpleNamespace(id=42, edit=AsyncMock())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = None

    assert await adapter._bind_product_delivery_or_cleanup(
        object(), "42", message, content="summary",
    ) is False
    message.edit.assert_awaited_once_with(content="summary", view=None)


@pytest.mark.asyncio
async def test_forum_send_discards_structured_handle_and_keeps_summary_delivery(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = DiscordProductDetailStore(tmp_path)
    metadata = {"discord_product_details": {
        "items": [{"label": "one", "title": "A", "body": "secret"}],
        "ttl_seconds": 60,
    }}
    handle = await adapter._structured_delivery_begin(
        chat_id="123", content="summary", reply_to=None, metadata=metadata,
        logical_delivery_id="forum-logical",
    )
    forum = SimpleNamespace(id=123)
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: forum,
        fetch_channel=AsyncMock(return_value=forum),
    )
    adapter._is_forum_parent = lambda channel: True
    adapter._send_to_forum = AsyncMock(
        return_value=SimpleNamespace(
            success=True, message_id="42", raw_response={},
            delivery_certainty=None, structured_failure=None,
        )
    )
    assert handle is not None

    result = await adapter.send(
        "123", "summary", metadata={
            **metadata,
            "_discord_structured_delivery_handle": handle,
        },
    )

    assert result.success is True
    assert result.structured_failure == "unsupported_forum"
    assert adapter._product_details_store.discard_pending(handle.delivery) is False


def test_restart_registers_persistent_view_for_bound_message(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._product_details_store = DiscordProductDetailStore(tmp_path)
    delivery = adapter._product_details_store.prepare_delivery(
        logical_id="restart", envelope=_envelope(), guild_id=None,
        channel_id="123", owner_user_id=None,
    )
    assert adapter._product_details_store.bind_delivery(delivery, "456")
    client = SimpleNamespace(add_view=MagicMock())

    adapter._restore_product_details_views(client)

    client.add_view.assert_called_once()
    args, kwargs = client.add_view.call_args
    assert args[0].timeout is None
    assert kwargs == {"message_id": 456}


@pytest.mark.asyncio
async def test_product_detail_callback_checks_component_auth_before_store_lookup(monkeypatch):
    from plugins.platforms.discord import adapter as discord_adapter

    store = SimpleNamespace(lookup=AsyncMock())
    adapter = SimpleNamespace(
        _product_details_store=store,
        _allowed_user_ids=set(),
        _allowed_role_ids=set(),
    )
    item = SimpleNamespace(label="one")
    button = discord_adapter.ProductDetailButton(adapter, item, "hpd:v1:x:0:1:sig")
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id="unauthorized", roles=[]),
        guild=SimpleNamespace(id="g"),
        channel=SimpleNamespace(id="c"),
        message=SimpleNamespace(id="m"),
    )
    monkeypatch.setattr(discord_adapter, "_component_check_auth", lambda *args: False)

    await type(button).callback(button, interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    store.lookup.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
