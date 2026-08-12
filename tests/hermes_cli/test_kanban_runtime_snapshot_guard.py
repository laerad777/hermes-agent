from __future__ import annotations

import importlib
import importlib.machinery
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from hermes_cli import kanban_runtime_snapshot as snapshot_module
from hermes_cli.kanban_runtime_snapshot import (
    RuntimeSnapshotError,
    build_runtime_snapshot,
    clear_snapshot_bootstrap_capability_after_fork,
    install_snapshot_bootstrap_capability,
    runtime_binding_for_directory,
    runtime_binding_pass_fds,
    runtime_bindings_from_json,
    runtime_bindings_json,
    runtime_snapshot_from_verified_bundle,
    snapshot_runtime_sys_path,
    sealed_python_argv,
    sealed_resource_file,
    sealed_resource_text,
    with_runtime_bindings,
    verified_snapshot_bundle,
)


@pytest.fixture(autouse=True)
def _clear_capability():
    clear_snapshot_bootstrap_capability_after_fork()
    yield
    clear_snapshot_bootstrap_capability_after_fork()


def _source(root: Path) -> Path:
    files = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/kanban_runtime_snapshot.py": "# sealed bootstrap\n",
        "hermes_cli/reviewer_bootstrap.py": "VALUE = 'sealed'\n",
        "hermes_cli/kanban_windows_spawn.py": "VALUE = 'sealed'\n",
        "hermes_cli/npm_engine.py": "VALUE = 'sealed'\n",
        "model_tools.py": "VALUE = 'sealed'\n",
        "tools/__init__.py": "",
        "tools/approved.py": "VALUE = 'sealed'\n",
        "tools/helper.py": "import os,pathlib\npathlib.Path(os.environ['SEALED_MARKER']).write_text('sealed')\n",
        "tools/reviewer_authority.py": "VALUE = 'sealed'\n",
        "tools/reviewer_surface.py": "VALUE = 'sealed'\n",
        "tools/review_exec_tool.py": "VALUE = 'sealed'\n",
        "plugins/__init__.py": "",
        "plugins/web/__init__.py": "",
        "plugins/web/ddgs/__init__.py": "",
        "plugins/web/ddgs/provider.py": "VALUE = 'sealed'\n",
        "plugins/web/ddgs/_search_worker.py": "VALUE = 'sealed'\n",
        "agent/__init__.py": "",
        "agent/i18n.py": "SUPPORTED_LANGUAGES = ('en',)\n",
        "locales/en.yaml": "hello: sealed\n",
        "package.json": "{}\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _snapshot(tmp_path: Path):
    snapshot = build_runtime_snapshot(
        _source(tmp_path / "source"),
        repository_id="repo",
        source_revision="a" * 40,
        source_dirty=True,
        cache_root=tmp_path / "cache",
    )
    clean_stdlib = tmp_path / "clean-stdlib-binding"
    clean_stdlib.mkdir()
    return with_runtime_bindings(
        snapshot,
        (runtime_binding_for_directory("stdlib", clean_stdlib),),
    )


def test_installed_guard_rejects_uninventoried_import_before_module_executes(tmp_path):
    snapshot = _snapshot(tmp_path)
    marker = tmp_path / "marker"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "late_module.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(outside))
    try:
        install_snapshot_bootstrap_capability(snapshot)
        with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
            importlib.import_module("late_module")
    finally:
        sys.path.remove(str(outside))
        sys.modules.pop("late_module", None)

    assert not marker.exists()


def test_installed_guard_allows_stdlib_and_manifested_module(tmp_path):
    snapshot = _snapshot(tmp_path)
    sys.path.insert(0, str(snapshot.payload_root))
    try:
        install_snapshot_bootstrap_capability(snapshot)
        assert importlib.import_module("email").__name__ == "email"
        assert importlib.import_module("tools.approved").VALUE == "sealed"
    finally:
        sys.path.remove(str(snapshot.payload_root))
        sys.modules.pop("tools.approved", None)


