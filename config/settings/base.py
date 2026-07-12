"""Base settings shared across environments.

Service addresses and secrets come from the environment only (no localhost hardcoded
in code). Safe non-network defaults (SQLite, in-memory broker/cache) let the project
boot and run its test suite without any external services.
"""

from pathlib import Path

import environ

# config/settings/base.py -> project root is three levels up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Load a local .env if present (never committed). Real deployments inject env directly.
environ.Env.read_env(BASE_DIR / ".env")

# --- Core ---------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", default="django-insecure-dev-only-change-in-prod")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    # local
    "apps.common",
    "apps.users",
    "apps.catalog",
    "apps.bookings",
    "apps.payments",
    "apps.lessons",
    "apps.processing",
    "apps.notifications",
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

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

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

# --- Database (all times stored in UTC) -----------------------------------
if env("DATABASE_URL", default=""):
    DATABASES = {"default": env.db("DATABASE_URL")}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "db.sqlite3"),
        }
    }

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18n / timezone: store UTC, render in the user's tz at the edges -----
LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static / media -----------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Safe local default; prod/dev switch to Redis via env.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- DRF / OpenAPI ------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # API-wide convention: full-text search query param is ?q= (not DRF's ?search=).
    "SEARCH_PARAM": "q",
    # Scoped rates for public (unauthenticated) endpoints; counters live in the
    # default cache (Redis in prod, so limits are shared across workers).
    "DEFAULT_THROTTLE_RATES": {
        # Registration is limited per client IP to block mass account creation
        # and to slow down email enumeration.
        "auth.register": "10/hour",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Tetradka API",
    "DESCRIPTION": "Tutoring marketplace with video lessons and AI lesson notes.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Celery: named queues for future horizontal split -------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="memory://")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="cache+memory://")
CELERY_TIMEZONE = "UTC"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_QUEUES = {
    "default": {},  # reminders, notifications
    "media": {},  # ffmpeg, audio extraction
    "gpu": {},  # transcription
}
CELERY_TASK_ROUTES: dict[str, dict] = {}

# At-least-once delivery: a task that dies mid-run (worker crash/OOM) is redelivered
# instead of silently ACKed and lost. Every task here is written to be idempotent,
# so a duplicate run is safe; this closes the window where a crash after an external
# side effect (e.g. opening a PSP hold) but before the DB write would drop the write.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Periodic jobs (run by `celery -A config beat`). Sweeping every minute keeps the
# 15-minute pending-payment timeout tight without polling the DB aggressively.
CELERY_BEAT_SCHEDULE = {
    "expire-pending-bookings": {
        "task": "bookings.expire_pending_bookings",
        "schedule": 60.0,
    },
    # Completion is not time-critical — the grace already absorbs a lesson running
    # over — so it sweeps less often than the pending-payment timeout.
    "complete-confirmed-bookings": {
        "task": "bookings.complete_confirmed_bookings",
        "schedule": 300.0,
    },
}

# --- Payments -----------------------------------------------------------------
# The PSP is hidden behind apps.payments.providers.PaymentProvider; dev and tests
# use the mock. PAYMENT_WEBHOOK_SECRET signs/verifies webhook payloads (the mock
# uses HMAC-SHA256 over the raw body); the real value is injected via env in prod.
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", default="mock")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET", default="dev-mock-webhook-secret")
