"""Public catalog routes, mounted under /api/v1/."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import SubjectListView, TutorViewSet

# No trailing slashes — matches the URL style of the rest of the API.
router = SimpleRouter(trailing_slash=False)
router.register("tutors", TutorViewSet, basename="tutor")

urlpatterns = [
    path("subjects", SubjectListView.as_view(), name="subject_list"),
    *router.urls,
]
