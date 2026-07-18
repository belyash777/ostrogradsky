"""Entry point: wire everything together and run the poll loop.

Handles SIGTERM/SIGINT for graceful shutdown so Docker can stop the container
cleanly (the loop finishes its current tick, then the DB connection is closed).
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .basecamp import BasecampClient
from .config import Config
from .db import Database
from .poller import Poller


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Set ``stop`` when a termination signal arrives (best-effort per platform)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Signal handlers via the loop are unavailable on some platforms
            # (e.g. Windows); fall back to signal.signal and hop back onto the
            # loop thread to wake a select()-blocked loop reliably.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def main() -> None:
    config = Config.from_env()
    _configure_logging(config.log_level)
    logger = logging.getLogger("bcworker")

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    db = Database(config.db_path)
    await db.connect()
    try:
        applied = await db.migrate(config.migrations_dir)
        if applied:
            logger.info("Applied migrations: %s", ", ".join(applied))

        client = BasecampClient(
            bin_path=config.basecamp_bin,
            config_dir=config.basecamp_config_dir,
            account_id=config.basecamp_account_id,
            timeout_seconds=config.basecamp_timeout_seconds,
        )
        poller = Poller(config, client, db)
        logger.info("Worker started (poll interval: %ss)", config.poll_interval_seconds)
        await poller.run(stop)
    finally:
        await db.close()
        logger.info("Worker stopped")


def run() -> None:
    """Synchronous console-script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
