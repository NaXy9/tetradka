# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Serializers for the booking API: creation payload and read representation."""

from django.utils import timezone
from rest_framework import serializers

from apps.catalog.models import Subject, TutorProfile
from apps.catalog.serializers import SubjectSerializer
from apps.users.models import User

from .models import Booking


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
