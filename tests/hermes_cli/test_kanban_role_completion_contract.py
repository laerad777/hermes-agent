"""Behavior contracts for typed Kanban workflow completion receipts."""

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _review_provenance(conn, task_id: str, *, revision: str = "a" * 40) -> dict:
    task = kb.get_task(conn, task_id)
    assert task is not None
    authority = kb.get_review_run_authority(conn, task.current_run_id)
    if authority is None:
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task_id, workspace)
        authority = kb.bind_review_run_authority(conn, task_id, workspace)
    comment_id = kb.add_comment(
        conn,
        task_id,
        author=task.assignee or "reviewer",
        body="Inspected the requested contracts and recorded durable evidence.",
        expected_run_id=task.current_run_id,
        reviewer_profile=task.assignee,
    )
    return {
        "durable_comment_id": comment_id,
        "durable_comment_read_back": True,
        "inspected_revision": authority["inspected_revision"],
        "inspected_symbols": ["hermes_cli.kanban_db.complete_task"],
        "red_tests": ["empty metadata is rejected atomically"],
    }


def test_typed_architect_clear_rejects_empty_metadata_atomically(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(
            conn,
            title="architecture review",
            assignee="architect",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="architect",
        )
        child = kb.create_task(
            conn,
            title="executor",
            assignee="executor",
            parents=[parent],
        )
        assert kb.claim_task(conn, parent, claimer="architect") is not None
        run_before = kb.latest_run(conn, parent)
        events_before = kb.list_events(conn, parent)

        with pytest.raises(
            kb.RoleCompletionContractError,
            match="review provenance",
        ):
            kb.complete_task(conn, parent, summary="CLEAR", metadata={})

        task = kb.get_task(conn, parent)
        run_after = kb.latest_run(conn, parent)
        assert task is not None
        assert run_before is not None
        assert run_after is not None
        assert task.status == "running"
        assert task.result is None
        assert task.completed_at is None
        assert run_after.id == run_before.id
        assert run_after.status == "running"
        assert run_after.ended_at is None
        assert kb.list_events(conn, parent) == events_before
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "todo"


def test_typed_critic_okay_rejects_session_stamp_only_atomically(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="critic review",
            assignee="critic",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="critic",
        )
        assert kb.claim_task(conn, task_id, claimer="critic") is not None
        run_before = kb.latest_run(conn, task_id)

        with pytest.raises(
            kb.RoleCompletionContractError,
            match="review provenance",
        ):
            kb.complete_task(
                conn,
                task_id,
                summary="OKAY",
                metadata={"worker_session_id": "session-only"},
            )

        task = kb.get_task(conn, task_id)
        run_after = kb.latest_run(conn, task_id)
        assert task is not None
        assert run_before is not None
        assert run_after is not None
        assert task.status == "running"
        assert run_after.id == run_before.id
        assert run_after.ended_at is None


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("durable_comment_id", True, "positive durable_comment_id"),
        ("durable_comment_id", 999_999, "same task"),
        ("durable_comment_read_back", 1, "read_back=true"),
        ("inspected_revision", "not-a-revision", "hexadecimal"),
        ("inspected_symbols", [], "inspected_symbols"),
        ("inspected_symbols", ["   "], "inspected_symbols"),
        ("red_tests", "not-a-list", "red_tests"),
    ],
)
def test_typed_review_rejects_malformed_provenance_atomically(
    kanban_home: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="critic review",
            assignee="critic",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="critic",
        )
        assert kb.claim_task(conn, task_id, claimer="critic") is not None
        metadata = _review_provenance(conn, task_id)
        metadata[field] = value
        events_before = kb.list_events(conn, task_id)

        with pytest.raises(kb.RoleCompletionContractError, match=error):
            kb.complete_task(conn, task_id, summary="OKAY", metadata=metadata)

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task is not None
        assert run is not None
        assert task.status == "running"
        assert run.ended_at is None
        assert kb.list_events(conn, task_id) == events_before


