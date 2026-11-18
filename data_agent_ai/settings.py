"""
Data-Agent AI — Settings
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables: check agent_app/.env first, then root .env
agent_env = BASE_DIR / "agent_app" / ".env"
root_env = BASE_DIR / ".env"
if agent_env.exists():
    load_dotenv(agent_env)
if root_env.exists():
    load_dotenv(root_env)

# Debug
DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

# Secret Key
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG and "test" not in sys.argv:
        raise ValueError("DJANGO_SECRET_KEY must be set in production when DEBUG=False.")
    SECRET_KEY = "django-insecure-dev-key-change-me-in-production-only"

# Allowed Hosts
allowed_hosts_env = os.getenv("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()] or ["*"]

# CSRF Trusted Origins
csrf_env = os.getenv("CSRF_TRUSTED_ORIGINS", "")
if csrf_env:
    CSRF_TRUSTED_ORIGINS = [orig.strip() for orig in csrf_env.split(",") if orig.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "agent_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "data_agent_ai.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "data_agent_ai.wsgi.application"
ASGI_APPLICATION = "data_agent_ai.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Auth Redirects
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ═══════════════════════════════════════════════════
# DATA-AGENT AI CUSTOM SETTINGS
# ═══════════════════════════════════════════════════
USER_DATA_DIR = BASE_DIR / "user_data"
MAX_UPLOAD_MB = 10
MAX_UPLOAD_ROWS = 200_000
MAX_SOURCES_PER_USER = 5
MAX_RESULT_ROWS = 1000

# Private DB Hosts permission (SSRF prevention)
ALLOW_PRIVATE_DB_HOSTS = os.getenv("ALLOW_PRIVATE_DB_HOSTS", str(DEBUG)).lower() in ("true", "1", "yes")

# Daily Question Limit per user
DAILY_QUESTION_LIMIT = int(os.getenv("DAILY_QUESTION_LIMIT", "50"))

# Production Security Flags
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