def test_installed_guard_allows_module_only_from_verified_site_binding(tmp_path):
    snapshot = _snapshot(tmp_path)
    approved_site = tmp_path / "approved-site"
    approved_site.mkdir()
    package = approved_site / "approved_dependency"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'approved'\n", encoding="utf-8")
    binding = runtime_binding_for_directory("venv_site", approved_site)
    snapshot = with_runtime_bindings(snapshot, (*snapshot.runtime_bindings, binding))
    original_path = list(sys.path)
    sys.path[:] = [str(snapshot.payload_root), str(approved_site)]

    try:
        install_snapshot_bootstrap_capability(snapshot)
        module = importlib.import_module("approved_dependency")
        assert module.VALUE == "approved"
        assert Path(module.__file__).resolve().is_relative_to(approved_site.resolve())
    finally:
        sys.path[:] = original_path
        sys.modules.pop("approved_dependency", None)


def test_installed_guard_rejects_user_site_injection(tmp_path):
    snapshot = _snapshot(tmp_path)
    approved_site = tmp_path / "approved-site"
    approved_site.mkdir()
    unknown = tmp_path / "user-site"
    unknown.mkdir()
    (unknown / "unapproved_dependency.py").write_text("VALUE = 'attacker'\n", encoding="utf-8")
    snapshot = with_runtime_bindings(
        snapshot,
        (
            *snapshot.runtime_bindings,
            runtime_binding_for_directory("venv_site", approved_site),
        ),
    )
    original_path = list(sys.path)
    sys.path[:] = [str(snapshot.payload_root), str(approved_site), str(unknown)]

    try:
        install_snapshot_bootstrap_capability(snapshot)
        with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
            importlib.import_module("unapproved_dependency")
    finally:
        sys.path[:] = original_path
        sys.modules.pop("unapproved_dependency", None)


@pytest.mark.parametrize("path_form", ["symlink", "dotdot"])
def test_installed_guard_rejects_binding_escape_aliases(monkeypatch, tmp_path, path_form):
    snapshot = _snapshot(tmp_path)
    approved = tmp_path / "approved"
    approved.mkdir()
    package = approved / "package"
    package.mkdir()
    escaped = tmp_path / "escaped.py"
    escaped.write_text("VALUE = 'attacker'\n", encoding="utf-8")
    snapshot = with_runtime_bindings(
        snapshot, (*snapshot.runtime_bindings, runtime_binding_for_directory("venv_site", approved)),
    )
    if path_form == "symlink":
        alias = approved / "alias.py"
        alias.symlink_to(escaped)
        origin = str(alias)
    else:
        origin = str(package / ".." / ".." / escaped.name)
    module_name = f"escaped_alias_{path_form}"
    spec = importlib.machinery.ModuleSpec(
        module_name,
        importlib.machinery.SourceFileLoader(module_name, origin),
        origin=origin,
    )
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *args: spec)

    install_snapshot_bootstrap_capability(snapshot)
    with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
        importlib.import_module(module_name)


def test_installed_guard_rejects_namespace_with_one_escaping_search_location(monkeypatch, tmp_path):
    snapshot = _snapshot(tmp_path)
    approved = tmp_path / "approved"
    escaped = tmp_path / "escaped"
    approved.mkdir()
    escaped.mkdir()
    snapshot = with_runtime_bindings(
        snapshot, (*snapshot.runtime_bindings, runtime_binding_for_directory("venv_site", approved)),
    )
    spec = importlib.machinery.ModuleSpec("mixed_namespace", loader=None, is_package=True)
    spec.submodule_search_locations = [str(approved), str(escaped)]
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *args: spec)

    install_snapshot_bootstrap_capability(snapshot)
    with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
        importlib.import_module("mixed_namespace")


