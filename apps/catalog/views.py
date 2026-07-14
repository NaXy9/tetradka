"""Public read-only catalog API: subjects and tutor profiles."""

import datetime as dt

from django.db import transaction
from django.db.models import Prefetch
from django.utils.dateparse import parse_datetime
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, generics, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.bookings.services import free_slots
from apps.common.permissions import IsTutor

from .filters import TutorFilter
from .models import Subject, TutorProfile, TutorSubject
from .serializers import (
    SlotSerializer,
    SubjectSerializer,
    TutorDetailSerializer,
    TutorListSerializer,
    TutorProfileSelfSerializer,
    TutorSubjectManageSerializer,
)

# Upper bound on the from/to span, so a single public request cannot force the
# expansion of an unbounded date range. A month covers the weekly calendar UI.
MAX_SLOTS_RANGE = dt.timedelta(days=31)


def _parse_utc_range(params) -> tuple[dt.datetime, dt.datetime]:
    """Validate the required `from`/`to` query params into an aware UTC range.

    Raises ValidationError (rendered as 400) with a per-field message when a
    bound is missing, unparseable, timezone-naive, or when the resulting range
    is empty or wider than MAX_SLOTS_RANGE.
    """
    errors: dict[str, str] = {}
    bounds: dict[str, dt.datetime] = {}
    for name in ("from", "to"):
        raw = params.get(name)
        if not raw:
            errors[name] = "This query parameter is required."
            continue
        try:
            parsed = parse_datetime(raw)
        except ValueError:  # well-formed but out of range, e.g. month 13
            parsed = None
        if parsed is None:
            errors[name] = "Enter a valid ISO 8601 datetime."
        elif parsed.utcoffset() is None:
            errors[name] = "Datetime must include a timezone offset (e.g. a trailing 'Z')."
        else:
            bounds[name] = parsed.astimezone(dt.UTC)
    if errors:
        raise ValidationError(errors)

    start, end = bounds["from"], bounds["to"]
    if end <= start:
        raise ValidationError({"to": "Must be after 'from'."})
    if end - start > MAX_SLOTS_RANGE:
        raise ValidationError({"to": f"The range must not exceed {MAX_SLOTS_RANGE.days} days."})
    return start, end


class SubjectListView(generics.ListAPIView):
    """GET /subjects — the full subject list for filter chips.

    Unpaginated: the subject dictionary is small and the client needs
    all of it at once to render the chips row.
    """

    permission_classes = [permissions.AllowAny]
    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer
    pagination_class = None


class TutorViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /tutors, GET /tutors/{id} — public tutor catalog.

    Publicly readable by design: the same data backs the future web
    landing/catalog, which must work without an account.
    """

    permission_classes = [permissions.AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TutorFilter
    search_fields = ["user__first_name", "user__last_name", "bio"]
    ordering_fields = ["rating", "hourly_rate"]
    # id tie-break keeps page boundaries stable for equal ratings.
    ordering = ["-rating", "id"]

    def get_queryset(self):
        # hourly_rate=0 marks a profile whose tutor onboarding is unfinished
        # (created by role=tutor registration) — hidden until a rate is set.
        # Prefetch with select_related pulls subject rows in the same query as
        # tutor_subjects (one prefetch query instead of two).
        subject_rows = TutorSubject.objects.select_related("subject")
        return (
            TutorProfile.objects.filter(hourly_rate__gt=0)
            .select_related("user")
            .prefetch_related(Prefetch("tutor_subjects", queryset=subject_rows))
        )

    def get_serializer_class(self):
        return TutorListSerializer if self.action == "list" else TutorDetailSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "from",
                type=OpenApiTypes.DATETIME,
                required=True,
                description="Range start, ISO 8601 with timezone offset.",
            ),
            OpenApiParameter(
                "to",
                type=OpenApiTypes.DATETIME,
                required=True,
                description="Range end, ISO 8601 with timezone offset.",
            ),
        ],
        responses=SlotSerializer(many=True),
        description=(
            "Free availability intervals (UTC): the tutor's weekly rules expanded "
            "and minus active bookings, clipped to the range. This is a raw "
            "availability projection — booking additionally enforces a minimum lead "
            "time, so an interval starting very soon may still be rejected at booking."
        ),
    )
    @action(detail=True, methods=["get"])
    def slots(self, request, pk=None):
        """GET /tutors/{id}/slots?from=&to= — free UTC availability intervals.

        Availability expanded from the tutor's weekly rules, minus time taken by
        active bookings, clipped to the requested range and returned in UTC for
        the client to render in the student's timezone. This is a faithful
        projection of free availability: it does NOT apply the booking lead time
        (MIN_BOOKING_LEAD) — that horizon is enforced only when a booking is
        created, so a slot starting very soon may appear here yet be rejected on
        booking. Hidden (unfinished) profiles 404 here as on the detail endpoint.
        """
        tutor = self.get_object()
        start, end = _parse_utc_range(request.query_params)
        slots = [{"starts_at": s, "ends_at": e} for s, e in free_slots(tutor, start, end)]
        return Response(SlotSerializer(slots, many=True).data)


class TutorProfileSelfView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /tutor/profile — the caller's own editable tutor profile.

    Setting a positive hourly_rate here is what makes an onboarding tutor
    visible in the public catalog.
    """

    permission_classes = [IsTutor]
    serializer_class = TutorProfileSelfSerializer
    # PUT is intentionally not exposed: partial updates only, matching /me.
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self) -> TutorProfile:
        return self.request.user.tutor_profile


class TutorSubjectViewSet(viewsets.ModelViewSet):
    """CRUD under /tutor/subjects for the subjects the caller teaches.

    Every action is scoped to the caller's own profile, so another tutor's row
    id is a 404 (never a 403 that would confirm it exists), and `tutor` is set
    from the request user rather than trusted from the body. The onboarding UI
    loads the whole list at once, hence no pagination.
    """

    permission_classes = [IsTutor]
    serializer_class = TutorSubjectManageSerializer
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no user
            return TutorSubject.objects.none()
        return TutorSubject.objects.filter(tutor=self.request.user.tutor_profile).select_related(
            "subject"
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if not getattr(self, "swagger_fake_view", False):
            context["tutor"] = self.request.user.tutor_profile
        return context

    def create(self, request: Request, *args, **kwargs) -> Response:
        with transaction.atomic():
            self._lock_own_profile()
            return super().create(request, *args, **kwargs)

    def update(self, request: Request, *args, **kwargs) -> Response:
        with transaction.atomic():
            self._lock_own_profile()
            return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(tutor=self.request.user.tutor_profile)

    def _lock_own_profile(self) -> None:
        """Serialize a tutor's concurrent subject writes on their profile row.

        Two concurrent creates of the same (subject, level) could each pass the
        serializer's duplicate check against stale state; one would then hit the
        unique constraint and 500. Locking the profile row makes the
        check-then-write see committed state, leaving the DB constraint as a
        backstop. A no-op on SQLite; the real serialization point on PostgreSQL.
        """
        TutorProfile.objects.select_for_update().get(pk=self.request.user.tutor_profile.pk)
