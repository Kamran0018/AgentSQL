"""
Data-Agent AI — Database Utilities
====================================
SQLAlchemy-based database engine, connection pooling, schema inspection,
sqlglot read-only validator, tabular file importer, and execution guards.
"""

import ipaddress
import os
import re
import socket
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd
from django.conf import settings
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool
import sqlglot
from sqlglot import exp

# Demo DB path
DEMO_DB_PATH = Path(__file__).resolve().parent / "ecommerce.db"


def demo_db_url() -> str:
    """Return read-only SQLite URL for the bundled demo database."""
    return sqlite_readonly_url(DEMO_DB_PATH)


def sqlite_readonly_url(path: Path | str) -> str:
    """
    Construct a cross-platform SQLite URI connection string with mode=ro.
    """
    resolved = Path(path).resolve()
    # url formatting for sqlite: sqlite:///<path>?mode=ro&uri=true
    uri = resolved.as_uri()  # file:///...
    # strip file://
    path_part = uri.replace("file://", "")
    if not path_part.startswith("/"):
        path_part = "/" + path_part
    return f"sqlite://{path_part}?mode=ro&uri=true"


def dialect_of(url: str) -> str:
    """Identify the SQL dialect from a SQLAlchemy connection string."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if "postgres" in scheme:
        return "postgresql"
    if "mysql" in scheme or "mariadb" in scheme:
        return "mysql"
    return "sqlite"


def _assert_host_allowed(host: str) -> None:
    """
    SSRF Protection: Ensure host does not resolve to private/loopback/link-local IP
    unless ALLOW_PRIVATE_DB_HOSTS is explicitly enabled.
    """
    if getattr(settings, "ALLOW_PRIVATE_DB_HOSTS", False):
        return

    if not host:
        raise ValueError("Database host cannot be empty.")

    try:
        # Resolve all addresses
        addr_infos = socket.getaddrinfo(host, None)
        for family, _, _, _, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError(
                    f"Access to private/local database host '{host}' ({ip_str}) is blocked for security."
                )
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host '{host}': {e}")


def normalize_remote_url(raw: str) -> str:
    """
    Validate, sanitize, and normalize remote PostgreSQL/MySQL URLs.
    - Only postgresql, postgres, mysql, mariadb schemes allowed.
    - Whitelist query params: sslmode, ssl, ssl-mode, ssl_mode, charset, application_name.
    - SSRF host checking.
    - Return standardized SQLAlchemy URL.
    """
    if not raw or not raw.strip():
        raise ValueError("Connection URL cannot be empty.")

    raw = raw.strip()
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme in ("postgres", "postgresql"):
        sa_scheme = "postgresql+psycopg2"
    elif scheme in ("mysql", "mariadb"):
        sa_scheme = "mysql+pymysql"
    else:
        raise ValueError(
            "Unsupported database scheme. Only PostgreSQL ('postgresql://...') and MySQL ('mysql://...') are supported."
        )

    if not parsed.hostname:
        raise ValueError("Connection URL must include a host.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("Connection URL must specify a database name.")

    _assert_host_allowed(parsed.hostname)

    # Whitelist allowed query parameters
    allowed_query_keys = {
        "sslmode", "ssl", "ssl-mode", "ssl_mode", "charset", "application_name"
    }
    raw_query = parse_qs(parsed.query, keep_blank_values=True)
    filtered_query = {}
    for k, v in raw_query.items():
        if k.lower() in allowed_query_keys:
            filtered_query[k] = v
        else:
            raise ValueError(f"Disallowed query parameter in connection URL: '{k}'")

    new_query = urlencode(filtered_query, doseq=True)
    clean_url = urlunparse((
        sa_scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))
    return clean_url


def scrub_password(url_or_message: str) -> str:
    """Strip password components from URLs or error messages to prevent credential leakage."""
    if not url_or_message:
        return ""
    # Pattern matching :password@ in URLs
    scrubbed = re.sub(r":([^:@\s]+)@", ":****@", str(url_or_message))
    return scrubbed


def make_engine(url: str) -> sa.engine.Engine:
    """
    Create a SQLAlchemy Engine with NullPool and strict timeouts.
    """
    parsed = urlparse(url)
    dialect = dialect_of(url)

    if dialect in ("postgresql", "mysql") and parsed.hostname:
        _assert_host_allowed(parsed.hostname)

    if dialect == "sqlite":
        # Extract file path from sqlite URL
        db_path = parsed.path
        if parsed.netloc:
            # Handles URLs like sqlite:////... or sqlite:///C:/...
            db_path = f"{parsed.netloc}{db_path}"
        # On Windows, strip leading slash before drive letter (e.g. /E:/ -> E:/)
        if len(db_path) >= 3 and db_path[0] == "/" and db_path[2] == ":":
            db_path = db_path[1:]

        uri_str = f"file:{db_path}?mode=ro"

        def _sqlite_creator():
            return sqlite3.connect(uri_str, uri=True, check_same_thread=False, timeout=15)

        return create_engine(
            "sqlite://",
            poolclass=NullPool,
            creator=_sqlite_creator,
        )

    connect_args = {}
    if dialect == "postgresql":
        connect_args["connect_timeout"] = 10
    elif dialect == "mysql":
        connect_args["connect_timeout"] = 10

    return create_engine(
        url,
        poolclass=NullPool,
        connect_args=connect_args,
    )


def test_remote_connection(url: str) -> Tuple[bool, int]:
    """
    Test connection to a remote database, verify read access with SELECT 1,
    and return table count. Raises ValueError with scrubbed message on failure.
    """
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            inspector = inspect(conn)
            tables = inspector.get_table_names()
            return True, len(tables)
    except Exception as e:
        msg = scrub_password(str(e))
        raise ValueError(f"Could not connect to database: {msg}")
    finally:
        engine.dispose()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Schema Extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_db_schema(url: Optional[str] = None) -> str:
    """
    Read tables, views, columns, primary keys, and foreign keys from the DB
    and format them for the LLM prompt context (capped at 40 tables).
    """
    if not url:
        url = demo_db_url()

    engine = make_engine(url)
    schema_parts: List[str] = []
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()[:40]
        view_names = inspector.get_view_names()[:10]
        all_entities = [(t, "Table") for t in table_names] + [(v, "View") for v in view_names]

        for entity_name, kind in all_entities:
            quoted_name = engine.dialect.identifier_preparer.quote(entity_name)
            col_lines = []
            try:
                columns = inspector.get_columns(entity_name)
                pk_constraint = inspector.get_pk_constraint(entity_name)
                pks = set(pk_constraint.get("constrained_columns") or [])
                fks = inspector.get_foreign_keys(entity_name)
                fk_map = {
                    col: f"REFERENCES {fk.get('referred_table')}({', '.join(fk.get('referred_columns') or [])})"
                    for fk in fks
                    for col in fk.get("constrained_columns", [])
                }

                for col in columns:
                    cname = col["name"]
                    ctype = str(col["type"])
                    flags = []
                    if cname in pks:
                        flags.append("PRIMARY KEY")
                    if not col.get("nullable", True) and cname not in pks:
                        flags.append("NOT NULL")
                    if cname in fk_map:
                        flags.append(fk_map[cname])
                    suffix = f" ({', '.join(flags)})" if flags else ""
                    col_lines.append(f"  - {cname} {ctype}{suffix}")
            except Exception:
                col_lines.append("  - (schema unavailable)")

            schema_parts.append(f"{kind}: {quoted_name}\n" + "\n".join(col_lines))

    finally:
        engine.dispose()

    return "\n\n".join(schema_parts)


def get_db_schema_structured(url: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return schema list structured for the frontend UI drawer/rail.
    Normalizes types into INTEGER, REAL, TEXT, etc.
    """
    if not url:
        url = demo_db_url()

    dialect = dialect_of(url)
    engine = make_engine(url)
    schema = []
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()[:100]
        views = inspector.get_view_names()[:20]

        with engine.connect() as conn:
            for tname in table_names + views:
                # Row count: only calculate for local SQLite or small tables
                row_count = None
                if dialect == "sqlite":
                    try:
                        res = conn.execute(text(f"SELECT COUNT(*) FROM {engine.dialect.identifier_preparer.quote(tname)}"))
                        row_count = res.scalar()
                    except Exception:
                        row_count = None

                columns = []
                try:
                    cols = inspector.get_columns(tname)
                    pk_constraint = inspector.get_pk_constraint(tname)
                    pks = set(pk_constraint.get("constrained_columns") or [])
                    for c in cols:
                        raw_type = str(c["type"]).upper()
                        if any(k in raw_type for k in ("INT", "BIGINT", "SMALLINT", "SERIAL")):
                            norm_type = "INTEGER"
                        elif any(k in raw_type for k in ("FLOAT", "DOUBLE", "REAL", "NUMERIC", "DECIMAL")):
                            norm_type = "REAL"
                        elif any(k in raw_type for k in ("CHAR", "TEXT", "STRING", "UUID")):
                            norm_type = "TEXT"
                        else:
                            norm_type = raw_type

                        columns.append({
                            "name": c["name"],
                            "type": norm_type,
                            "pk": c["name"] in pks,
                            "not_null": not c.get("nullable", True),
                        })
                except Exception:
                    pass

                schema.append({
                    "name": tname,
                    "row_count": row_count if row_count is not None else "–",
                    "columns": columns,
                })
    finally:
        engine.dispose()

    return schema


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Read-Only SQL Validator (sqlglot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Forbidden AST node types
DISALLOWED_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Into,
    exp.TruncateTable,
    exp.Set,
    exp.Use,
    exp.Pragma,
    exp.Lock,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.LoadData,
)