def test_installed_guard_rejects_loader_filename_outside_verified_bindings(monkeypatch, tmp_path):
    snapshot = _snapshot(tmp_path)
    approved = tmp_path / "approved"
    escaped = tmp_path / "escaped.py"
    approved.mkdir()
    module_path = approved / "approved_loader.py"
    module_path.write_text("VALUE = 'approved'\n", encoding="utf-8")
    escaped.write_text("VALUE = 'escaped'\n", encoding="utf-8")
    snapshot = with_runtime_bindings(
        snapshot, (*snapshot.runtime_bindings, runtime_binding_for_directory("venv_site", approved)),
    )

    class SwappedLoader(importlib.machinery.SourceFileLoader):
        def get_filename(self, fullname=None):
            return str(escaped)

    spec = importlib.machinery.ModuleSpec(
        "approved_loader", SwappedLoader("approved_loader", str(module_path)), origin=str(module_path),
    )
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *args: spec)

    install_snapshot_bootstrap_capability(snapshot)
    with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
        importlib.import_module("approved_loader")


@pytest.mark.parametrize("forged_origin", ["built-in", "frozen"])
@pytest.mark.parametrize("escape_metadata", ["loader", "search"])
def test_installed_guard_rejects_forged_special_origin_with_filesystem_metadata(
    monkeypatch, tmp_path, forged_origin, escape_metadata
):
    snapshot = _snapshot(tmp_path)
    escaped = tmp_path / "escaped"
    escaped.mkdir()

    class ForgedLoader:
        def get_filename(self, fullname):
            return str(escaped / f"{fullname}.py")

    loader = ForgedLoader() if escape_metadata == "loader" else (
        importlib.machinery.BuiltinImporter
        if forged_origin == "built-in"
        else importlib.machinery.FrozenImporter
    )
    spec = importlib.machinery.ModuleSpec("forged_special", loader, origin=forged_origin)
    if escape_metadata == "search":
        spec.submodule_search_locations = [str(escaped)]
    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", lambda *args: spec)

    install_snapshot_bootstrap_capability(snapshot)
    with pytest.raises(RuntimeSnapshotError, match="snapshot_import_origin_forbidden"):
        importlib.import_module("forged_special")


@pytest.mark.parametrize(
    "startup_name",
    ["sitecustomize.py", "usercustomize.py", "path-only.pth", "executable.pth"],
)
def test_runtime_binding_rejects_site_startup_files(tmp_path, startup_name):
    site_root = tmp_path / "site"
    site_root.mkdir()
    (site_root / startup_name).write_text("raise RuntimeError('must not run')\n", encoding="utf-8")

    with pytest.raises(RuntimeSnapshotError, match="snapshot_runtime_startup_forbidden"):
        runtime_binding_for_directory("venv_site", site_root)


@pytest.mark.parametrize("startup_name", ["sitecustomize.py", "usercustomize.py", "dangling.pth"])
def test_runtime_binding_rejects_dangling_site_startup_links(tmp_path, startup_name):
    site_root = tmp_path / "site"
    site_root.mkdir()
    (site_root / startup_name).symlink_to(tmp_path / "missing-target")

    with pytest.raises(RuntimeSnapshotError, match="snapshot_runtime_startup_forbidden"):
        runtime_binding_for_directory("venv_site", site_root)


def test_runtime_binding_startup_scan_stays_bound_to_opened_directory(monkeypatch, tmp_path):
    site_root = tmp_path / "site"
    site_root.mkdir()
    moved = tmp_path / "moved-site"
    original_open = snapshot_module._open_directory_no_follow

    def swap_after_open(path):
        descriptor, opened = original_open(path)
        Path(path).rename(moved)
        Path(path).mkdir()
        (moved / "injected.pth").symlink_to(tmp_path / "missing-target")
        return descriptor, opened

    monkeypatch.setattr(snapshot_module, "_open_directory_no_follow", swap_after_open)

    with pytest.raises(RuntimeSnapshotError, match="snapshot_runtime_startup_forbidden"):
        runtime_binding_for_directory("venv_site", site_root)


def test_runtime_binding_payload_carries_inherited_directory_handle(tmp_path):
    site_root = tmp_path / "site"
    site_root.mkdir()
    binding = runtime_binding_for_directory("venv_site", site_root)
    raw = runtime_bindings_json(with_runtime_bindings(_snapshot(tmp_path / "snapshot"), (binding,)))

    inherited = runtime_bindings_from_json(raw)

    assert inherited == (binding,)
    assert runtime_binding_pass_fds(inherited) == (binding.handle,)
    os.fstat(binding.handle)


