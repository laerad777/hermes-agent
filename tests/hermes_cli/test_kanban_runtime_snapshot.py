from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_runtime_snapshot as snapshot_module
from hermes_cli.kanban_runtime_snapshot import (
    RuntimeSnapshotError,
    build_runtime_snapshot,
    manifest_resource_bytes,
    verify_published_snapshot,
)


def test_locale_bijection_uses_captured_i18n_bytes(monkeypatch, tmp_path):
    source = _source(tmp_path / "source")
    original_iter = snapshot_module.iter_selected_entries
    calls = 0

    def mutate_after_capture(root):
        nonlocal calls
        entries = list(original_iter(root))
        calls += 1
        if calls == 2:
            (source / "agent/i18n.py").write_text(
                "SUPPORTED_LANGUAGES = {'fr': 'French'}\n",
                encoding="utf-8",
            )
        yield from entries

    monkeypatch.setattr(snapshot_module, "iter_selected_entries", mutate_after_capture)

    snapshot = build_runtime_snapshot(
        source,
        repository_id="repo",
        source_revision="a" * 40,
        source_dirty=True,
        cache_root=tmp_path / "cache",
    )

    assert manifest_resource_bytes(snapshot, "agent/i18n.py") == (
        b"SUPPORTED_LANGUAGES = {'en': 'English', 'ko': 'Korean'}\n"
    )


def _source(root: Path) -> Path:
    files = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/main.py": "VALUE = 'sealed'\n",
        "tools/__init__.py": "",
        "tools/omitted.py": "VALUE = 'approved'\n",
        "agent/__init__.py": "",
        "agent/i18n.py": "SUPPORTED_LANGUAGES = {'en': 'English', 'ko': 'Korean'}\n",
        "locales/en.yaml": "hello: Hello\n",
        "locales/ko.yaml": "hello: 안녕\n",
        "package.json": '{"engines":{"npm":">=10"}}\n',
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _build(tmp_path: Path):
    return build_runtime_snapshot(
        _source(tmp_path / "source"),
        repository_id="repo",
        source_revision="a" * 40,
        source_dirty=True,
        cache_root=tmp_path / "cache",
    )


def test_snapshot_manifest_is_deterministic_and_covers_locales(tmp_path):
    first = _build(tmp_path)
    second = build_runtime_snapshot(
        tmp_path / "source",
        repository_id="repo",
        source_revision="a" * 40,
        source_dirty=True,
        cache_root=tmp_path / "other-cache",
    )

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.cache_key == second.cache_key
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert set(by_path) >= {"locales/en.yaml", "locales/ko.yaml", "package.json", "tools/omitted.py"}
    assert by_path["locales/ko.yaml"]["sha256"] == hashlib.sha256("hello: 안녕\n".encode()).hexdigest()
    assert not ({"created_at", "source_root", "pid", "run_id"} & manifest.keys())


def test_manifest_resource_stays_bound_to_verified_bytes_after_path_mutation(tmp_path):
    snapshot = _build(tmp_path)
    captured = manifest_resource_bytes(snapshot, "locales/en.yaml")
    target = snapshot.payload_root / "locales/en.yaml"
    target.chmod(0o600)
    target.write_text("hello: attacker\n", encoding="utf-8")

    assert captured == b"hello: Hello\n"
    assert manifest_resource_bytes(snapshot, "locales/en.yaml") == captured


def test_selected_symlink_hardlink_and_native_files_fail_closed(tmp_path):
    source = _source(tmp_path / "source")
    outside = tmp_path / "outside.py"
    outside.write_text("x", encoding="utf-8")
    (source / "tools/link.py").symlink_to(outside)
    with pytest.raises(RuntimeSnapshotError, match="source_link"):
        build_runtime_snapshot(source, repository_id="r", source_revision="a" * 40, source_dirty=False, cache_root=tmp_path / "c1")

    (source / "tools/link.py").unlink()
    os.link(source / "tools/omitted.py", source / "tools/hard.py")
    with pytest.raises(RuntimeSnapshotError, match="source_link"):
        build_runtime_snapshot(source, repository_id="r", source_revision="a" * 40, source_dirty=False, cache_root=tmp_path / "c2")

    (source / "tools/hard.py").unlink()
    (source / "tools/native.so").write_bytes(b"native")
    with pytest.raises(RuntimeSnapshotError, match="first_party_native_unsupported"):
        build_runtime_snapshot(source, repository_id="r", source_revision="a" * 40, source_dirty=False, cache_root=tmp_path / "c3")


def test_verified_snapshot_import_ignores_post_seal_workspace_mutation(tmp_path):
    snapshot = _build(tmp_path)
    source_module = tmp_path / "source/tools/omitted.py"
    marker = tmp_path / "marker"
    source_module.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('attacker')\nVALUE='attacker'\n",
        encoding="utf-8",
    )
    verify_published_snapshot(snapshot.object_root, snapshot.manifest_sha256, snapshot.cache_key)
    code = "import tools.omitted as m; print(m.VALUE)"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", f"import sys;sys.path.insert(0,{str(snapshot.payload_root)!r});{code}"],
        cwd=tmp_path / "source",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "approved"
    assert not marker.exists()


def test_import_spec_executes_bytes_verified_before_path_swap(tmp_path):
    snapshot = _build(tmp_path)
    capability = snapshot_module.install_snapshot_bootstrap_capability(snapshot)
    marker = tmp_path / "attacker-ran"

    guard = snapshot_module._SnapshotImportGuard(capability)
    spec = guard.find_spec("tools.omitted", [str(snapshot.payload_root / "tools")])
    assert spec is not None and spec.loader is not None

    target = snapshot.payload_root / "tools/omitted.py"
    target.chmod(0o600)
    target.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nVALUE='attacker'\n",
        encoding="utf-8",
    )
    module = __import__("types").ModuleType("tools.omitted")
    module.__spec__ = spec
    module.__file__ = spec.origin
    spec.loader.exec_module(module)

    assert module.VALUE == "approved"
    assert not marker.exists()


def test_missing_required_locale_fails_before_publication(tmp_path):
    source = _source(tmp_path / "source")
    (source / "locales/ko.yaml").unlink()
    with pytest.raises(RuntimeSnapshotError, match="snapshot_locale_missing"):
        build_runtime_snapshot(source, repository_id="r", source_revision="a" * 40, source_dirty=False, cache_root=tmp_path / "cache")
    assert not list((tmp_path / "cache/objects").glob("*")) if (tmp_path / "cache/objects").exists() else True


def test_verify_rejects_unmanifested_payload_file(tmp_path):
    snapshot = _build(tmp_path)
    added = snapshot.payload_root / "tools/late.py"
    snapshot.object_root.chmod(0o700)
    snapshot.payload_root.chmod(0o700)
    added.parent.chmod(0o700)
    added.write_text("VALUE = 'late'\n", encoding="utf-8")
    added.chmod(0o400)
    with pytest.raises(RuntimeSnapshotError, match="snapshot_content_mismatch"):
        verify_published_snapshot(snapshot.object_root, snapshot.manifest_sha256, snapshot.cache_key)
