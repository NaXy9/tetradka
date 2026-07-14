"""Serializers for the public catalog: subjects and tutor profiles with reviews."""

from rest_framework import serializers

from apps.bookings.models import Booking, Review

from .models import Subject, TutorProfile, TutorSubject


class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ["id", "name", "slug"]


class SlotSerializer(serializers.Serializer):
    """One free UTC interval of a tutor's availability; rendered in the client's tz."""

    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()


class TutorSubjectSerializer(serializers.ModelSerializer):
    """A subject the tutor teaches, with the teaching level of that row."""

    name = serializers.CharField(source="subject.name")
    slug = serializers.SlugField(source="subject.slug")

    class Meta:
        model = TutorSubject
        fields = ["name", "slug", "level"]


class TutorProfileSelfSerializer(serializers.ModelSerializer):
    """The tutor's own editable profile (GET/PATCH /tutor/profile).

    Onboarding starts at ``hourly_rate = 0``, which keeps the profile hidden
    from the public catalog; setting a positive rate is what publishes the
    tutor. Denormalized stats, the verification flag and the cancellation
    policy are read-only here — the latter feeds refund maths and is changed
    through a dedicated flow, not free-form profile edits.
    """

    subjects = TutorSubjectSerializer(source="tutor_subjects", many=True, read_only=True)

    class Meta:
        model = TutorProfile
        fields = [
            "id",
            "bio",
            "hourly_rate",
            "education",
            "experience_years",
            "video_intro_url",
            "late_cancellation_refund_percent",
            "is_verified",
            "rating",
            "lessons_count",
            "balance",
            "subjects",
        ]
        read_only_fields = [
            "id",
            "late_cancellation_refund_percent",
            "is_verified",
            "rating",
            "lessons_count",
            "balance",
            "subjects",
        ]


class TutorSubjectManageSerializer(serializers.ModelSerializer):
    """A subject the authenticated tutor teaches, for self-service management.

    `subject` is writable by id and echoed back with its name/slug; `tutor` is
    taken from the request, never the body, so a tutor edits only their own
    rows. `level` is a free-text teaching level (exam prep, conversational, ...).
    """

    subject = serializers.PrimaryKeyRelatedField(queryset=Subject.objects.all())
    name = serializers.CharField(source="subject.name", read_only=True)
    slug = serializers.SlugField(source="subject.slug", read_only=True)

    class Meta:
        model = TutorSubject
        fields = ["id", "subject", "name", "slug", "level"]

    def validate(self, attrs: dict) -> dict:
        # On a partial update the omitted fields keep their stored values, so
        # the full resulting (subject, level) pair is checked, not just the part
        # being changed.
        subject = attrs.get("subject", getattr(self.instance, "subject", None))
        level = attrs.get("level", getattr(self.instance, "level", "") or "")
        self._reject_duplicate(subject, level)
        return attrs

    def _reject_duplicate(self, subject: Subject, level: str) -> None:
        # A friendly 400 for the common case; the (tutor, subject, level) unique
        # constraint is the last line of defense against a concurrent duplicate.
        existing = TutorSubject.objects.filter(
            tutor=self.context["tutor"], subject=subject, level=level
        )
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("You already teach this subject at this level.")


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
