"""The post-completion "save the code?" lifecycle.

When the customer completes a task, five minutes later the worker posts one
comment on it — asking whether to save the code it used — and watches for the
customer's answer. The answer can be a reply comment (a word like "так"/"ні" or
an emoji) or a boost (reaction) on the prompt; :mod:`bcworker.decision` maps it
to save / discard / unclear. If the customer says yes (and not no), the task's
claude session is resumed with an instruction to store the query/analysis script
it used in ``results/`` (with an index entry in ``results/INDEX.md``) for reuse
on similar future tasks. Silence past a deadline resolves the flow as discarded.

``tick`` is idempotent and takes the current time explicitly, so it can be
driven on a slow cadence from the poll loop and unit-tested with an injected
clock. Per-flow failures are recorded and never crash the loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import decision
from .basecamp import BasecampClient, BasecampError
from .claude_runner import ClaudeError, ClaudeRunner, session_id_for
from .config import Config
from .db import (
    FLOW_AWAITING_DELAY,
    FLOW_DISCARDED,
    FLOW_PROMPTS_CREATED,
    FLOW_SAVED,
    FLOW_SAVING,
    CodeSaveFlow,
    Database,
)

logger = logging.getLogger(__name__)

# The prompt posted on the completed task (Ukrainian: it is text for the
# customer). Kept plain so a "так"/"ні"/👍 reply — or a boost on it — is natural.
PROMPT_MESSAGE = (
    "Зберегти код цієї задачі для повторного використання в майбутньому? "
    "Відповідь «так» (чи 👍) — збережу, «ні» — ні."
)


def _comment_id(item: dict) -> int:
    value = item.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _creator_id(item: dict) -> str:
    creator = item.get("creator")
    if isinstance(creator, dict) and creator.get("id") is not None:
        return str(creator.get("id"))
    return ""


def _content(item: dict) -> str:
    return str(item.get("content") or item.get("body") or "").strip()


class CodeSaveManager:
    """Detects task completion and drives the save/discard comment prompt."""

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
        self._worker_id: str | None = None
        self._worker_id_resolved = False

    def _project_id(self, bucket_id: int) -> int:
        return bucket_id or self._config.basecamp_project_id

    async def _worker_person_id(self) -> str | None:
        """The worker (CLI account) person id, used to skip its own comments/boosts."""
        if not self._worker_id_resolved:
            self._worker_id = await self._client.whoami()
            self._worker_id_resolved = True
        return self._worker_id

    async def tick(self, now: datetime) -> None:
        """Advance every code-save flow by one step (safe to call repeatedly)."""
        await self._detect_completions(now)
        await self._post_prompt(now)
        await self._resolve_decisions(now)
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

    async def _post_prompt(self, now: datetime) -> None:
        """After the delay, post the "save the code?" comment (idempotently)."""
        for flow in await self._db.flows_in_stage(FLOW_AWAITING_DELAY):
            if datetime.fromisoformat(flow.prompt_due_at) > now:
                continue  # not due yet
            deadline = (
                now + timedelta(seconds=self._config.code_save_reply_timeout_seconds)
            ).isoformat()
            try:
                # Reuse the prompt if a prior attempt posted it but crashed before
                # persisting its id, so a restart never double-asks.
                comment_id = await self._existing_prompt_id(
                    flow
                ) or await self._client.create_comment(
                    flow.todo_id, PROMPT_MESSAGE, flow.project_id
                )
                if comment_id is None:
                    # Without the prompt's id there is nothing to anchor the reply
                    # to; leave the flow armed and retry on the next tick.
                    logger.warning(
                        "Code-save prompt for #%s returned no comment id; will retry",
                        flow.todo_id,
                    )
                    continue
                await self._db.set_flow_prompt(flow.todo_id, comment_id, deadline)
                logger.info("Posted code-save prompt for #%s", flow.todo_id)
            except BasecampError as exc:
                logger.warning("Could not post code-save prompt for #%s", flow.todo_id)
                await self._db.fail_flow(flow.todo_id, str(exc))

    async def _existing_prompt_id(self, flow: CodeSaveFlow) -> int | None:
        """Return the id of an already-posted prompt comment on the task, if any."""
        worker_id = await self._worker_person_id()
        comments = await self._client.list_comments(flow.todo_id, flow.project_id)
        return max(
            (
                _comment_id(c)
                for c in comments
                if _creator_id(c) == worker_id and _content(c) == PROMPT_MESSAGE
            ),
            default=None,
        )

    async def _resolve_decisions(self, now: datetime) -> None:
        """Read the customer's reply/boost and act on the first clear answer."""
        worker_id = await self._worker_person_id()
        for flow in await self._db.flows_in_stage(FLOW_PROMPTS_CREATED):
            if flow.prompt_comment_id is None:
                continue
            try:
                verdict = await self._read_decision(flow, worker_id)
            except BasecampError:
                logger.warning("Decision check failed for #%s", flow.todo_id, exc_info=True)
                continue

            if verdict == decision.DISCARD:
                await self._db.resolve_flow(flow.todo_id, FLOW_DISCARDED, "discard")
                logger.info("Code for #%s discarded by customer", flow.todo_id)
            elif verdict == decision.SAVE:
                await self._save_code(flow.todo_id, flow.session_id)
            elif flow.reply_deadline and datetime.fromisoformat(flow.reply_deadline) <= now:
                await self._db.resolve_flow(flow.todo_id, FLOW_DISCARDED, "timeout")
                logger.info("Code-save prompt for #%s timed out unanswered; not saved", flow.todo_id)

    async def _read_decision(self, flow: CodeSaveFlow, worker_id: str | None) -> str | None:
        """Classify the customer's answer to a flow's prompt (discard wins ties)."""
        assert flow.prompt_comment_id is not None  # guarded by the caller
        verdict: str | None = None

        # A boost (reaction) on the prompt — the simplest "smiley" answer.
        for boost in await self._client.list_boosts(flow.prompt_comment_id, flow.project_id):
            if _creator_id(boost) == worker_id:
                continue
            if decision.classify_boost(_content(boost)) == decision.DISCARD:
                return decision.DISCARD
            verdict = decision.SAVE

        # A reply comment posted after the prompt.
        comments = await self._client.list_comments(flow.todo_id, flow.project_id)
        for comment in sorted(comments, key=_comment_id):
            if _comment_id(comment) <= flow.prompt_comment_id or _creator_id(comment) == worker_id:
                continue
            answer = decision.classify(_content(comment))
            if answer == decision.DISCARD:
                return decision.DISCARD
            if answer == decision.SAVE:
                verdict = decision.SAVE
        return verdict

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