def test_typed_review_rejects_foreign_comment_id(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="critic review",
            assignee="critic",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="critic",
        )
        other = kb.create_task(conn, title="other", assignee="worker")
        assert kb.claim_task(conn, task_id, claimer="critic") is not None
        metadata = _review_provenance(conn, task_id)
        metadata["durable_comment_id"] = kb.add_comment(
            conn, other, author="reviewer", body="foreign evidence"
        )

        with pytest.raises(kb.RoleCompletionContractError, match="same task"):
            kb.complete_task(conn, task_id, summary="OKAY", metadata=metadata)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"


@pytest.mark.parametrize(
    ("role", "verdict"),
    [("architect", "CLEAR"), ("critic", "OKAY")],
)
def test_typed_review_accepts_bound_provenance_and_preserves_metadata(
    kanban_home: Path,
    role: str,
    verdict: str,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(
            conn,
            title=f"{role} review",
            assignee=role,
            workflow_template_id="jerome-kanban-v1",
            current_step_key=role,
        )
        child = kb.create_task(
            conn, title="consumer", assignee="worker", parents=[parent]
        )
        assert kb.claim_task(conn, parent, claimer=role) is not None
        metadata = _review_provenance(conn, parent)
        expected = json.loads(json.dumps(metadata))

        assert kb.complete_task(
            conn, parent, summary=verdict, metadata=metadata
        )

        run = kb.latest_run(conn, parent)
        completed = [
            event for event in kb.list_events(conn, parent)
            if event.kind == "completed"
        ][-1]
        assert run is not None
        assert completed.payload is not None
        assert metadata == expected
        assert run.metadata is not None
        assert {key: run.metadata[key] for key in expected} == expected
        assert run.metadata["review_authority"]["authority_mode"] == "trusted_workspace_snapshot_v1"
        assert completed.payload["metadata"] == run.metadata
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "ready"
        context = kb.build_worker_context(conn, child)
        for field in expected:
            assert field in context


def test_architect_block_verdict_completes_as_review_artifact(
    kanban_home: Path,
) -> None:
    """Architect BLOCK is a durable finding, not dependency success."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review unsafe design",
            assignee="architect",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="architect",
        )
        assert kb.claim_task(conn, task_id, claimer="architect") is not None

        assert kb.complete_task(
            conn,
            task_id,
            summary="BLOCK\nThe proposed design violates the storage boundary.",
            metadata={
                **_review_provenance(conn, task_id),
                "findings": ["storage boundary violation"],
            },
            expected_run_id=kb.get_task(conn, task_id).current_run_id,
        )

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.result.startswith("BLOCK\n")
        run = kb.latest_run(conn, task_id)
        review_event = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "review_blocked"
        ][-1]
        assert run is not None
        assert review_event.payload is not None
        assert run.status == "blocked"
        assert run.outcome == "review_blocked"
        assert run.summary == task.result
        assert run.metadata is not None
        assert run.metadata["findings"] == ["storage boundary violation"]
        assert review_event.payload["metadata"] == run.metadata


@pytest.mark.parametrize(
    ("workflow_template_id", "body"),
    [
        ("jerome-kanban-v1", None),
        (None, "WORKFLOW_CONTRACT: jerome-kanban-v1"),
    ],
)
def test_architect_block_receipt_keeps_child_dependency_gated(
    kanban_home: Path,
    workflow_template_id: str | None,
    body: str | None,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(
            conn,
            title="architecture review",
            body=body,
            assignee="architect",
            workflow_template_id=workflow_template_id,
            current_step_key="architect",
        )
        child = kb.create_task(
            conn,
            title="executor",
            assignee="executor",
            parents=[parent],
        )
        assert kb.claim_task(conn, parent, claimer="architect") is not None

        metadata = _review_provenance(conn, parent)
        assert kb.complete_task(
            conn, parent, summary="BLOCK\nunsafe boundary", metadata=metadata
        )

        assert kb.get_task(conn, parent).status == "blocked"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        with pytest.raises(
            kb.RoleCompletionContractError,
            match="explicitly unblocked",
        ):
            kb.complete_task(
                conn, parent, summary="BLOCK\nunsafe boundary", metadata=metadata
            )


def test_typed_completion_rejects_conflicting_result_and_summary_atomically(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="architecture review",
            assignee="architect",
            workflow_template_id="jerome-kanban-v1",
            current_step_key="architect",
        )
        assert kb.claim_task(conn, task_id, claimer="architect") is not None
        run_before = kb.latest_run(conn, task_id)

        with pytest.raises(kb.RoleCompletionContractError, match="conflicting"):
            kb.complete_task(conn, task_id, result="BLOCK", summary="WATCH")

        task = kb.get_task(conn, task_id)
        run_after = kb.latest_run(conn, task_id)
        assert task.status == "running"
        assert task.result is None
        assert run_after.id == run_before.id
        assert run_after.status == "running"
        assert run_after.outcome is None
        assert not any(
            event.kind in {"completed", "review_blocked"}
            for event in kb.list_events(conn, task_id)
        )


def test_generic_block_text_completion_still_releases_child(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="generic", assignee="worker")
        child = kb.create_task(
            conn,
            title="child",
            assignee="worker",
            parents=[parent],
        )
        assert kb.claim_task(conn, parent, claimer="worker") is not None

        assert kb.complete_task(conn, parent, summary="BLOCK")

        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, child).status == "ready"


def test_verifier_metadata_reaches_gate_and_all_handoff_surfaces(
    kanban_home: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    metadata = {
        "reviewed_commit": revision,
        "integration_head": revision,
        "reviewer_identity": "independent-verifier",
        "author_identity": "executor",
        "verification": [
            {
                "command": "scripts/run_tests.sh tests/hermes_cli/test_kanban_role_completion_contract.py",
                "result": "passed",
                "exit_code": 0,
                "head": revision,
            }
        ],
        "generic": {"nested": [1, {"ok": True}]},
    }
    expected = {key: value for key, value in metadata.items()}

    with kb.connect_closing() as conn:
        parent = kb.create_task(
            conn,
            title="verification",
            assignee="verifier",
            workspace_kind="dir",
            workspace_path=str(repo),
            workflow_template_id="jerome-kanban-v1",
            current_step_key="verifier",
        )
        child = kb.create_task(
            conn,
            title="consume verification",
            assignee="integrator",
            parents=[parent],
        )
        assert kb.claim_task(conn, parent, claimer="verifier") is not None

        assert kb.complete_task(conn, parent, summary="WATCH", metadata=metadata)

        run = kb.latest_run(conn, parent)
        completed = [e for e in kb.list_events(conn, parent) if e.kind == "completed"][-1]
        parent_task = kb.get_task(conn, parent)
        child_task = kb.get_task(conn, child)
        assert run is not None
        assert completed.payload is not None
        assert parent_task is not None
        assert child_task is not None
        assert metadata == expected
        assert run.metadata == expected
        assert completed.payload["metadata"] == expected
        assert parent_task.status == "done"
        assert child_task.status == "ready"
        context = kb.build_worker_context(conn, child)
        for key in ("reviewed_commit", "integration_head", "generic"):
            assert key in context


@pytest.mark.parametrize("metadata", [["not", "an", "object"], "string"])
def test_complete_task_rejects_non_object_metadata_without_state_change(
    kanban_home: Path,
    metadata: object,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="generic", assignee="worker")
        assert kb.claim_task(conn, task_id, claimer="worker") is not None

        with pytest.raises(TypeError, match="metadata must be an object/dict"):
            kb.complete_task(conn, task_id, summary="done", metadata=metadata)  # type: ignore[arg-type]

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task is not None
        assert run is not None
        assert task.status == "running"
        assert run.ended_at is None
