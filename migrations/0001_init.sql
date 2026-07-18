-- Initial schema.
--
-- schema_migrations tracks which migration files have been applied so the
-- runner can stay idempotent across restarts.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- processed_todos records every Basecamp to-do the worker has picked up. Its
-- primary key doubles as the de-duplication guard: a to-do id present here is
-- never claimed twice, even after the process (or whole container) restarts.
--
-- status transitions (also used to resume an interrupted run):
--   'claimed' -> 'accepted' -> 'done'   (happy path)
--   any active status -> 'error'        (processing failed)
CREATE TABLE IF NOT EXISTS processed_todos (
    todo_id      INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,
    accepted_at  TEXT NOT NULL,
    completed_at TEXT,
    error        TEXT
);
