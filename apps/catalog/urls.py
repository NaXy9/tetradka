"""Public catalog routes plus a tutor's self-service profile, mounted under /api/v1/."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    SubjectListView,
    TutorProfileSelfView,
    TutorSubjectViewSet,
    TutorViewSet,
)

# No trailing slashes — matches the URL style of the rest of the API.
router = SimpleRouter(trailing_slash=False)
router.register("tutors", TutorViewSet, basename="tutor")
router.register("tutor/subjects", TutorSubjectViewSet, basename="tutor-subject")

urlpatterns = [
    path("subjects", SubjectListView.as_view(), name="subject_list"),
    path("tutor/profile", TutorProfileSelfView.as_view(), name="tutor_profile"),
    *router.urls,
]
