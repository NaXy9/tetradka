# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""AI pipeline artifacts: transcript (Whisper) and summary (LLM) per recording (§5).

Pipeline statuses must be visible in admin (§8). Real LLM prompts never live in
this repo (§11) — providers load them from env/private config.
"""

from django.db import models

from apps.common.models import TimeStampedModel


class Transcript(TimeStampedModel):
    """Whisper output: full text plus timestamped segments."""

    recording = models.OneToOneField(
        "lessons.Recording", on_delete=models.CASCADE, related_name="transcript"
    )
    text = models.TextField(blank=True)
    # [{"start": float_sec, "end": float_sec, "text": str}, ...]
    segments = models.JSONField(default=list, blank=True)

    def __str__(self) -> str:
        return f"Transcript<recording={self.recording_id}>"


class Summary(TimeStampedModel):
    """LLM-generated lesson recap: markdown notes, homework, key terms."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    recording = models.OneToOneField(
        "lessons.Recording", on_delete=models.CASCADE, related_name="summary"
    )
    markdown = models.TextField(blank=True)
    homework = models.TextField(blank=True)
    key_terms = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    class Meta:
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"Summary<recording={self.recording_id}: {self.status}>"
