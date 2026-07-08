"""Lessons: the LiveKit room bound to a booking, and its server-side recordings (§5)."""

from django.db import models

from apps.common.models import TimeStampedModel


class Lesson(TimeStampedModel):
    """1-1 with a confirmed Booking. Consent flags are the legal basis for recording (§9)."""

    booking = models.OneToOneField(
        "bookings.Booking", on_delete=models.PROTECT, related_name="lesson"
    )
    livekit_room = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    consent_student = models.BooleanField(default=False)
    consent_tutor = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Lesson<{self.livekit_room}>"


class Recording(TimeStampedModel):
    """Egress output stored in S3/MinIO. Retention 30 days → expires_at (§9)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # egress started, file not delivered yet
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"  # past expires_at, object removed by lifecycle

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="recordings")
    file_key = models.CharField(max_length=512, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    size_bytes = models.BigIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"Recording<{self.file_key or self.pk}: {self.status}>"
