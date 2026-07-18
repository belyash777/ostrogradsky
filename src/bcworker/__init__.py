"""Basecamp-driven worker package.

Polls Basecamp for to-dos assigned to the CLI account and runs each new one
through Claude Code (`claude -p`), posting the result back. Also syncs
skills/documents from Docs & Files, handles follow-up edits, and offers to save
the used code after completion.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
