# ostrogradsky

A Basecamp-driven worker that lets Claude Code be controlled through Basecamp.

A person creates a to-do in Basecamp and assigns a dedicated "CLI account" as the assignee. A
Python worker running in Docker drives [`basecamp-cli`](https://github.com/basecamp/basecamp-cli),
polls Basecamp every few seconds, and for each **new** to-do assigned to that account it:

1. posts the comment `Задача прийнята, виконую роботу`;
2. runs the task handler (currently a stub that prints `hello world` — a placeholder for the real
   Claude Code invocation);
3. posts the comment `Роботу виконано`.

No webhooks — polling only. Only Basecamp **to-dos** are tracked. See [CLAUDE.md](CLAUDE.md) for
architecture and development notes.

## Quick start

```bash
# 1. One-time interactive login (device-code). Open the shown URL, authenticate
#    as the CLI account, and paste the callback back into the terminal.
docker compose run --rm auth

# 2. Start the worker
docker compose up -d worker
docker compose logs -f worker
```

All local state lives in the gitignored `./data` folder:
`./data/basecamp/` holds the credentials and `./data/bcworker.sqlite3` the processed-todo history.

## Configuration (`.env`)

**`.env` is optional.** The worker runs fine without it: container-critical values are set directly
in [`docker-compose.yml`](docker-compose.yml), and everything else has a built-in default
(see [`src/bcworker/config.py`](src/bcworker/config.py)). Create a `.env` only to override a
default, or to set `BASECAMP_ACCOUNT_ID` when the login can access more than one account.

To start from the template:

```bash
cp .env.example .env
```

Compose loads `.env` automatically (it is declared with `required: false`).

### Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SECONDS` | `5` | How often to poll Basecamp for newly assigned to-dos. |
| `BASECAMP_ACCOUNT_ID` | *(empty)* | Basecamp account id. Leave empty to auto-detect when the login has exactly one account; set it explicitly if several are accessible. |
| `DB_PATH` | `/data/bcworker.sqlite3` | SQLite database used to de-duplicate processed to-dos. |
| `BASECAMP_CONFIG_DIR` | `/data/basecamp` | Credentials directory. Must end in `/basecamp` (the CLI reads `$XDG_CONFIG_HOME/basecamp`). |
| `BASECAMP_BIN` | `basecamp` | Name or absolute path of the Basecamp CLI binary. |
| `BASECAMP_TIMEOUT_SECONDS` | `30` | Per-command timeout for Basecamp CLI invocations. |
| `LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `MIGRATIONS_DIR` | `/app/migrations` | Directory of `*.sql` migration files (already set inside the image). |

> **When you actually need `.env`:** if the CLI login can access **multiple** Basecamp accounts,
> auto-detection cannot choose one, so set `BASECAMP_ACCOUNT_ID` (via `.env` or the compose
> `environment:` block). In every other case the defaults are sufficient.

## Development

```bash
# Run the tests (any Python 3.11+; the container uses 3.14)
PYTHONPATH=src pytest
```

Tests need neither Docker nor a real Basecamp — the CLI is replaced by a fake binary and SQLite
runs on a temporary file.
