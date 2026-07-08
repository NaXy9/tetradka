"""In-app/push notifications (§5): type + payload, read state."""

from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class Notification(TimeStampedModel):
    class Type(models.TextChoices):
        BOOKING_CONFIRMED = "booking_confirmed", "Booking confirmed"
        BOOKING_CANCELLED = "booking_cancelled", "Booking cancelled"
        LESSON_REMINDER = "lesson_reminder", "Lesson reminder"
        SUMMARY_READY = "summary_ready", "Summary ready"
        PAYOUT_PAID = "payout_paid", "Payout paid"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(max_length=32, choices=Type.choices)
    payload = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self) -> str:
        return f"Notification<{self.user_id}: {self.type}>"
