"""Public read-only catalog API: subjects and tutor profiles."""

from django.db.models import Prefetch
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, permissions, viewsets

from .filters import TutorFilter
from .models import Subject, TutorProfile, TutorSubject
from .serializers import SubjectSerializer, TutorDetailSerializer, TutorListSerializer


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
