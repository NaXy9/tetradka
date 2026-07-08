"""Shared abstract models."""

from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base adding UTC created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
