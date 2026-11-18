# AgentSQL: Multi-Agent Autonomous Data Assistant

> Ask plain-English questions about your databases and spreadsheet files. Powered by **Django**, **LangGraph**, **Groq** (`openai/gpt-oss-20b`), and **SQLAlchemy**.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Django](https://img.shields.io/badge/framework-Django-green.svg)](https://www.djangoproject.com/)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-purple.svg)](https://github.com/langchain-ai/langgraph)

---

## 🌟 Overview & Capabilities

AgentSQL is an enterprise-grade AI analytics workspace. It enables business users, developers, and analysts to query data using natural language while strictly enforcing multi-tenant isolation and database-level read-only safety.

- **Multi-Source Support:** Query the built-in demo e-commerce SQLite database, upload your own spreadsheet files (`.csv`, `.tsv`, `.txt`, `.xlsx`), or connect remote cloud PostgreSQL and MySQL databases.
- **Autonomous Multi-Agent Loop:** Orchestrated using LangGraph (`sql_writer` → `sql_executor` with self-healing retry up to 3x → `visualizer`).
- **Interactive "Aurora Light Pro" UI:** Schema explorer drawer, live conversation history, SQL explanation, one-click CSV export, and auto-generated Chart.js visualizations.
- **Enterprise-Grade Security:** Strict read-only AST enforcement via `sqlglot`, Fernet-encrypted credentials, SSRF protection against private IP probing, 15-second timeouts, and a 1,000-row cap.
- **Usage Governance:** Per-user daily question quotas to protect LLM quota limits.

---

## 🏗️ Architecture & Pipeline Flow

The workflow is driven by a compiled LangGraph state graph with conversational memory and dynamic dialect detection:

```
                  ┌────────────────────────┐
                  │    User Question       │
                  └───────────┬────────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │       sql_writer       │◄─────────────┐
                  │  (Schema + Memory +    │              │
                  │   Dialect Hints)       │              │
                  └───────────┬────────────┘              │
                              │                           │
                              ▼                           │
                  ┌────────────────────────┐              │
                  │   sqlglot AST Guard    │              │
                  │  (Blocks DDL, DML,     │              │
                  │   Locks & Functions)   │              │
                  └───────────┬────────────┘              │
                              │                           │
                              ▼                           │
                  ┌────────────────────────┐              │ Retry on Error
                  │      sql_executor      │              │ (max 3 times)
                  │ (DB-level Read-Only    │              │
                  │  + 15s Timeout Guard)  │              │
                  └───────────┬────────────┘              │
                              │                           │
                     Execution Succeeded?                 │
                         /          \                     │
                       Yes           No ──────────────────┘
                       /              \ (After 3 attempts)
                      ▼                ▼
          ┌─────────────────────┐   ┌─────────────────────┐
          │     visualizer      │   │   Friendly Error    │
          │ (Auto Chart Config) │   │  + Technical Trace  │
          └───────────┬─────────┘   └─────────────────────┘
                      │
                      ▼
          ┌─────────────────────┐
          │ Table + Chart.js UI │
          └─────────────────────┘
```

---

## 🛡️ Security Architecture

| Defense Layer | Mechanism | Details |
|---|---|---|
| **SQL AST Validation** | `sqlglot` | Parses and validates queries prior to DB execution. Allows exactly one statement (must be `SELECT` / `WITH ... SELECT`). Rejects all DDL (`CREATE`, `DROP`, `ALTER`), DML (`INSERT`, `UPDATE`, `DELETE`), `ATTACH`, `PRAGMA`, `SELECT INTO`, and `FOR UPDATE`. |
| **Dangerous Functions** | Blacklist | Blocks execution of malicious DB functions such as `pg_read_file`, `pg_sleep`, `load_file`, `load_extension`, `dblink`, `system`, and `benchmark`. |
| **Database-Level Guard** | Connection Session | PostgreSQL: `SET TRANSACTION READ ONLY` + `SET LOCAL statement_timeout=15000`.<br>MySQL: `SET SESSION TRANSACTION READ ONLY`.<br>SQLite: `mode=ro` URI + `set_progress_handler` 15-second timeout. |
| **SSRF Prevention** | DNS & IP Resolution | When `ALLOW_PRIVATE_DB_HOSTS=False`, database host resolution rejects loopback (`127.0.0.1`), private RFC1918 subnets, and cloud metadata endpoints (`169.254.169.254`). |
| **Credential Encryption** | Fernet (AES-128-CBC) | Remote database credentials are encrypted at rest using keys derived via SHA-256. Passwords are scrubbed from HTML, JSON responses, and server logs. |
| **Multi-Tenant Isolation** | Django ORM Filtering | Every `DataSource` and `ChatTurn` belongs to `request.user`. Attempts to access or delete foreign sources return HTTP 404. |

---

## ⚙️ Environment Variables

Copy `.env.example` to `agent_app/.env` (or project root `.env`):

| Variable | Type | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | String | *Required* | API key for Groq Cloud LLM service. |
| `GROQ_MODEL` | String | `openai/gpt-oss-20b` | Model name on Groq. |
| `DJANGO_SECRET_KEY` | String | *Required in Prod* | Django cryptographic secret key. |
| `DJANGO_DEBUG` | Boolean | `True` | Set to `False` in production deployment. |
| `DJANGO_ALLOWED_HOSTS`| String | `*` | Comma-separated list of host/domain names. |
| `CSRF_TRUSTED_ORIGINS`| String | `""` | Comma-separated list of trusted origins for CSRF. |
| `DATASOURCE_ENCRYPTION_KEY` | String | *(Derived)* | Base64-encoded 32-byte Fernet key. If omitted, derived from `DJANGO_SECRET_KEY`. |
| `ALLOW_PRIVATE_DB_HOSTS` | Boolean | `=DEBUG` | Set to `False` in production to prevent SSRF against internal hosts. |
| `DAILY_QUESTION_LIMIT` | Integer | `50` | Maximum queries permitted per user per day. |

---

## 🚀 Local Quickstart

### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/your-username/data_agent_ai.git
cd data_agent_ai

python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create `agent_app/.env`:
```ini
GROQ_API_KEY=gsk_your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-20b
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=local-dev-secret-key-replace-in-production
ALLOW_PRIVATE_DB_HOSTS=True
DAILY_QUESTION_LIMIT=50
```

### 3. Initialize Databases
```bash
# Seed the demo SQLite e-commerce database:
python setup_database.py

# Run Django database migrations:
python manage.py migrate
```

### 4. Run Development Server
```bash
python manage.py runserver
```
Visit `http://127.0.0.1:8000` in your browser. Create an account via **Sign Up** or use existing test credentials (`admin` / `admin123`).

---

## 📊 Using Your Own Data

1. Log into your account and open the **Data Sources** modal in the sidebar.
2. **File Upload Tab:** Upload any `.csv`, `.tsv`, `.txt`, or `.xlsx` spreadsheet (up to 10 MB, 200,000 rows). The application sanitizes table and column names and loads them into an isolated, dedicated SQLite database.
3. **Cloud Database Tab:** Provide a connection string (`postgresql://user:pass@host:5432/dbname` or `mysql://user:pass@host:3306/dbname`). The application tests connectivity, verifies read access, encrypts the connection string, and extracts the live schema.
4. Switch seamlessly between your sources and the bundled demo e-commerce database at any time.

---

## 🧪 Running Automated Tests

The repository includes a comprehensive 34-test suite validating SQL security, SSRF defenses, file parsing, multi-tenancy isolation, daily rate limits, and multi-agent retry logic (mocking the Groq API):

```bash
python manage.py test agent_app
```

---

## 🚢 Production Deployment (Render / Railway / Docker)

This application is ready for containerized or PaaS deployment using `Procfile`, `render.yaml`, and `whitenoise`:

1. **Procfile:** Contains `web: gunicorn data_agent_ai.wsgi`.
2. **Static Files:** WhiteNoise is configured with compressed manifest storage (`CompressedManifestStaticFilesStorage`). Run `python manage.py collectstatic --noinput`.
3. **Persistent Disk:** Ensure a persistent volume is mounted for `user_data/` and `db.sqlite3` (or connect a managed PostgreSQL instance via `DATABASES["default"]`).
4. **Environment Variables:** Set `DJANGO_DEBUG=False`, `DJANGO_SECRET_KEY`, `ALLOW_PRIVATE_DB_HOSTS=False`, and `GROQ_API_KEY`.

---

## ⚠️ Limitations & Notes

- **Supported Engines:** Read-only SQLite, PostgreSQL, and MySQL. Other engines (MSSQL, Oracle, Redshift) are not currently supported.
- **Read-Only Guardrails:** All queries must be read-only `SELECT` statements. Modifications, schema changes, and transactions are blocked.
- **Row Cap:** Query results are capped at 1,000 rows for browser performance and responsiveness.
- **AI-Generated SQL:** While queries pass AST validation and automated healing loops, large language models can still occasionally misinterpret complex questions. Users should review the generated SQL before making business decisions.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