# Dangerous function names (case-insensitive)
DANGEROUS_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "dblink",
    "load_file",
    "load_extension",
    "readfile",
    "writefile",
    "sleep",
    "benchmark",
    "get_lock",
    "set_config",
    "pg_terminate_backend",
    "system",
}


def validate_read_only(sql: str, dialect: str = "sqlite") -> Tuple[bool, str]:
    """
    Validate that a SQL statement is strictly read-only and safe to execute.
    - Exactly one statement.
    - Root must be Select, Union, or CTE with SELECT.
    - No DDL/DML, multi-statement, or data-modifying CTEs.
    - No SELECT INTO, FOR UPDATE.
    - No dangerous functions.
    """
    if not sql or not sql.strip():
        return False, "Query cannot be empty."

    glot_dialect = "postgres" if dialect == "postgresql" else dialect

    try:
        expressions = sqlglot.parse(sql, read=glot_dialect)
    except Exception as e:
        return False, f"SQL syntax error: {str(e)}"

    if not expressions:
        return False, "No valid SQL statement found."
    if len(expressions) > 1:
        return False, "Multi-statement queries are strictly prohibited."

    ast = expressions[0]
    if ast is None:
        return False, "Empty or invalid SQL syntax."

    # Must be a Select, Union, or CTE starting with Select
    if not isinstance(ast, (exp.Select, exp.Union)):
        return False, f"Only SELECT queries are allowed. Found: {ast.key.upper()}"

    # Check for disallowed nodes anywhere in the syntax tree
    for node in ast.walk():
        if isinstance(node, DISALLOWED_NODES):
            return False, f"Disallowed SQL operation detected: {type(node).__name__}"

        # Disallow SELECT ... INTO
        if isinstance(node, exp.Into):
            return False, "SELECT INTO is prohibited."

        # Disallow FOR UPDATE / FOR SHARE locking
        if isinstance(node, exp.Lock):
            return False, "Row locking (FOR UPDATE / FOR SHARE) is prohibited."

        # Check function calls
        if isinstance(node, (exp.Anonymous, exp.Func)):
            fn_name = node.name.lower()
            if fn_name in DANGEROUS_FUNCTIONS:
                return False, f"Call to dangerous function '{fn_name}' is blocked."

    # Additional text-based checks for edge-case keywords that sqlglot might tokenize differently
    sql_upper = sql.upper()
    if "ATTACH " in sql_upper or "DETACH " in sql_upper:
        return False, "ATTACH/DETACH database is prohibited."
    if "FOR UPDATE" in sql_upper or "FOR SHARE" in sql_upper:
        return False, "Row-level locking (FOR UPDATE) is prohibited."
    if "INTO OUTFILE" in sql_upper or "INTO DUMPFILE" in sql_upper:
        return False, "Writing to files is prohibited."

    return True, ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Execution Engine with Guards & Limits
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def execute_sql(
    url: Optional[str],
    query: str,
    max_rows: Optional[int] = None
) -> Dict[str, Any]:
    """
    Safely execute a SQL query against the given DB URL.
    - Validates read-only status with sqlglot.
    - Sets connection read-only flags & statement timeouts.
    - Caps rows at max_rows (default settings.MAX_RESULT_ROWS).
    - Returns structured result dict with columns, data, truncated status, or error.
    """
    if not url:
        url = demo_db_url()

    if max_rows is None:
        max_rows = getattr(settings, "MAX_RESULT_ROWS", 1000)

    dialect = dialect_of(url)

    # 1. Validation
    is_safe, reason = validate_read_only(query, dialect=dialect)
    if not is_safe:
        return {"success": False, "error": f"Security validation failed: {reason}"}

    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            # 2. Database-level execution guards
            if dialect == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql("SET LOCAL statement_timeout = 15000")  # 15s
            elif dialect == "mysql":
                conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                try:
                    conn.exec_driver_sql("SET SESSION max_execution_time = 15000")
                except Exception:
                    pass
            elif dialect == "sqlite":
                # SQLite progress handler to interrupt queries taking > 15 seconds
                deadline = time.time() + 15
                raw_conn = conn.connection.dbapi_connection
                def progress_handler():
                    if time.time() > deadline:
                        return 1  # 1 aborts execution
                    return 0
                raw_conn.set_progress_handler(progress_handler, 1000)

            # 3. Execution
            cursor = conn.exec_driver_sql(query)
            if not cursor.returns_rows:
                return {
                    "success": True,
                    "columns": [],
                    "data": [],
                    "row_count": 0,
                    "truncated": False,
                }

            columns = list(cursor.keys())
            # Fetch max_rows + 1 to detect truncation
            rows = cursor.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            if truncated:
                rows = rows[:max_rows]

            # Format data values (convert Decimal to float, bytes to representation)
            data = []
            for row in rows:
                formatted_row = []
                for val in row:
                    if isinstance(val, Decimal):
                        formatted_row.append(float(val))
                    elif isinstance(val, (bytes, bytearray)):
                        formatted_row.append(f"<{len(val)} bytes>")
                    else:
                        formatted_row.append(val)
                data.append(formatted_row)

            # Rollback transaction
            conn.rollback()

            return {
                "success": True,
                "columns": columns,
                "data": data,
                "row_count": len(data),
                "truncated": truncated,
            }

    except Exception as e:
        msg = scrub_password(str(e))
        return {"success": False, "error": msg}
    finally:
        engine.dispose()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tabular File Importer (CSV / TSV / Excel)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def sanitize_identifier(name: str) -> str:
    """
    Sanitize table and column names to lower case [a-z0-9_].
    Prefix with 'c_' if it begins with a digit or is empty.
    """
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", str(name)).strip("_").lower()
    if not clean:
        clean = "col"
    if clean[0].isdigit():
        clean = f"c_{clean}"
    return clean


