# Task: Let users query their own data (CSV/Excel upload + cloud PostgreSQL/MySQL) in AgentSQL

## Context
AgentSQL is a Django app (apps: `agent_app`, project: `data_agent_ai`) where a logged-in user asks
plain-English questions, and a LangGraph pipeline (`agent_app/ai_agent.py`: sql_writer → sql_executor
(retry up to 3x) → visualizer) writes and runs SQL and returns a table + Chart.js chart.
Today it only queries one hardcoded SQLite file (`agent_app/ecommerce.db`), and `db_utils.execute_sql`
runs whatever SQL the LLM produces with a normal read/write connection.

Stack: Django, langgraph, langchain-groq, python-dotenv. Frontend: single template
`agent_app/templates/agent_app/index.html` (vanilla JS, CSRF header `X-CSRFToken`).

## Goal
Each logged-in user can (a) upload a CSV/TSV/XLSX file or (b) paste a cloud PostgreSQL/MySQL connection
string, switch between their sources and the demo DB, and ask questions against the selected source.
Everything must be read-only and private per user.

## Do NOT
- Do not remove existing features (chat memory, Explain SQL, auto chart, CSV export, schema drawer).
- Do not log or return connection strings/passwords anywhere (logs, JSON, HTML, error messages).
- Do not use user-supplied filenames as filesystem paths.

## Steps (do in this order, run tests after each)

### 1. Dependencies
Add to `requirements.txt`: sqlalchemy, sqlglot, pandas, openpyxl, cryptography, psycopg2-binary, pymysql.

### 2. Settings (`data_agent_ai/settings.py`)
Read from env (load `agent_app/.env` with python-dotenv): `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`,
`DJANGO_ALLOWED_HOSTS`. Add constants: `USER_DATA_DIR = BASE_DIR/"user_data"`, `MAX_UPLOAD_MB=10`,
`MAX_UPLOAD_ROWS=200_000`, `MAX_SOURCES_PER_USER=5`, `MAX_RESULT_ROWS=1000`,
`ALLOW_PRIVATE_DB_HOSTS` (env, defaults to DEBUG). Add `user_data/` to `.gitignore`.

### 3. Model + encryption
- `agent_app/crypto.py`: Fernet `encrypt()/decrypt()`; key from env `DATASOURCE_ENCRYPTION_KEY`, else
  derived from SECRET_KEY (sha256 → urlsafe base64).
- `agent_app/models.py`: `DataSource(owner FK User, name, kind in {"file","remote"}, dialect, file_path,
  encrypted_url, display_info (never contains password), created_at)`. Run `makemigrations`.

### 4. Rewrite `agent_app/db_utils.py` on SQLAlchemy (data layer)
All functions take a SQLAlchemy URL string, not a global DB path.
- `sqlite_readonly_url(path)` → `sqlite:///<Path.resolve().as_uri()>?mode=ro&uri=true` (cross-platform);
  `demo_db_url()`; `dialect_of(url)`.
- `make_engine(url)`: `NullPool`; connect timeout 10s; for postgres/mysql first call
  `_assert_host_allowed` (SSRF: unless `ALLOW_PRIVATE_DB_HOSTS`, resolve the host and reject any
  non-global IP); for MySQL translate `?ssl=true` / `?ssl-mode=REQUIRED` into a PyMySQL ssl context.
- `normalize_remote_url(raw)`: accept only `postgres://`, `postgresql://`, `mysql://`, `mariadb://`
  → map to `postgresql+psycopg2` / `mysql+pymysql`. Require host and database. Whitelist query keys
  to `sslmode, ssl, ssl-mode, ssl_mode, charset, application_name` (reject `host`, `sslrootcert`, etc.).
  Reject `sqlite://` and everything else.
- `test_remote_connection(url)`: connect, `SELECT 1`, count tables; raise `ValueError` with a message
  that has the password scrubbed.
