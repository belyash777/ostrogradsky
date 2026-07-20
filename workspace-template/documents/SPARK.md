# Working with Apache Spark / Hadoop

You write and run **read-only** SparkSQL queries (and PySpark where appropriate) through the
`spark-sql` MCP server. Reusable query scripts may later be saved under `results/`, but **only after
the customer explicitly confirms it** — never on your own initiative (see *Saving and reusing
scripts*).

## Accessing Spark (MCP)

- Run SparkSQL / PySpark through the `spark-sql` MCP server. Treat it as **read-only**: only read
  and aggregate data, never write or drop.
- The data lives in **HDFS** as Avro topics under `hdfs://hadoop:9000/...`. Each dataset below lists
  its topic path(s) and refresh cadence.
- Some datasets also have an **improved** copy under `hdfs://hadoop:9000/topics-improved/...` — a
  more consolidated storage layout. Prefer it when both exist.
- Some datasets have separate **test / sandbox** topics under `hdfs://hadoop:9000/topics_test/...`.
  Use production topics unless the task is explicitly about sandbox data.

## Read topics directly (file-source SELECT)

**Prefer querying the HDFS topic path directly** over relying on any pre-registered metastore table.
The raw topic data is clean and its layout is known; what a named table actually points at is not
guaranteed.

The `spark-sql` MCP server is **strictly read-only**: it accepts a statement only when its first
keyword is `SELECT`, `SHOW`, `DESCRIBE`, `DESC`, `EXPLAIN`, or `WITH`. Anything starting with
`CREATE`, `SET`, `USE`, `ADD JAR`, etc. is rejected before it reaches Spark — so
`CREATE TEMPORARY VIEW` does **not** work here. Read the path directly with the file-source syntax
`FROM <format>.\`<path>\`` inside a plain `SELECT` (or `WITH`), which passes the read-only filter:

```sql
SELECT typeid, userid, actiondatetime
FROM parquet.`hdfs://hadoop:9000/topics/communicationEvents_avro/`
LIMIT 100;
```

Point the path at the deepest directory you need. On a partitioned topic, drill straight into the
partition folders (`year=YYYY/month=MM/day=DD/`) instead of scanning the whole topic:

```sql
SELECT userid, actionid, actiondatetime
FROM parquet.`hdfs://hadoop:9000/topics/userEvents_avro/year=2022/month=02/day=09/`
LIMIT 100;
```

`WITH` is also an allowed leading keyword, so CTEs over a file source work too:

```sql
WITH events AS (
    SELECT userid, actiondatetime
    FROM parquet.`hdfs://hadoop:9000/topics/userEvents_avro/year=2022/month=02/day=09/`
)
SELECT userid, COUNT(*) AS n FROM events GROUP BY userid LIMIT 100;
```

Notes:

- Use the format matching the physical files: `parquet.\`...\`` or `avro.\`...\``. If one errors on
  a path, try the other and confirm with a small `LIMIT` sample.
- `SET` is blocked, so options like `spark.sql.parquet.mergeSchema` cannot be toggled per query.
  Avoid schema-merge situations by reading a **single partition** (one `day=` folder) whose files
  share one schema, rather than a whole multi-schema topic at once.
- If a file-source `SELECT` still fails (not with a read-only rejection but a Spark/HDFS error), the
  cause is access/permissions on the path from the Thrift server — that is a cluster-side config
  issue, not something query syntax can work around. Report the exact error in your answer.

## Before you write a query

1. **Never guess columns** — confirm them against the live data: `DESCRIBE parquet.\`<path>\`` (a
   `DESCRIBE` is allowed by the read-only filter), or read a small `SELECT ... LIMIT` sample from the
   file source; the attribute lists below are a map of the domain, not a substitute for the live
   schema.
2. **Iterate small** — draft the query, test it with a small `LIMIT`, then scale up.
3. **Mind the earliest data** — see the collection start dates in *Data collection notes*; a range
   before them returns nothing.

## Query rules

- Always add `LIMIT` while exploring data.
- Never `SELECT *` — list the columns you actually need.
- Table and column names are `snake_case`.
- **All timestamps are UTC.** Use explicit half-open date ranges and state the timezone in your
  answer.
- **Partitioned tables** — constrain the partition columns (`year` / `month` / `day`) in the
  `WHERE` clause so Spark prunes partitions instead of scanning the whole topic.

## Data collection notes

- **Nginx request logs** have been collected since **2021-03-24**.
- **Communication events** (Telegram messages) have been collected since **2021-08-31**.
- Most topics refresh **every 10 minutes, or as soon as 200,000 records accumulate**.
- **Improved** topics refresh **every 30 minutes**.
- A few datasets (resume statistics, device info, user classification) are refreshed **overnight on a
  schedule**.

