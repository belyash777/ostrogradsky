"""Follow-up edits: react to new customer comments on a finished task.

After the worker posts a result, the customer may ask for changes by simply
commenting on the same to-do. This manager polls the comments of finished
(but not customer-completed) to-dos, and when it sees a new comment authored by
someone other than the worker, it resumes that task's claude session with the
comment text and posts the new answer back.

De-duplication uses ``processed_todos.last_comment_id`` (the highest comment id
already handled) plus a filter on the comment author, so the worker never reacts
to its own comments and never handles the same comment twice.
"""

from __future__ import annotations

import logging

from .basecamp import BasecampClient, BasecampError
from .claude_runner import ClaudeError, ClaudeRunner, session_id_for
from .config import COMPLETED_MESSAGE, Config
from .db import Database

logger = logging.getLogger(__name__)


def _comment_id(comment: dict) -> int:
    value = comment.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _creator_id(comment: dict) -> str:
    creator = comment.get("creator")
    if isinstance(creator, dict) and creator.get("id") is not None:
        return str(creator.get("id"))
    return ""


def _content(comment: dict) -> str:
    return str(comment.get("content") or comment.get("body") or "").strip()


class FollowupManager:
    """Turns new customer comments into resumed claude sessions."""

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
        """The worker (CLI account) person id, used to skip its own comments."""
        if not self._worker_id_resolved:
            try:
                status = await self._client.auth_status()
                raw = status.get("user_id") or status.get("person_id") or status.get("identity_id")
                self._worker_id = str(raw) if raw is not None else None
            except BasecampError:
                self._worker_id = None
            self._worker_id_resolved = True
        return self._worker_id

    async def tick(self) -> None:
        """Handle new customer comments on every finished, still-open task."""
        worker_id = await self._worker_person_id()
        if worker_id is None:
            logger.warning("Cannot resolve worker person id; skipping follow-up polling")
            return
        for todo_id, bucket_id, last_comment_id in await self._db.active_done_todos():
            try:
                await self._process_todo(
                    todo_id, self._project_id(bucket_id), last_comment_id, worker_id
                )
            except BasecampError:
                logger.warning("Follow-up poll failed for #%s", todo_id, exc_info=True)

    async def _process_todo(
        self, todo_id: int, project_id: int, last_comment_id: int | None, worker_id: str
    ) -> None:
        comments = await self._client.list_comments(todo_id, project_id)
        if not comments:
            return
        max_id = max(_comment_id(c) for c in comments)

        # First sighting: the baseline is the worker's OWN latest comment, not the
        # newest comment overall. This way a customer edit posted in the window
        # between completion and this first poll is still processed (it sits above
        # the worker's acceptance/result comments), instead of being folded into
        # the baseline and lost.
        if last_comment_id is None:
            last_comment_id = max(
                (_comment_id(c) for c in comments if _creator_id(c) == worker_id),
                default=0,
            )
            await self._db.set_last_comment_id(todo_id, last_comment_id)

        new_customer = [
            _content(c)
            for c in sorted(comments, key=_comment_id)
            if _comment_id(c) > last_comment_id
            and _creator_id(c) != worker_id
            and _content(c)
        ]
        if not new_customer:
            # Advance past the worker's own newer comments so they never re-trigger.
            if max_id > last_comment_id:
                await self._db.set_last_comment_id(todo_id, max_id)
            return

        instruction = "\n\n".join(new_customer)
        try:
            result = await self._runner.resume(session_id_for(todo_id), instruction)
        except ClaudeError:
            logger.warning("Follow-up resume failed for #%s", todo_id, exc_info=True)
            # Skip these comments so a persistent failure does not loop forever.
            await self._db.set_last_comment_id(todo_id, max_id)
            return

        await self._client.create_comment(todo_id, result or COMPLETED_MESSAGE, project_id)
        await self._db.set_result(todo_id, result)
        await self._db.set_last_comment_id(todo_id, max_id)
        logger.info("Handled follow-up edit for #%s", todo_id)