- `get_db_schema(url)` (text for the LLM prompt) and `get_db_schema_structured(url)` (for the UI) using
  `sqlalchemy.inspect`: tables AND views, columns, PKs, foreign keys. Cap tables (40 for prompt,
  100 for UI). Quote identifiers via `engine.dialect.identifier_preparer.quote`. Skip row counts for
  big remote schemas. Normalize UI types to INTEGER/REAL/TEXT/other.
- `validate_read_only(sql, dialect)` using `sqlglot` (dialects sqlite/postgres/mysql): exactly one
  statement; root must be Select/Union/Subquery; reject any Insert, Update, Delete, Drop, Create,
  Alter, Merge, Command, Into, Copy, TruncateTable, Set, Use, Pragma, Attach, Detach, Lock,
  Transaction, Commit, Rollback, Grant, LoadData anywhere in the tree (catches data-modifying CTEs);
  reject dangerous functions (pg_sleep, pg_read_file, pg_read_binary_file, pg_ls_dir, lo_import,
  lo_export, dblink, load_file, load_extension, readfile, writefile, sleep, benchmark, get_lock,
  set_config, pg_terminate_backend...). Return `(ok, reason)`; the reason is fed back to the LLM.
- `execute_sql(url, query, max_rows=settings.MAX_RESULT_ROWS)`: validate first, then run with
  `conn.exec_driver_sql` (not `text()`), apply per-transaction guards, `fetchmany(max_rows+1)`, return
  `{"success", "columns", "data", "truncated"}` or `{"success": False, "error": ...}`; always
  `rollback()`. Guards: Postgres `SET TRANSACTION READ ONLY` + `SET LOCAL statement_timeout=15000`;
  MySQL `SET SESSION TRANSACTION READ ONLY` + best-effort `MAX_EXECUTION_TIME`; SQLite
  `set_progress_handler` deadline of 15 s. Use SQL (not connection options) so it also works behind
  PgBouncer/Neon/Supabase poolers. Convert Decimal→float, bytes→"<N bytes>".
- `import_tabular_file(fileobj, filename, dest_path)`: .csv/.tsv/.txt/.xlsx only (reject .xls with a
  helpful message). Do NOT use pandas' `sep=None` sniffer (fails on valid CSVs); detect the delimiter
  as whichever of `, ; \t |` appears most in the header line. Try utf-8-sig then cp1252. Each CSV →
  one table, each Excel sheet → one table (max 20 sheets). Sanitize table/column names to unique
  lowercase `[a-z0-9_]` identifiers (prefix `c_` if starting with a digit). Enforce `MAX_UPLOAD_ROWS`.
  Write into a new SQLite file; delete it on any failure. Raise `ValueError` with friendly messages.

### 5. Agent + prompts
- `AgentState` gets `db_url`. `run_analytics_agent(question, db_url, history=None)`.
- `sql_writer_node`: schema from `get_db_schema(db_url)`; pass `dialect_name` and `dialect_hints`
  into the prompt. `sql_executor_node`: `execute_sql(db_url, sql)`.
- `prompts.py`: replace "SQLite" with `{dialect_name}`/`{dialect_hints}`; add `DIALECT_NAMES` and
  `DIALECT_HINTS` for sqlite/postgresql/mysql (e.g. Postgres: `date_trunc`, `to_char`, cast to numeric
  before ROUND, quote mixed-case names). Add rules: read-only SELECT only, one statement, use only
  schema names, quote reserved words, add LIMIT to listings.
- `ChatGroq(model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), temperature=0)`.

### 6. Views + URLs (`agent_app/views.py`, `urls.py`)
- Session key `active_source_id` ("demo" or a DataSource id). `_active_source(request)` returns
  `(db_url, info)`; it MUST filter by `owner=request.user` and fall back to the demo DB otherwise.
  Switching sources clears `chat_history`.
- `home` GET: render schema for the active source; if it fails (e.g. cloud DB offline) still render
  the page with a `schema_error` banner. Pass `active_source`, `data_sources`, `max_sources`,
  `max_upload_mb`. `home` POST: use the active source; include `truncated` in the JSON.
