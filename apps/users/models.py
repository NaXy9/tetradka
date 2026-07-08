"""Custom user: email login, IANA timezone, Expo push token (§5)."""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    username = None
    email = models.EmailField(_("email address"), unique=True)

    # IANA name (e.g. "Europe/Berlin"). Times are stored in UTC and rendered in this tz.
    timezone = models.CharField(max_length=64, default="UTC")
    # Expo push token for lesson reminders and the "notes are ready" push (Stage 3).
    push_token = models.CharField(max_length=255, blank=True)
    # Storage key of the avatar object in S3/MinIO (media never touches local disk, §14).
    avatar_key = models.CharField(max_length=255, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self) -> str:
        return self.email
