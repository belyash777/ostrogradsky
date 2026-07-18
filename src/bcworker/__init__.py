"""Basecamp-driven worker package.

Polls Basecamp for to-dos assigned to the CLI account and dispatches each new
one to a task handler (currently a stub that will later invoke Claude Code).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
