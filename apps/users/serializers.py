"""Serializers for registration and the authenticated user's own profile."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.catalog.models import TutorProfile

from .models import User


def validate_iana_timezone(value: str) -> str:
    """Reject timezone names that the zoneinfo database does not know.

    Raises:
        serializers.ValidationError: If ``value`` is not a valid IANA name.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise serializers.ValidationError(f"Unknown IANA timezone: {value!r}.") from None
    return value


class MeSerializer(serializers.ModelSerializer):
    """The authenticated user's own profile (GET/PATCH /me)."""

    is_tutor = serializers.SerializerMethodField()
    timezone = serializers.CharField(
        max_length=64, required=False, validators=[validate_iana_timezone]
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "timezone",
            "push_token",
            "avatar_key",
            "is_tutor",
        ]
        read_only_fields = ["id", "email", "avatar_key"]

    def get_is_tutor(self, obj: User) -> bool:
        # hasattr resolves the reverse OneToOne and caches it on the instance,
        # so repeated serialization of the same object costs one query at most.
        return hasattr(obj, "tutor_profile")


class RegisterSerializer(serializers.ModelSerializer):
    """Sign-up payload. One account can hold both roles: registering with
    role=tutor additionally creates an empty TutorProfile to be filled in
    during tutor onboarding."""

    ROLE_STUDENT = "student"
    ROLE_TUTOR = "tutor"

    # Neutral wording instead of DRF's default "already exists": the error must
    # not advertise which emails have accounts. A patient attacker can still
    # tell a duplicate from a success by the status code; making the responses
    # fully indistinguishable requires the email-verification flow.
    # TODO(igor): fold duplicate-email handling into email verification when it lands.
    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=User.objects.all(),
                message="Unable to register with this email address.",
            )
        ],
    )
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    role = serializers.ChoiceField(choices=[ROLE_STUDENT, ROLE_TUTOR], write_only=True)
    timezone = serializers.CharField(
        max_length=64, required=False, validators=[validate_iana_timezone]
    )

    class Meta:
        model = User
        fields = ["email", "password", "role", "first_name", "last_name", "timezone"]

    def validate(self, attrs: dict) -> dict:
        # Build a throwaway user so UserAttributeSimilarityValidator can compare
        # the password against email/name; the instance is never saved.
        candidate = User(
            email=attrs["email"],
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
        )
        password_validation.validate_password(attrs["password"], user=candidate)
        return attrs

    def create(self, validated_data: dict) -> User:
        role = validated_data.pop("role")
        password = validated_data.pop("password")
        with transaction.atomic():
            user = User.objects.create_user(password=password, **validated_data)
            if role == self.ROLE_TUTOR:
                # hourly_rate=0 marks an unfinished profile; the catalog will
                # only list tutors once onboarding sets a real rate.
                TutorProfile.objects.create(user=user, hourly_rate=0)
        return user


class RegisterResponseSerializer(serializers.Serializer):
    """Response shape of POST /auth/register (documentation only)."""

    user = MeSerializer()
    access = serializers.CharField()
    refresh = serializers.CharField()
