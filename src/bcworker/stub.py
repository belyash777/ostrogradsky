"""Task handler stub.

This is a placeholder for the future Claude Code integration. It already takes
the full task text so the call site (the poller) is wired the way the real
handler will be: swapping ``print("hello world")`` for an actual Claude Code
invocation is the only change needed later.
"""

from __future__ import annotations

import asyncio


async def run_task(task_text: str) -> None:
    """Handle a single Basecamp to-do.

    Args:
        task_text: The task description assembled from the to-do (title plus any
            body). Currently unused by the stub, but part of the stable contract
            the real handler will consume.
    """
    # Placeholder work. Replaced later by a real Claude Code call.
    print("hello world")
    # Yield control so the coroutine behaves like the future async handler.
    await asyncio.sleep(0)
