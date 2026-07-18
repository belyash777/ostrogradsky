# ostrogradsky

A Basecamp-driven worker that lets Claude Code be controlled through Basecamp.

A person creates a to-do in Basecamp and assigns a dedicated "CLI account" as the assignee. A
Python worker running in Docker drives [`basecamp-cli`](https://github.com/basecamp/basecamp-cli),
polls Basecamp every few seconds, and for each **new** to-do assigned to that account it:

1. posts the comment `Задача прийнята, виконую роботу`;
2. runs the task through Claude Code (`claude -p`), passing the to-do's Notes;
3. posts Claude's result back as a comment.

Claude works with MySQL and Spark/Hadoop (via MCP servers) to compute metrics and A/B tests. The
worker also **syncs knowledge** from the project's Docs & Files (a `skills` folder → Claude Code
skills, a `documents` folder → references, a `CLAUDE.md` → workspace guidance), **handles follow-up
edits** (a new customer comment resumes the task's session), and **offers to save code** after a
task is completed (two under-to-dos "Зберегти код" / "Не зберігати код").

No webhooks — polling only. Only Basecamp **to-dos** are tracked, scoped to one project. See
[CLAUDE.md](CLAUDE.md) for architecture and development notes.

## Quick start

```bash
# 1. Two one-time interactive logins.
docker compose run --rm auth          # Basecamp (device-code)
docker compose run --rm claude-auth   # Claude Code (subscription OAuth)

# 2. Start the worker
docker compose up -d worker
docker compose logs -f worker
```

All local state lives in the gitignored `./data` folder: `./data/basecamp/` and `./data/claude/`
hold the credentials, `./data/workspace/` is the claude working dir, and `./data/bcworker.sqlite3`
the processed-todo history.

## Configuration (`.env`)

Create a `.env` (from the template) to set `BASECAMP_PROJECT_ID`, the MySQL/Spark MCP credentials,
and any tuning. Container-critical paths are set directly in
[`docker-compose.yml`](docker-compose.yml); everything else has a built-in default
(see [`src/bcworker/config.py`](src/bcworker/config.py)).

```bash
cp .env.example .env
```

Compose loads `.env` automatically (it is declared with `required: false`).

### Key variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BASECAMP_PROJECT_ID` | *(empty → 0)* | The single project the worker serves: tasks and skills/documents/CLAUDE.md come from here. Required in production. |
| `POLL_INTERVAL_SECONDS` | `5` | How often to poll for newly assigned to-dos. |
| `CLAUDE_TIMEOUT_SECONDS` | `900` | Per-run timeout for `claude -p`. |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | Claude permission mode; use `bypassPermissions` if MCP servers hang on a headless trust prompt. |
| `SYNC_INTERVAL_SECONDS` / `CLAUDE_MD_REFRESH_SECONDS` | `60` / `1800` | Docs & Files sync and CLAUDE.md refresh cadences. |
| `CODE_SAVE_DELAY_SECONDS` | `300` | Delay after task completion before posting the save/discard under-to-dos. |
| `MYSQL_*` | *(empty)* | MySQL MCP credentials (no database pinned — all databases are queryable). |
| `SPARK_*` | *(empty)* | Spark MCP credentials (host/port + login/password). |

The full list (with sync folder names, poll cadences and concurrency) is in
[`.env.example`](.env.example).

## Development

```bash
# Run the tests (any Python 3.11+; the container uses 3.14)
PYTHONPATH=src pytest
```

Tests need neither Docker nor a real Basecamp — the CLI is replaced by a fake binary and SQLite
runs on a temporary file.
