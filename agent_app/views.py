"""
Data-Agent AI — Views
=====================
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .ai_agent import explain_sql, run_analytics_agent
from .crypto import encrypt
from .db_utils import (
    demo_db_url,
    dialect_of,
    get_db_schema_structured,
    import_tabular_file,
    normalize_remote_url,
    scrub_password,
    sqlite_readonly_url,
    test_remote_connection,
)
from .models import ChatTurn, DataSource, UsageDay

logger = logging.getLogger("agent_app")


# ═══════════════════════════════════════════════════
# HELPER: Active Source Resolver
# ═══════════════════════════════════════════════════

def _active_source(request) -> Tuple[str, dict]:
    """
    Determine the active database URL and metadata for the current user.
    Falls back safely to the demo database if unassigned or invalid.
    """
    active_id = request.session.get("active_source_id", "demo")
    demo_url = demo_db_url()

    if active_id != "demo":
        try:
            source = DataSource.objects.get(id=int(active_id), owner=request.user)
            if source.kind == "file":
                fpath = Path(source.file_path)
                if fpath.exists():
                    return sqlite_readonly_url(fpath), {
                        "id": source.id,
                        "name": source.name,
                        "kind": source.kind,
                        "dialect": source.dialect,
                        "display_info": source.display_info or fpath.name,
                        "is_demo": False,
                    }
            elif source.kind == "remote":
                return source.get_url(), {
                    "id": source.id,
                    "name": source.name,
                    "kind": source.kind,
                    "dialect": source.dialect,
                    "display_info": source.display_info,
                    "is_demo": False,
                }
        except (DataSource.DoesNotExist, ValueError, TypeError):
            request.session["active_source_id"] = "demo"

    # Default: Demo SQLite DB
    return demo_url, {
        "id": "demo",
        "name": "Demo E-Commerce DB",
        "kind": "file",
        "dialect": "sqlite",
        "display_info": "ecommerce.db (20 products, 100 orders)",
        "is_demo": True,
    }


def _check_and_increment_daily_usage(user) -> Tuple[bool, int, int]:
    """
    Check if user has remaining questions today.
    Returns: (is_allowed, remaining_questions, total_limit)
    """
    today = timezone.now().date()
    limit = getattr(settings, "DAILY_QUESTION_LIMIT", 50)
    with transaction.atomic():
        usage, _ = UsageDay.objects.select_for_update().get_or_create(
            user=user, date=today, defaults={"count": 0}
        )
        if usage.count >= limit:
            return False, 0, limit
        usage.count += 1
        usage.save()
        remaining = max(0, limit - usage.count)
        return True, remaining, limit


# ═══════════════════════════════════════════════════
# AUTH VIEWS
# ═══════════════════════════════════════════════════

@require_http_methods(["GET", "POST"])
def signup_view(request):
    """User Registration."""
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("home")
    else:
        form = UserCreationForm()

    return render(request, "agent_app/signup.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request):
    """User Login."""
    if request.user.is_authenticated:
        return redirect("home")

    error = None
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username", "").strip(),
            password=request.POST.get("password", ""),
        )
        if user is not None:
            login(request, user)

            if not request.POST.get("remember"):
                request.session.set_expiry(0)

            next_url = request.POST.get("next") or request.GET.get("next")
            if next_url and not next_url.startswith("/login") and not next_url.startswith("/logout"):
                return redirect(next_url)
            return redirect("home")

        error = "Invalid username or password. Please try again."

    return render(request, "agent_app/login.html", {"error": error})


def logout_view(request):
    """User Logout."""
    logout(request)
    return redirect("login")


# ═══════════════════════════════════════════════════
# HEALTHCHECK
# ═══════════════════════════════════════════════════

def healthz_view(request):
    """Unauthenticated health probe."""
    return JsonResponse({"status": "ok"})


# ═══════════════════════════════════════════════════
# MAIN CONSOLE
# ═══════════════════════════════════════════════════

@login_required
def home(request):
    db_url, active_info = _active_source(request)

    if request.method == "GET":
        schema = []
        schema_error = None
        try:
            schema = get_db_schema_structured(db_url)
        except Exception as e:
            schema_error = f"Could not load schema for {active_info['name']}: {scrub_password(str(e))}"

        # Fetch recent chat turns for this active source
        source_obj = None
        if not active_info["is_demo"]:
            source_obj = DataSource.objects.filter(id=active_info["id"], owner=request.user).first()

        turns = ChatTurn.objects.filter(user=request.user, source=source_obj).order_by("-created_at")[:10]
        # reverse chronological for UI display
        chat_history = [
            {"question": t.question, "sql": t.sql, "created_at": t.created_at.strftime("%H:%M")}
            for t in reversed(turns)
        ]

        # Calculate remaining questions
        today = timezone.now().date()
        usage = UsageDay.objects.filter(user=request.user, date=today).first()
        used_count = usage.count if usage else 0
        limit = getattr(settings, "DAILY_QUESTION_LIMIT", 50)
        remaining_questions = max(0, limit - used_count)

        user_sources = DataSource.objects.filter(owner=request.user).order_by("-created_at")

        return render(
            request,
            "agent_app/index.html",
            {
                "database_schema": schema,
                "schema_error": schema_error,
                "active_source": active_info,
                "data_sources": user_sources,
                "chat_history": chat_history,
                "max_sources": getattr(settings, "MAX_SOURCES_PER_USER", 5),
                "max_upload_mb": getattr(settings, "MAX_UPLOAD_MB", 10),
                "remaining_questions": remaining_questions,
                "daily_limit": limit,
            },
        )

    if request.method == "POST":
        start_time = time.time()
        try:
            body = json.loads(request.body)
            question = body.get("question", "").strip()

            if not question:
                return JsonResponse({"error": "Please provide a question."}, status=400)

            # Check daily rate limit
            is_allowed, remaining, limit = _check_and_increment_daily_usage(request.user)
            if not is_allowed:
                return JsonResponse({
                    "error": f"Daily question limit reached ({limit}/{limit}). Please try again tomorrow.",
                    "daily_limit_exceeded": True,
                    "remaining_questions": 0,
                }, status=429)

            # Retrieve last 10 turns as LLM memory
            source_obj = None
            if not active_info["is_demo"]:
                source_obj = DataSource.objects.filter(id=active_info["id"], owner=request.user).first()

            past_turns = ChatTurn.objects.filter(user=request.user, source=source_obj).order_by("-created_at")[:10]
            history_context = [
                {"question": t.question, "sql": t.sql}
                for t in reversed(past_turns)
            ]

            # Execute pipeline
            result = run_analytics_agent(question, db_url=db_url, history=history_context)

            intent = result.get("intent", "query")
            message = result.get("message", "")
            sql = result.get("sql_query", "")

            # Persistent history: only persist real database queries
            if intent == "query":
                ChatTurn.objects.create(
                    user=request.user,
                    source=source_obj,
                    question=question,
                    sql=sql,
                )

            # Structured response
            data_res = result.get("data_result", {})
            columns = data_res.get("columns", [])
            data = data_res.get("data", [])
            error = result.get("error", "")
            iterations = result.get("iteration_count", 0)
            truncated = data_res.get("truncated", False)

            # Friendly error handling
            friendly_error = None
            raw_error = None
            if error:
                friendly_error = "I couldn't answer that with a valid query — try rephrasing your question."
                raw_error = error

            duration = round(time.time() - start_time, 2)
            logger.info(
                "Query executed: user=%s source=%s dialect=%s intent=%s iter=%s success=%s duration=%ss",
                request.user.id,
                active_info["id"],
                active_info["dialect"],
                intent,
                iterations,
                bool(not error),
                duration,
            )

            return JsonResponse({
                "intent": intent,
                "message": message,
                "sql_query": sql,
                "columns": columns,
                "data": data,
                "row_count": len(data),
                "truncated": truncated,
                "error": friendly_error,
                "raw_error": raw_error,
                "iterations": iterations,
                "chart_config": result.get("chart_config", {}),
                "remaining_questions": remaining,
            })

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)
        except Exception as e:
            err_msg = str(e)
            if "groq" in err_msg.lower() or "rate_limit" in err_msg.lower() or "429" in err_msg:
                return JsonResponse({
                    "error": "The AI service is temporarily busy or rate-limited. Please retry in a moment."
                }, status=503)
            return JsonResponse({"error": f"Server error: {scrub_password(err_msg)}"}, status=500)

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ═══════════════════════════════════════════════════
# DATA SOURCE MANAGEMENT APIS
# ═══════════════════════════════════════════════════

@login_required
@require_http_methods(["POST"])
def upload_source_view(request):
    """Upload CSV/TSV/XLSX and create an isolated SQLite DataSource."""
    user = request.user
    max_sources = getattr(settings, "MAX_SOURCES_PER_USER", 5)

    if DataSource.objects.filter(owner=user).count() >= max_sources:
        return JsonResponse({"error": f"You have reached the maximum limit of {max_sources} data sources."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"error": "No file uploaded."}, status=400)

    max_mb = getattr(settings, "MAX_UPLOAD_MB", 10)
    if uploaded_file.size > max_mb * 1024 * 1024:
        return JsonResponse({"error": f"File exceeds maximum allowed size of {max_mb} MB."}, status=400)

    # Destination directory: user_data/<user_id>/
    user_dir = settings.USER_DATA_DIR / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    random_filename = f"{uuid.uuid4().hex}.db"
    dest_path = user_dir / random_filename

    try:
        tables = import_tabular_file(uploaded_file, uploaded_file.name, dest_path)
        source_name = request.POST.get("name", "").strip() or Path(uploaded_file.name).stem
        source = DataSource.objects.create(
            owner=user,
            name=source_name,
            kind="file",
            dialect="sqlite",
            file_path=str(dest_path),
            display_info=f"{len(tables)} table(s) ({', '.join(tables[:3])})",
        )
        request.session["active_source_id"] = source.id
        return JsonResponse({"status": "ok", "source_id": source.id, "name": source.name})

    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Failed to process file: {str(e)}"}, status=500)


@login_required
@require_http_methods(["POST"])
def connect_source_view(request):
    """Validate and connect a remote PostgreSQL / MySQL database."""
    user = request.user
    max_sources = getattr(settings, "MAX_SOURCES_PER_USER", 5)

    if DataSource.objects.filter(owner=user).count() >= max_sources:
        return JsonResponse({"error": f"You have reached the maximum limit of {max_sources} data sources."}, status=400)

    try:
        body = json.loads(request.body)
        raw_url = body.get("connection_url", "").strip()
        custom_name = body.get("name", "").strip()

        if not raw_url:
            return JsonResponse({"error": "Connection URL is required."}, status=400)

        clean_url = normalize_remote_url(raw_url)
        _, table_count = test_remote_connection(clean_url)

        dialect = dialect_of(clean_url)
        parsed = clean_url.split("@")[-1]  # host:port/db
        host_db_info = parsed.split("?")[0]

        source_name = custom_name or f"{dialect.capitalize()} DB ({host_db_info})"
        source = DataSource(
            owner=user,
            name=source_name,
            kind="remote",
            dialect=dialect,
            display_info=host_db_info,
        )
        source.set_url(clean_url)
        source.save()

        request.session["active_source_id"] = source.id
        return JsonResponse({"status": "ok", "source_id": source.id, "tables": table_count})

    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Connection failed: {scrub_password(str(e))}"}, status=400)


@login_required
@require_http_methods(["POST"])
def select_source_view(request):
    """Switch active data source."""
    try:
        body = json.loads(request.body)
        source_id = str(body.get("source_id", "")).strip()

        if source_id == "demo":
            request.session["active_source_id"] = "demo"
            return JsonResponse({"status": "ok", "source": "demo"})

        source = get_object_or_404(DataSource, id=int(source_id), owner=request.user)
        request.session["active_source_id"] = source.id
        return JsonResponse({"status": "ok", "source": source.id, "name": source.name})

    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid source ID."}, status=400)


@login_required
@require_http_methods(["POST"])
def delete_source_view(request):
    """Delete a DataSource and its underlying file if file-based."""
    try:
        body = json.loads(request.body)
        source_id = body.get("source_id")
        if not source_id:
            return JsonResponse({"error": "Missing source_id"}, status=400)

        # Ensure source exists and belongs to current user (raises 404 otherwise)
        source = get_object_or_404(DataSource, id=int(source_id), owner=request.user)

        # Remove local file safely
        if source.kind == "file" and source.file_path:
            fpath = Path(source.file_path).resolve()
            user_data_root = settings.USER_DATA_DIR.resolve()
            if user_data_root in fpath.parents and fpath.exists():
                try:
                    fpath.unlink()
                except Exception as e:
                    logger.warning("Could not unlink %s: %s", fpath, e)

        # If deleted source was active, revert to demo
        if request.session.get("active_source_id") == source.id:
            request.session["active_source_id"] = "demo"

        source.delete()
        return JsonResponse({"status": "ok"})

    except Http404:
        raise
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid source ID."}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# ═══════════════════════════════════════════════════
# EXPLAIN SQL
# ═══════════════════════════════════════════════════

@login_required
@require_http_methods(["POST"])
def explain_view(request):
    """Generate a natural-language explanation of a SQL query."""
    try:
        body = json.loads(request.body)
        sql = body.get("sql", "").strip()

        if not sql:
            return JsonResponse({"error": "No SQL query provided."}, status=400)

        explanation = explain_sql(sql)
        return JsonResponse({"explanation": explanation})

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)
    except Exception as e:
        return JsonResponse({"error": f"Server error: {str(e)}"}, status=500)


# ═══════════════════════════════════════════════════
# CLEAR HISTORY
# ═══════════════════════════════════════════════════

@login_required
@require_http_methods(["POST"])
def clear_history_view(request):
    """Clear chat turns for the active source."""
    db_url, active_info = _active_source(request)
    source_obj = None
    if not active_info["is_demo"]:
        source_obj = DataSource.objects.filter(id=active_info["id"], owner=request.user).first()

    ChatTurn.objects.filter(user=request.user, source=source_obj).delete()
    return JsonResponse({"status": "cleared"})