def detect_delimiter(header_line: str) -> str:
    """
    Detect delimiter by checking highest frequency among comma, tab, semicolon, pipe.
    """
    delims = [",", "\t", ";", "|"]
    counts = {d: header_line.count(d) for d in delims}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def import_tabular_file(
    fileobj: Any,
    filename: str,
    dest_path: Path | str
) -> List[str]:
    """
    Imports a CSV/TSV/TXT/XLSX file into a new SQLite database file.
    - Rejects .xls with helpful message.
    - Sanitizes table & column names.
    - Limits total imported rows to MAX_UPLOAD_ROWS.
    - Cleanly deletes the destination file on failure.
    - Returns list of created table names.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    lower_name = filename.lower()
    if lower_name.endswith(".xls"):
        raise ValueError("Legacy .xls format is not supported. Please save your file as .xlsx or .csv.")

    max_rows = getattr(settings, "MAX_UPLOAD_ROWS", 200_000)
    created_tables: List[str] = []

    try:
        # Check empty data before opening database file
        raw_data = fileobj.read() if hasattr(fileobj, "read") else open(fileobj, "rb").read()
        if len(raw_data) == 0:
            raise ValueError("Uploaded file is empty.")

        # Connect to destination sqlite file
        conn = sqlite3.connect(str(dest))

        if lower_name.endswith((".csv", ".tsv", ".txt")):

            # Try utf-8-sig first, then cp1252
            text_content = None
            for enc in ("utf-8-sig", "utf-8", "cp1252"):
                try:
                    text_content = raw_data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

            if text_content is None:
                raise ValueError("Unable to decode text file. Ensure it is encoded in UTF-8 or CP1252.")

            lines = text_content.splitlines()
            if not lines:
                raise ValueError("Uploaded text file contains no content.")

            delim = detect_delimiter(lines[0])
            import io
            df = pd.read_csv(
                io.StringIO(text_content),
                sep=delim,
                nrows=max_rows + 1,
                dtype=object
            )

            if len(df) == 0:
                raise ValueError("Uploaded table contains no rows of data.")
            if len(df) > max_rows:
                raise ValueError(f"Uploaded file exceeds row limit of {max_rows:,} rows.")

            # Base table name from filename
            base_tname = sanitize_identifier(Path(filename).stem)
            table_name = base_tname or "data"

            # Sanitize column names
            clean_cols = []
            seen = set()
            for col in df.columns:
                sc = sanitize_identifier(str(col))
                orig = sc
                idx = 1
                while sc in seen:
                    sc = f"{orig}_{idx}"
                    idx += 1
                seen.add(sc)
                clean_cols.append(sc)
            df.columns = clean_cols

            df.to_sql(table_name, conn, if_exists="replace", index=False)
            created_tables.append(table_name)

        elif lower_name.endswith(".xlsx"):
            # Excel file with possible multiple sheets
            excel_file = pd.ExcelFile(fileobj, engine="openpyxl")
            sheets = excel_file.sheet_names[:20]  # Cap at 20 sheets
            if not sheets:
                raise ValueError("Excel file contains no sheets.")

            total_rows = 0
            seen_tables = set()

            for sname in sheets:
                df = pd.read_excel(excel_file, sheet_name=sname, nrows=max_rows + 1, dtype=object)
                if df.empty or len(df.columns) == 0:
                    continue

                total_rows += len(df)
                if total_rows > max_rows:
                    raise ValueError(f"Total rows across sheets exceeds maximum of {max_rows:,} rows.")

                tname = sanitize_identifier(sname)
                orig_t = tname
                idx = 1
                while tname in seen_tables:
                    tname = f"{orig_t}_{idx}"
                    idx += 1
                seen_tables.add(tname)

                clean_cols = []
                seen_cols = set()
                for col in df.columns:
                    sc = sanitize_identifier(str(col))
                    orig = sc
                    idx = 1
                    while sc in seen_cols:
                        sc = f"{orig}_{idx}"
                        idx += 1
                    seen_cols.add(sc)
                    clean_cols.append(sc)
                df.columns = clean_cols

                df.to_sql(tname, conn, if_exists="replace", index=False)
                created_tables.append(tname)

            if not created_tables:
                raise ValueError("Excel file contained no data rows across any sheets.")

        else:
            raise ValueError("Unsupported file format. Please upload .csv, .tsv, .txt, or .xlsx.")

        conn.commit()
        conn.close()
        return created_tables

    except Exception:
        # Clean up corrupted/partially written destination file
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        raise
