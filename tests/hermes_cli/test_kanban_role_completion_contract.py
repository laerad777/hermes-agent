"""Behavior contracts for typed Kanban workflow completion receipts."""

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
            metadata={"findings": ["storage boundary violation"]},
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
        assert run.metadata == {"findings": ["storage boundary violation"]}
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

        assert kb.complete_task(conn, parent, summary="BLOCK\nunsafe boundary")

        assert kb.get_task(conn, parent).status == "blocked"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"
        with pytest.raises(
            kb.RoleCompletionContractError,
            match="explicitly unblocked",
        ):
            kb.complete_task(conn, parent, summary="BLOCK\nunsafe boundary")


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
    revision = "606be246522e5715b7b55ea6800ef892df02b195"
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
            workspace_path=str(Path(__file__).resolve().parents[2]),
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
