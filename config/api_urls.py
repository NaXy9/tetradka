"""Versioned API routes mounted under /api/v1/. Feature routers are added per app."""

from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

app_name = "api"

urlpatterns = [
    path("auth/token", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh", TokenRefreshView.as_view(), name="token_refresh"),
]