def test_verified_bundle_rejects_runtime_binding_path_swap(tmp_path):
    snapshot = _snapshot(tmp_path)
    site_root = tmp_path / "site"
    site_root.mkdir()
    snapshot = with_runtime_bindings(
        snapshot,
        (*snapshot.runtime_bindings, runtime_binding_for_directory("venv_site", site_root)),
    )
    bundle = verified_snapshot_bundle(snapshot)
    moved = tmp_path / "moved"
    site_root.rename(moved)
    site_root.mkdir()

    with pytest.raises(RuntimeSnapshotError, match="snapshot_runtime_binding_mismatch"):
        runtime_snapshot_from_verified_bundle(bundle)


@pytest.mark.parametrize("reconstruct", ["json", "bundle"])
def test_child_reconstruction_rescans_site_startup_files(tmp_path, reconstruct):
    snapshot = _snapshot(tmp_path)
    site_root = tmp_path / "site"
    site_root.mkdir()
    binding = runtime_binding_for_directory("venv_site", site_root)
    snapshot = with_runtime_bindings(snapshot, (*snapshot.runtime_bindings, binding))
    raw = runtime_bindings_json(snapshot)
    bundle = verified_snapshot_bundle(snapshot)
    (site_root / "injected.pth").write_text("import attacker\n", encoding="utf-8")

    with pytest.raises(RuntimeSnapshotError, match="snapshot_runtime_startup_forbidden"):
        if reconstruct == "json":
            runtime_bindings_from_json(raw)
        else:
            runtime_snapshot_from_verified_bundle(bundle)


def test_snapshot_runtime_sys_path_has_only_payload_and_ordered_bindings(tmp_path):
    snapshot = _snapshot(tmp_path)
    base_site = tmp_path / "base-site"
    venv_site = tmp_path / "venv-site"
    base_site.mkdir()
    venv_site.mkdir()
    snapshot = with_runtime_bindings(
        snapshot,
        (
            *snapshot.runtime_bindings,
            runtime_binding_for_directory("base_site", base_site),
            runtime_binding_for_directory("venv_site", venv_site),
        ),
    )

    assert snapshot_runtime_sys_path(snapshot) == [
        str(snapshot.payload_root),
        str(snapshot.runtime_bindings[0].path),
        str(base_site.resolve()),
        str(venv_site.resolve()),
    ]


