-- Claude Code execution + skills/documents sync + code-save lifecycle.
--
-- The migration runner tracks applied versions, so each ADD COLUMN runs exactly
-- once; SQLite's ADD COLUMN has no IF NOT EXISTS, and none is needed here.

-- Extra context on every processed to-do:
--   description     - full task body, so a restart no longer loses the text
--   session_id      - the claude --session-id used, reused for follow-ups/code save
--   bucket_id/name  - the Basecamp project the to-do lives in
--   result          - the last claude stdout posted back as a comment
--   last_comment_id - highest customer comment already handled (follow-up edits)
ALTER TABLE processed_todos ADD COLUMN description     TEXT;
ALTER TABLE processed_todos ADD COLUMN session_id      TEXT;
ALTER TABLE processed_todos ADD COLUMN bucket_id       INTEGER;
ALTER TABLE processed_todos ADD COLUMN bucket_name     TEXT;
ALTER TABLE processed_todos ADD COLUMN result          TEXT;
ALTER TABLE processed_todos ADD COLUMN last_comment_id INTEGER;

-- Skills and documents mirrored from the project's Docs & Files. One row per
-- remote file; the checksum lets the syncer skip unchanged files, and a row that
-- disappears from Basecamp drives deletion of the local copy + skill.
CREATE TABLE IF NOT EXISTS synced_files (
    file_id     INTEGER NOT NULL,   -- Basecamp file/upload recording id
    project_id  INTEGER NOT NULL,   -- bucket id it lives in
    kind        TEXT    NOT NULL,   -- 'skill' | 'document'
    name        TEXT    NOT NULL,   -- filename on disk
    checksum    TEXT,               -- updated_at or sha256, whichever is available
    local_path  TEXT    NOT NULL,   -- where it was written in the workspace
    synced_at   TEXT    NOT NULL,
    PRIMARY KEY (project_id, kind, file_id)
);

-- The post-completion "save the code?" lifecycle. One row per task the customer
-- has completed; drives the +5min prompt and the save/discard decision.
--
-- stage transitions:
--   awaiting_delay -> prompts_created -> saving  -> saved
--                                     \-> discarded
--   any active stage -> error
CREATE TABLE IF NOT EXISTS code_save_flow (
    todo_id               INTEGER PRIMARY KEY,
    project_id            INTEGER NOT NULL,
    session_id            TEXT    NOT NULL,
    stage                 TEXT    NOT NULL,
    customer_completed_at TEXT    NOT NULL,
    prompt_due_at         TEXT    NOT NULL,
    save_todo_id          INTEGER,
    discard_todo_id       INTEGER,
    decision              TEXT,
    resolved_at           TEXT,
    error                 TEXT
);
