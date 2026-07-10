# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking domain: availability, bookings with a strict status machine, reviews.

All datetimes are stored in UTC. AvailabilityRule/Exception are the only place
where the tutor's local timezone appears: rules are expanded into concrete UTC
slots at query time (that expansion lives in services, not here).
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction

from apps.common.models import TimeStampedModel


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class AvailabilityRule(TimeStampedModel):
    """Recurring weekly window in the TUTOR'S timezone (User.timezone)."""

    tutor = models.ForeignKey(
        "catalog.TutorProfile", on_delete=models.CASCADE, related_name="availability_rules"
    )
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="availability_rule_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.start_time}–{self.end_time}"


class AvailabilityException(TimeStampedModel):
    """One-off change for a specific local date: a day off or a replacement window."""

    tutor = models.ForeignKey(
        "catalog.TutorProfile", on_delete=models.CASCADE, related_name="availability_exceptions"
    )
    date = models.DateField()
    is_day_off = models.BooleanField(default=True)
    # Replacement window (tutor's tz); required when is_day_off is False.
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(is_day_off=True, start_time__isnull=True, end_time__isnull=True)
                    | models.Q(
                        is_day_off=False,
                        start_time__isnull=False,
                        end_time__isnull=False,
                        end_time__gt=models.F("start_time"),
                    )
                ),
                name="availability_exception_window_valid",
            ),
            # expand_availability keys exceptions by date and honours one override
            # per date, so a second row for the same date would silently shadow the
            # first; keep the model incapable of representing that.
            models.UniqueConstraint(
                fields=["tutor", "date"], name="uniq_availability_exception_tutor_date"
            ),
        ]

    def __str__(self) -> str:
        kind = "day off" if self.is_day_off else f"{self.start_time}–{self.end_time}"
        return f"{self.date}: {kind}"


class InvalidStatusTransition(Exception):
    """Raised on an attempt to move a Booking along a non-existent edge."""


class Booking(TimeStampedModel):
    """A student's booked slot with a tutor. starts_at/ends_at are UTC."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        COMPLETED = "completed", "Completed"
        CANCELLED_BY_STUDENT = "cancelled_by_student", "Cancelled by student"
        CANCELLED_BY_TUTOR = "cancelled_by_tutor", "Cancelled by tutor"
        NO_SHOW = "no_show", "No-show"

    # The only legal edges of the status machine. completed is terminal.
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.CONFIRMED, Status.CANCELLED_BY_STUDENT},
        Status.CONFIRMED: {
            Status.COMPLETED,
            Status.CANCELLED_BY_STUDENT,
            Status.CANCELLED_BY_TUTOR,
            Status.NO_SHOW,
        },
    }

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings_as_student"
    )
    tutor = models.ForeignKey(
        "catalog.TutorProfile", on_delete=models.PROTECT, related_name="bookings"
    )
    subject = models.ForeignKey(
        "catalog.Subject", on_delete=models.PROTECT, related_name="bookings"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["-starts_at"]
        indexes = [
            models.Index(fields=["tutor", "starts_at"]),
            models.Index(fields=["student", "starts_at"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="booking_ends_after_starts",
            ),
            # The GiST exclusion constraint (no overlapping pending/confirmed bookings
            # per tutor) is added in migration 0002 — PostgreSQL only.
        ]

    def __str__(self) -> str:
        return f"Booking#{self.pk} {self.status} {self.starts_at:%Y-%m-%d %H:%M}"

    def transition_to(self, new_status: str, *, actor=None, reason: str = "") -> None:
        """Move along one edge of the status machine and record an audit log entry.

        Locks the row first: concurrent transitions (payment webhook vs. Celery
        pending-timeout) must serialize, so the edge check runs against the
        committed status, not a stale in-memory one.

        Raises InvalidStatusTransition if the current status has no edge to
        `new_status`, and ValueError if `new_status` is not a valid Status.
        """
        new_status = self.Status(new_status)
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            old_status = self.Status(locked.status)
            if new_status not in self.ALLOWED_TRANSITIONS.get(old_status, set()):
                raise InvalidStatusTransition(f"{old_status} → {new_status} is not allowed")
            locked.status = new_status
            locked.save(update_fields=["status", "updated_at"])
            BookingStatusTransition.objects.create(
                booking=locked,
                from_status=old_status,
                to_status=new_status,
                actor=actor,
                reason=reason,
            )
        self.status = new_status


class BookingStatusTransition(models.Model):
    """Audit log of status-machine transitions. actor=None means the system (Celery)."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=32, choices=Booking.Status.choices)
    to_status = models.CharField(max_length=32, choices=Booking.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.booking_id}: {self.from_status} → {self.to_status}"


class Review(TimeStampedModel):
    """Student's review of a completed booking; feeds TutorProfile.rating (denorm)."""

    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    text = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5),
                name="review_rating_1_to_5",
            ),
        ]

    def __str__(self) -> str:
        return f"Review<{self.booking_id}: {self.rating}>"
