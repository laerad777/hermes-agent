"""Immutable, content-addressed runtime snapshots for Kanban workers.

This module is deliberately stdlib-only.  It is imported by the already-running
Kanban dispatcher before any code from a candidate worktree is executed.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import hashlib
import importlib.abc
import importlib.machinery
import io
import json
import os
import shutil
import stat
import sys
import sysconfig
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, Mapping, Optional


_SCHEMA_VERSION = 1
_SELECTION_POLICY_VERSION = 5
_SELECTED_DIRECTORIES = (
    "agent", "cron", "gateway", "hermes_cli", "plugins", "providers", "skills",
    "tools", "tui_gateway", "acp_adapter", "locales",
)
_SELECTED_TOP_LEVEL = frozenset({"pyproject.toml", "package.json"})
_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", "__pycache__", "dist", "build", ".worktrees",
    ".hermes", ".venv", "venv",
})
_NATIVE_SUFFIXES = (".so", ".dylib", ".dll", ".pyd")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 50_000
_BUFFER = 1024 * 1024
_CAPABILITY_LOCK = threading.Lock()
_INSTALLED_CAPABILITY: Optional["SnapshotCapability"] = None
_INSTALLED_IMPORT_GUARD: Optional["_SnapshotImportGuard"] = None


class RuntimeSnapshotError(RuntimeError):
    """Stable fail-closed snapshot error."""

    def __init__(self, code: str, relative_path: Optional[str] = None):
        self.code = code
        self.relative_path = relative_path
        suffix = f": {relative_path}" if relative_path else ""
        super().__init__(f"{code}{suffix}")


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    size: int
    sha256: str
    executable: bool
    mode_class: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": "regular",
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
            "mode_class": self.mode_class,
        }


@dataclass(frozen=True)
class RuntimeSnapshotSpec:
    object_root: Path
    payload_root: Path
    manifest_path: Path
    cache_key: str
    manifest_sha256: str
    content_root_sha256: str
    source_revision: str
    source_dirty: bool
    total_files: int
    total_bytes: int
    reused: bool = False
    verified_bytes: Mapping[str, bytes] = field(
        default_factory=dict, repr=False, compare=False,
    )
    runtime_bindings: tuple["RuntimeDirectoryBinding", ...] = field(
        default_factory=tuple, repr=False, compare=False,
    )


@dataclass(frozen=True)
class RuntimeDirectoryBinding:
    role: str
    path: Path
    handle: int
    device: int
    inode: int
    owner: int
    mode: int


@dataclass(frozen=True)
class SnapshotLease:
    cache_key: str
    run_id: int
    path: Path
    nonce: str
    state: str = "prepared"


@dataclass(frozen=True)
class SnapshotCapability:
    spec: RuntimeSnapshotSpec
    manifest: Mapping[str, object]
    guard_version: int = 1


@dataclass(frozen=True)
class SealedResourceFile:
    path: str
    pass_fds: tuple[int, ...]


class _VerifiedSourceLoader(importlib.abc.Loader):
    """Execute source bytes retained by the verified snapshot capability."""

    def __init__(self, fullname: str, origin: str, source: bytes, *, is_package: bool):
        self.fullname = fullname
        self.origin = origin
        self.source = source
        self.is_package = is_package

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        module.__file__ = self.origin
        module.__loader__ = self
        if self.is_package:
            module.__package__ = self.fullname
            module.__path__ = [str(Path(self.origin).parent)]
        exec(compile(self.source, self.origin, "exec"), module.__dict__)

    def get_code(self, fullname: str):
        if fullname != self.fullname:
            raise ImportError(fullname)
        return compile(self.source, self.origin, "exec")

    def get_source(self, fullname: str) -> str:
        if fullname != self.fullname:
            raise ImportError(fullname)
        return self.source.decode("utf-8")


class _SnapshotImportGuard:
    """Resolve imports once, then reject mutable first/third-party origins."""

    def __init__(self, capability: SnapshotCapability):
        self.capability = capability
        self.payload_root = capability.spec.payload_root.resolve(strict=True)
        raw_files = capability.manifest.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeSnapshotError("snapshot_manifest_invalid")
        self.manifest_paths = frozenset(
            str(entry["path"]) for entry in raw_files if isinstance(entry, dict)
        )
        self.first_party = frozenset(
            path.split("/", 1)[0].removesuffix(".py")
            for path in self.manifest_paths
            if path.endswith(".py")
        )
        try:
            self.payload_handle, payload_info = _open_directory_no_follow(
                capability.spec.payload_root,
            )
        except (OSError, RuntimeSnapshotError) as exc:
            raise RuntimeSnapshotError("snapshot_import_origin_forbidden") from exc
        self.payload_identity = (int(payload_info.st_dev), int(payload_info.st_ino))
        self.runtime_identities = frozenset(
            (binding.device, binding.inode) for binding in capability.spec.runtime_bindings
        )

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.payload_handle)

    def _location_class(self, raw_path: str) -> Optional[str]:
        path = Path(raw_path)
        if not path.is_absolute() or ".." in path.parts:
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path.anchor, directory_flags)
        matched: Optional[str] = None
        try:
            for index, component in enumerate(path.parts[1:]):
                final = index == len(path.parts[1:]) - 1
                try:
                    next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                except NotADirectoryError:
                    if not final:
                        raise
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
                opened = os.fstat(descriptor)
                identity = (int(opened.st_dev), int(opened.st_ino))
                if identity == self.payload_identity:
                    matched = "payload"
                elif identity in self.runtime_identities:
                    matched = "runtime"
            return matched
        except OSError:
            return None
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _validate_spec_locations(self, fullname: str, spec) -> None:
        locations: list[str] = []
        origin = getattr(spec, "origin", None)
        loader = getattr(spec, "loader", None)
        search_locations = getattr(spec, "submodule_search_locations", None)
        if origin in {"built-in", "frozen"}:
            expected_loader = (
                importlib.machinery.BuiltinImporter
                if origin == "built-in"
                else importlib.machinery.FrozenImporter
            )
            if loader is not expected_loader or search_locations:
                raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname)
            return
        if origin not in {None, "built-in", "frozen"}:
            locations.append(str(origin))
        get_filename = getattr(loader, "get_filename", None)
        if callable(get_filename):
            try:
                loader_filename = get_filename(fullname)
            except (ImportError, OSError, TypeError) as exc:
                raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname) from exc
            if loader_filename:
                locations.append(str(loader_filename))
        if search_locations is not None:
            locations.extend(str(location) for location in search_locations)
        if origin is None and not locations:
            raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname)
        for location in locations:
            if self._location_class(location) is None:
                raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname)

    def find_spec(self, fullname: str, path=None, target=None):
        parts = fullname.split(".")
        module_relative = "/".join(parts) + ".py"
        package_relative = "/".join((*parts, "__init__.py"))
        for relative, is_package in (
            (module_relative, False),
            (package_relative, True),
        ):
            source = self.capability.spec.verified_bytes.get(relative)
            if source is None or relative not in self.manifest_paths:
                continue
            origin = str(self.payload_root.joinpath(*PurePosixPath(relative).parts))
            loader = _VerifiedSourceLoader(
                fullname, origin, bytes(source), is_package=is_package,
            )
            return importlib.machinery.ModuleSpec(
                fullname,
                loader,
                origin=origin,
                is_package=is_package,
            )
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        self._validate_spec_locations(fullname, spec)
        origin = getattr(spec, "origin", None)
        if origin in {"built-in", "frozen"}:
            return spec
        if origin is None:
            return spec
        origin_class = self._location_class(str(origin))
        if origin_class == "runtime":
            return spec
        try:
            relative = Path(origin).relative_to(self.payload_root).as_posix()
        except ValueError as exc:
            raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname) from exc
        source_relative = relative[:-1] if relative.endswith((".pyc", ".pyo")) else relative
        if source_relative not in self.manifest_paths:
            raise RuntimeSnapshotError("snapshot_import_origin_forbidden", fullname)
        manifest_resource_bytes(self.capability.spec, source_relative)
        return spec


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_stream(handle: BinaryIO) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = handle.read(_BUFFER)
        if not chunk:
            break
        size += len(chunk)
        if size > _MAX_FILE_BYTES:
            raise RuntimeSnapshotError("source_limits_exceeded")
        digest.update(chunk)
        chunks.append(chunk)
    return digest.hexdigest(), size, b"".join(chunks)


def _normalized_relative(path: Path, root: Path) -> str:
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeSnapshotError("source_path_invalid") from exc
    if not parts or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise RuntimeSnapshotError("source_path_invalid")
    normalized = unicodedata.normalize("NFC", PurePosixPath(*parts).as_posix())
    if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise RuntimeSnapshotError("source_path_invalid", normalized)
    return normalized


def _selected_roots(source_root: Path) -> list[Path]:
    roots = [source_root / name for name in _SELECTED_DIRECTORIES if (source_root / name).exists()]
    roots.extend(
        path for path in sorted(source_root.glob("*.py"), key=lambda item: item.name.encode())
        if path.name not in _SELECTED_TOP_LEVEL
    )
    roots.extend(source_root / name for name in sorted(_SELECTED_TOP_LEVEL) if (source_root / name).exists())
    return roots


def iter_selected_entries(source_root: Path | str) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Yield the deterministic selected closure without following links."""
    root = Path(source_root).resolve(strict=True)
    seen_names: dict[str, str] = {}
    seen_identity: dict[tuple[int, int], str] = {}
    selected: list[tuple[str, Path, os.stat_result]] = []

    def visit(path: Path) -> None:
        relative = _normalized_relative(path, root)
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeSnapshotError("source_unstable", relative) from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeSnapshotError("source_link", relative)
        if stat.S_ISDIR(info.st_mode):
            if path.name in _EXCLUDED_DIRECTORIES or path.name.startswith("venv-"):
                return
            try:
                children = sorted(path.iterdir(), key=lambda item: unicodedata.normalize("NFC", item.name).encode("utf-8"))
            except OSError as exc:
                raise RuntimeSnapshotError("source_unstable", relative) from exc
            for child in children:
                visit(child)
            return
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeSnapshotError("source_special_file", relative)
        if relative.endswith((".pyc", ".pyo")) or path.name == ".DS_Store" or path.name.endswith(("~", ".swp", ".swo")):
            return
        if relative.lower().endswith(_NATIVE_SUFFIXES):
            raise RuntimeSnapshotError("first_party_native_unsupported", relative)
        if getattr(info, "st_nlink", 1) != 1:
            raise RuntimeSnapshotError("source_link", relative)
        collision_key = unicodedata.normalize("NFC", relative).casefold()
        previous = seen_names.get(collision_key)
        if previous is not None and previous != relative:
            raise RuntimeSnapshotError("source_case_collision", relative)
        seen_names[collision_key] = relative
        identity = (int(info.st_dev), int(info.st_ino))
        previous_identity = seen_identity.get(identity)
        if previous_identity is not None and previous_identity != relative:
            raise RuntimeSnapshotError("source_link", relative)
        seen_identity[identity] = relative
        selected.append((relative, path, info))

    for selected_root in _selected_roots(root):
        visit(selected_root)
    selected.sort(key=lambda row: row[0].encode("utf-8"))
    yield from selected