## Topics (datasets)

### Nginx request logs

- Topic: `hdfs://hadoop:9000/topics/nginx_avro/`
- Refresh: every 10 minutes or every 200,000 records. Collected since 2021-03-24.

| Attribute | Description |
|-----------|-------------|
| `HOST` | Host the request was sent to |
| `SERVER_ADDR` | Server address |
| `HTTP_X_FORWARDED_FOR` | Chain of proxy addresses; the last one is the IP that reached the proxy directly |
| `REMOTE_ADDR` | IP address the request was sent from |
| `REMOTE_USER` | User name used in Basic auth (employer API) |
| `DATETIME` | Date and time of the request |
| `REQUEST_METHOD` | Request method |
| `REQUEST_URI` | Request URI (with arguments) |
| `CONNECTION_REQUESTS` | Current number of requests in the connection |
| `STATUS` | Request status |
| `BODY_BYTES_SENT` | Number of bytes sent to the client, excluding the header |
| `HTTP_REFERER` | Where the request was sent from |
| `HTTP_USER_AGENT` | Client application identifier |
| `UPSTREAM_ADDR` | IP and port, or path to the UNIX socket, of the upstream server |
| `UPSTREAM_STATUS` | Status of the response received from the upstream server |
| `UPSTREAM_RESPONSE_TIME` | Time spent receiving the response from the upstream server |
| `REQUEST_TIME` | Time spent handling the request |
| `USER_ID` | Internal user id (`trud_user.id`, stored in cookies) |
| `SESSION_ID` | Unique session identifier (stored in cookies) |
| `DEVICE_ID` | Unique browser identifier (stored in cookies) |

### User events

- Topic: `hdfs://hadoop:9000/topics/userEvents_avro/`
- Test / sandbox: `hdfs://hadoop:9000/topics_test/userEvents_test_avro/`
- Improved: `hdfs://hadoop:9000/topics-improved/userEvents/` (refreshes every 30 minutes)
- Refresh (main topic): every 10 minutes or every 200,000 records.

| Attribute | Description |
|-----------|-------------|
| `REMOTEADDRESS` | IP address the request was sent from |
| `REQUESTURI` | Request URI (with arguments) |
| `REFERERURI` | Where the request was sent from |
| `USERAGENT` | Client application identifier |
| `USERID` | Internal user id (`trud_user.id`, stored in cookies) |
| `SESSIONID` | Unique session identifier (stored in cookies) |
| `DEVICEID` | Unique browser identifier (stored in cookies) |
| `ACTIONDATETIME` | Date and time of the event |
| `ACTIONID` | Id of the user event (see the user-events reference document for details) |
| `ADDITIONAL` | JSON string with extra information about the event |

**Mobile app screens.** For a screen that maps 1:1 to an endpoint, the endpoint path is written
without the `/api/v<version>/` prefix. Screens that cannot be mapped to a path use dedicated aliases.

### Communication events

- Topic: `hdfs://hadoop:9000/topics/communicationEvents_avro/`
- Test / sandbox: `hdfs://hadoop:9000/topics_test/communicationEvents_test_avro/`
- Refresh: every 10 minutes or every 200,000 records. Collected (Telegram messages) since 2021-08-31.
- **Partitioned by `year`, `month`, `day`** — constrain these columns in the `WHERE` clause.

| Attribute | Description |
|-----------|-------------|
| `TYPEID` | Message id (see the list of all message ids) |
| `NOTIFICATIONID` | Message id from `work-utf.notifications` |
| `COMMID` | Unique id linking a message send to its click; unique per user per message |
| `USERID` | Internal user id (`trud_user.id`) |
| `TYPE` | Action type (send, view, click, etc.) |
| `CHANNEL` | Interaction channel (Telegram, email, notification center, etc.) |
| `ACTIONDATETIME` | Date and time of the interaction |
| `SUBJECT` | Message title |
| `ADDITIONAL` | Per-message private parameters (see the reference document) |
| `year` | Event year (partition column) |
| `month` | Event month (partition column) |
| `day` | Event day (partition column) |

### Resume statistics

- Topic: `hdfs://hadoop:9000/topics/resumeStatistics`
- Source: `` `work-stat`.published_resume_statistics_7days `` — converted to the target attribute
  types and written to HDFS.
- Refresh: overnight on a schedule.
- Contains statistics for posted resumes. Only resumes visible in search as of a given date are
  present (blocked resumes are excluded).
- To read the source column descriptions, run in MySQL:
  `` SHOW FULL COLUMNS FROM `work-stat`.published_resume_statistics_7days ``.

