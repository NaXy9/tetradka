"""Local development settings.

Boots out of the box on SQLite with an in-memory broker. Point the env at Docker
services (Postgres, Redis, LiveKit, MinIO) via .env to run the full stack.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])
