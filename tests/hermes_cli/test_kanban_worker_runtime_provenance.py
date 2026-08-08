from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_runtime_snapshot import (
    RuntimeSnapshotError,
    clear_snapshot_bootstrap_capability_after_fork,
    install_snapshot_bootstrap_capability,
    runtime_binding_for_directory,
)


@pytest.fixture(autouse=True)
def _clear_capability():
    clear_snapshot_bootstrap_capability_after_fork()
    yield
    clear_snapshot_bootstrap_capability_after_fork()


def _task(*, task_id: str = "t_runtime", status: str = "running") -> kb.Task:
    return kb.Task(
        id=task_id,
        title="runtime provenance",
        body=None,
        assignee="default",
        status=status,
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _use_clean_runtime_bindings(monkeypatch, tmp_path: Path) -> None:
    stdlib = tmp_path / "stdlib"
    site = tmp_path / "site-packages"
    stdlib.mkdir(parents=True)
    site.mkdir()
    bindings = (
        runtime_binding_for_directory("stdlib", stdlib),
        runtime_binding_for_directory("venv_site", site),
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_runtime_snapshot.dispatcher_runtime_bindings",
        lambda: bindings,
    )


def _git_hermes_source(root: Path) -> Path:
    root.mkdir(parents=True)
    files = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/main.py": "VALUE = 'sealed'\n",
        "hermes_cli/kanban_db.py": "VALUE = 'approved dirty'\n",
        "hermes_cli/kanban_runtime_snapshot.py": "# snapshot bootstrap\n",
        "tools/__init__.py": "",
        "tools/kanban_tools.py": "VALUE = 'approved'\n",
        "agent/__init__.py": "",
        "agent/i18n.py": "SUPPORTED_LANGUAGES = {'en': 'English'}\n",
        "locales/en.yaml": "hello: Hello\n",
        "package.json": "{}\n",
        "hermes_bootstrap.py": "# bootstrap\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"],
        check=True,
    )
    return root


def test_same_repository_workspace_uses_sealed_snapshot_without_pythonpath(monkeypatch, tmp_path):
    workspace = _git_hermes_source(tmp_path / "repo")
    identity = kb._git_checkout_identity(workspace)
    monkeypatch.setattr(kb, "_dispatcher_runtime_identity", lambda: identity)
    monkeypatch.setattr(kb, "_runtime_snapshot_cache_root", lambda: tmp_path / "cache")
    _use_clean_runtime_bindings(monkeypatch, tmp_path / "runtime-bindings")
    monkeypatch.setenv("PYTHONPATH", "/mutable/runtime")

    spec = kb._worker_runtime_spec(_task(), workspace)

    assert spec.argv[:4] == [kb.sys.executable, "-I", "-S", "-c"]
    assert spec.snapshot is not None
    assert spec.snapshot.payload_root != workspace
    assert spec.snapshot.payload_root.is_relative_to(tmp_path / "cache")
    assert "PYTHONPATH" not in spec.env
    assert "HERMES_KANBAN_RUNTIME_ATTESTATION" not in spec.env
    assert "HERMES_KANBAN_RUNTIME_ROOT" not in spec.env
    assert spec.snapshot.runtime_bindings
    assert spec.snapshot.runtime_bindings[0].role == "stdlib"
    assert spec.snapshot.runtime_bindings[0].path.name == "stdlib"
    assert [binding.role for binding in spec.snapshot.runtime_bindings] == [
        "stdlib", "venv_site",
    ]
    assert spec.pass_fds == tuple(
        dict.fromkeys(binding.handle for binding in spec.snapshot.runtime_bindings)
    )
    assert all(os.fstat(descriptor) for descriptor in spec.pass_fds)
    assert spec.argv[5] == str(spec.snapshot.object_root)
    assert json.loads(spec.argv[8])[0]["role"] == "stdlib"
    assert json.loads(spec.argv[8])[0]["handle"] == spec.snapshot.runtime_bindings[0].handle


def test_post_seal_workspace_mutation_cannot_execute(monkeypatch, tmp_path):
    workspace = _git_hermes_source(tmp_path / "repo")
    identity = kb._git_checkout_identity(workspace)
    monkeypatch.setattr(kb, "_dispatcher_runtime_identity", lambda: identity)
    monkeypatch.setattr(kb, "_runtime_snapshot_cache_root", lambda: tmp_path / "cache")
    _use_clean_runtime_bindings(monkeypatch, tmp_path / "runtime-bindings")
    spec = kb._worker_runtime_spec(_task(), workspace)
    marker = tmp_path / "attacker-ran"
    (workspace / "tools/kanban_tools.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nVALUE='attacker'\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            kb.sys.executable,
            "-I",
            "-S",
            "-c",
            f"import sys;sys.path.insert(0,{str(spec.snapshot.payload_root)!r});import tools.kanban_tools as m;print(m.VALUE)",
        ],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "approved"
    assert not marker.exists()


