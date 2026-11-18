"""
Data-Agent AI — Comprehensive Test Suite
=========================================
Tests covering:
  • Read-only SQL validator across dialects (sqlglot)
  • Tabular file importer (CSV, TSV, Excel, sanitization, errors)
  • Remote URL normalization, security & SSRF prevention
  • Fernet connection string encryption
  • Database execution engine, guards, and limits
  • Views & permissions (login, upload, connect, switch, delete, multi-tenant isolation)
  • Rate limiting & daily question quota
  • Multi-agent pipeline with fake LLM
  • Unauthenticated healthcheck /healthz/
  • Production configuration and security checks
"""

import io
import os
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("GROQ_API_KEY", "test-key-mock")

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from agent_app import crypto, db_utils
from agent_app.models import ChatTurn, DataSource, UsageDay

User = get_user_model()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. SQL Validator Tests (sqlglot read-only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SQLValidatorTests(TestCase):
    def test_allows_valid_select_queries(self):
        valid_queries = [
            ("SELECT * FROM products;", "sqlite"),
            ("SELECT name, SUM(price) FROM products GROUP BY name HAVING SUM(price) > 100;", "sqlite"),
            ("WITH recent_orders AS (SELECT * FROM orders WHERE amount > 50) SELECT * FROM recent_orders;", "sqlite"),
            ("SELECT a.id, b.name FROM orders a JOIN products b ON a.product_id = b.id;", "postgresql"),
            ("SELECT date_trunc('month', created_at), COUNT(*) FROM orders GROUP BY 1;", "postgresql"),
            ("SELECT `id`, `name` FROM `products` WHERE `price` >= 10.5 ORDER BY `id` DESC LIMIT 10;", "mysql"),
            ("SELECT id FROM t1 UNION SELECT id FROM t2;", "sqlite"),
        ]
        for query, dialect in valid_queries:
            ok, reason = db_utils.validate_read_only(query, dialect=dialect)
            self.assertTrue(ok, f"Expected query to be valid: {query} (reason: {reason})")

    def test_blocks_ddl_and_dml(self):
        blocked_queries = [
            ("INSERT INTO products (name) VALUES ('Hacked')", "sqlite"),
            ("UPDATE products SET price = 0", "sqlite"),
            ("DELETE FROM products WHERE id = 1", "sqlite"),
            ("DROP TABLE products", "sqlite"),
            ("ALTER TABLE products ADD COLUMN secret TEXT", "sqlite"),
            ("CREATE TABLE evil (id INT)", "sqlite"),
            ("TRUNCATE TABLE orders", "postgresql"),
        ]
        for query, dialect in blocked_queries:
            ok, reason = db_utils.validate_read_only(query, dialect=dialect)
            self.assertFalse(ok, f"Expected DDL/DML query to be blocked: {query}")

    def test_blocks_multi_statements(self):
        query = "SELECT 1; DROP TABLE products;"
        ok, reason = db_utils.validate_read_only(query, dialect="sqlite")
        self.assertFalse(ok)
        self.assertIn("Multi-statement", reason)

    def test_blocks_data_modifying_ctes(self):
        query = "WITH deleted AS (DELETE FROM orders RETURNING *) SELECT * FROM deleted;"
        ok, reason = db_utils.validate_read_only(query, dialect="postgresql")
        self.assertFalse(ok)

    def test_blocks_select_into_and_locking(self):
        q1 = "SELECT * INTO new_table FROM orders;"
        ok1, _ = db_utils.validate_read_only(q1, dialect="postgresql")
        self.assertFalse(ok1)

        q2 = "SELECT * FROM orders FOR UPDATE;"
        ok2, _ = db_utils.validate_read_only(q2, dialect="postgresql")
        self.assertFalse(ok2)

    def test_blocks_dangerous_functions(self):
        dangerous = [
            "SELECT pg_sleep(5);",
            "SELECT pg_read_file('/etc/passwd');",
            "SELECT load_file('/etc/passwd');",
            "SELECT sleep(5);",
            "SELECT load_extension('evil');",
        ]
        for q in dangerous:
            ok, reason = db_utils.validate_read_only(q, dialect="postgresql")
            self.assertFalse(ok, f"Expected function to be blocked: {q}")

    def test_blocks_attach_detach(self):
        ok, _ = db_utils.validate_read_only("ATTACH DATABASE 'foo.db' AS foo;", dialect="sqlite")
        self.assertFalse(ok)

    def test_blocks_garbage_input(self):
        ok, reason = db_utils.validate_read_only("NOT A VALID SQL STATEMENT AT ALL !!!", dialect="sqlite")
        self.assertFalse(ok)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Database Execution & Guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DatabaseExecutionTests(TestCase):
    def test_demo_db_schema_extraction(self):
        schema_text = db_utils.get_db_schema()
        self.assertIn("Table: products", schema_text)
        self.assertIn("Table: orders", schema_text)
        self.assertIn("PRIMARY KEY", schema_text)

    def test_demo_db_schema_includes_foreign_keys(self):
        schema_text = db_utils.get_db_schema()
        self.assertIn("REFERENCES", schema_text)

    def test_execute_sql_success_and_row_cap(self):
        res = db_utils.execute_sql(db_utils.demo_db_url(), "SELECT * FROM orders;", max_rows=10)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]), 10)
        self.assertTrue(res["truncated"])

    def test_execute_sql_blocks_write_at_validator_level(self):
        res = db_utils.execute_sql(db_utils.demo_db_url(), "DELETE FROM products;")
        self.assertFalse(res["success"])
        self.assertIn("Security validation failed", res["error"])

    def test_execute_sql_handles_syntax_errors_gracefully(self):
        res = db_utils.execute_sql(db_utils.demo_db_url(), "SELECT * FROM nonexistent_table_12345;")
        self.assertFalse(res["success"])
        self.assertIn("error", res)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Tabular File Importer Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TabularImportTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_import_csv_and_sanitize_columns(self):
        csv_data = b"Product ID,Total Revenue $,1st Order Date\n1,100.50,2026-01-01\n2,250.00,2026-01-02\n"
        dest_db = Path(self.temp_dir) / "test.db"
        tables = db_utils.import_tabular_file(io.BytesIO(csv_data), "My Sales.csv", dest_db)

        self.assertIn("my_sales", tables)
        self.assertTrue(dest_db.exists())

        url = db_utils.sqlite_readonly_url(dest_db)
        res = db_utils.execute_sql(url, "SELECT * FROM my_sales;")
        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]), 2)
        # Check column names sanitized
        self.assertIn("product_id", res["columns"])
        self.assertIn("c_1st_order_date", res["columns"])

    def test_import_tsv_delimiter_detection(self):
        tsv_data = b"product\tprice\tquantity\nLaptop\t1200\t5\nMouse\t25\t20\n"
        dest_db = Path(self.temp_dir) / "tsv_test.db"
        tables = db_utils.import_tabular_file(io.BytesIO(tsv_data), "hardware.tsv", dest_db)
        self.assertIn("hardware", tables)
        url = db_utils.sqlite_readonly_url(dest_db)
        res = db_utils.execute_sql(url, "SELECT count(*) FROM hardware;")
        self.assertTrue(res["success"])
        self.assertEqual(res["data"][0][0], 2)

    def test_rejects_legacy_xls(self):
        dest_db = Path(self.temp_dir) / "test.db"
        with self.assertRaises(ValueError) as ctx:
            db_utils.import_tabular_file(io.BytesIO(b"dummy"), "report.xls", dest_db)
        self.assertIn("Legacy .xls format is not supported", str(ctx.exception))

    def test_rejects_empty_file(self):
        dest_db = Path(self.temp_dir) / "test.db"
        with self.assertRaises(ValueError):
            db_utils.import_tabular_file(io.BytesIO(b""), "empty.csv", dest_db)
        self.assertFalse(dest_db.exists())

    def test_rejects_unsupported_extensions(self):
        dest_db = Path(self.temp_dir) / "test.db"
        with self.assertRaises(ValueError):
            db_utils.import_tabular_file(io.BytesIO(b"binary"), "malware.exe", dest_db)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Remote URLs & Encryption Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RemoteURLAndCryptoTests(TestCase):
    def test_fernet_encryption_roundtrip(self):
        secret = "postgresql://user:SuperP@ss123!@db.example.com:5432/production"
        enc = crypto.encrypt(secret)
        self.assertNotEqual(secret, enc)
        dec = crypto.decrypt(enc)
        self.assertEqual(secret, dec)

    def test_normalize_remote_url_success(self):
        url = "postgresql://user:pass@remote-host.example.com:5432/analytics?sslmode=require"
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 0, "", ("93.184.216.34", 5432))]
            normalized = db_utils.normalize_remote_url(url)
            self.assertTrue(normalized.startswith("postgresql+psycopg2://"))
            self.assertIn("sslmode=require", normalized)

    def test_normalize_remote_url_mysql(self):
        url = "mysql://root:password123@remote-host.example.com:3306/shop"
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 0, "", ("93.184.216.34", 3306))]
            normalized = db_utils.normalize_remote_url(url)
            self.assertTrue(normalized.startswith("mysql+pymysql://"))

    def test_normalize_remote_url_rejects_sqlite(self):
        with self.assertRaises(ValueError):
            db_utils.normalize_remote_url("sqlite:///local.db")

    def test_normalize_remote_url_rejects_disallowed_params(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 0, "", ("93.184.216.34", 5432))]
            with self.assertRaises(ValueError):
                db_utils.normalize_remote_url("postgresql://u:p@remote-host.example.com/db?sslrootcert=/etc/evil")

    def test_blocks_ssrf_private_and_loopback_hosts(self):
        private_hosts = ["localhost", "127.0.0.1", "192.168.1.100", "10.0.0.5", "169.254.169.254"]
        with override_settings(ALLOW_PRIVATE_DB_HOSTS=False):
            for host in private_hosts:
                with self.assertRaises(ValueError):
                    db_utils._assert_host_allowed(host)

    def test_scrubs_password_from_error_messages(self):
        err = "Connection failed: postgresql://admin:MySecretPass123@1.2.3.4:5432/db"
        scrubbed = db_utils.scrub_password(err)
        self.assertNotIn("MySecretPass123", scrubbed)
        self.assertIn(":****@", scrubbed)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Views & Multi-Tenant Access Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ViewAndIsolationTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.user_a = User.objects.create_user(username="user_a", password="pass_user_a")
        self.user_b = User.objects.create_user(username="user_b", password="pass_user_b")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_home_redirects_anonymous_users(self):
        res = self.client.get("/")
        self.assertRedirects(res, "/login/?next=/")

    @override_settings(USER_DATA_DIR=Path(tempfile.gettempdir()) / "test_user_data")
    def test_upload_creates_source_and_selects_it(self):
        self.client.force_login(self.user_a)
        csv_content = b"item,qty\nApple,5\nBanana,10\n"
        uploaded = io.BytesIO(csv_content)
        uploaded.name = "fruits.csv"

        response = self.client.post("/api/sources/upload/", {"file": uploaded, "name": "Fruits DB"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(DataSource.objects.filter(owner=self.user_a, name="Fruits DB").exists())

    def test_user_cannot_select_or_delete_other_user_source(self):
        src_b = DataSource.objects.create(
            owner=self.user_b,
            name="Private B DB",
            kind="file",
            dialect="sqlite",
            file_path="fake/path.db",
        )
        self.client.force_login(self.user_a)

        # Attempt to select user B's source
        res_sel = self.client.post(
            "/api/sources/select/",
            {"source_id": src_b.id},
            content_type="application/json",
        )
        self.assertEqual(res_sel.status_code, 404)

        # Attempt to delete user B's source
        res_del = self.client.post(
            "/api/sources/delete/",
            {"source_id": src_b.id},
            content_type="application/json",
        )
        self.assertEqual(res_del.status_code, 404)
        self.assertTrue(DataSource.objects.filter(id=src_b.id).exists())

    def test_source_limit_enforced(self):
        self.client.force_login(self.user_a)
        for i in range(5):
            DataSource.objects.create(
                owner=self.user_a,
                name=f"Src {i}",
                kind="file",
                dialect="sqlite",
            )
        csv_content = b"a,b\n1,2\n"
        up = io.BytesIO(csv_content)
        up.name = "limit.csv"
        res = self.client.post("/api/sources/upload/", {"file": up})
        self.assertEqual(res.status_code, 400)
        self.assertIn("maximum limit", res.json()["error"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Usage Quota & History Persistence Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QuotaAndHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="quota_user", password="password")

    @override_settings(DAILY_QUESTION_LIMIT=2)
    def test_daily_question_limit_enforced_and_resets(self):
        self.client.force_login(self.user)

        with patch("agent_app.views.run_analytics_agent") as mock_agent:
            mock_agent.return_value = {
                "sql_query": "SELECT 1;",
                "data_result": {"columns": ["1"], "data": [[1]]},
                "error": "",
                "iteration_count": 1,
            }

            # Query 1: OK
            r1 = self.client.post("/", {"question": "q1"}, content_type="application/json")
            self.assertEqual(r1.status_code, 200)

            # Query 2: OK
            r2 = self.client.post("/", {"question": "q2"}, content_type="application/json")
            self.assertEqual(r2.status_code, 200)

            # Query 3: Limit Exceeded (HTTP 429)
            r3 = self.client.post("/", {"question": "q3"}, content_type="application/json")
            self.assertEqual(r3.status_code, 429)
            self.assertIn("Daily question limit reached", r3.json()["error"])

            # Verify reset on next day
            usage = UsageDay.objects.get(user=self.user)
            usage.date = timezone.now().date() - timedelta(days=1)
            usage.save()

            r4 = self.client.post("/", {"question": "q4"}, content_type="application/json")
            self.assertEqual(r4.status_code, 200)

    def test_chat_history_persisted_and_cleared(self):
        self.client.force_login(self.user)
        ChatTurn.objects.create(user=self.user, question="Test Question?", sql="SELECT 1;")
        self.assertEqual(ChatTurn.objects.filter(user=self.user).count(), 1)

        r_clear = self.client.post("/api/clear-history/")
        self.assertEqual(r_clear.status_code, 200)
        self.assertEqual(ChatTurn.objects.filter(user=self.user).count(), 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Multi-Agent Pipeline & Retry with Fake LLM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AgentPipelineTests(TestCase):
    def test_pipeline_retry_and_correction_flow(self):
        from agent_app.ai_agent import run_analytics_agent

        class MockResponse:
            def __init__(self, content):
                self.content = content

        call_count = 0
        def fake_invoke(prompt):
            nonlocal call_count
            prompt_str = str(prompt)
            if "intent" in prompt_str.lower() or "classify the user's intent" in prompt_str.lower():
                return MockResponse('{"intent": "query", "message": ""}')
            if "visualizer" in prompt_str.lower() or "chart_type" in prompt_str:
                return MockResponse('{"chart_type": "bar", "title": "Top Sales", "labels": ["A"], "datasets": [{"label": "Qty", "data": [10]}]}')
            call_count += 1
            # On first attempt return blocked DELETE, on second return valid SELECT
            if call_count == 1:
                return MockResponse("DELETE FROM products;")
            return MockResponse("SELECT name, price FROM products LIMIT 5;")

        with patch("agent_app.ai_agent.llm.invoke", side_effect=fake_invoke):
            res = run_analytics_agent("Show products")
            self.assertTrue(res.get("data_result", {}).get("success"))
            self.assertEqual(res.get("iteration_count"), 2)
            self.assertIn("SELECT name, price FROM products", res.get("sql_query"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. Healthcheck & Groq 503 Handling
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HealthAndResilienceTests(TestCase):
    def test_healthz_unauthenticated(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_groq_failure_returns_503(self):
        user = User.objects.create_user(username="groq_user", password="password")
        self.client.force_login(user)

        with patch("agent_app.views.run_analytics_agent") as mock_agent:
            mock_agent.side_effect = Exception("Groq API rate_limit reached 429")
            response = self.client.post("/", {"question": "How much revenue?"}, content_type="application/json")
            self.assertEqual(response.status_code, 503)
            self.assertIn("busy or rate-limited", response.json()["error"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. Intent Routing & History Guard Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IntentRoutingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="intent_user", password="password")
        self.client.force_login(self.user)

    def test_greeting_intent_flow_and_no_chat_turn(self):
        with patch("agent_app.views.run_analytics_agent") as mock_agent:
            mock_agent.return_value = {
                "intent": "greeting",
                "message": "Hello! I am AgentSQL.",
                "sql_query": "",
                "data_result": {},
                "iteration_count": 0,
            }
            res = self.client.post("/", {"question": "Hi"}, content_type="application/json")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["intent"], "greeting")
            self.assertEqual(data["message"], "Hello! I am AgentSQL.")
            self.assertEqual(data["sql_query"], "")
            self.assertEqual(ChatTurn.objects.filter(user=self.user).count(), 0)

    def test_query_intent_creates_chat_turn(self):
        with patch("agent_app.views.run_analytics_agent") as mock_agent:
            mock_agent.return_value = {
                "intent": "query",
                "message": "",
                "sql_query": "SELECT * FROM orders LIMIT 10;",
                "data_result": {"columns": ["id"], "data": [[1]], "success": True},
                "iteration_count": 1,
            }
            res = self.client.post("/", {"question": "Show 10 orders"}, content_type="application/json")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["intent"], "query")
            self.assertEqual(ChatTurn.objects.filter(user=self.user).count(), 1)
            turn = ChatTurn.objects.filter(user=self.user).first()
            self.assertEqual(turn.question, "Show 10 orders")
            self.assertEqual(turn.sql, "SELECT * FROM orders LIMIT 10;")

