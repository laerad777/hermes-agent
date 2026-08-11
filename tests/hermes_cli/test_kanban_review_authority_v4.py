"""Behavior contracts for kernel-owned review authority v4."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _review_task(conn, workspace: Path, *, role: str = "critic", body: str = "") -> str:
    task_id = kb.create_task(
        conn,
        title=f"{role} review",
        body=body,
        assignee=role,
        workspace_kind="dir",
        workspace_path=str(workspace),
        workflow_template_id="jerome-kanban-v1",
        current_step_key=role,
    )
    assert kb.claim_task(conn, task_id, claimer=role) is not None
    kb.bind_review_run_authority(conn, task_id, workspace)
    return task_id


def _metadata(comment_id: int, revision: str) -> dict:
    return {
        "durable_comment_id": comment_id,
        "durable_comment_read_back": True,
        "inspected_revision": revision,
        "inspected_symbols": ["e\u0301vidence"],
        "red_tests": ["production command RED"],
    }


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def _init_repo_with_submodule(
    tmp_path: Path,
    *,
    linked: bool,
    ignore_in_local_config: bool,
    logical_name: str = "dependency",
    physical_path: str = "dependency",
) -> tuple[Path, Path, str]:
    submodule_source = tmp_path / "submodule-source"
    _init_repo(submodule_source)
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            "--name", logical_name, str(submodule_source), physical_path,
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qam", "add submodule"], cwd=repo, check=True)
    if ignore_in_local_config:
        subprocess.run(
            ["git", "config", f"submodule.{logical_name}.ignore", "all"],
            cwd=repo,
            check=True,
        )
    else:
        subprocess.run(
            [
                "git", "config", "-f", ".gitmodules",
                f"submodule.{logical_name}.ignore", "all",
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore submodule dirt"], cwd=repo, check=True)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    workspace = repo
    if linked:
        workspace = tmp_path / "linked"
        subprocess.run(
            ["git", "worktree", "add", "-q", str(workspace), "-b", "linked-submodule"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                "git", "-c", "protocol.file.allow=always", "submodule", "update",
                "--init", "-q",
            ],
            cwd=workspace,
            check=True,
        )
    return workspace, workspace / physical_path, head


def _add_nested_submodule(tmp_path: Path, submodule: Path) -> Path:
    nested_source = tmp_path / "nested-source"
    _init_repo(nested_source)
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            str(nested_source), "nested-dependency",
        ],
        cwd=submodule,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qam", "add nested submodule"], cwd=submodule, check=True
    )
    return submodule / "nested-dependency"


def _add_named_nested_submodule(tmp_path: Path, submodule: Path) -> Path:
    nested_source = tmp_path / "named-nested-source"
    _init_repo(nested_source)
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            "--name", "nested-logical", str(nested_source), "deps/nested-physical",
        ],
        cwd=submodule,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qam", "add named nested submodule"],
        cwd=submodule,
        check=True,
    )
    return submodule / "deps" / "nested-physical"


def _complete_review(conn, task_id: str) -> bool:
    task = kb.get_task(conn, task_id)
    assert task is not None
    authority = kb.get_review_run_authority(conn, task.current_run_id)
    assert authority is not None
    revision = authority["inspected_revision"]
    assert isinstance(revision, str)
    summary = "CLEAR" if task.current_step_key == "architect" else "OKAY"
    comment_id = kb.add_comment(
        conn,
        task_id,
        author=task.assignee or "critic",
        body="bound evidence",
        expected_run_id=task.current_run_id,
        reviewer_profile=task.assignee or "critic",
    )
    return kb.complete_task(
        conn,
        task_id,
        summary=summary,
        metadata=_metadata(comment_id, revision),
        expected_run_id=task.current_run_id,
    )


def test_non_git_authority_is_insert_only_and_kernel_owned(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        task = kb.get_task(conn, task_id)
        assert task is not None
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        assert authority["authority_mode"] == "trusted_workspace_snapshot_v1"
        assert authority["threat_model"] == "trusted workspace snapshot; no concurrent hostile filesystem mutation"
        assert authority["subject_kind"] == "task_snapshot_v1"
        assert authority["inspected_revision"] == authority["subject_digest"]
        with pytest.raises(kb.ReviewAuthorityError, match="already bound"):
            kb.bind_review_run_authority(conn, task_id, workspace)


def test_real_normal_and_linked_worktree_capture_exact_head(kanban_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", str(linked), "-b", "linked-test"], cwd=repo, check=True)
    git_file = linked / ".git"
    admin = Path(git_file.read_text(encoding="utf-8").split(":", 1)[1].strip())
    assert (admin / "commondir").read_text(encoding="utf-8") == "../..\n"

    with kb.connect_closing() as conn:
        normal_id = _review_task(conn, repo)
        linked_id = _review_task(conn, linked, role="architect")
        for task_id in (normal_id, linked_id):
            task = kb.get_task(conn, task_id)
            authority = kb.get_review_run_authority(conn, task.current_run_id)
            assert authority["subject_kind"] == "git_workspace_head_v1"
            assert authority["inspected_revision"] == head
            assert authority["workspace_realpath"] == str(Path(task.workspace_path).resolve())
            assert authority["common_dir_realpath"] == str((repo / ".git").resolve())
            assert _complete_review(conn, task_id)


@pytest.mark.parametrize("linked", [False, True], ids=["normal-superproject", "linked-superproject"])
def test_real_submodule_workspace_can_bind_and_complete(
    kanban_home: Path, tmp_path: Path, linked: bool
) -> None:
    _workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=linked, ignore_in_local_config=False
    )
    submodule_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=submodule, text=True
    ).strip()
    git_dir = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=submodule, text=True
        ).strip()
    ).resolve()

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, submodule)
        task = kb.get_task(conn, task_id)
        assert task is not None
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        assert authority is not None
        assert authority["inspected_revision"] == submodule_head
        assert authority["git_dir_realpath"] == str(git_dir)
        assert authority["common_dir_realpath"] == str(git_dir)
        assert _complete_review(conn, task_id)


@pytest.mark.parametrize("linked", [False, True], ids=["normal", "linked"])
def test_named_submodule_workspace_can_bind_and_complete(
    kanban_home: Path, tmp_path: Path, linked: bool
) -> None:
    _workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path,
        linked=linked,
        ignore_in_local_config=False,
        logical_name="logical-name",
        physical_path="deps/physical",
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, submodule)
        task = kb.get_task(conn, task_id)
        assert task is not None
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        assert authority is not None
        expected_git_dir = subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=submodule, text=True
        ).strip()
        assert authority["git_dir_realpath"] == str(Path(expected_git_dir).resolve())
        assert _complete_review(conn, task_id)


def test_nested_named_submodule_workspace_can_bind_and_complete(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path,
        linked=False,
        ignore_in_local_config=False,
        logical_name="outer-logical",
        physical_path="deps/outer-physical",
    )
    nested = _add_named_nested_submodule(tmp_path, submodule)
    subprocess.run(["git", "add", "deps/outer-physical"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "advance named outer submodule"],
        cwd=workspace,
        check=True,
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, nested)
        assert _complete_review(conn, task_id)


def test_named_submodule_rejects_admin_mapping_mismatch(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path,
        linked=False,
        ignore_in_local_config=False,
        logical_name="logical-name",
        physical_path="deps/physical",
    )
    subprocess.run(
        [
            "git", "config", "-f", ".gitmodules",
            "submodule.logical-name.path", "deps/forged",
        ],
        cwd=workspace,
        check=True,
    )

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule|relationship"):
            _review_task(conn, submodule)


def test_named_submodule_rejects_untracked_gitmodules_after_committed_copy_is_deleted(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path,
        linked=False,
        ignore_in_local_config=False,
        logical_name="logical-name",
        physical_path="deps/physical",
    )
    committed = (workspace / ".gitmodules").read_bytes()
    subprocess.run(["git", "rm", "-q", ".gitmodules"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "remove submodule mapping"],
        cwd=workspace,
        check=True,
    )
    (workspace / ".gitmodules").write_bytes(committed)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule"):
            _review_task(conn, submodule)


@pytest.mark.parametrize(
    "mutation",
    [
        "staged_replacement",
        "worktree_modified",
        "worktree_missing",
        "worktree_symlink",
        "worktree_mode_change",
    ],
)
def test_named_submodule_rejects_gitmodules_outside_committed_regular_blob(
    kanban_home: Path, tmp_path: Path, mutation: str
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path,
        linked=False,
        ignore_in_local_config=False,
        logical_name="logical-name",
        physical_path="deps/physical",
    )
    gitmodules = workspace / ".gitmodules"
    committed = gitmodules.read_bytes()
    if mutation == "staged_replacement":
        gitmodules.write_bytes(committed + b"\n# staged replacement\n")
        subprocess.run(["git", "add", ".gitmodules"], cwd=workspace, check=True)
    elif mutation == "worktree_modified":
        gitmodules.write_bytes(committed + b"\n# worktree modification\n")
    elif mutation == "worktree_missing":
        gitmodules.unlink()
    elif mutation == "worktree_mode_change":
        gitmodules.chmod(0o755)
    else:
        target = workspace / "gitmodules-target"
        target.write_bytes(committed)
        gitmodules.unlink()
        gitmodules.symlink_to(target)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule"):
            _review_task(conn, submodule)


def test_submodule_workspace_ignores_git_environment_and_core_worktree(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    unrelated = tmp_path / "unrelated"
    _init_repo(unrelated)
    subprocess.run(
        ["git", "config", "core.worktree", str(unrelated)], cwd=submodule, check=True
    )
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, submodule)
        (submodule / "workspace-only.txt").write_text("changed\n", encoding="utf-8")
        with pytest.raises(
            kb.RoleCompletionContractError, match="review authority workspace changed"
        ):
            _complete_review(conn, task_id)


def test_arbitrary_gitdir_file_is_not_accepted_as_a_submodule_workspace(
    kanban_home: Path, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    _init_repo(repository)
    workspace = tmp_path / "forged-workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(
        f"gitdir: {repository / '.git'}\n", encoding="utf-8"
    )

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="Git workspace .git file"):
            _review_task(conn, workspace)


def test_separate_git_dir_under_modules_is_not_accepted_as_a_submodule(
    kanban_home: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    superproject = tmp_path / "superproject"
    _init_repo(superproject)
    (superproject / ".git" / "modules").mkdir()
    forged = superproject / "forged"
    subprocess.run(
        [
            "git", "clone", "-q",
            f"--separate-git-dir={superproject / '.git' / 'modules' / 'forged'}",
            str(source), str(forged),
        ],
        check=True,
    )
    assert not subprocess.check_output(
        ["git", "ls-files", "--stage", "--", "forged"],
        cwd=superproject,
    )

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule"):
            _review_task(conn, forged)


def test_removed_submodule_index_entry_is_not_accepted(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    subprocess.run(["git", "rm", "--cached", "dependency"], cwd=workspace, check=True)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule"):
            _review_task(conn, submodule)


def test_deinitialized_submodule_workspace_is_not_accepted(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    preserved_workspace = tmp_path / "deinitialized-workspace"
    submodule.rename(preserved_workspace)
    subprocess.run(
        ["git", "submodule", "deinit", "-f", "dependency"], cwd=workspace, check=True
    )
    preserved_workspace.rename(submodule)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="registered submodule"):
            _review_task(conn, submodule)


def test_deinitialized_submodule_rejects_forged_canonical_repo_with_wrong_head(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    expected_gitlink = subprocess.check_output(
        ["git", "rev-parse", ":dependency"], cwd=workspace, text=True
    ).strip()
    forged_source = tmp_path / "forged-source"
    _init_repo(forged_source)
    (forged_source / "tracked.txt").write_text("forged\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "forged"], cwd=forged_source, check=True)
    forged_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=forged_source, text=True
    ).strip()
    assert forged_head != expected_gitlink

    subprocess.run(
        ["git", "submodule", "deinit", "-f", "dependency"], cwd=workspace, check=True
    )
    shutil.rmtree(workspace / ".git" / "modules" / "dependency")
    subprocess.run(
        [
            "git", "clone", "-q",
            f"--separate-git-dir={workspace / '.git' / 'modules' / 'dependency'}",
            str(forged_source), str(submodule),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "config", "submodule.dependency.url", str(forged_source)],
        cwd=workspace,
        check=True,
    )

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="gitlink.*HEAD|HEAD.*gitlink"):
            _review_task(conn, submodule)


def test_submodule_bind_rejects_clean_checkout_that_differs_from_index_gitlink(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    original = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=submodule, text=True
    ).strip()
    (submodule / "tracked.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "second"], cwd=submodule, check=True)
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=submodule
    ) == b""
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=submodule, text=True
    ).strip() != original

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="gitlink.*HEAD|HEAD.*gitlink"):
            _review_task(conn, submodule)


@pytest.mark.parametrize(
    "records",
    [
        b"160000 " + b"a" * 40 + b" 1\tdependency\0",
        b"160000 " + b"a" * 40 + b" 0\tdependency\0"
        b"160000 " + b"a" * 40 + b" 0\tdependency\0",
        b"160000 " + b"a" * 40 + b" 0\tdependency\0"
        b"160000 " + b"b" * 40 + b" 2\tdependency\0",
    ],
    ids=["no-stage-zero", "duplicate-stage-zero", "conflicted-multi-stage"],
)
def test_gitlink_index_parser_rejects_non_unique_stage_zero_records(records: bytes) -> None:
    with pytest.raises(kb.ReviewAuthorityError, match="stage-0"):
        kb._parse_gitlink_index_oid(records, b"dependency", 40)


def test_gitlink_index_parser_preserves_path_bytes_and_nul_record_boundaries() -> None:
    path = os.fsencode("dependencies/라이브러리")
    oid = b"a" * 64
    assert kb._parse_gitlink_index_oid(
        b"160000 " + oid + b" 0\t" + path + b"\0", path, 64
    ) == oid
    with pytest.raises(kb.ReviewAuthorityError, match="stage-0|malformed"):
        kb._parse_gitlink_index_oid(
            b"160000 " + oid + b" 0\t" + path + b"\0suffix\0", path, 64
        )


def test_nested_submodule_workspace_can_bind_and_complete(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    nested = _add_nested_submodule(tmp_path, submodule)
    subprocess.run(["git", "add", "dependency"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "advance submodule pointer"], cwd=workspace, check=True
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, nested)
        assert _complete_review(conn, task_id)


def test_submodule_workspace_with_multi_component_path_can_bind_and_complete(
    kanban_home: Path, tmp_path: Path
) -> None:
    source = tmp_path / "path-source"
    _init_repo(source)
    superproject = tmp_path / "path-superproject"
    _init_repo(superproject)
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            str(source), "dependencies/library",
        ],
        cwd=superproject,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qam", "add path submodule"], cwd=superproject, check=True
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, superproject / "dependencies" / "library")
        assert _complete_review(conn, task_id)


def test_submodule_registration_check_does_not_execute_gitmodules_update_command(
    kanban_home: Path, tmp_path: Path
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=False, ignore_in_local_config=False
    )
    marker = tmp_path / "gitmodules-command-ran"
    subprocess.run(
        [
            "git", "config", "-f", ".gitmodules",
            "submodule.dependency.update", f"!touch {marker}",
        ],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "add", ".gitmodules"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "configure malicious update command"],
        cwd=workspace,
        check=True,
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, submodule)
        assert _complete_review(conn, task_id)
    assert not marker.exists()


def test_unchanged_dirty_git_snapshot_can_complete(kanban_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("review me\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, repo)
        task = kb.get_task(conn, task_id)
        assert task is not None
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        assert authority is not None
        assert authority["git_snapshot_digest"]
        assert _complete_review(conn, task_id)


@pytest.mark.parametrize("linked", [False, True], ids=["normal", "linked"])
@pytest.mark.parametrize(
    "ignore_in_local_config", [False, True], ids=["gitmodules-ignore", "local-ignore"]
)
def test_unchanged_ignored_submodule_can_complete(
    kanban_home: Path,
    tmp_path: Path,
    linked: bool,
    ignore_in_local_config: bool,
) -> None:
    workspace, _submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=linked, ignore_in_local_config=ignore_in_local_config
    )

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        assert _complete_review(conn, task_id)


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked", "nested"])
@pytest.mark.parametrize("linked", [False, True], ids=["normal", "linked"])
@pytest.mark.parametrize(
    "ignore_in_local_config", [False, True], ids=["gitmodules-ignore", "local-ignore"]
)
def test_authority_bind_rejects_already_dirty_ignored_submodule(
    kanban_home: Path,
    tmp_path: Path,
    dirty_kind: str,
    linked: bool,
    ignore_in_local_config: bool,
) -> None:
    workspace, submodule, _head = _init_repo_with_submodule(
        tmp_path, linked=linked, ignore_in_local_config=ignore_in_local_config
    )
    dirty_repo = submodule
    if dirty_kind == "tracked":
        (submodule / "tracked.txt").write_text("dirty before bind\n", encoding="utf-8")
    elif dirty_kind == "untracked":
        (submodule / "untracked.txt").write_text("dirty before bind\n", encoding="utf-8")
    else:
        dirty_repo = _add_nested_submodule(tmp_path, submodule)
        subprocess.run(["git", "add", "dependency"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "advance submodule pointer"], cwd=workspace, check=True
        )
        (dirty_repo / "tracked.txt").write_text(
            "nested dirty before bind\n", encoding="utf-8"
        )

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="dirty submodule"):
            _review_task(conn, workspace)


@pytest.mark.parametrize("linked", [False, True], ids=["normal", "linked"])
@pytest.mark.parametrize(
    "ignore_in_local_config", [False, True], ids=["gitmodules-ignore", "local-ignore"]
)
@pytest.mark.parametrize(
    "mutation",
    [
        "tracked_modify", "tracked_add", "tracked_delete", "untracked_add",
        "head_change", "nested_modify",
    ],
)
def test_completion_rejects_ignored_submodule_mutation_with_unchanged_superproject_head(
    kanban_home: Path,
    tmp_path: Path,
    linked: bool,
    ignore_in_local_config: bool,
    mutation: str,
) -> None:
    workspace, submodule, head = _init_repo_with_submodule(
        tmp_path, linked=linked, ignore_in_local_config=ignore_in_local_config
    )
    nested = None
    if mutation == "nested_modify":
        nested = _add_nested_submodule(tmp_path, submodule)
        subprocess.run(["git", "add", "dependency"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "advance submodule pointer"], cwd=workspace, check=True
        )
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
        ).strip()

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        if mutation == "tracked_modify":
            (submodule / "tracked.txt").write_text("modified\n", encoding="utf-8")
        elif mutation == "tracked_add":
            (submodule / "added.txt").write_text("added\n", encoding="utf-8")
            subprocess.run(["git", "add", "added.txt"], cwd=submodule, check=True)
        elif mutation == "tracked_delete":
            (submodule / "tracked.txt").unlink()
        elif mutation == "untracked_add":
            (submodule / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        elif mutation == "nested_modify":
            assert nested is not None
            (nested / "tracked.txt").write_text("nested dirty\n", encoding="utf-8")
        else:
            (submodule / "tracked.txt").write_text("next commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=submodule, check=True)
            subprocess.run(["git", "commit", "-qm", "advance submodule"], cwd=submodule, check=True)

        assert subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
        ).strip() == head
        with pytest.raises(
            kb.RoleCompletionContractError, match="review authority workspace changed"
        ):
            _complete_review(conn, task_id)


@pytest.mark.parametrize(
    "mutation",
    [
        "tracked_modify",
        "staged_change",
        "untracked_add",
        "untracked_remove",
        "rename",
        "mode_change",
    ],
)
def test_completion_rejects_git_working_snapshot_mutation_with_unchanged_head(
    kanban_home: Path, tmp_path: Path, mutation: str
) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    if mutation == "untracked_remove":
        (repo / "remove-me.txt").write_text("present at bind\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, repo)
        if mutation == "tracked_modify":
            (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        elif mutation == "staged_change":
            (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        elif mutation == "untracked_add":
            (repo / "new.txt").write_text("new\n", encoding="utf-8")
        elif mutation == "untracked_remove":
            (repo / "remove-me.txt").unlink()
        elif mutation == "rename":
            (repo / "tracked.txt").rename(repo / "renamed.txt")
        else:
            (repo / "tracked.txt").chmod(0o755)

        assert subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip() == head
        with pytest.raises(
            kb.RoleCompletionContractError, match="review authority workspace changed"
        ):
            _complete_review(conn, task_id)


@pytest.mark.parametrize("mutation", ["add", "change", "remove"])
def test_completion_rejects_ignored_file_mutation_with_unchanged_head(
    kanban_home: Path, tmp_path: Path, mutation: str
) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    (repo / ".gitignore").write_text("ignored-proof.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore proof"], cwd=repo, check=True)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    ignored = repo / "ignored-proof.txt"
    if mutation != "add":
        ignored.write_text("bound\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, repo)
        if mutation == "add":
            ignored.write_text("added\n", encoding="utf-8")
        elif mutation == "change":
            ignored.write_text("changed\n", encoding="utf-8")
        else:
            ignored.unlink()

        assert subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip() == head
        if mutation != "remove":
            assert subprocess.check_output(
                ["git", "status", "--porcelain", "--ignored"], cwd=repo
            ).startswith(b"!! ignored-proof.txt")
        with pytest.raises(
            kb.RoleCompletionContractError, match="review authority workspace changed"
        ):
            _complete_review(conn, task_id)


def test_git_snapshot_fails_closed_when_content_bound_is_exceeded(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "large.bin").write_bytes(b"x" * 32)
    monkeypatch.setattr(kb, "_REVIEW_GIT_SNAPSHOT_MAX_BYTES", 16)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="snapshot byte limit"):
            _review_task(conn, repo)


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "message"),
    [
        ("_REVIEW_GIT_SNAPSHOT_MAX_ITEMS", 0, "snapshot item limit"),
        ("_REVIEW_GIT_SNAPSHOT_MAX_SECONDS", -1.0, "snapshot time limit"),
    ],
)
def test_git_snapshot_fails_closed_when_item_or_time_bound_is_exceeded(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int | float,
    message: str,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(kb, limit_name, limit_value)

    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match=message):
            _review_task(conn, repo)


@pytest.mark.parametrize("config_scope", ["local", "global"])
def test_git_snapshot_does_not_execute_configured_fsmonitor(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_scope: str,
) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    marker = tmp_path / f"{config_scope}-fsmonitor-ran"
    helper = tmp_path / f"{config_scope}-fsmonitor"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    if config_scope == "local":
        subprocess.run(
            ["git", "config", "core.fsmonitor", str(helper)], cwd=repo, check=True
        )
    else:
        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(
            ["git", "config", "--file", str(home / ".gitconfig"), "core.fsmonitor", str(helper)],
            check=True,
        )
        monkeypatch.setenv("HOME", str(home))

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, repo)
        task = kb.get_task(conn, task_id)
        assert task is not None
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        assert authority is not None
        assert authority["inspected_revision"] == head
        assert _complete_review(conn, task_id)
    assert not marker.exists()


@pytest.mark.parametrize("config_scope", ["local", "global"])
def test_git_snapshot_ignores_configured_core_worktree(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_scope: str,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    alternate = tmp_path / "alternate"
    _init_repo(alternate)
    (alternate / "alternate-only.txt").write_text("alternate\n", encoding="utf-8")
    if config_scope == "local":
        subprocess.run(
            ["git", "config", "core.worktree", str(alternate)], cwd=repo, check=True
        )
    else:
        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(
            [
                "git", "config", "--file", str(home / ".gitconfig"),
                "core.worktree", str(alternate),
            ],
            check=True,
        )
        monkeypatch.setenv("HOME", str(home))

    with kb.connect_closing() as conn:
        task_id = _review_task(conn, repo)
        (repo / "workspace-only.txt").write_text("workspace\n", encoding="utf-8")
        with pytest.raises(
            kb.RoleCompletionContractError, match="review authority workspace changed"
        ):
            _complete_review(conn, task_id)


@pytest.mark.live_system_guard_bypass
def test_git_snapshot_subprocesses_share_one_wall_clock_deadline(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_git = subprocess.check_output(["command", "-v", "git"], text=True).strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        f"#!/bin/sh\nsleep 0.06\nexec {real_git} \"$@\"\n", encoding="utf-8"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(kb, "_REVIEW_GIT_SNAPSHOT_MAX_SECONDS", 0.1)

    started = time.monotonic()
    with kb.connect_closing() as conn:
        with pytest.raises(kb.ReviewAuthorityError, match="time limit"):
            _review_task(conn, repo)
    assert time.monotonic() - started < 0.75


def test_run_bound_comment_and_authority_are_required_for_completion(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        task = kb.get_task(conn, task_id)
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        old = kb.add_comment(conn, task_id, author="critic", body="old manual evidence")
        with pytest.raises(kb.RoleCompletionContractError, match="current review run"):
            kb.complete_task(conn, task_id, summary="OKAY", metadata=_metadata(old, authority["inspected_revision"]))

        comment_id = kb.add_comment(
            conn,
            task_id,
            author="critic",
            body="bound evidence",
            expected_run_id=task.current_run_id,
            reviewer_profile="critic",
        )
        assert kb.complete_task(
            conn,
            task_id,
            summary="OKAY",
            metadata=_metadata(comment_id, authority["inspected_revision"]),
            expected_run_id=task.current_run_id,
        )
        run = kb.latest_run(conn, task_id)
        persisted = run.metadata["review_authority"]
        assert persisted["authority_mode"] == "trusted_workspace_snapshot_v1"
        assert persisted["run_id"] == task.current_run_id
        assert "fd_bound" not in json.dumps(persisted)
        assert "toctou_safe" not in json.dumps(persisted)


def test_subject_mutation_is_blocked_during_bound_review(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        with pytest.raises(kb.ReviewAuthorityError, match="active review"):
            kb.update_task_subject(conn, task_id, title="changed")
        with pytest.raises(Exception, match="active review subject is immutable"):
            conn.execute("UPDATE tasks SET body='changed' WHERE id=?", (task_id,))
        task = kb.get_task(conn, task_id)
        assert task.title == "critic review"
        assert task.body == ""
        assert task.subject_version == 0


def test_subject_change_before_review_increments_version_once(kanban_home: Path, tmp_path: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old", body="old", assignee="critic")
        assert kb.update_task_subject(conn, task_id, title="new", body="new")
        task = kb.get_task(conn, task_id)
        assert task.subject_version == 1
        assert kb.update_task_subject(conn, task_id, title="new", body="new")
        assert kb.get_task(conn, task_id).subject_version == 1


@pytest.mark.parametrize("bad", ["\ud800", "x\x00y", "x\ny", "x\u202ey", "x\u200dy"])
def test_review_metadata_rejects_unsafe_unicode(kanban_home: Path, tmp_path: Path, bad: str) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        task = kb.get_task(conn, task_id)
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        comment_id = kb.add_comment(conn, task_id, author="critic", body="bound", expected_run_id=task.current_run_id, reviewer_profile="critic")
        metadata = _metadata(comment_id, authority["inspected_revision"])
        metadata["red_tests"] = [bad]
        with pytest.raises(kb.RoleCompletionContractError, match="review provenance"):
            kb.complete_task(conn, task_id, summary="OKAY", metadata=metadata, expected_run_id=task.current_run_id)


def test_review_metadata_enforces_count_item_and_aggregate_bounds(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        task = kb.get_task(conn, task_id)
        authority = kb.get_review_run_authority(conn, task.current_run_id)
        comment_id = kb.add_comment(conn, task_id, author="critic", body="bound", expected_run_id=task.current_run_id, reviewer_profile="critic")
        base = _metadata(comment_id, authority["inspected_revision"])
        for value in ([], ["x"] * 257, ["x" * 1025], ["x" * 1024] * 65):
            metadata = dict(base)
            metadata["inspected_symbols"] = value
            with pytest.raises(kb.RoleCompletionContractError, match="review provenance"):
                kb.complete_task(conn, task_id, summary="OKAY", metadata=metadata, expected_run_id=task.current_run_id)


def test_spawn_failure_removes_authority_and_old_run_cannot_complete(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        old_run = kb.get_task(conn, task_id).current_run_id
        kb._record_spawn_failure(conn, task_id, "boom", failure_limit=2)
        assert kb.get_review_run_authority(conn, old_run) is None
        assert kb.get_task(conn, task_id).current_run_id is None
        assert kb.claim_task(conn, task_id, claimer="critic") is not None
        kb.bind_review_run_authority(conn, task_id, workspace)
        new_run = kb.get_task(conn, task_id).current_run_id
        assert new_run != old_run
        assert not kb.complete_task(conn, task_id, summary="OKAY", metadata={}, expected_run_id=old_run)


def test_caller_cannot_forge_kernel_review_authority(kanban_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = _review_task(conn, workspace)
        with pytest.raises(kb.ReservedRunMetadataError, match="review_authority"):
            kb.complete_task(conn, task_id, summary="OKAY", metadata={"review_authority": {"authority_mode": "fd_bound"}})


def test_generic_and_markerless_completion_remain_compatible(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="generic", assignee="critic")
        assert kb.claim_task(conn, task_id, claimer="critic") is not None
        assert kb.complete_task(conn, task_id, summary="ordinary completion", metadata={})
