"""Content-addressed, code-only snapshots for disposable deployment slots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

from .model import PriorRuntimeSnapshot

_FORBIDDEN_PARTS = {".env", "sessions", "session", "gateway-state", "locks", "pids", "logs", "artifacts", "cache", "caches", "secrets", "browser"}
_FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".journal"}
_PROTECTED_PREFIXES = ("/Users/jerome/.hermes", "/current")


def _resolved_with_existing_ancestor(path: Path) -> Path:
    """Resolve symlinks in the nearest existing ancestor without requiring the leaf."""
    candidate = Path(os.path.abspath(path))
    suffix: list[str] = []
    while not os.path.lexists(candidate):
        suffix.append(candidate.name)
        candidate = candidate.parent
    return candidate.resolve(strict=False).joinpath(*reversed(suffix))


def _reject_live_path(path: Path) -> None:
    raw = str(_resolved_with_existing_ancestor(path))
    if any(raw == prefix or raw.startswith(prefix + "/") for prefix in _PROTECTED_PREFIXES):
        raise ValueError("live runtime paths are outside this package's contract")


def _mkdir_output_dir(path: Path) -> None:
    """Create an output directory without following symlinked parents."""
    path = Path(os.path.abspath(path))
    _reject_live_path(path)
    missing: list[str] = []
    ancestor = path
    while not os.path.lexists(ancestor):
        missing.append(ancestor.name)
        ancestor = ancestor.parent
    _reject_live_path(ancestor)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(ancestor, flags)
    try:
        current = ancestor
        for name in reversed(missing):
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child_fd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
            current /= name
            _reject_live_path(current)
        if missing:
            os.chmod(path, 0o700)
        elif path.is_symlink() or not path.is_dir():
            raise ValueError("output path must be a directory")
    finally:
        os.close(fd)


def _copy_to_new_output(source: Path, destination: Path, mode: int) -> None:
    """Copy to a newly-created regular output file without following a leaf link."""
    _mkdir_output_dir(destination.parent)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with source.open("rb") as input_file, os.fdopen(fd, "wb") as output:
            fd = -1
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    os.chmod(destination, mode)


def _is_forbidden(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    name = relative.name.lower()
    return (
        any(part in _FORBIDDEN_PARTS or part.startswith("auth") for part in parts)
        or name.startswith("config") and name.endswith((".yaml", ".yml", ".json"))
        or name.startswith("profile") and name.endswith((".yaml", ".yml", ".json"))
        or any(name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES)
    )


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entries(source_root: Path, allowlist: Iterable[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for requested in sorted(allowlist):
        relative = Path(requested)
        if relative.is_absolute() or ".." in relative.parts or str(relative) in seen:
            raise ValueError("allowlist entries must be unique relative paths")
        seen.add(str(relative))
        if _is_forbidden(relative):
            raise ValueError(f"forbidden snapshot path: {relative}")
        source = source_root / relative
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"snapshot accepts regular code files only: {relative}")
        entries.append({
            "relative_path": relative.as_posix(), "type": "file",
            "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
            "sha256": _sha256(source),
        })
    return entries


def _snapshot(source_root: Path, cas_root: Path, allowlist: Iterable[str], kind: str, composition_digest: str = "") -> PriorRuntimeSnapshot:
    source_root, cas_root = Path(source_root), Path(cas_root)
    _reject_live_path(cas_root)
    if not source_root.is_dir():
        raise ValueError("snapshot source root must be a directory")
    source_resolved, cas_resolved = source_root.resolve(), cas_root.resolve()
    try:
        cas_resolved.relative_to(source_resolved)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot CAS must be outside the read-only source root")
    entries = _entries(source_root, allowlist)
    if composition_digest and (len(composition_digest) != 64 or any(char not in "0123456789abcdef" for char in composition_digest)):
        raise ValueError("snapshot composition identity must be a SHA-256 digest")
    manifest = {"kind": kind, "composition_digest": composition_digest, "entries": entries}
    manifest_bytes = _canonical(manifest)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    tree_digest = manifest_digest
    target = cas_root / "snapshots" / manifest_digest
    _reject_live_path(target)
    if os.path.lexists(target):
        snapshot = PriorRuntimeSnapshot(manifest_digest, tree_digest, target, kind, len(entries), composition_digest)
        verify_snapshot(snapshot)
        return snapshot
    _mkdir_output_dir(cas_root)
    _mkdir_output_dir(cas_root / "snapshots")
    _mkdir_output_dir(target)
    try:
        payload_root = target / "payload"
        _mkdir_output_dir(payload_root)
        for entry in entries:
            relative = Path(str(entry["relative_path"]))
            destination = payload_root / relative
            _copy_to_new_output(source_root / relative, destination, int(entry["mode"]))
            if _sha256(destination) != entry["sha256"]:
                raise RuntimeError("CAS copy digest mismatch")
        manifest_file = target / "manifest.json"
        manifest_file_fd = os.open(manifest_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(manifest_file_fd, "wb") as output:
            output.write(manifest_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(manifest_file, 0o600)
        directory_fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    snapshot = PriorRuntimeSnapshot(manifest_digest, tree_digest, target, kind, len(entries), composition_digest)
    verify_snapshot(snapshot)
    return snapshot


def create_dirty_source_snapshot(source_root: Path, cas_root: Path, allowlist: Iterable[str], *, composition_digest: str = "") -> PriorRuntimeSnapshot:
    return _snapshot(source_root, cas_root, allowlist, "dirty-source", composition_digest)


def create_active_release_snapshot(source_root: Path, cas_root: Path, allowlist: Iterable[str], *, composition_digest: str = "") -> PriorRuntimeSnapshot:
    return _snapshot(source_root, cas_root, allowlist, "active-release", composition_digest)


def verify_snapshot(snapshot: PriorRuntimeSnapshot) -> None:
    _reject_live_path(snapshot.cas_path)
    if not stat.S_ISDIR(snapshot.cas_path.lstat().st_mode) or stat.S_IMODE(snapshot.cas_path.lstat().st_mode) != 0o700:
        raise ValueError("snapshot CAS directory must be mode 0700")
    manifest_path = snapshot.cas_path / "manifest.json"
    if not stat.S_ISREG(manifest_path.lstat().st_mode) or stat.S_IMODE(manifest_path.lstat().st_mode) != 0o600:
        raise ValueError("snapshot manifest must be mode 0600")
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != snapshot.manifest_digest:
        raise ValueError("snapshot manifest digest mismatch")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid snapshot manifest") from exc
    if _canonical(manifest) != raw or manifest.get("kind") != snapshot.kind or manifest.get("composition_digest") != snapshot.composition_digest:
        raise ValueError("snapshot manifest is not canonical")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != snapshot.entry_count:
        raise ValueError("snapshot entry count mismatch")
    payload_path = snapshot.cas_path / "payload"
    _reject_live_path(payload_path)
    if not stat.S_ISDIR(payload_path.lstat().st_mode) or stat.S_IMODE(payload_path.lstat().st_mode) != 0o700:
        raise ValueError("snapshot payload directory must be mode 0700")
    payload_root = payload_path.resolve()
    for entry in entries:
        if (
            not isinstance(entry, dict) or not isinstance(entry.get("relative_path"), str)
            or not isinstance(entry.get("mode"), int) or not isinstance(entry.get("size"), int)
            or not isinstance(entry.get("sha256"), str) or entry.get("type") != "file"
        ):
            raise ValueError("snapshot manifest path is invalid")
        relative = Path(entry["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or relative == Path(".") or _is_forbidden(relative):
            raise ValueError("snapshot contains forbidden path")
        payload = snapshot.cas_path / "payload" / relative
        if stat.S_IMODE(payload.parent.lstat().st_mode) != 0o700:
            raise ValueError("snapshot payload parent must be mode 0700")
        if payload.resolve().parent != (payload_root / relative.parent).resolve():
            raise ValueError("snapshot payload escapes containment")
        info = payload.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"] or info.st_size != entry["size"] or _sha256(payload) != entry["sha256"]:
            raise ValueError(f"snapshot payload mismatch: {relative}")
    if snapshot.tree_digest != snapshot.manifest_digest:
        raise ValueError("snapshot tree digest mismatch")


def restore_snapshot_to_slot(snapshot: PriorRuntimeSnapshot, slot_root: Path) -> Path:
    """Restore a verified snapshot only into a fresh disposable slot."""
    verify_snapshot(snapshot)
    slot_root = Path(slot_root)
    _reject_live_path(slot_root)
    if os.path.lexists(slot_root):
        raise ValueError("restore destination must be a new slot")
    _mkdir_output_dir(slot_root)
    try:
        manifest = json.loads((snapshot.cas_path / "manifest.json").read_bytes())
        destination_root = slot_root.resolve()
        for entry in manifest["entries"]:
            relative = Path(entry["relative_path"])
            if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
                raise ValueError("snapshot restore path is invalid")
            destination = slot_root / relative
            if destination.resolve().parent != (destination_root / relative.parent).resolve():
                raise ValueError("snapshot restore escapes containment")
            _copy_to_new_output(snapshot.cas_path / "payload" / relative, destination, entry["mode"])
            info = destination.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != entry["mode"]
                or info.st_size != entry["size"]
                or _sha256(destination) != entry["sha256"]
            ):
                raise RuntimeError(f"restore verification failed: {relative}")
    except BaseException:
        shutil.rmtree(slot_root, ignore_errors=True)
        raise
    return slot_root
