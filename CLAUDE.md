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
2. runs the task through Claude Code (`claude -p`), passing the to-do's Notes;
3. posts Claude's result back as a comment.

The worker does **not** sync knowledge from Basecamp: the claude working dir
(`CLAUDE.md`, `.mcp.json`, `documents/`, `.claude/skills/`, `results/`) is populated by hand under
`./data/workspace/` (the bind-mounted volume). `ensure_workspace()` seeds `CLAUDE.md`/`.mcp.json`
from the baked template on first start and creates the directories; everything else is placed there
directly.

Around that core the worker also:
- **Handles follow-up edits**: a new customer comment on a finished to-do resumes that task's
  claude session and posts an updated answer.
- **Offers to save code**: five minutes after the customer completes a task, the worker posts one
  comment asking whether to save the code it used. The answer is read from the customer's reply —
  a reply comment (a word like "так"/"ні" or an emoji) or a boost (reaction) on the prompt;
  [`decision.py`](src/bcworker/decision.py) maps it to save / discard / unclear (discard wins ties,
  silence past a deadline discards). On "save" the session is resumed so Claude stores the
  query/analysis script it used under `results/` (indexed in `results/INDEX.md`) for reuse on
  similar future tasks.

Each task gets a deterministic `--session-id` (`uuid5` of the to-do id), persisted in SQLite and
reused for the edit/code-save resumes.

Deliberate decisions:
- **No webhooks** — periodic polling only.
- Track **to-dos only** (via `basecamp reports assigned`), scoped to `BASECAMP_PROJECT_ID`.
- **Python runtime stays third-party-free** — standard library only. The external CLIs
  (`basecamp`, `claude`) and the MCP servers (MySQL via `npx`, `spark-sql-mcp-server` via `uvx`)
  are separate processes, not Python packages. `pytest`/`pytest-asyncio` are for tests only.

## Architecture

Package [`src/bcworker/`](src/bcworker/):

| Module | Role |
|--------|------|
| [`__main__.py`](src/bcworker/__main__.py) | Entry point: logging, SIGTERM/SIGINT handling, runs the loop. |
| [`config.py`](src/bcworker/config.py) | `Config.from_env()` — configuration from environment variables. |
| [`basecamp.py`](src/bcworker/basecamp.py) | `BasecampClient` — async wrapper over the `basecamp` CLI (`--json`). |
| [`db.py`](src/bcworker/db.py) | `Database` — async wrapper over SQLite (via `asyncio.to_thread`). |
| [`migrations.py`](src/bcworker/migrations.py) | Idempotent `.sql` migration runner. |
| [`poller.py`](src/bcworker/poller.py) | `Poller` — loop: poll → dispatch tasks → run periodic concerns. |
| [`claude_runner.py`](src/bcworker/claude_runner.py) | `ClaudeRunner` — async wrapper over `claude -p`; deterministic session ids. |
| [`followup.py`](src/bcworker/followup.py) | `FollowupManager` — new customer comments → resumed session. |
| [`codesave.py`](src/bcworker/codesave.py) | `CodeSaveManager` — the post-completion save/discard lifecycle. |
| [`decision.py`](src/bcworker/decision.py) | `classify()` — map a customer reply/boost to save / discard / unclear. |
| [`workspace.py`](src/bcworker/workspace.py) | `ensure_workspace()` — seed `/data/workspace` from the baked template. |

De-duplication: [`db.try_claim()`](src/bcworker/db.py) performs an atomic `INSERT` into
`processed_todos` (PK = `todo_id`). A primary-key conflict means the to-do was already claimed, so
it is never processed twice, even after the container restarts.

Recovery: the to-do status (`claimed` → `accepted` → `done`/`error`) encodes the stage. If the
process is interrupted, [`Poller._recover_pending()`](src/bcworker/poller.py) finishes any
unfinished to-dos on startup, without re-posting the acceptance comment for those already at
`accepted`. The full task text (title + body) is persisted, so recovery reproduces the task; the
deterministic session id means the resumed claude session is the same one.

Concurrency: a `claude -p` run takes minutes, so each task is driven in a bounded background
`asyncio.Task` (semaphore `TASK_MAX_CONCURRENCY`, default 1) while the poll loop keeps ticking. The
loop also runs the periodic concerns on their own cadences: follow-up comment polling
(`COMMENT_POLL_SECONDS`) and the code-save lifecycle (`CODESAVE_POLL_SECONDS`). The single shared
SQLite connection under one `asyncio.Lock` keeps all writes safe and ordered.

