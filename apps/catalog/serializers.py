"""Serializers for the public catalog: subjects and tutor profiles with reviews."""

from rest_framework import serializers

from apps.bookings.models import Booking, Review

from .models import Subject, TutorProfile, TutorSubject


class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ["id", "name", "slug"]


class TutorSubjectSerializer(serializers.ModelSerializer):
    """A subject the tutor teaches, with the teaching level of that row."""

    name = serializers.CharField(source="subject.name")
    slug = serializers.SlugField(source="subject.slug")

    class Meta:
        model = TutorSubject
        fields = ["name", "slug", "level"]


class ReviewSerializer(serializers.ModelSerializer):
    # Only the student's first name is exposed: reviews are public, full
    # identity of the reviewer is not.
    student_first_name = serializers.CharField(source="booking.student.first_name")

    class Meta:
        model = Review
        fields = ["rating", "text", "student_first_name", "created_at"]


class TutorListSerializer(serializers.ModelSerializer):
    """Catalog card: enough for the list screen, no heavy text fields."""

    first_name = serializers.CharField(source="user.first_name")
    last_name = serializers.CharField(source="user.last_name")
    subjects = TutorSubjectSerializer(source="tutor_subjects", many=True)

    class Meta:
        model = TutorProfile
        fields = [
            "id",
            "first_name",
            "last_name",
            "subjects",
            "hourly_rate",
            "rating",
            "lessons_count",
            "experience_years",
            "is_verified",
        ]


class TutorDetailSerializer(TutorListSerializer):
    """Full tutor page: adds profile texts, cancellation policy and reviews."""

    reviews = serializers.SerializerMethodField()

    class Meta(TutorListSerializer.Meta):
        fields = TutorListSerializer.Meta.fields + [
            "bio",
            "education",
            "video_intro_url",
            "late_cancellation_refund_percent",
            "reviews",
        ]

    def get_reviews(self, obj: TutorProfile) -> list[dict]:
        # The detail endpoint serves a single object, so this is one extra
        # query, not an N+1; revisit with pagination if reviews grow large.
        #
        # The completed-status guard is defensive: review creation (not built
        # yet) should only allow completed bookings, but the public page must
        # not rely on that unstated invariant — e.g. a booking disputed after
        # being reviewed must not keep its review visible.
        reviews = (
            Review.objects.filter(booking__tutor=obj, booking__status=Booking.Status.COMPLETED)
            .select_related("booking__student")
            .order_by("-created_at")
        )
        return ReviewSerializer(reviews, many=True).data
