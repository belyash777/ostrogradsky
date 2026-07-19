"""Tests for the code-save lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bcworker.basecamp import Todo
from bcworker.claude_runner import ClaudeError
from bcworker.codesave import CodeSaveManager, _discard_title, _save_title
from bcworker.config import Config
from bcworker.db import (
    FLOW_AWAITING_DELAY,
    FLOW_DISCARDED,
    FLOW_ERROR,
    FLOW_PROMPTS_CREATED,
    FLOW_SAVED,
    FLOW_SAVING,
    Database,
)

PROJECT = 55
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.completed: set[int] = set()
        self.created: list[tuple[int, str]] = []
        self._todos: list[Todo] = []
        self._next_id = 1000

    async def is_todo_completed(self, todo_id: int, project_id: int) -> bool:
        return todo_id in self.completed

    async def list_todos(self, project_id: int) -> list[Todo]:
        return list(self._todos)

    async def create_todo(
        self,
        project_id: int,
        content: str,
        list_id: int | None = None,
        assignee_ids: list[int] | None = None,
    ) -> int:
        self._next_id += 1
        self.created.append((project_id, content))
        self._todos.append(Todo(id=self._next_id, title=content, bucket_id=project_id))
        return self._next_id


class FakeRunner:
    def __init__(self, fail: bool = False) -> None:
        self.resumed: list[tuple[str, str]] = []
        self.fail = fail

    async def resume(self, session_id: str, instruction: str) -> str:
        self.resumed.append((session_id, instruction))
        if self.fail:
            raise ClaudeError("resume failed")
        return "saved"


async def _seed_done(db: Database, todo_id: int) -> None:
    await db.try_claim(Todo(id=todo_id, title="T", bucket_id=PROJECT))
    await db.mark_accepted(todo_id)
    await db.mark_done(todo_id)


def _manager(db: Database, client: FakeClient, runner: FakeRunner) -> CodeSaveManager:
    return CodeSaveManager(client, db, runner, Config(basecamp_project_id=PROJECT))


async def _stage(db: Database, todo_id: int) -> str | None:
    row = db.connection.execute(
        "SELECT stage FROM code_save_flow WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    return row[0] if row else None


async def test_completion_opens_flow_after_delay(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)

    await mgr.tick(T0)

    assert await _stage(db, 1) == FLOW_AWAITING_DELAY
    assert client.created == []  # prompt not created until the delay elapses


async def test_incomplete_task_opens_no_flow(db: Database) -> None:
    client = FakeClient()  # nothing completed
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)

    await mgr.tick(T0)

    assert await _stage(db, 1) is None


async def test_prompts_created_after_delay(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)
    await mgr.tick(T0)

    await mgr.tick(T0 + timedelta(seconds=301))

    assert await _stage(db, 1) == FLOW_PROMPTS_CREATED
    assert client.created == [(PROJECT, _save_title(1)), (PROJECT, _discard_title(1))]


async def test_prompt_creation_is_idempotent(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)
    await mgr.tick(T0)

    await mgr.tick(T0 + timedelta(seconds=301))
    await mgr.tick(T0 + timedelta(seconds=302))

    assert client.created == [(PROJECT, _save_title(1)), (PROJECT, _discard_title(1))]


async def test_prompt_creation_reuses_existing_after_crash(db: Database) -> None:
    # Simulate a crash: the flow is still awaiting_delay (ids not persisted), but
    # the two under-to-dos already exist in Basecamp from the prior attempt.
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)
    await mgr.tick(T0)
    client._todos = [
        Todo(id=701, title=_save_title(1), bucket_id=PROJECT),
        Todo(id=702, title=_discard_title(1), bucket_id=PROJECT),
    ]

    await mgr.tick(T0 + timedelta(seconds=301))

    assert client.created == []  # reused, not duplicated
    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    assert (flow.save_todo_id, flow.discard_todo_id) == (701, 702)


async def test_save_decision_resumes_session(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))

    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    client.completed.add(flow.save_todo_id)
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_SAVED
    assert len(runner.resumed) == 1
    assert runner.resumed[0][0] == flow.session_id
    assert "results/" in runner.resumed[0][1]


async def test_discard_wins_when_both_completed(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))

    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    client.completed.update({flow.save_todo_id, flow.discard_todo_id})
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_DISCARDED
    assert runner.resumed == []  # code was NOT saved


async def test_child_todo_ids_are_tracked(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))

    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    ids = await db.child_todo_ids()
    assert ids == {flow.save_todo_id, flow.discard_todo_id}


async def test_save_resume_failure_marks_error(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    runner = FakeRunner(fail=True)
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))

    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    client.completed.add(flow.save_todo_id)
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_ERROR


async def test_stuck_saving_flow_is_retried(db: Database) -> None:
    client = FakeClient()
    runner = FakeRunner()
    mgr = _manager(db, client, runner)
    # A flow interrupted mid-resume: stage left at 'saving'.
    await db.create_flow(1, PROJECT, "sess-1", "2026-01-01T00:00:00+00:00")
    await db.set_flow_stage(1, FLOW_SAVING)

    await mgr.tick(T0)

    assert await _stage(db, 1) == FLOW_SAVED
    assert runner.resumed == [("sess-1", runner.resumed[0][1])]
