"""Test settings: self-contained, fast, no external services.

Runs on in-memory SQLite by default. CI sets TETRADKA_TEST_DATABASE_URL to a PostgreSQL
DSN so the @pytest.mark.postgres tests (GiST exclusion, row-level locking) execute there.
"""

from .base import *  # noqa: F403
from .base import env

SECRET_KEY = "test-secret-key-not-for-production"
DEBUG = False

if env("TETRADKA_TEST_DATABASE_URL", default=""):
    DATABASES = {"default": env.db("TETRADKA_TEST_DATABASE_URL")}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

# WhiteNoise scans STATIC_ROOT on startup and isn't needed in tests.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m.lower()]  # noqa: F405

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