Code-save / follow-up routing: `active_done_todos()` returns finished to-dos that have **no**
`code_save_flow` row, so once the customer completes a task (which arms its flow) follow-up polling
stops touching it and the code-save prompt/reply own its comment stream — the two never contend for
the same comment. The prompt is idempotent across a crash: before posting, the manager looks for its
own already-posted `PROMPT_MESSAGE` comment on the task and reuses that id. The workspace lives under
the writable `/data` mount (owned by the non-root user); `ensure_workspace()` seeds it from the baked
`/app/workspace-template`.

Account context: in non-interactive mode (`--json`) the CLI will not pick an account itself. The
client passes `BASECAMP_ACCOUNT_ID`; if it is unset,
[`ensure_account()`](src/bcworker/basecamp.py) auto-detects it via `accounts list` when there is
exactly one account. The client always sets `BASECAMP_NO_KEYRING=1` for a deterministic
file-based credential store.

Key CLI commands the worker relies on (project-scoped ones take `--in <project>`):
- `basecamp auth status --json` — authentication check;
- `basecamp reports assigned --json` — defaults to `me` (the CLI account); filtered to
  `BASECAMP_PROJECT_ID`;
- `basecamp todos show <id> --in <p> --json` — read Notes + `completed` flag;
- `basecamp comments create <id> <text> --in <p> --json` / `comments list <id> --in <p> --json`;
- `basecamp boost list <id> --in <p> --json` — reactions on the code-save prompt comment;
- `basecamp people show me --json` — the worker's own person id (skip its own comments).

**Note:** exact flag names for the `claude` and newer `basecamp` subcommands (and the MCP package
names in `workspace-template/.mcp.json`) are best verified against the real CLIs at build time —
see the risks section of the plan. The tests use generic fake binaries and do not depend on them.

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
# 1. Two one-time interactive logins (Basecamp device-code + Claude subscription).
docker compose run --rm auth          # Basecamp
docker compose run --rm claude-auth   # Claude Code (verify: setup-token vs login)

# 2. Start the worker
docker compose up -d worker
docker compose logs -f worker
```

All local state lives in the single gitignored `./data` folder (bind-mounted at `/data`):
`./data/basecamp/` and `./data/claude/` hold the credentials, `./data/workspace/` is the claude
working dir (CLAUDE.md, `.mcp.json`, `documents/`, `.claude/skills/`, `results/`), and
`./data/bcworker.sqlite3` the history. The `auth`, `claude-auth` and `worker` services share it, so
the logins are picked up by the worker automatically.

## Configuration (env)

See [`.env.example`](.env.example). Basecamp: `POLL_INTERVAL_SECONDS`, `DB_PATH`, `BASECAMP_BIN`,
`BASECAMP_CONFIG_DIR` (must end in `/basecamp`), **`BASECAMP_PROJECT_ID`**
(the single project for tasks; 0 disables filtering, used only in tests),
`BASECAMP_TIMEOUT_SECONDS`, `LOG_LEVEL`, `MIGRATIONS_DIR`. Claude: `CLAUDE_BIN`,
`CLAUDE_TIMEOUT_SECONDS`, `CLAUDE_WORKSPACE_DIR`, `CLAUDE_CONFIG_DIR`, `CLAUDE_PERMISSION_MODE`
(switch to `bypassPermissions` if MCP servers hang on a headless trust prompt). Lifecycle:
`COMMENT_POLL_SECONDS`, `CODESAVE_POLL_SECONDS`, `CODE_SAVE_DELAY_SECONDS`,
`CODE_SAVE_REPLY_TIMEOUT_SECONDS`, `TASK_MAX_CONCURRENCY`.
MCP creds (`MYSQL_*`, `SPARK_*`) are consumed by `.mcp.json`, not parsed by `Config`.

## Code conventions

- Async everywhere there is I/O; the poll loop never crashes on a single failed cycle or to-do —
  errors are logged and work continues.
- New migrations: add a `NNNN_name.sql` file under [`migrations/`](migrations/) (lexicographic
  order). Prefer idempotent statements (`CREATE ... IF NOT EXISTS`); a plain `ALTER TABLE ADD
  COLUMN` is fine too since the runner's version tracking runs each file exactly once.
- Keep the runtime free of third-party dependencies; new packages go into the `dev` extra only.
- Cover every module with tests under [`tests/`](tests/).