| Attribute | Description |
|-----------|-------------|
| `stat_date` | Data-collection date (daily database snapshot) |
| `resume_first_creation_datetime` | Date and time the resume was first created |
| `jobseeker_creation_datetime` | Date and time the jobseeker registered |
| `resume_creation_datetime` | Date and time the resume was created or posted |
| `resume_id` | Resume id |
| `jobseeker_id` | Jobseeker id |
| `jobseeker_identity_hidden` | Personal-data visibility flag: 1 = hidden, 0 = open |
| `sex_rid` | Sex (id from `trud_restriction.part_id = 9`) |
| `birth_year` | Jobseeker's birth year |
| `language_levels` | Language skills stated in the resume |
| `disability_type` | Disability group (id from `trud_restriction.part_id = 54`) |
| `resume_name` | Job title from the resume |
| `resume_type` | Resume completeness type |
| `total_resume_points` | Total resume-completeness score |
| `resume_count_show_total` | Impressions all-time (in search results or on the resume page) |
| `resume_count_show_today` | Impressions today |
| `resume_count_look_total` | Views all-time (opening the resume page) |
| `resume_count_look_today` | Views today |
| `opened_contacts_num_today` | Number of times the jobseeker's contacts were opened today |
| `sent_job_num_total` | Total job offers sent to the resume all-time |
| `sent_job_num_today` | Job offers sent to the resume today (today minus yesterday) |
| `resume_salary` | Salary stated in the resume |
| `resume_region_ids` | Resume cities (list from `trud_resume_region.region_rid`) |
| `jobseeker_region_id` | Jobseeker's city of residence (`town.id`) |
| `education_type_ids` | Education from the resume (list from `trud_education.type_rid`) |
| `category_ids` | Resume categories (list from `trud_resume_category.category_rid`) |
| `jobseeker_skill_unique_ids` | Jobseeker's unique skill ids |
| `resume_jobtype_rids` | Employment type from the resume, array (74 = full-time, 75 = part-time; match `trud_restriction.id`) |
| `resume_area_ids` | Resume area ids |
| `resume_position_ids` | Array of position clouds the candidate considers (list from `work-utf.search_dictionary.syn_group_id`); only the first position appears in the title |
| `position_unique_ids` | Array of position clouds the candidate considers (list from `work-utf.position_unique.id`); only the first position appears in the title |
| `year` | Collection year (derived from `stat_date`) |
| `month` | Collection month (derived from `stat_date`) |
| `day` | Collection day (derived from `stat_date`) |

### Device info

- Topic: `hdfs://hadoop:9000/data/device_type/`
- Improved: `hdfs://hadoop:9000/topics-improved/deviceType/`
- Refresh: overnight on a schedule.

| Attribute | Description |
|-----------|-------------|
| `device_id` | User's device identifier |
| `device` | Device type (`web` = desktop site, `mobile` = mobile site, `app` = jobseeker app, `app_employer` = employer app) |
| `last_visit` | Date of the last visit from this device |

### User classification (jobseekers vs. employers)

- Topic: `hdfs://hadoop:9000/data/user_classification/`
- Improved: `hdfs://hadoop:9000/topics-improved/userClassification/`
- Refresh: overnight on a schedule.

| Attribute | Description |
|-----------|-------------|
| `user_id` | User identifier (`trud_user.id`) |
| `jobseeker_id` | Jobseeker identifier (`trud_jobseeker.id`) |
| `is_jobseeker` | Whether the user is a jobseeker (1 = jobseeker, 0 = recruiter) |
| `is_confirmed_email` | Whether the user's email is confirmed (matches `trud_user.is_confirmed`) |
| `employer_id` | Employer identifier, for recruiter profiles (`trud_employer.id`) |
| `reg_date` | Registration date |

## Reporting the answer

Your stdout is posted back to a person, so make it self-contained: state the number, the method
(which topics and the date range), and any caveats or assumptions.

## Saving and reusing scripts

- **Never save on your own initiative.** Finishing a task and printing the answer must not write
  anything to `results/` or touch `results/INDEX.md`. Saving happens **only after the customer
  explicitly confirms it** (the worker's post-completion code-save prompt).
- Before starting, check `results/INDEX.md` for a script saved from a similar past task; if one fits,
  read it in `results/` and adapt it instead of starting from scratch.
- **Only once save is confirmed**, write the query/analysis script to `results/<name>.py` (PySpark)
  or `results/<name>.sql` (SparkSQL), begin the file with a short English comment describing what it
  does, and add a one-line entry to `results/INDEX.md`: `file_name — short description`.
