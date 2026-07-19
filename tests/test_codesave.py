"""Tests for the code-save lifecycle (comment/boost driven)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bcworker.basecamp import Todo
from bcworker.claude_runner import ClaudeError
from bcworker.codesave import PROMPT_MESSAGE, CodeSaveManager
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
WORKER_ID = "7"
CUSTOMER_ID = "99"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.completed: set[int] = set()
        self.comments: dict[int, list[dict]] = {}  # todo_id -> comments
        self.boosts: dict[int, list[dict]] = {}  # comment_id -> boosts
        self.posted: list[tuple[int, str]] = []
        self._next_id = 5000

    async def whoami(self) -> str | None:
        return WORKER_ID

    async def is_todo_completed(self, todo_id: int, project_id: int) -> bool:
        return todo_id in self.completed

    async def create_comment(
        self, todo_id: int, text: str, project_id: int | None = None
    ) -> int | None:
        self._next_id += 1
        self.comments.setdefault(todo_id, []).append(
            {"id": self._next_id, "creator": {"id": WORKER_ID}, "content": text}
        )
        self.posted.append((todo_id, text))
        return self._next_id

    async def list_comments(self, todo_id: int, project_id: int) -> list[dict]:
        return list(self.comments.get(todo_id, []))

    async def list_boosts(self, recording_id: int, project_id: int) -> list[dict]:
        return list(self.boosts.get(recording_id, []))

    # --- test helpers ---
    def reply(self, todo_id: int, content: str, creator: str = CUSTOMER_ID) -> int:
        self._next_id += 1
        self.comments.setdefault(todo_id, []).append(
            {"id": self._next_id, "creator": {"id": creator}, "content": content}
        )
        return self._next_id

    def boost(self, comment_id: int, content: str, creator: str = CUSTOMER_ID) -> None:
        self.boosts.setdefault(comment_id, []).append(
            {"creator": {"id": creator}, "content": content}
        )


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


def _manager(
    db: Database, client: FakeClient, runner: FakeRunner, *, reply_timeout: int = 604800
) -> CodeSaveManager:
    return CodeSaveManager(
        client,
        db,
        runner,
        Config(basecamp_project_id=PROJECT, code_save_reply_timeout_seconds=reply_timeout),
    )


async def _stage(db: Database, todo_id: int) -> str | None:
    row = db.connection.execute(
        "SELECT stage FROM code_save_flow WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    return row[0] if row else None


async def _armed(db: Database, client: FakeClient, runner: FakeRunner) -> CodeSaveManager:
    """Drive a completed task through completion + prompt, return the manager."""
    client.completed = {1}
    mgr = _manager(db, client, runner)
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))
    return mgr


async def test_completion_opens_flow_after_delay(db: Database) -> None:
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)

    await mgr.tick(T0)

    assert await _stage(db, 1) == FLOW_AWAITING_DELAY
    assert client.posted == []  # prompt not posted until the delay elapses


async def test_incomplete_task_opens_no_flow(db: Database) -> None:
    client = FakeClient()  # nothing completed
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)

    await mgr.tick(T0)

    assert await _stage(db, 1) is None


async def test_prompt_posted_after_delay(db: Database) -> None:
    client = FakeClient()
    mgr = await _armed(db, client, FakeRunner())

    assert await _stage(db, 1) == FLOW_PROMPTS_CREATED
    assert client.posted == [(1, PROMPT_MESSAGE)]
    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    assert flow.prompt_comment_id is not None


async def test_prompt_posting_is_idempotent(db: Database) -> None:
    client = FakeClient()
    mgr = await _armed(db, client, FakeRunner())

    await mgr.tick(T0 + timedelta(seconds=302))

    assert client.posted == [(1, PROMPT_MESSAGE)]  # not re-posted


async def test_prompt_reuses_existing_after_crash(db: Database) -> None:
    # Flow still awaiting_delay (id not persisted), but the prompt comment already
    # exists in Basecamp from a prior attempt: it must be reused, not duplicated.
    client = FakeClient()
    client.completed = {1}
    mgr = _manager(db, client, FakeRunner())
    await _seed_done(db, 1)
    await mgr.tick(T0)
    client.comments[1] = [{"id": 4200, "creator": {"id": WORKER_ID}, "content": PROMPT_MESSAGE}]

    await mgr.tick(T0 + timedelta(seconds=301))

    assert client.posted == []  # reused, not duplicated
    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    assert flow.prompt_comment_id == 4200


async def test_save_via_reply_word(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "так, збережи")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_SAVED
    assert len(runner.resumed) == 1
    assert "results/" in runner.resumed[0][1]


async def test_save_via_reply_emoji(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "👍")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_SAVED


async def test_save_via_boost_on_prompt(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    (flow,) = await db.flows_in_stage(FLOW_PROMPTS_CREATED)
    client.boost(flow.prompt_comment_id, "🎉")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_SAVED


async def test_discard_via_reply(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "ні, не треба")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_DISCARDED
    assert runner.resumed == []


async def test_discard_wins_over_save(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "так")
    client.reply(1, "ой ні, не варто")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_DISCARDED
    assert runner.resumed == []


async def test_unclear_reply_keeps_waiting(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "дякую за роботу")
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_PROMPTS_CREATED  # still open
    assert runner.resumed == []


async def test_worker_own_reply_is_ignored(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "так", creator=WORKER_ID)  # the worker, not the customer
    await mgr.tick(T0 + timedelta(seconds=400))

    assert await _stage(db, 1) == FLOW_PROMPTS_CREATED


async def test_no_reply_times_out_as_discard(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner()
    client.completed = {1}
    mgr = _manager(db, client, runner, reply_timeout=100)
    await _seed_done(db, 1)
    await mgr.tick(T0)
    await mgr.tick(T0 + timedelta(seconds=301))  # prompt posted; deadline = +401

    await mgr.tick(T0 + timedelta(seconds=301 + 101))

    assert await _stage(db, 1) == FLOW_DISCARDED
    assert runner.resumed == []


async def test_save_resume_failure_marks_error(db: Database) -> None:
    client, runner = FakeClient(), FakeRunner(fail=True)
    await _armed(db, client, runner)
    mgr = _manager(db, client, runner)

    client.reply(1, "збережи")
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
