# Working with MySQL

You write and run **read-only** MySQL queries through the `mysql` MCP server. Reusable query scripts
may later be saved under `results/`, but **only after the customer explicitly confirms it** — never
on your own initiative (see *Saving and reusing scripts*).

## Accessing the database (MCP)

- The `mysql` MCP server exposes a **single tool**, `mysql_query`, that executes SQL.
  Send `SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN` through it.
- Schema is also published as the MCP **resource** `mysql://tables` (all tables plus column
  metadata). Read it, or query the schema with SQL — both work.
- The server is **read-only by default**: `INSERT` / `UPDATE` / `DELETE` / DDL are disabled
  and will fail. Never attempt to modify data.
- **No default database is pinned** and every database is queryable. Do not assume a current
  database: discover them with `SHOW DATABASES`, and always qualify names as `db.table`.
  Databases or columns whose names contain special characters (e.g. the `work-stat` DB) must
  be backtick-quoted: `` `work-stat`.jobs_202607 ``.

## Before you write a query

1. **Discover tables** — read the `mysql://tables` resource, or run `SHOW TABLES` /
   `SHOW TABLES FROM db`.
2. **Never guess columns** — confirm them with `DESCRIBE db.table`
   (or `SHOW COLUMNS FROM db.table`, or a query against `information_schema.columns`).
3. **Plan the access path** — for any query with 2 or more JOINs, run `EXPLAIN` first.
   Prefer indexed columns in `WHERE` / `JOIN`, and avoid full table scans.
4. **Iterate small** — draft the query, test it with a small `LIMIT`, then scale up.

## Query rules

- Always add `LIMIT` while exploring data.
- Never `SELECT *` — list the columns you actually need.
- `EXPLAIN` before any query with 2+ JOINs.
- Use `COUNT(DISTINCT user_id)` for unique-user metrics (DAU/MAU), and handle `NULL`s
  explicitly.
- **All timestamps are UTC.** Use explicit half-open date ranges
  (`sdate >= '2026-07-01' AND sdate < '2026-08-01'`) and state the timezone in your answer.
- Table and column names are `snake_case`.
- **Partitioned tables** (`work-stat.jobs_YYYYMM`, one table per month): select the correct
  monthly table(s), constrain the date column (`sdate`), and only `UNION` across months when
  the requested range spans more than one.

## Reporting the answer

Your stdout is posted back to a person, so make it self-contained: state the number, the
method (which tables/joins and the date range), and any caveats or assumptions.

## Saving and reusing scripts

- **Never save on your own initiative.** Finishing a task and printing the answer must not write
  anything to `results/` or touch `results/INDEX.md`. Saving happens **only after the customer
  explicitly confirms it** (the worker's post-completion code-save prompt).
- Before starting, check `results/INDEX.md` for a script saved from a similar past task; if
  one fits, read it in `results/` and adapt it instead of starting from scratch.
- **Only once save is confirmed**, write the query/analysis script to `results/<name>.sql`
  (or `.py` for PySpark), begin the file with a short English comment describing what it
  does, and add a one-line entry to `results/INDEX.md`: `file_name — short description`.

## Key tables (verified)

Use this list to understand the data — it is a map of the domain, not a style guide, and
column names should still be re-confirmed with `DESCRIBE` before use.

Default database:
- `trud_user` — main users table (`id`, `email`, `phone`, `is_blocked`, `is_confirmed`).
  Has **no** registration date.
- `trud_user_stat` — user statistics (`user_id`, `lastvisit_date`, `visit_count`, `locale`,
  `registration_source`). Use for activity / DAU.
- `trud_user_online` — online status of employers (`user_id`, `company_id`, `last_time`).
  Employers only.
- `trud_user_new` — employer email changes (`user_id`, `company_id`, `last_time`).
  Employers only.
- `trud_jobseeker` — jobseeker profiles.
- `trud_employer` — employer / company profiles.
- `trud_job` — job postings.
- `trud_resume` — resumes.

`work-stat` database:
- `` `work-stat`.jobs_YYYYMM `` — job statistics partitioned by month
  (`job_id`, `sdate`, `social_groups`, `town_id`, `is_blocked`).