def test_generic_workspace_preserves_installed_runtime(monkeypatch, tmp_path):
    workspace = tmp_path / "generic"
    workspace.mkdir()
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["/installed/hermes"])

    spec = kb._worker_runtime_spec(_task(), workspace)

    assert spec.argv == ["/installed/hermes"]
    assert spec.env == {}
    assert spec.snapshot is None


def test_symlinked_workspace_alias_never_activates_snapshot(monkeypatch, tmp_path):
    workspace = _git_hermes_source(tmp_path / "repo")
    alias = tmp_path / "repo-link"
    alias.symlink_to(workspace, target_is_directory=True)
    identity = kb._git_checkout_identity(workspace)
    monkeypatch.setattr(kb, "_dispatcher_runtime_identity", lambda: identity)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["/installed/hermes"])

    assert kb._worker_runtime_spec(_task(), alias).argv == ["/installed/hermes"]


def test_selected_symlink_fails_closed_instead_of_falling_back(monkeypatch, tmp_path):
    workspace = _git_hermes_source(tmp_path / "repo")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE='attacker'\n", encoding="utf-8")
    target = workspace / "tools/kanban_tools.py"
    target.unlink()
    target.symlink_to(outside)
    identity = kb._git_checkout_identity(workspace)
    monkeypatch.setattr(kb, "_dispatcher_runtime_identity", lambda: identity)
    monkeypatch.setattr(kb, "_runtime_snapshot_cache_root", lambda: tmp_path / "cache")
    _use_clean_runtime_bindings(monkeypatch, tmp_path / "runtime-bindings")

    with pytest.raises(RuntimeSnapshotError, match="source_link"):
        kb._worker_runtime_spec(_task(), workspace)


def test_forged_diagnostic_environment_does_not_activate_provenance(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RUNTIME_ROOT", "/attacker")
    monkeypatch.setenv("HERMES_KANBAN_SNAPSHOT_CACHE_KEY", "f" * 64)
    monkeypatch.setenv("HERMES_KANBAN_RUNTIME_ATTESTATION", "{}")

    assert kb._verify_worker_runtime_provenance() is None
    assert kb.record_worker_runtime_provenance() is None


def _installed_capability(monkeypatch, tmp_path):
    workspace = _git_hermes_source(tmp_path / "repo")
    identity = kb._git_checkout_identity(workspace)
    monkeypatch.setattr(kb, "_dispatcher_runtime_identity", lambda: identity)
    monkeypatch.setattr(kb, "_runtime_snapshot_cache_root", lambda: tmp_path / "cache")
    _use_clean_runtime_bindings(monkeypatch, tmp_path / "runtime-bindings")
    spec = kb._worker_runtime_spec(_task(), workspace)
    return install_snapshot_bootstrap_capability(spec.snapshot)


def test_runtime_receipt_comes_from_installed_capability(monkeypatch, tmp_path):
    capability = _installed_capability(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_RUNTIME_ROOT", "/attacker")

    receipt = kb._verify_worker_runtime_provenance(capability=capability)

    assert receipt["snapshot_cache_key"] == capability.spec.cache_key
    assert receipt["manifest_sha256"] == capability.spec.manifest_sha256
    assert receipt["runtime_root"] == str(capability.spec.payload_root)
    with pytest.raises(kb.WorkerRuntimeMismatch, match="capability mismatch"):
        kb._verify_worker_runtime_provenance(capability=object())


def test_runtime_receipt_is_persisted_and_survives_review_block(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board(slug="default", name="Test")
    capability = _installed_capability(monkeypatch, tmp_path / "runtime")

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="receipt", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        receipt = kb.record_worker_runtime_provenance(capability=capability)
        assert receipt is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="review-required: inspect sealed runtime",
            expected_run_id=claimed.current_run_id,
        )
        row = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
        assert json.loads(row["metadata"])["runtime_provenance"] == receipt
        event = [event for event in kb.list_events(conn, task_id) if event.kind == "runtime_provenance"][-1]
        assert event.payload == receipt


def test_complete_rejects_runtime_provenance_collision_atomically(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board(slug="default", name="Test")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="reserved", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        authoritative = {"snapshot_cache_key": "trusted"}
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"runtime_provenance": authoritative}), claimed.current_run_id),
            )

        with pytest.raises(kb.ReservedRunMetadataError, match="runtime_provenance"):
            kb.complete_task(
                conn,
                task_id,
                summary="done",
                metadata={"runtime_provenance": {"snapshot_cache_key": "caller"}},
                expected_run_id=claimed.current_run_id,
            )

        assert kb.get_task(conn, task_id).status == "running"
