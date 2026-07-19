-- Switch the code-save prompt from two under-to-dos to a single comment.
--
-- Instead of creating "Зберегти код" / "Не зберігати код" to-dos, the worker now
-- posts one comment ("save the code?") on the completed task and reads the
-- customer's reply — a reply comment (text or emoji) or a boost (reaction) on the
-- prompt. Two new columns support that:
--   prompt_comment_id - id of the posted prompt comment; the decision is any
--                       customer reply/boost that references it
--   reply_deadline    - after this the flow is resolved as discarded (the
--                       customer never answered), so it stops being polled
-- The old save_todo_id / discard_todo_id columns stay (unused) — SQLite cannot
-- drop columns and leaving them is harmless.
ALTER TABLE code_save_flow ADD COLUMN prompt_comment_id INTEGER;
ALTER TABLE code_save_flow ADD COLUMN reply_deadline    TEXT;
