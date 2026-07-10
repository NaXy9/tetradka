# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Serializers for the booking API: creation payload and read representation."""

from django.utils import timezone
from rest_framework import serializers

from apps.catalog.models import Subject, TutorProfile
from apps.catalog.serializers import SubjectSerializer
from apps.users.models import User

from .models import AvailabilityException, AvailabilityRule, Booking


class AwareDateTimeField(serializers.DateTimeField):
    """DateTimeField that rejects timezone-naive input.

    With USE_TZ, DRF silently assumes the server timezone for a naive datetime,
    which would misread a client's wall-clock time as UTC. A booking must carry
    an explicit offset, matching the strictness of the /slots endpoint.
    """

    def enforce_timezone(self, value):
        if timezone.is_naive(value):
            raise serializers.ValidationError(
                "Datetime must include a timezone offset (e.g. a trailing 'Z')."
            )
        return super().enforce_timezone(value)


class BookingTutorSerializer(serializers.ModelSerializer):
    """The tutor side of a booking: profile id (for linking) plus display name."""

    first_name = serializers.CharField(source="user.first_name")
    last_name = serializers.CharField(source="user.last_name")

    class Meta:
        model = TutorProfile
        fields = ["id", "first_name", "last_name"]


class BookingStudentSerializer(serializers.ModelSerializer):
    """The student side of a booking; only the display name is exposed."""

    class Meta:
        model = User
        fields = ["id", "first_name", "last_name"]


class BookingSerializer(serializers.ModelSerializer):
    """Read representation of a booking for both parties' lists and detail."""

    tutor = BookingTutorSerializer(read_only=True)
    student = BookingStudentSerializer(read_only=True)
    subject = SubjectSerializer(read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "status",
            "starts_at",
            "ends_at",
            "price",
            "subject",
            "tutor",
            "student",
            "created_at",
        ]


class BookingCancelRequestSerializer(serializers.Serializer):
    """POST /bookings/{id}/cancel payload: an optional free-text audit reason."""

    # Capped to the audit log's reason column so an over-long note is a 400, not
    # a silent truncation.
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class BookingCancelSerializer(BookingSerializer):
    """Cancellation response: the updated booking plus the refund owed to the student."""

    refund_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta(BookingSerializer.Meta):
        fields = [*BookingSerializer.Meta.fields, "refund_amount"]


class BookingCreateSerializer(serializers.Serializer):
    """POST /bookings payload. The slot's validity (availability, overlap, lead
    time) and price are decided by create_booking under a row lock; this
    serializer only validates the request shape and the tutor/subject pairing."""

    # Unfinished profiles (hourly_rate=0) are excluded, so booking a hidden tutor
    # fails validation just as it 404s in the catalog.
    tutor = serializers.PrimaryKeyRelatedField(
        queryset=TutorProfile.objects.filter(hourly_rate__gt=0)
    )
    subject = serializers.PrimaryKeyRelatedField(queryset=Subject.objects.all())
    starts_at = AwareDateTimeField()
    ends_at = AwareDateTimeField()

    def validate(self, attrs: dict) -> dict:
        tutor, subject = attrs["tutor"], attrs["subject"]
        if attrs["ends_at"] <= attrs["starts_at"]:
            raise serializers.ValidationError({"ends_at": "Must be after 'starts_at'."})
        if tutor.user_id == self.context["request"].user.id:
            raise serializers.ValidationError("You cannot book a lesson with yourself.")
        if not tutor.tutor_subjects.filter(subject=subject).exists():
            raise serializers.ValidationError(
                {"subject": "This tutor does not teach the selected subject."}
            )
        return attrs


class AvailabilityRuleSerializer(serializers.ModelSerializer):
    """A tutor's recurring weekly availability window, in the tutor's timezone.

    `tutor` is never taken from the request body: the view injects the
    authenticated caller's profile, so a tutor can only touch their own rules.
    """

    class Meta:
        model = AvailabilityRule
        fields = ["id", "weekday", "start_time", "end_time"]

    def validate(self, attrs: dict) -> dict:
        # On a partial update the omitted fields keep their stored values, so the
        # full resulting window is validated, not just the changed part.
        weekday = attrs.get("weekday", getattr(self.instance, "weekday", None))
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if end <= start:
            raise serializers.ValidationError({"end_time": "Must be after 'start_time'."})
        self._reject_overlap(weekday, start, end)
        return attrs

    def _reject_overlap(self, weekday: int, start, end) -> None:
        # Two half-open windows [a, b) and [c, d) overlap iff a < d and c < b.
        # Rules carry only a weekday and wall-clock times, so this comparison is
        # DST-free; the timezone maths lives in expand_availability at projection
        # time. Overlapping rules would otherwise yield duplicated free intervals.
        clashing = AvailabilityRule.objects.filter(
            tutor=self.context["tutor"], weekday=weekday, start_time__lt=end, end_time__gt=start
        )
        if self.instance is not None:
            clashing = clashing.exclude(pk=self.instance.pk)
        if clashing.exists():
            raise serializers.ValidationError(
                "This window overlaps an existing availability rule for that weekday."
            )


class AvailabilityExceptionSerializer(serializers.ModelSerializer):
    """A one-off override for a single local date: a day off or a replacement window.

    Belongs to the authenticated tutor, like a rule. At most one exception may
    exist per date — expand_availability applies a single override per date, so a
    duplicate would silently shadow the rest.
    """

    class Meta:
        model = AvailabilityException
        fields = ["id", "date", "is_day_off", "start_time", "end_time"]

    def validate(self, attrs: dict) -> dict:
        is_day_off = attrs.get("is_day_off", getattr(self.instance, "is_day_off", True))
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if is_day_off:
            if start is not None or end is not None:
                raise serializers.ValidationError(
                    "A day-off exception must not carry a replacement window."
                )
        else:
            if start is None or end is None:
                raise serializers.ValidationError(
                    "A replacement exception needs both 'start_time' and 'end_time'."
                )
            if end <= start:
                raise serializers.ValidationError({"end_time": "Must be after 'start_time'."})
        self._reject_duplicate_date(attrs.get("date", getattr(self.instance, "date", None)))
        return attrs

    def _reject_duplicate_date(self, date) -> None:
        # A friendly 400 for the common case; the DB unique constraint is the last
        # line of defense against a concurrent duplicate (near-impossible for a
        # single tutor editing their own schedule).
        existing = AvailabilityException.objects.filter(tutor=self.context["tutor"], date=date)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError(
                {"date": "An availability exception already exists for this date."}
            )
