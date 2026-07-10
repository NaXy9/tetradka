"""API views: registration and the authenticated user's own profile."""

from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .serializers import MeSerializer, RegisterResponseSerializer, RegisterSerializer


class RegisterView(generics.CreateAPIView):
    """POST /auth/register — create an account and return a JWT pair.

    Tokens are issued right away so mobile clients skip a second
    round-trip to /auth/token after sign-up.
    """

    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth.register"

    @extend_schema(responses={201: RegisterResponseSerializer})
    def post(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        payload = {
            "user": MeSerializer(user).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }
        return Response(payload, status=status.HTTP_201_CREATED)


class MeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /me — the authenticated user's own profile."""

    serializer_class = MeSerializer
    # PUT is intentionally not exposed: partial updates only.
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self) -> User:
        return self.request.user
