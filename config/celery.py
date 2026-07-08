"""Celery application entry point (`celery -A config worker/beat`)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("tetradka")
# All Celery settings live in Django settings under the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