def _supported_languages(data: bytes) -> set[str]:
    try:
        tree = ast.parse(data.decode("utf-8"), filename="agent/i18n.py")
    except (UnicodeError, SyntaxError) as exc:
        raise RuntimeSnapshotError("snapshot_resource_inventory_incomplete", "agent/i18n.py") from exc
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "SUPPORTED_LANGUAGES" for target in targets):
                value = node.value
                if isinstance(value, ast.Dict):
                    keys = {key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
                    if keys:
                        return keys
                if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
                    keys = {item.value for item in value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                    if keys:
                        return keys
    return set()


def _validate_locales(staged_bytes: Mapping[str, bytes]) -> None:
    i18n_bytes = staged_bytes.get("agent/i18n.py")
    if i18n_bytes is None:
        return
    languages = _supported_languages(i18n_bytes)
    if not languages:
        return
    selected_paths = set(staged_bytes)
    expected = {f"locales/{language}.yaml" for language in languages}
    missing = sorted(expected - selected_paths)
    if missing:
        raise RuntimeSnapshotError("snapshot_locale_missing", missing[0])
    actual = {path for path in selected_paths if path.startswith("locales/") and path.endswith(".yaml")}
    if actual != expected:
        extra = sorted(actual - expected)
        raise RuntimeSnapshotError("snapshot_locale_unsupported", extra[0] if extra else None)


def _resource_inventory(files: list[SnapshotFile]) -> list[dict[str, str]]:
    """Describe the policy for every executable Python input deterministically."""
    return [
        {
            "path": entry.path,
            "policy": "sealed-resource" if entry.path == "agent/i18n.py" else "sealed-import",
        }
        for entry in files
        if entry.path.endswith(".py")
    ]


def _content_root(files: list[SnapshotFile]) -> str:
    digest = hashlib.sha256(b"hermes-runtime-snapshot-content-v1\0")
    for entry in files:
        digest.update(entry.path.encode("utf-8") + b"\0")
        digest.update(str(entry.size).encode("ascii") + b"\0")
        digest.update((b"1" if entry.executable else b"0") + b"\0")
        digest.update(entry.sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


def _cache_key(identity: Mapping[str, object], content_root: str) -> str:
    value = {
        "repository_id": identity["repository_id"],
        "source_revision": identity["source_revision"],
        "source_dirty": identity["source_dirty"],
        "selection_policy_version": _SELECTION_POLICY_VERSION,
        "python_implementation": sys.implementation.name,
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_abi_tag": sysconfig.get_config_var("SOABI") or "none",
        "platform_tag": sys.platform,
        "content_root_sha256": content_root,
    }
    return _sha256_bytes(b"hermes-runtime-snapshot-key-v1\0" + _canonical_json(value))


def _manifest_to_spec(
    object_root: Path,
    manifest: Mapping[str, object],
    manifest_sha256: str,
    *,
    reused: bool,
    verified_bytes: Optional[Mapping[str, bytes]] = None,
) -> RuntimeSnapshotSpec:
    return RuntimeSnapshotSpec(
        object_root=object_root,
        payload_root=object_root / "payload",
        manifest_path=object_root / "manifest.json",
        cache_key=str(manifest["cache_key"]),
        manifest_sha256=manifest_sha256,
        content_root_sha256=str(manifest["content_root_sha256"]),
        source_revision=str(manifest["source_revision"]),
        source_dirty=bool(manifest["source_dirty"]),
        total_files=int(manifest["total_files"]),
        total_bytes=int(manifest["total_bytes"]),
        reused=reused,
        verified_bytes=dict(verified_bytes or {}),
    )


def _open_directory_no_follow(path: Path) -> tuple[int, os.stat_result]:
    """Open an absolute directory component-wise without following links."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeSnapshotError("snapshot_runtime_binding_unsupported")
    absolute = path.absolute()
    if not absolute.is_absolute() or any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise RuntimeSnapshotError("snapshot_runtime_binding_invalid")
    flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise RuntimeSnapshotError("snapshot_runtime_binding_invalid")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _scan_runtime_startup_files(role: str, descriptor: int) -> None:
    """Reject interpreter startup hooks using the already-bound directory fd."""
    if role not in {"base_site", "venv_site"}:
        return
    try:
        for name in sorted(os.listdir(descriptor), key=lambda value: value.encode("utf-8")):
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if name in {"sitecustomize.py", "usercustomize.py"} or name.endswith(".pth"):
                raise RuntimeSnapshotError("snapshot_runtime_startup_forbidden", name)
    except RuntimeSnapshotError:
        raise
    except OSError as exc:
        raise RuntimeSnapshotError("snapshot_runtime_binding_invalid", role) from exc


def runtime_binding_for_directory(role: str, path: Path | str) -> RuntimeDirectoryBinding:
    """Open and bind one dispatcher-selected runtime directory capability."""
    if role not in {"stdlib", "base_site", "venv_site"}:
        raise RuntimeSnapshotError("snapshot_runtime_binding_invalid", role)
    candidate = Path(path).absolute()
    descriptor = -1
    try:
        descriptor, opened = _open_directory_no_follow(candidate)
    except (OSError, RuntimeSnapshotError) as exc:
        raise RuntimeSnapshotError("snapshot_runtime_binding_invalid", role) from exc
    current_uid = getattr(os, "getuid", lambda: int(opened.st_uid))()
    if int(opened.st_uid) not in {0, current_uid} or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(descriptor)
        raise RuntimeSnapshotError("snapshot_runtime_binding_permissions", role)
    try:
        _scan_runtime_startup_files(role, descriptor)
    except RuntimeSnapshotError:
        os.close(descriptor)
        raise
    return RuntimeDirectoryBinding(
        role=role,
        path=candidate,
        handle=descriptor,
        device=int(opened.st_dev),
        inode=int(opened.st_ino),
        owner=int(opened.st_uid),
        mode=int(opened.st_mode),
    )


def with_runtime_bindings(
    spec: RuntimeSnapshotSpec,
    bindings: tuple[RuntimeDirectoryBinding, ...],
) -> RuntimeSnapshotSpec:
    """Return a snapshot spec carrying ordered dispatcher-verified bindings."""
    return RuntimeSnapshotSpec(
        object_root=spec.object_root,
        payload_root=spec.payload_root,
        manifest_path=spec.manifest_path,
        cache_key=spec.cache_key,
        manifest_sha256=spec.manifest_sha256,
        content_root_sha256=spec.content_root_sha256,
        source_revision=spec.source_revision,
        source_dirty=spec.source_dirty,
        total_files=spec.total_files,
        total_bytes=spec.total_bytes,
        reused=spec.reused,
        verified_bytes=spec.verified_bytes,
        runtime_bindings=bindings,
    )


def snapshot_runtime_sys_path(spec: RuntimeSnapshotSpec) -> list[str]:
    """Return the complete sealed child path in fixed role order."""
    expected_order = {"stdlib": 0, "base_site": 1, "venv_site": 2}
    previous = -1
    result = [str(spec.payload_root)]
    seen: dict[tuple[int, int], str] = {}
    for binding in spec.runtime_bindings:
        order = expected_order[binding.role]
        if order < previous:
            raise RuntimeSnapshotError("snapshot_runtime_binding_order")
        previous = order
        identity = (binding.device, binding.inode)
        prior_role = seen.get(identity)
        if prior_role is not None:
            if prior_role != binding.role:
                raise RuntimeSnapshotError("snapshot_runtime_binding_collision")
            continue
        seen[identity] = binding.role
        result.append(str(binding.path))
    return result


def dispatcher_runtime_bindings() -> tuple[RuntimeDirectoryBinding, ...]:
    """Resolve roots from the already-running dispatcher interpreter."""
    # Python installations commonly expose convenience symlinks (Homebrew's
    # ``opt`` path, for example). Resolve those before the strict no-follow
    # walk so the sealed binding itself contains no link component.
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    bindings = [runtime_binding_for_directory("stdlib", stdlib)]
    dynload = stdlib / "lib-dynload"
    if dynload.is_dir():
        bindings.append(runtime_binding_for_directory("stdlib", dynload))
    purelib = sysconfig.get_path("purelib")
    if purelib:
        role = "venv_site" if sys.prefix != sys.base_prefix else "base_site"
        bindings.append(
            runtime_binding_for_directory(role, Path(purelib).resolve(strict=True))
        )
    return tuple(bindings)


def runtime_bindings_json(spec: RuntimeSnapshotSpec) -> str:
    return json.dumps(
        [
            {
                "role": item.role, "path": str(item.path), "handle": item.handle,
                "device": item.device,
                "inode": item.inode, "owner": item.owner, "mode": item.mode,
            }
            for item in spec.runtime_bindings
        ],
        sort_keys=True, separators=(",", ":"),
    )


def runtime_bindings_from_json(raw: str) -> tuple[RuntimeDirectoryBinding, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch") from exc
    if not isinstance(values, list):
        raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch")
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "role", "path", "handle", "device", "inode", "owner", "mode",
        }:
            raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch")
        expected = RuntimeDirectoryBinding(
            str(value["role"]), Path(str(value["path"])), int(value["handle"]),
            int(value["device"]),
            int(value["inode"]), int(value["owner"]), int(value["mode"]),
        )
        path_descriptor: Optional[int] = None
        try:
            opened = os.fstat(expected.handle)
            path_descriptor, path_info = _open_directory_no_follow(expected.path)
        except (OSError, RuntimeSnapshotError) as exc:
            raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch", expected.role) from exc
        finally:
            if path_descriptor is not None:
                os.close(path_descriptor)
        recorded = (expected.device, expected.inode, expected.owner, expected.mode)
        actual = (int(opened.st_dev), int(opened.st_ino), int(opened.st_uid), int(opened.st_mode))
        path_actual = (
            int(path_info.st_dev), int(path_info.st_ino),
            int(path_info.st_uid), int(path_info.st_mode),
        )
        if actual != recorded or path_actual != recorded:
            raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch", expected.role)
        _scan_runtime_startup_files(expected.role, expected.handle)
        result.append(expected)
    return tuple(result)


def runtime_binding_pass_fds(bindings: tuple[RuntimeDirectoryBinding, ...]) -> tuple[int, ...]:
    """Return dispatcher-opened directory capabilities for child inheritance."""
    return tuple(dict.fromkeys(binding.handle for binding in bindings))


def build_runtime_snapshot(
    source_root: Path | str,
    *,
    repository_id: str,
    source_revision: str,
    source_dirty: bool,
    cache_root: Path | str,
) -> RuntimeSnapshotSpec:
    """Capture, verify, seal, and atomically publish selected source bytes."""
    source = Path(source_root).resolve(strict=True)
    cache = Path(cache_root)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.is_symlink():
        raise RuntimeSnapshotError("cache_permissions")
    for child in ("staging", "objects", "leases", "locks", "quarantine"):
        (cache / child).mkdir(exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix="build-", dir=cache / "staging"))
    payload = staging / "payload"
    payload.mkdir(mode=0o700)
    files: list[SnapshotFile] = []
    staged_bytes: dict[str, bytes] = {}
    total = 0
    initial = list(iter_selected_entries(source))
    if len(initial) > _MAX_FILES:
        raise RuntimeSnapshotError("source_limits_exceeded")
    try:
        for relative, path, before in initial:
            destination = payload.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise RuntimeSnapshotError("source_unstable", relative) from exc
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise RuntimeSnapshotError("source_link", relative)
                with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
                    first_hash, size, data = _sha256_stream(handle)
                os.lseek(descriptor, 0, os.SEEK_SET)
                with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
                    second_hash, second_size, _ = _sha256_stream(handle)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or first_hash != second_hash or size != second_size:
                raise RuntimeSnapshotError("source_unstable", relative)
            total += size
            if total > _MAX_TOTAL_BYTES:
                raise RuntimeSnapshotError("source_limits_exceeded", relative)
            with destination.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if _sha256_bytes(destination.read_bytes()) != first_hash:
                raise RuntimeSnapshotError("snapshot_content_mismatch", relative)
            executable = bool(before.st_mode & stat.S_IXUSR)
            destination.chmod(0o500 if executable else 0o400)
            files.append(SnapshotFile(relative, size, first_hash, executable, "executable" if executable else "data"))
            staged_bytes[relative] = data
        final = list(iter_selected_entries(source))
        initial_shape = [(r, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink) for r, _p, s in initial]
        final_shape = [(r, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink) for r, _p, s in final]
        if initial_shape != final_shape:
            raise RuntimeSnapshotError("source_unstable")
        _validate_locales(staged_bytes)
        content_root = _content_root(files)
        identity = {"repository_id": repository_id, "source_revision": source_revision, "source_dirty": bool(source_dirty)}
        key = _cache_key(identity, content_root)
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "selection_policy_version": _SELECTION_POLICY_VERSION,
            "hash_algorithm": "sha256",
            **identity,
            "python_implementation": sys.implementation.name,
            "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "python_abi_tag": sysconfig.get_config_var("SOABI") or "none",
            "platform_tag": sys.platform,
            "files": [entry.as_dict() for entry in files],
            "resource_inventory": _resource_inventory(files),
            "total_files": len(files),
            "total_bytes": total,
            "content_root_sha256": content_root,
            "cache_key": key,
        }
        manifest_bytes = _canonical_json(manifest)
        manifest_hash = _sha256_bytes(manifest_bytes)
        manifest_path = staging / "manifest.json"
        with manifest_path.open("xb") as output:
            output.write(manifest_bytes)
            output.flush()
            os.fsync(output.fileno())
        manifest_path.chmod(0o400)
        (staging / "SEALED").write_bytes(b"")
        (staging / "SEALED").chmod(0o400)
        for directory, dirs, _names in os.walk(payload, topdown=False):
            Path(directory).chmod(0o500)
        target = cache / "objects" / key
        try:
            staging.rename(target)
            target.chmod(0o500)
            reused = False
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EEXIST, errno.ENOTEMPTY} or not target.exists():
                raise
            verified = verify_published_snapshot(target, manifest_hash, key)
            if verified.content_root_sha256 != content_root:
                raise RuntimeSnapshotError("cache_collision")
            shutil.rmtree(staging, ignore_errors=True)
            return verified
        return _manifest_to_spec(
            target, manifest, manifest_hash, reused=reused,
            verified_bytes=staged_bytes,
        )
    except Exception:
        if staging.exists():
            try:
                staging.chmod(0o700)
                for directory, dirs, names in os.walk(staging):
                    Path(directory).chmod(0o700)
                    for name in names:
                        with contextlib.suppress(OSError):
                            (Path(directory) / name).chmod(0o600)
                shutil.rmtree(staging)
            except OSError:
                pass
        raise


def _load_canonical_manifest(object_root: Path, expected_hash: str) -> tuple[dict, bytes]:
    try:
        data = (object_root / "manifest.json").read_bytes()
        manifest = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSnapshotError("snapshot_manifest_invalid") from exc
    if not isinstance(manifest, dict) or data != _canonical_json(manifest) or _sha256_bytes(data) != expected_hash:
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    allowed = {
        "schema_version", "selection_policy_version", "hash_algorithm", "repository_id",
        "source_revision", "source_dirty", "python_implementation", "python_major_minor",
        "python_abi_tag", "platform_tag", "files", "total_files", "total_bytes",
        "content_root_sha256", "cache_key", "resource_inventory",
    }
    if set(manifest) != allowed or manifest.get("schema_version") != 1 or manifest.get("hash_algorithm") != "sha256":
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    expected_inventory = _resource_inventory([
        SnapshotFile(
            str(raw["path"]), int(raw["size"]), str(raw["sha256"]),
            bool(raw["executable"]), str(raw["mode_class"]),
        )
        for raw in manifest.get("files", []) if isinstance(raw, dict)
    ])
    if manifest.get("resource_inventory") != expected_inventory:
        raise RuntimeSnapshotError("snapshot_resource_inventory_incomplete")
    return manifest, data


def verify_published_snapshot(object_root: Path | str, expected_manifest_sha256: str, expected_cache_key: str) -> RuntimeSnapshotSpec:
    """Rehash every published byte and reject any unmanifested object."""
    root = Path(object_root)
    if root.is_symlink() or not (root / "SEALED").is_file():
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    manifest, _ = _load_canonical_manifest(root, expected_manifest_sha256)
    if manifest.get("cache_key") != expected_cache_key or root.name != expected_cache_key:
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    entries: list[SnapshotFile] = []
    verified_bytes: dict[str, bytes] = {}
    expected_paths: set[str] = set()
    for raw in manifest["files"]:
        if not isinstance(raw, dict) or raw.get("kind") != "regular":
            raise RuntimeSnapshotError("snapshot_manifest_invalid")
        relative = str(raw.get("path", ""))
        if relative in expected_paths or _normalized_relative(root / "payload" / relative, root / "payload") != relative:
            raise RuntimeSnapshotError("snapshot_manifest_invalid")
        expected_paths.add(relative)
        path = root / "payload" / relative
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeSnapshotError("snapshot_content_mismatch", relative) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeSnapshotError("snapshot_content_mismatch", relative)
        data = path.read_bytes()
        if len(data) != raw.get("size") or _sha256_bytes(data) != raw.get("sha256"):
            raise RuntimeSnapshotError("snapshot_content_mismatch", relative)
        verified_bytes[relative] = bytes(data)
        entries.append(SnapshotFile(relative, len(data), raw["sha256"], bool(raw["executable"]), str(raw["mode_class"])))
    actual_paths = {
        _normalized_relative(path, root / "payload")
        for path in (root / "payload").rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_paths != expected_paths or _content_root(entries) != manifest.get("content_root_sha256"):
        raise RuntimeSnapshotError("snapshot_content_mismatch")
    identity = {key: manifest[key] for key in ("repository_id", "source_revision", "source_dirty")}
    if _cache_key(identity, str(manifest["content_root_sha256"])) != expected_cache_key:
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    return _manifest_to_spec(
        root, manifest, expected_manifest_sha256, reused=True,
        verified_bytes=verified_bytes,
    )


def _entry_for(spec: RuntimeSnapshotSpec, relative_path: str) -> Mapping[str, object]:
    normalized = PurePosixPath(relative_path)
    if normalized.is_absolute() or ".." in normalized.parts or normalized.as_posix() != relative_path:
        raise RuntimeSnapshotError("snapshot_resource_path_forbidden")
    manifest, _ = _load_canonical_manifest(spec.object_root, spec.manifest_sha256)
    for entry in manifest["files"]:
        if entry["path"] == relative_path:
            return entry
    raise RuntimeSnapshotError("snapshot_resource_path_forbidden", relative_path)


def manifest_resource_bytes(spec: RuntimeSnapshotSpec, relative_path: str) -> bytes:
    normalized = PurePosixPath(relative_path)
    if normalized.is_absolute() or ".." in normalized.parts or normalized.as_posix() != relative_path:
        raise RuntimeSnapshotError("snapshot_resource_path_forbidden")
    verified = spec.verified_bytes.get(relative_path)
    if verified is not None:
        return bytes(verified)
    entry = _entry_for(spec, relative_path)
    path = spec.payload_root.joinpath(*normalized.parts)
    try:
        info = path.lstat()
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeSnapshotError("snapshot_content_mismatch", relative_path) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or len(data) != entry["size"] or _sha256_bytes(data) != entry["sha256"]:
        raise RuntimeSnapshotError("snapshot_content_mismatch", relative_path)
    return bytes(data)


def verified_snapshot_bundle(spec: RuntimeSnapshotSpec) -> dict[str, object]:
    """Serialize only bytes already retained by verification for child bootstrap."""
    manifest, _ = _load_canonical_manifest(spec.object_root, spec.manifest_sha256)
    expected = {str(entry["path"]) for entry in manifest["files"]}
    if set(spec.verified_bytes) != expected:
        raise RuntimeSnapshotError("snapshot_content_mismatch")
    return {
        "object_root": str(spec.object_root),
        "manifest_sha256": spec.manifest_sha256,
        "manifest": manifest,
        "verified_bytes": {
            path: data.hex() for path, data in spec.verified_bytes.items()
        },
        "runtime_bindings": [
            {
                "role": binding.role,
                "path": str(binding.path),
                "handle": binding.handle,
                "device": binding.device,
                "inode": binding.inode,
                "owner": binding.owner,
                "mode": binding.mode,
            }
            for binding in spec.runtime_bindings
        ],
    }


def runtime_snapshot_from_verified_bundle(bundle: Mapping[str, object]) -> RuntimeSnapshotSpec:
    """Reconstruct a snapshot capability without reopening its payload paths."""
    if set(bundle) != {
        "object_root", "manifest_sha256", "manifest", "verified_bytes",
        "runtime_bindings",
    }:
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    manifest = bundle["manifest"]
    raw_bytes = bundle["verified_bytes"]
    if not isinstance(manifest, dict) or not isinstance(raw_bytes, dict):
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    manifest_hash = str(bundle["manifest_sha256"])
    if _sha256_bytes(_canonical_json(manifest)) != manifest_hash:
        raise RuntimeSnapshotError("snapshot_manifest_invalid")
    expected = {str(entry["path"]): entry for entry in manifest.get("files", [])}
    try:
        verified = {str(path): bytes.fromhex(str(data)) for path, data in raw_bytes.items()}
    except ValueError as exc:
        raise RuntimeSnapshotError("snapshot_content_mismatch") from exc
    if set(verified) != set(expected):
        raise RuntimeSnapshotError("snapshot_content_mismatch")
    for path, data in verified.items():
        entry = expected[path]
        if len(data) != entry["size"] or _sha256_bytes(data) != entry["sha256"]:
            raise RuntimeSnapshotError("snapshot_content_mismatch", path)
    raw_bindings = bundle["runtime_bindings"]
    if not isinstance(raw_bindings, list):
        raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch")
    bindings: list[RuntimeDirectoryBinding] = []
    for raw in raw_bindings:
        if not isinstance(raw, dict) or set(raw) != {
            "role", "path", "handle", "device", "inode", "owner", "mode",
        }:
            raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch")
        expected = RuntimeDirectoryBinding(
            role=str(raw["role"]),
            path=Path(str(raw["path"])),
            handle=int(raw["handle"]),
            device=int(raw["device"]),
            inode=int(raw["inode"]),
            owner=int(raw["owner"]),
            mode=int(raw["mode"]),
        )
        path_descriptor: Optional[int] = None
        try:
            opened = os.fstat(expected.handle)
            path_descriptor, path_info = _open_directory_no_follow(expected.path)
        except (OSError, RuntimeSnapshotError) as exc:
            raise RuntimeSnapshotError(
                "snapshot_runtime_binding_mismatch", expected.role,
            ) from exc
        finally:
            if path_descriptor is not None:
                os.close(path_descriptor)
        actual = (int(opened.st_dev), int(opened.st_ino), int(opened.st_uid), int(opened.st_mode))
        path_actual = (
            int(path_info.st_dev), int(path_info.st_ino),
            int(path_info.st_uid), int(path_info.st_mode),
        )
        recorded = (expected.device, expected.inode, expected.owner, expected.mode)
        if actual != recorded or path_actual != recorded:
            raise RuntimeSnapshotError("snapshot_runtime_binding_mismatch", str(raw["role"]))
        _scan_runtime_startup_files(expected.role, expected.handle)
        bindings.append(expected)
    root = Path(str(bundle["object_root"]))
    return with_runtime_bindings(_manifest_to_spec(
        root, manifest, manifest_hash, reused=True, verified_bytes=verified,
    ), tuple(bindings))


def manifest_resource_text(spec: RuntimeSnapshotSpec, relative_path: str, encoding: str = "utf-8") -> str:
    return manifest_resource_bytes(spec, relative_path).decode(encoding)


def manifest_resource_stream(spec: RuntimeSnapshotSpec, relative_path: str) -> io.BytesIO:
    return io.BytesIO(manifest_resource_bytes(spec, relative_path))


def sealed_resource_bytes(relative_path: str) -> bytes:
    capability = snapshot_bootstrap_capability()
    if capability is None:
        raise RuntimeSnapshotError("snapshot_authority_unavailable")
    return manifest_resource_bytes(capability.spec, relative_path)


def sealed_resource_text(relative_path: str, encoding: str = "utf-8") -> str:
    return sealed_resource_bytes(relative_path).decode(encoding)


def sealed_python_argv(relative_path: str) -> tuple[str, list[str]]:
    """Return a Python command that executes the verified in-memory source."""
    import base64

    source = sealed_resource_bytes(relative_path)
    encoded = base64.b64encode(source).decode("ascii")
    launcher = (
        "import base64,sys;"
        f"_p={relative_path!r};"
        f"_b=base64.b64decode({encoded!r});"
        "sys.argv[0]=_p;exec(compile(_b,_p,'exec'),{'__name__':'__main__','__file__':_p})"
    )
    return sys.executable, ["-c", launcher]


@contextlib.contextmanager
def sealed_resource_file(relative_path: str) -> Iterator[SealedResourceFile]:
    """Expose verified bytes through an inherited fd, never the snapshot path."""
    data = sealed_resource_bytes(relative_path)
    handle = tempfile.TemporaryFile()
    try:
        handle.write(data)
        handle.flush()
        handle.seek(0)
        descriptor = handle.fileno()
        path = f"/dev/fd/{descriptor}" if Path("/dev/fd").exists() else f"/proc/self/fd/{descriptor}"
        yield SealedResourceFile(path=path, pass_fds=(descriptor,))
    finally:
        handle.close()


def sealed_resource_path(relative_path: str) -> Path:
    """Return an audited sealed absolute path for helper-process execution only."""
    capability = snapshot_bootstrap_capability()
    if capability is None:
        raise RuntimeSnapshotError("snapshot_authority_unavailable")
    manifest_resource_bytes(capability.spec, relative_path)
    return capability.spec.payload_root.joinpath(*PurePosixPath(relative_path).parts)


def install_snapshot_bootstrap_capability(spec: RuntimeSnapshotSpec) -> SnapshotCapability:
    """Single-assignment bootstrap hook; environment values cannot activate it."""
    global _INSTALLED_CAPABILITY, _INSTALLED_IMPORT_GUARD
    manifest, _ = _load_canonical_manifest(spec.object_root, spec.manifest_sha256)
    capability = SnapshotCapability(spec=spec, manifest=manifest)
    with _CAPABILITY_LOCK:
        if _INSTALLED_CAPABILITY is not None:
            raise RuntimeSnapshotError("launcher_handshake_failed")
        _INSTALLED_CAPABILITY = capability
        guard = _SnapshotImportGuard(capability)
        _INSTALLED_IMPORT_GUARD = guard
        sys.meta_path.insert(0, guard)
        for name, module in tuple(sys.modules.items()):
            if name.split(".", 1)[0] not in guard.first_party or not hasattr(module, "__path__"):
                continue
            package_dir = capability.spec.payload_root.joinpath(*name.split("."))
            if package_dir.is_dir():
                module.__path__ = [str(package_dir)]
    return capability


def snapshot_bootstrap_capability() -> Optional[SnapshotCapability]:
    return _INSTALLED_CAPABILITY


def clear_snapshot_bootstrap_capability_after_fork() -> None:
    global _INSTALLED_CAPABILITY, _INSTALLED_IMPORT_GUARD
    if _INSTALLED_IMPORT_GUARD is not None:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(_INSTALLED_IMPORT_GUARD)
        _INSTALLED_IMPORT_GUARD.close()
    _INSTALLED_IMPORT_GUARD = None
    _INSTALLED_CAPABILITY = None


_LOCK_STATE = threading.local()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _ordered_lock(cache_root: Path | str, cache_key: str, kind: str):
    """Take cache then lease locks and reject the reverse order."""
    if kind not in {"cache", "lease"}:
        raise ValueError("unknown snapshot lock kind")
    held = list(getattr(_LOCK_STATE, "held", []))
    if kind == "cache" and "lease" in held:
        raise RuntimeSnapshotError("cache_lock_order_violation")
    lock_name = f"{Path(cache_root).resolve()}:{cache_key}:{kind}"
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.setdefault(lock_name, threading.Lock())
    lock.acquire()
    held.append(kind)
    _LOCK_STATE.held = held
    try:
        yield
    finally:
        held.pop()
        _LOCK_STATE.held = held
        lock.release()


def prepare_snapshot_lease(
    spec: RuntimeSnapshotSpec,
    *,
    cache_root: Path | str,
    task_id: str,
    run_id: int,
) -> SnapshotLease:
    """Verify an object and create a durable prepared lease under lock order."""
    cache = Path(cache_root)
    with _ordered_lock(cache, spec.cache_key, "cache"):
        verify_published_snapshot(spec.object_root, spec.manifest_sha256, spec.cache_key)
        with _ordered_lock(cache, spec.cache_key, "lease"):
            directory = cache / "leases" / spec.cache_key
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"{int(run_id)}.json"
            nonce = os.urandom(32).hex()
            record = {
                "schema_version": 1,
                "task_id": task_id,
                "run_id": int(run_id),
                "state": "prepared",
                "cache_key": spec.cache_key,
                "nonce": nonce,
                "dispatcher_pid": os.getpid(),
                "created_at": int(time.time()),
            }
            data = _canonical_json(record)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return SnapshotLease(spec.cache_key, int(run_id), path, nonce)


def _transition_snapshot_lease(
    lease: SnapshotLease,
    *,
    expected_state: str,
    next_state: str,
    pid: int,
) -> SnapshotLease:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeSnapshotError("snapshot_lease_binding_mismatch")
    with _ordered_lock(lease.path.parents[2], lease.cache_key, "lease"):
        try:
            raw = lease.path.read_bytes()
            record = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeSnapshotError("snapshot_lease_binding_mismatch") from exc
        if (
            not isinstance(record, dict)
            or raw != _canonical_json(record)
            or record.get("cache_key") != lease.cache_key
            or record.get("run_id") != lease.run_id
            or record.get("nonce") != lease.nonce
            or record.get("state") != expected_state
            or lease.state != expected_state
            or ("pid" in record and record.get("pid") != pid)
        ):
            raise RuntimeSnapshotError("snapshot_lease_binding_mismatch")
        record["state"] = next_state
        record["pid"] = pid
        data = _canonical_json(record)
        replacement = lease.path.with_name(f".{lease.path.name}.{os.urandom(8).hex()}.tmp")
        descriptor = os.open(
            replacement,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        replacement.replace(lease.path)
        return SnapshotLease(
            lease.cache_key, lease.run_id, lease.path, lease.nonce, next_state,
        )


def mark_snapshot_lease_ready(lease: SnapshotLease, *, pid: int) -> SnapshotLease:
    return _transition_snapshot_lease(
        lease, expected_state="prepared", next_state="ready", pid=pid,
    )


def bind_snapshot_lease(lease: SnapshotLease, *, pid: int) -> SnapshotLease:
    return _transition_snapshot_lease(
        lease, expected_state="ready", next_state="bound", pid=pid,
    )


def release_snapshot_lease(lease: SnapshotLease) -> None:
    with contextlib.suppress(FileNotFoundError):
        lease.path.unlink()
    with contextlib.suppress(OSError):
        lease.path.parent.rmdir()


def gc_runtime_snapshots(cache_root: Path | str, *, max_objects: int) -> list[Path]:
    """Quarantine excess unleased objects using cache→lease ordering."""
    cache = Path(cache_root)
    objects = cache / "objects"
    if not objects.exists():
        return []
    candidates = sorted(
        (path for path in objects.iterdir() if path.is_dir()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    remove_count = max(0, len(candidates) - max(0, int(max_objects)))
    quarantined: list[Path] = []
    for candidate in candidates[:remove_count]:
        cache_key = candidate.name
        with _ordered_lock(cache, cache_key, "cache"):
            with _ordered_lock(cache, cache_key, "lease"):
                lease_dir = cache / "leases" / cache_key
                if lease_dir.exists() and any(lease_dir.iterdir()):
                    continue
                quarantine = cache / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = quarantine / f"gc-{cache_key}-{os.urandom(8).hex()}"
                candidate.chmod(0o700)
                candidate.rename(target)
                target.chmod(0o500)
                quarantined.append(target)
    return quarantined


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=clear_snapshot_bootstrap_capability_after_fork)
