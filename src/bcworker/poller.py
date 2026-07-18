"""The polling loop: detect newly assigned to-dos and react to each one.

Each new to-do is handed to Claude Code (`claude -p`) and its result is posted
back. Because a claude run can take minutes, tasks are dispatched as bounded
background coroutines while the loop keeps ticking to run the lighter periodic
concerns on their own cadence: syncing skills/documents, refreshing CLAUDE.md,
follow-up edits from new customer comments, and the code-save lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .basecamp import BasecampClient, BasecampError, Todo
from .claude_runner import ClaudeRunner, session_id_for
from .codesave import CodeSaveManager
from .config import ACCEPTED_MESSAGE, COMPLETED_MESSAGE, Config
from .db import STATUS_CLAIMED, Database
from .followup import FollowupManager
from .sync import Syncer

logger = logging.getLogger(__name__)


class Poller:
    """Periodically polls Basecamp and dispatches new to-dos to Claude Code."""

    def __init__(
        self,
        config: Config,
        client: BasecampClient,
        db: Database,
        runner: ClaudeRunner,
        syncer: Syncer,
        codesave: CodeSaveManager,
        followup: FollowupManager,
    ):
        self._config = config
        self._client = client
        self._db = db
        self._runner = runner
        self._syncer = syncer
        self._codesave = codesave
        self._followup = followup
        self._sem = asyncio.Semaphore(max(1, config.task_max_concurrency))
        self._inflight: set[asyncio.Task] = set()
        # Follow-up and code-save ticks can invoke claude (minutes long), so they
        # run as background tasks too; keep a handle to avoid overlapping runs.
        self._followup_task: asyncio.Task | None = None
        self._codesave_task: asyncio.Task | None = None
        # Cadence gates (event-loop clock); 0 means "run on the first tick".
        self._next_sync = 0.0
        self._next_claude_md = 0.0
        self._next_comment_poll = 0.0
        self._next_codesave = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        """Run the poll loop until ``stop`` is set.

        The loop never crashes on a single failed cycle or to-do: errors are
        logged and processing continues on the next tick.
        """
        if not await self._wait_until_ready(stop):
            return  # stopped before we became ready

        await self._recover_pending(stop)

        interval = self._config.poll_interval_seconds
        while not stop.is_set():
            try:
                await self._poll_once(stop)
            except BasecampError:
                logger.warning("Poll cycle failed; will retry", exc_info=True)
            except Exception:  # noqa: BLE001 - keep the loop alive on any error
                logger.exception("Unexpected error during poll cycle")

            await self._sleep_or_stop(stop, interval)

        await self._await_inflight()

    async def _wait_until_ready(self, stop: asyncio.Event) -> bool:
        """Block until authenticated and an account is resolved.

        Returns True once ready, or False if ``stop`` was set first.
        """
        while not stop.is_set():
            try:
                if not await self._client.is_authenticated():
                    logger.error(
                        "Basecamp CLI is not authenticated. Run the one-time login: "
                        "`docker compose run --rm auth` "
                        "(basecamp auth login --device-code --scope full). Retrying..."
                    )
                else:
                    account_id = await self._client.ensure_account()
                    logger.info("Ready: authenticated, using account %s", account_id)
                    return True
            except BasecampError:
                logger.error("Could not resolve Basecamp account; retrying", exc_info=True)

            await self._sleep_or_stop(stop, self._config.poll_interval_seconds)
        return False

    async def _recover_pending(self, stop: asyncio.Event) -> None:
        """Resume to-dos left interrupted by a previous run/restart."""
        pending = await self._db.pending_todos()
        if not pending:
            return
        logger.info("Resuming %d interrupted to-do(s)", len(pending))
        for todo in pending:
            if stop.is_set():
                return
            await self._drive(
                todo.todo_id,
                todo.title,
                todo.task_text,
                self._project_id(todo.bucket_id),
                from_status=todo.status,
            )

    async def _poll_once(self, stop: asyncio.Event) -> None:
        """Fetch assigned to-dos, dispatch new ones, and run periodic concerns."""
        todos = self._filter_project(await self._client.assigned_todos())
        child_ids = await self._db.child_todo_ids()
        for todo in todos:
            if stop.is_set():
                return
            if todo.id in child_ids:
                continue  # a code-save under-to-do, never a real task
            await self._process_todo(todo)

        await self._run_periodic(stop)

    def _filter_project(self, todos: list[Todo]) -> list[Todo]:
        """Keep only to-dos in the configured project (no filtering if unset)."""
        pid = self._config.basecamp_project_id
        if not pid:
            return todos
        return [t for t in todos if t.bucket_id == pid]

    def _project_id(self, bucket_id: int) -> int:
        return bucket_id or self._config.basecamp_project_id

    async def _run_periodic(self, stop: asyncio.Event) -> None:
        """Run sync / CLAUDE.md refresh / follow-up / code-save on their cadences.

        Sync is short (bounded by the Basecamp CLI timeout) and runs inline. The
        follow-up and code-save ticks can each invoke ``claude`` for minutes, so
        they are launched as background tasks and skipped while a prior run is
        still in flight — the poll loop must stay responsive to new to-dos.
        """
        pid = self._config.basecamp_project_id
        now = asyncio.get_running_loop().time()

        if pid and now >= self._next_sync:
            try:
                await self._syncer.sync_project(pid)
            except BasecampError:
                logger.warning("Docs & Files sync failed; will retry", exc_info=True)
            self._next_sync = now + self._config.sync_interval_seconds

        if pid and now >= self._next_claude_md:
            try:
                await self._syncer.sync_claude_md(pid)
            except BasecampError:
                logger.warning("CLAUDE.md refresh failed; will retry", exc_info=True)
            self._next_claude_md = now + self._config.claude_md_refresh_seconds

        if now >= self._next_comment_poll:
            self._next_comment_poll = now + self._config.comment_poll_seconds
            if self._followup_task is None or self._followup_task.done():
                self._followup_task = self._track(asyncio.create_task(self._followup.tick()))

        if now >= self._next_codesave:
            self._next_codesave = now + self._config.codesave_poll_seconds
            if self._codesave_task is None or self._codesave_task.done():
                self._codesave_task = self._track(
                    asyncio.create_task(self._codesave.tick(datetime.now(UTC)))
                )

    async def _process_todo(self, todo: Todo) -> None:
        """Claim (dedup guard) and dispatch one new to-do to the background."""
        if not await self._db.try_claim(todo):
            return  # already processed in a previous cycle or run
        logger.info("Accepted new to-do #%s: %s", todo.id, todo.title)
        self._dispatch(
            todo.id, todo.title, todo.task_text, self._project_id(todo.bucket_id), STATUS_CLAIMED
        )

    def _dispatch(
        self, todo_id: int, title: str, task_text: str, project_id: int, from_status: str
    ) -> None:
        """Start a to-do's processing as a bounded background task."""
        self._track(
            asyncio.create_task(
                self._run_guarded(todo_id, title, task_text, project_id, from_status)
            )
        )

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        """Track a background task and log any exception it raises."""
        self._inflight.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Background task failed", exc_info=exc)

    async def _run_guarded(
        self, todo_id: int, title: str, task_text: str, project_id: int, from_status: str
    ) -> None:
        async with self._sem:
            await self._drive(todo_id, title, task_text, project_id, from_status)

    async def _drive(
        self, todo_id: int, title: str, task_text: str, project_id: int, from_status: str
    ) -> None:
        """Run a to-do to completion from its current status.

        Resumable: a to-do that already reached 'accepted' skips re-posting the
        acceptance comment, so a restart does not duplicate it.

        NOTE (verify at build): the session id is deterministic, so a to-do that
        crashed mid-run is re-driven here with the same ``--session-id``. Confirm
        the real ``claude`` CLI treats a re-used ``--session-id`` as resume (not a
        hard collision); if it errors, recovery should switch to ``resume``.
        """
        session_id = session_id_for(todo_id)
        try:
            if from_status == STATUS_CLAIMED:
                await self._client.create_comment(todo_id, ACCEPTED_MESSAGE, project_id)
                await self._db.mark_accepted(todo_id)
            await self._db.set_session_id(todo_id, session_id)
            result = await self._runner.run_task(task_text, session_id)
            await self._client.create_comment(todo_id, result or COMPLETED_MESSAGE, project_id)
        except Exception as exc:  # noqa: BLE001 - record and move on
            logger.exception("Failed to process to-do #%s (%s)", todo_id, title)
            await self._db.mark_error(todo_id, str(exc))
            return

        await self._db.mark_done(todo_id, result=result or None)
        logger.info("Completed to-do #%s", todo_id)

    async def _await_inflight(self) -> None:
        """Wait for any dispatched task coroutines to finish."""
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
        """Sleep for ``seconds`` or wake early if ``stop`` is set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
