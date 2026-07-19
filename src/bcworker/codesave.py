"""The post-completion "save the code?" lifecycle.

When the customer completes a task, five minutes later the worker creates two
under-to-dos — "Зберегти код" / "Не зберігати код" — and watches which one the
customer completes. Basecamp has no buttons, so completing an under-to-do is the
signal. If the customer picks "save" (and not "discard"), the task's claude
session is resumed with an instruction to store the query/analysis script it used
in ``results/`` (with an index entry in ``results/INDEX.md``) for reuse on similar
future tasks.

``tick`` is idempotent and takes the current time explicitly, so it can be
driven on a slow cadence from the poll loop and unit-tested with an injected
clock. Per-flow failures are recorded and never crash the loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .basecamp import BasecampClient, BasecampError
from .claude_runner import ClaudeError, ClaudeRunner, session_id_for
from .config import Config
from .db import (
    FLOW_AWAITING_DELAY,
    FLOW_DISCARDED,
    FLOW_PROMPTS_CREATED,
    FLOW_SAVED,
    FLOW_SAVING,
    Database,
)

logger = logging.getLogger(__name__)

# Under-to-do titles (Ukrainian: they are text for the customer). The task id is
# appended so the prompt names its task and creation stays idempotent (a crash
# between the two `create_todo` calls will not duplicate them).
SAVE_TITLE = "Зберегти код"
DISCARD_TITLE = "Не зберігати код"


def _save_title(todo_id: int) -> str:
    return f"{SAVE_TITLE} (#{todo_id})"


def _discard_title(todo_id: int) -> str:
    return f"{DISCARD_TITLE} (#{todo_id})"


class CodeSaveManager:
    """Detects task completion and drives the save/discard under-to-dos."""

    def __init__(
        self,
        client: BasecampClient,
        db: Database,
        runner: ClaudeRunner,
        config: Config,
    ):
        self._client = client
        self._db = db
        self._runner = runner
        self._config = config

    def _project_id(self, bucket_id: int) -> int:
        return bucket_id or self._config.basecamp_project_id

    async def tick(self, now: datetime) -> None:
        """Advance every code-save flow by one step (safe to call repeatedly)."""
        await self._detect_completions(now)
        await self._create_prompts(now)
        await self._resolve_decisions()
        await self._resume_stuck_saves()

    async def _detect_completions(self, now: datetime) -> None:
        """Open a flow for each finished task the customer has now completed."""
        due = (now + timedelta(seconds=self._config.code_save_delay_seconds)).isoformat()
        for todo_id, bucket_id, _last_comment in await self._db.active_done_todos():
            project_id = self._project_id(bucket_id)
            try:
                if await self._client.is_todo_completed(todo_id, project_id):
                    await self._db.create_flow(
                        todo_id, project_id, session_id_for(todo_id), due
                    )
                    logger.info("Task #%s completed by customer; code-save armed", todo_id)
            except BasecampError:
                logger.warning("Completion check failed for #%s", todo_id, exc_info=True)

    async def _create_prompts(self, now: datetime) -> None:
        """After the delay, post the two under-to-dos for a flow (idempotently)."""
        for flow in await self._db.flows_in_stage(FLOW_AWAITING_DELAY):
            if datetime.fromisoformat(flow.prompt_due_at) > now:
                continue  # not due yet
            save_title = _save_title(flow.todo_id)
            discard_title = _discard_title(flow.todo_id)
            try:
                # Reuse existing under-to-dos if a prior attempt created them but
                # crashed before persisting their ids.
                existing = {t.title: t.id for t in await self._client.list_todos(flow.project_id)}
                save_id = existing.get(save_title) or await self._client.create_todo(
                    flow.project_id, save_title
                )
                discard_id = existing.get(discard_title) or await self._client.create_todo(
                    flow.project_id, discard_title
                )
                await self._db.set_flow_prompts(flow.todo_id, save_id, discard_id)
                logger.info("Posted code-save prompt for #%s", flow.todo_id)
            except BasecampError as exc:
                logger.warning("Could not post code-save prompt for #%s", flow.todo_id)
                await self._db.fail_flow(flow.todo_id, str(exc))

    async def _resolve_decisions(self) -> None:
        """Watch the under-to-dos and act once the customer completes one."""
        for flow in await self._db.flows_in_stage(FLOW_PROMPTS_CREATED):
            if flow.save_todo_id is None or flow.discard_todo_id is None:
                continue
            try:
                discard_done = await self._client.is_todo_completed(
                    flow.discard_todo_id, flow.project_id
                )
                save_done = await self._client.is_todo_completed(
                    flow.save_todo_id, flow.project_id
                )
            except BasecampError:
                logger.warning("Decision check failed for #%s", flow.todo_id, exc_info=True)
                continue

            # "Do not save" wins if both were completed.
            if discard_done:
                await self._db.resolve_flow(flow.todo_id, FLOW_DISCARDED, "discard")
                logger.info("Code for #%s discarded by customer", flow.todo_id)
            elif save_done:
                await self._save_code(flow.todo_id, flow.session_id)

    async def _resume_stuck_saves(self) -> None:
        """Retry any flow left in `saving` (e.g. crash mid-resume)."""
        for flow in await self._db.flows_in_stage(FLOW_SAVING):
            await self._do_save(flow.todo_id, flow.session_id)

    async def _save_code(self, todo_id: int, session_id: str) -> None:
        """Mark the flow as saving and resume the session to persist the code."""
        await self._db.set_flow_stage(todo_id, FLOW_SAVING)
        await self._do_save(todo_id, session_id)

    async def _do_save(self, todo_id: int, session_id: str) -> None:
        """Resume the task's session and ask claude to persist the used code."""
        instruction = (
            "Save the query/analysis script you used for this task into the results/ folder "
            "as a descriptively-named file (results/<name>.sql for SQL, results/<name>.py for "
            "PySpark). Begin the file with a short comment in English describing what it does, "
            "so the script explains itself when read later. Then add or update a one-line entry "
            "in results/INDEX.md mapping the file name to that description, so it serves as a "
            "hint index for similar future tasks."
        )
        try:
            await self._runner.resume(session_id, instruction)
        except ClaudeError as exc:
            logger.warning("Code-save resume failed for #%s", todo_id)
            await self._db.fail_flow(todo_id, str(exc))
            return
        await self._db.resolve_flow(todo_id, FLOW_SAVED, "save")
        logger.info("Saved code for #%s", todo_id)
