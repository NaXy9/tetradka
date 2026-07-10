"""User-facing auth and profile routes, mounted under /api/v1/."""

from django.urls import path

from .views import MeView, RegisterView

urlpatterns = [
    path("auth/register", RegisterView.as_view(), name="auth_register"),
    path("me", MeView.as_view(), name="me"),
]
