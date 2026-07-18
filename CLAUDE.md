# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## Language

- **Chat / conversation: Ukrainian only.**
- **Code comments and string literals: English only.**
- Messages posted into Basecamp to-dos are Ukrainian (they are text for people).

## What this project is

A worker that lets Claude Code be driven through Basecamp. A person creates a to-do in Basecamp
and assigns a dedicated "CLI account" as the assignee. A Python worker running in a Docker
container drives `basecamp-cli`, polls Basecamp every few seconds, and for each **new** to-do
assigned to that account it:

1. posts the comment "Задача прийнята, виконую роботу";
2. runs the task handler (currently a stub: `print("hello world")`);
3. posts the comment "Роботу виконано".

**Current stage** builds only the poller container. The real Claude Code invocation is a future
stage; in its place there is a stub, [`stub.run_task(task_text)`](src/bcworker/stub.py), which
already accepts the task text — a ready seam for the real command.

Deliberate decisions:
- **No webhooks** — periodic polling only.
- Track **to-dos only** (via `basecamp reports assigned`).
- **Zero third-party runtime dependencies** — Python standard library plus the external
  `basecamp` CLI. `pytest`/`pytest-asyncio` are needed for tests only.

## Architecture

Package [`src/bcworker/`](src/bcworker/):

| Module | Role |
|--------|------|
| [`__main__.py`](src/bcworker/__main__.py) | Entry point: logging, SIGTERM/SIGINT handling, runs the loop. |
| [`config.py`](src/bcworker/config.py) | `Config.from_env()` — configuration from environment variables. |
| [`basecamp.py`](src/bcworker/basecamp.py) | `BasecampClient` — async wrapper over the `basecamp` CLI (`--json`). |
| [`db.py`](src/bcworker/db.py) | `Database` — async wrapper over SQLite (via `asyncio.to_thread`). |
| [`migrations.py`](src/bcworker/migrations.py) | Idempotent `.sql` migration runner. |
| [`poller.py`](src/bcworker/poller.py) | `Poller` — loop: poll → detect new → react. |
| [`stub.py`](src/bcworker/stub.py) | `run_task(task_text)` — handler stub (future Claude Code). |

De-duplication: [`db.try_claim()`](src/bcworker/db.py) performs an atomic `INSERT` into
`processed_todos` (PK = `todo_id`). A primary-key conflict means the to-do was already claimed, so
it is never processed twice, even after the container restarts.

Recovery: the to-do status (`claimed` → `accepted` → `done`/`error`) encodes the stage. If the
process is interrupted, [`Poller._recover_pending()`](src/bcworker/poller.py) finishes any
unfinished to-dos on startup, without re-posting the acceptance comment for those already at
`accepted`.

Account context: in non-interactive mode (`--json`) the CLI will not pick an account itself. The
client passes `BASECAMP_ACCOUNT_ID`; if it is unset,
[`ensure_account()`](src/bcworker/basecamp.py) auto-detects it via `accounts list` when there is
exactly one account. The client always sets `BASECAMP_NO_KEYRING=1` for a deterministic
file-based credential store.

Key CLI commands the worker relies on:
- `basecamp auth status --json` — authentication check;
- `basecamp reports assigned --json` — with no person argument defaults to `me` (the CLI account),
  account-wide across all projects;
- `basecamp comments create <todo_id> <text> --json` — comment on a to-do.

## Development

Any Python 3.11+ is enough to run the tests locally (the container uses 3.14).

```bash
# Tests (locally; PYTHONPATH points at the package under src/)
PYTHONPATH=src pytest

# Lint (if ruff is available)
ruff check .
```

The tests need neither Docker nor a real Basecamp: the `basecamp` CLI is replaced by a fake
binary (see [`tests/conftest.py`](tests/conftest.py)) and SQLite runs on a temporary file.

## Running (Docker)

```bash
# 1. One-time interactive login (device-code). Open the shown URL, authenticate
#    as the CLI account, and paste the callback back into the terminal.
docker compose run --rm auth

# 2. Start the worker
docker compose up -d worker
docker compose logs -f worker
```

All local state lives in the single gitignored `./data` folder (bind-mounted at `/data`):
`./data/basecamp/` holds the credentials and `./data/bcworker.sqlite3` the history. The `auth` and
`worker` services share it, so the login is picked up by the worker automatically.

## Configuration (env)

See [`.env.example`](.env.example). Main variables: `POLL_INTERVAL_SECONDS` (default 5),
`DB_PATH`, `BASECAMP_BIN`, `BASECAMP_CONFIG_DIR` (must end in `/basecamp`),
`BASECAMP_ACCOUNT_ID` (empty = auto-detect when there is a single account),
`BASECAMP_TIMEOUT_SECONDS`, `LOG_LEVEL`, `MIGRATIONS_DIR` (already set to `/app/migrations` in the
image).

## Code conventions

- Async everywhere there is I/O; the poll loop never crashes on a single failed cycle or to-do —
  errors are logged and work continues.
- New migrations: add a `NNNN_name.sql` file under [`migrations/`](migrations/) (lexicographic
  order, only `CREATE ... IF NOT EXISTS` / idempotent operations).
- Keep the runtime free of third-party dependencies; new packages go into the `dev` extra only.
- Cover every module with tests under [`tests/`](tests/).