- New `@login_required` + POST-only endpoints, all JSON:
  - `api/sources/upload/` (multipart): enforce source limit and size limit; store at
    `USER_DATA_DIR/<user_id>/<uuid4>.db`; create DataSource; select it.
  - `api/sources/connect/` (JSON `connection_url`, `name`): `normalize_remote_url` →
    `test_remote_connection` → store `crypto.encrypt(url)`; `display_info` = "host / database" only.
  - `api/sources/select/`, `api/sources/delete/` (delete also removes the file; guard against path
    traversal by resolving the path and checking it stays under `USER_DATA_DIR`).
  - Return 404 for sources owned by other users.

### 7. UI (`index.html`)
- Sidebar button "Data Sources" + modal listing Demo + user's sources (Use / Delete, Active tag) and
  two tabs: "Upload a file" and "Connect a cloud database" (connection string in a
  `type="password"` input, optional name, hint to use a read-only DB user).
- Show the active source name in the sidebar footer and stat card. Show the demo example chips only
  for the demo source. Show a banner when the schema can't be read. Show a note in the table footer
  when `truncated` is true. Show "–" when a table's row count is unknown.
- Restrict the existing chip click handler to `.chip[data-q]`.
- After a successful upload/connect/select/delete, `location.reload()`.

### 8. Tests (`agent_app/tests.py`) — all must pass: `python manage.py test agent_app`
Set `os.environ.setdefault("GROQ_API_KEY","test-key")` at the top (the Groq client is created at import).
- SQL validator: allows SELECT/CTE/UNION on all 3 dialects; blocks DDL/DML, multi-statement,
  data-modifying CTEs, SELECT INTO, FOR UPDATE, PRAGMA/ATTACH, dangerous functions, garbage input.
- Demo DB: schema text includes FK; write attempts rejected AND the file refuses writes at DB level;
  row limit sets `truncated`; SQL errors are returned, not raised.
- Import: sanitized names, duplicate/blank headers, non-UTF8 CSV, Excel sheets→tables, bad inputs
  (empty, header-only, .exe, .xls, fake .xlsx) raise ValueError and leave no file, row limit.
- Remote URLs: accepts postgres/mysql URLs; rejects sqlite, no host/db, `?host=`, `?sslrootcert=`;
  private/loopback/link-local hosts blocked when `ALLOW_PRIVATE_DB_HOSTS=False`; password never in
  error messages; encryption round trip.
- Views (use a temp `USER_DATA_DIR` via `override_settings`): login required; upload creates and
  selects a source; bad/oversized uploads rejected; source limit; user A cannot select/delete/see
  user B's source (and a forged session id falls back to demo); delete removes row + file; path
  traversal refused; connect stores ciphertext (password absent from DB, HTML, and JSON; mock
  `test_remote_connection`); unreachable source doesn't break the page.
- Pipeline with a fake LLM object (patch `ai_agent.llm`): answers from an uploaded CSV with the right
  schema/dialect in the prompt; a blocked `DELETE` triggers a retry and a corrected query; gives up
  after 3 failures with data intact.

### 9. Docs
Rewrite `README.md` (features, security table, env var table, "Using your own data", tests) and add
`agent_app/.env.example` (GROQ_API_KEY, GROQ_MODEL, DJANGO_* vars, DATASOURCE_ENCRYPTION_KEY,
ALLOW_PRIVATE_DB_HOSTS).

## Acceptance criteria
1. `pip install -r requirements.txt && python manage.py migrate && python manage.py test agent_app` passes.
2. Uploading a CSV then asking "how many rows per month?" returns a table and a chart.
3. Pasting a `postgresql://...` string connects, lists tables in the Schema drawer, and answers questions.
4. Even with an admin/superuser DB account, no `INSERT/UPDATE/DELETE/DROP` can ever succeed.
5. No password appears in HTML, JSON responses, error messages, or the DataSource table (ciphertext only).
6. Two different users never see each other's sources or data.

## Notes
- Verify against a real Postgres instance if possible (mixed-case table names, views, numeric and
  timestamptz columns, statement timeout). MySQL support is untested against a real server.
- Commit on a feature branch, not directly on main.