def test_installed_guard_executes_preverified_module_bytes_after_path_mutation(tmp_path):
    snapshot = _snapshot(tmp_path)
    install_snapshot_bootstrap_capability(snapshot)
    target = snapshot.payload_root / "tools/approved.py"
    attacker_marker = tmp_path / "attacker-marker"
    target.chmod(0o600)
    target.write_text(
        f"from pathlib import Path\nPath({str(attacker_marker)!r}).write_text('attacker')\nVALUE='attacker'\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(snapshot.payload_root))
    try:
        module = importlib.import_module("tools.approved")
        assert module.VALUE == "sealed"
        assert not attacker_marker.exists()
    finally:
        sys.path.remove(str(snapshot.payload_root))
        sys.modules.pop("tools.approved", None)


def test_sealed_resource_text_remains_bound_to_preverified_bytes(tmp_path):
    snapshot = _snapshot(tmp_path)
    install_snapshot_bootstrap_capability(snapshot)
    assert sealed_resource_text("locales/en.yaml") == "hello: sealed\n"

    target = snapshot.payload_root / "locales/en.yaml"
    target.chmod(0o600)
    target.write_text("hello: attacker\n", encoding="utf-8")

    assert sealed_resource_text("locales/en.yaml") == "hello: sealed\n"


def test_sealed_python_argv_executes_verified_bytes_after_path_swap(tmp_path):
    snapshot = _snapshot(tmp_path)
    install_snapshot_bootstrap_capability(snapshot)
    sealed_marker = tmp_path / "sealed-marker"
    attacker_marker = tmp_path / "attacker-marker"
    command, args = sealed_python_argv("tools/helper.py")
    target = snapshot.payload_root / "tools/helper.py"
    target.chmod(0o600)
    target.write_text(
        f"from pathlib import Path\nPath({str(attacker_marker)!r}).write_text('attacker')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [command, *args],
        env={**os.environ, "SEALED_MARKER": str(sealed_marker)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert sealed_marker.read_text() == "sealed"
    assert not attacker_marker.exists()


def test_sealed_resource_file_exposes_verified_bytes_after_path_swap(tmp_path):
    snapshot = _snapshot(tmp_path)
    install_snapshot_bootstrap_capability(snapshot)
    with sealed_resource_file("locales/en.yaml") as resource:
        target = snapshot.payload_root / "locales/en.yaml"
        target.chmod(0o600)
        target.write_text("hello: attacker\n", encoding="utf-8")
        assert Path(resource.path).read_bytes() == b"hello: sealed\n"
        assert resource.pass_fds


def test_sealed_python_argv_uses_preverified_bytes_when_path_changes_before_capture(tmp_path):
    snapshot = _snapshot(tmp_path)
    install_snapshot_bootstrap_capability(snapshot)
    sealed_marker = tmp_path / "sealed-marker"
    attacker_marker = tmp_path / "attacker-marker"
    target = snapshot.payload_root / "tools/helper.py"
    target.chmod(0o600)
    target.write_text(
        f"from pathlib import Path\nPath({str(attacker_marker)!r}).write_text('attacker')\n",
        encoding="utf-8",
    )

    command, args = sealed_python_argv("tools/helper.py")
    completed = subprocess.run(
        [command, *args],
        env={**os.environ, "SEALED_MARKER": str(sealed_marker)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert sealed_marker.read_text() == "sealed"
    assert not attacker_marker.exists()


def test_inventory_is_deterministic_and_covers_runtime_resource_consumers(tmp_path):
    snapshot = _snapshot(tmp_path)
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))

    assert manifest["resource_inventory"] == sorted(
        manifest["resource_inventory"], key=lambda row: row["path"].encode("utf-8")
    )
    by_path = {row["path"]: row for row in manifest["resource_inventory"]}
    assert set(by_path) == {
        entry["path"] for entry in manifest["files"] if entry["path"].endswith(".py")
    }
    assert by_path["agent/i18n.py"]["policy"] == "sealed-resource"


@pytest.mark.parametrize(
    "module_name,relative_path",
    [
        ("hermes_cli.reviewer_bootstrap", "hermes_cli/reviewer_bootstrap.py"),
        ("hermes_cli.kanban_windows_spawn", "hermes_cli/kanban_windows_spawn.py"),
        ("tools.reviewer_authority", "tools/reviewer_authority.py"),
        ("tools.reviewer_surface", "tools/reviewer_surface.py"),
        ("tools.review_exec_tool", "tools/review_exec_tool.py"),
        ("tools.omitted", "tools/omitted.py"),
    ],
)
def test_generic_launcher_rejects_post_seal_module_mutation_before_marker(
    tmp_path, module_name, relative_path
):
    snapshot = _snapshot(tmp_path)
    marker = tmp_path / "marker"
    source = tmp_path / "source"
    target = source / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    runtime_root = Path(__file__).resolve().parents[2]
    code = (
        "import importlib,sys;"
        f"sys.path.insert(0,{str(runtime_root)!r});"
        "from hermes_cli.kanban_runtime_snapshot import "
        "install_snapshot_bootstrap_capability,verify_published_snapshot;"
        f"sys.path[:0]=[{str(snapshot.payload_root)!r},{str(source)!r}];"
        f"install_snapshot_bootstrap_capability(verify_published_snapshot({str(snapshot.object_root)!r},{snapshot.manifest_sha256!r},{snapshot.cache_key!r}));"
        f"importlib.import_module({module_name!r})"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        cwd=source,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        capture_output=True,
        check=False,
    )

    if relative_path == "tools/omitted.py":
        assert completed.returncode != 0
        assert (
            "snapshot_import_origin_forbidden" in completed.stderr
            or "No module named 'tools.omitted'" in completed.stderr
        )
    else:
        assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
