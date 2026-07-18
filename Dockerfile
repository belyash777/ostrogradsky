# syntax=docker/dockerfile:1

##############################################################################
# Base image: shared foundation with the Basecamp CLI installed.
##############################################################################
FROM python:3.14-slim AS base

# Install the external CLIs the worker drives:
#   - Basecamp CLI (static Go binary; installer drops it in root's ~/.local/bin,
#     unreadable by the non-root user, so relocate it to /usr/local/bin).
#   - Claude Code (installed globally via npm so `claude` is on PATH for all users).
#   - Node.js/npm and uv/uvx stay in the image at runtime: the MCP servers run as
#     claude subprocesses via `npx` (MySQL) and `uvx` (spark-sql-mcp-server).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm \
    && curl -fsSL https://basecamp.com/install-cli | bash \
    && install -m 0755 /root/.local/bin/basecamp /usr/local/bin/basecamp \
    && rm -rf /root/.local \
    && npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir uv \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN useradd --create-home --uid 10001 app
ENV HOME=/home/app

# All local state lives under /data (bind-mounted from ./data on the host):
#   /data/basecamp/           -> Basecamp CLI credentials
#   /data/bcworker.sqlite3    -> processed-todo history
# The CLI reads credentials from $XDG_CONFIG_HOME/basecamp, so pointing
# XDG_CONFIG_HOME at /data makes both the worker and the HEALTHCHECK use the same
# credentials directory. Pre-create and chown it for bare runs (a bind mount from
# the host supplies its own ownership at runtime).
# Claude Code stores its subscription OAuth credentials under CLAUDE_CONFIG_DIR,
# kept in /data so the one-time `claude-auth` login persists and the worker
# reuses it (and token refreshes are written back to the mounted volume).
#   /data/claude/       -> Claude Code OAuth credentials
#   /data/workspace/    -> the claude working dir (CLAUDE.md, .mcp.json, documents,
#                          .claude/skills, snippets), seeded from the template
ENV XDG_CONFIG_HOME=/data
ENV CLAUDE_CONFIG_DIR=/data/claude
ENV CLAUDE_WORKSPACE_DIR=/data/workspace
RUN mkdir -p /data/basecamp /data/claude /data/workspace \
    && chown -R app:app /data

WORKDIR /app

##############################################################################
# Runtime image: installs the package (no dev dependencies) and runs the worker.
##############################################################################
FROM base AS runtime

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
# Baked workspace template, copied into /data/workspace on startup when missing.
COPY workspace-template ./workspace-template

RUN pip install --no-cache-dir .

# Migrations are not part of the installed wheel; point the worker at the copy
# in the build context.
ENV MIGRATIONS_DIR=/app/migrations

USER app

# Report unhealthy when the CLI is no longer authenticated (e.g. token expired),
# turning a silent, perpetual failure into a visible container health signal.
HEALTHCHECK --interval=1m --timeout=15s --start-period=30s --retries=3 \
    CMD test "$(BASECAMP_NO_KEYRING=1 basecamp auth status --jq .data.authenticated 2>/dev/null)" = "true"

CMD ["python", "-m", "bcworker"]
