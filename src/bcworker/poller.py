"""The polling loop: detect newly assigned to-dos and react to each one."""

from __future__ import annotations

import asyncio
import logging

from .basecamp import BasecampClient, BasecampError, Todo
from .config import ACCEPTED_MESSAGE, COMPLETED_MESSAGE, Config
from .db import STATUS_CLAIMED, Database
from .stub import run_task

logger = logging.getLogger(__name__)


class Poller:
    """Periodically polls Basecamp and dispatches new to-dos to the handler."""

    def __init__(self, config: Config, client: BasecampClient, db: Database):
        self._config = config
        self._client = client
        self._db = db

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
        for todo_id, title, status in pending:
            if stop.is_set():
                return
            # Only the title survived in the DB; use it as the task text.
            await self._drive(todo_id, title, task_text=title, from_status=status)

    async def _poll_once(self, stop: asyncio.Event) -> None:
        """Fetch assigned to-dos and process any that are new."""
        todos = await self._client.assigned_todos()
        for todo in todos:
            if stop.is_set():
                return
            await self._process_todo(todo)

    async def _process_todo(self, todo: Todo) -> None:
        """Claim (dedup guard) and drive one new to-do."""
        if not await self._db.try_claim(todo):
            return  # already processed in a previous cycle or run
        logger.info("Accepted new to-do #%s: %s", todo.id, todo.title)
        await self._drive(todo.id, todo.title, todo.task_text, from_status=STATUS_CLAIMED)

    async def _drive(
        self, todo_id: int, title: str, task_text: str, from_status: str
    ) -> None:
        """Run a to-do to completion from its current status.

        Resumable: a to-do that already reached 'accepted' skips re-posting the
        acceptance comment, so a restart does not duplicate it.
        """
        try:
            if from_status == STATUS_CLAIMED:
                await self._client.create_comment(todo_id, ACCEPTED_MESSAGE)
                await self._db.mark_accepted(todo_id)
            await run_task(task_text)
            await self._client.create_comment(todo_id, COMPLETED_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - record and move on
            logger.exception("Failed to process to-do #%s (%s)", todo_id, title)
            await self._db.mark_error(todo_id, str(exc))
            return

        await self._db.mark_done(todo_id)
        logger.info("Completed to-do #%s", todo_id)

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
        """Sleep for ``seconds`` or wake early if ``stop`` is set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
