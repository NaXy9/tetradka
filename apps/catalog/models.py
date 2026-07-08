"""Catalog: subjects and public tutor profiles."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.common.models import TimeStampedModel


class Subject(models.Model):
    """A teachable subject (math, python, english, ...)."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TutorProfile(TimeStampedModel):
    """Tutor's public profile. Rating and lessons_count are denormalized."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tutor_profile"
    )
    bio = models.TextField(blank=True)
    video_intro_url = models.URLField(blank=True)
    hourly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(0)]
    )
    experience_years = models.PositiveSmallIntegerField(default=0)
    education = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    # Refund percentage when a student cancels less than 24h before the lesson
    # (cancelling more than 24h before the lesson always refunds 100%). 0 = no refund.
    late_cancellation_refund_percent = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(100)]
    )
    # Denormalized from Review / completed bookings; recalculated by services.
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    lessons_count = models.PositiveIntegerField(default=0)
    # Earnings after commission, credited when the payment is captured (the capture
    # and this balance credit happen in one DB transaction). Withdrawn via Payout.
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    subjects = models.ManyToManyField(Subject, through="TutorSubject", related_name="tutors")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(late_cancellation_refund_percent__lte=100),
                name="tutor_late_cancel_percent_lte_100",
            ),
        ]

    def __str__(self) -> str:
        return f"TutorProfile<{self.user}>"


class TutorSubject(models.Model):
    """M2M tutor↔subject with a teaching level (exam prep, conversational, ...)."""

    tutor = models.ForeignKey(TutorProfile, on_delete=models.CASCADE, related_name="tutor_subjects")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="tutor_subjects")
    level = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tutor", "subject", "level"], name="uniq_tutor_subject_level"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tutor_id}:{self.subject_id}:{self.level or '-'}"
