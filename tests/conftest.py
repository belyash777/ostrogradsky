"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from bcworker.basecamp import BasecampClient
from bcworker.db import Database

# A fake `basecamp` binary. It records each invocation's argv and a few relevant
# env vars to FAKE_RECORD (one JSON object per line) so tests can assert how the
# CLI was called, then emits whatever the test configured:
#   FAKE_STDOUT  - text written to stdout
#   FAKE_STDERR  - text written to stderr
#   FAKE_EXIT    - process exit code (default 0)
#   FAKE_SLEEP   - seconds to sleep before exiting (to exercise timeouts)
#   FAKE_RECORD  - path to append the invocation record to (optional)
_RECORDED_ENV = (
    "XDG_CONFIG_HOME",
    "BASECAMP_NO_KEYRING",
    "BASECAMP_NONINTERACTIVE",
    "BASECAMP_ACCOUNT_ID",
)
_FAKE_CLI = """\
#!{python}
import json
import os
import sys
import time

record = os.environ.get("FAKE_RECORD")
if record:
    entry = {{
        "argv": sys.argv[1:],
        "env": {{k: os.environ.get(k) for k in {recorded_env!r}}},
    }}
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")

time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    """Create an executable fake `basecamp` binary and return its path."""
    script = tmp_path / "fake_basecamp"
    script.write_text(
        _FAKE_CLI.format(python=sys.executable, recorded_env=_RECORDED_ENV),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def cli_records(tmp_path: Path) -> Path:
    """Enable invocation recording and return the record file path.

    Read parsed records with :func:`read_cli_records`.
    """
    record = tmp_path / "cli_records.jsonl"
    os.environ["FAKE_RECORD"] = str(record)
    return record


def read_cli_records(path: Path) -> list[dict]:
    """Parse the JSONL invocation records written by the fake CLI."""
    import json

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def make_client(fake_cli: Path, tmp_path: Path) -> Callable[..., BasecampClient]:
    """Return a factory building a BasecampClient wired to the fake CLI."""

    def _factory(**kwargs: object) -> BasecampClient:
        return BasecampClient(
            bin_path=str(fake_cli),
            config_dir=tmp_path / "config" / "basecamp",
            **kwargs,
        )

    return _factory


@pytest.fixture(autouse=True)
def _clean_fake_env() -> None:
    """Ensure FAKE_* variables never leak between tests."""
    for key in ("FAKE_STDOUT", "FAKE_STDERR", "FAKE_EXIT", "FAKE_SLEEP", "FAKE_RECORD"):
        os.environ.pop(key, None)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """A connected, migrated Database on a temporary file."""
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    await database.migrate()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def raw_conn(tmp_path: Path) -> sqlite3.Connection:
    """A bare sqlite3 connection for migration-runner tests."""
    conn = sqlite3.connect(tmp_path / "mig.sqlite3")
    try:
        yield conn
    finally:
        conn.close()
