"""GET/PATCH /tutor/profile — a tutor editing their own onboarding profile.

Covers the tutor-only permission, the editable vs. read-only field split, and
the catalog-visibility side effect of setting a positive hourly_rate.
"""

import pytest
from rest_framework.test import APIClient

from apps.bookings.tests.factories import SubjectFactory, TutorProfileFactory, UserFactory
from apps.catalog.models import TutorSubject

PROFILE_URL = "/api/v1/tutor/profile"
TUTORS_URL = "/api/v1/tutors"

pytestmark = pytest.mark.django_db


def tutor_client(**profile_kwargs):
    """An authenticated client whose user owns a tutor profile."""
    tutor = TutorProfileFactory(**profile_kwargs)
    client = APIClient()
    client.force_authenticate(tutor.user)
    return client, tutor


# --- read ---------------------------------------------------------------------


def test_get_returns_own_profile_with_subjects():
    subject = SubjectFactory()
    client, tutor = tutor_client(bio="Hi", hourly_rate=2000)
    TutorSubject.objects.create(tutor=tutor, subject=subject, level="exam prep")

    body = client.get(PROFILE_URL).json()

    assert body["id"] == tutor.id
    assert body["bio"] == "Hi"
    assert body["hourly_rate"] == "2000.00"
    assert body["subjects"] == [{"name": subject.name, "slug": subject.slug, "level": "exam prep"}]


def test_non_tutor_is_403():
    # A student (no tutor profile) has no profile to manage, on any verb.
    client = APIClient()
    client.force_authenticate(UserFactory())

    assert client.get(PROFILE_URL).status_code == 403
    assert client.patch(PROFILE_URL, {"bio": "x"}).status_code == 403


def test_anonymous_is_401():
    client = APIClient()

    assert client.get(PROFILE_URL).status_code == 401
    assert client.patch(PROFILE_URL, {"bio": "x"}).status_code == 401


# --- update -------------------------------------------------------------------


def test_patch_updates_editable_fields():
    client, tutor = tutor_client(hourly_rate=1000)

    response = client.patch(
        PROFILE_URL,
        {
            "bio": "Experienced maths tutor",
            "hourly_rate": "2500.00",
            "education": "MSU, applied maths",
            "experience_years": 7,
            "video_intro_url": "https://example.com/intro.mp4",
        },
        format="json",
    )

    assert response.status_code == 200
    tutor.refresh_from_db()
    assert tutor.bio == "Experienced maths tutor"
    assert str(tutor.hourly_rate) == "2500.00"
    assert tutor.education == "MSU, applied maths"
    assert tutor.experience_years == 7
    assert tutor.video_intro_url == "https://example.com/intro.mp4"


def test_setting_hourly_rate_publishes_to_catalog():
    # An onboarding profile (hourly_rate=0) is hidden; a positive rate makes it
    # visible in the public catalog with no extra publish step.
    client, tutor = tutor_client(hourly_rate=0)
    assert client.get(f"{TUTORS_URL}/{tutor.id}").status_code == 404

    client.patch(PROFILE_URL, {"hourly_rate": "1800.00"}, format="json")

    assert client.get(f"{TUTORS_URL}/{tutor.id}").status_code == 200


def test_patch_ignores_read_only_fields():
    client, tutor = tutor_client(
        hourly_rate=1000, rating=4, lessons_count=3, balance=500, is_verified=True
    )

    response = client.patch(
        PROFILE_URL,
        {
            "rating": "1.00",
            "lessons_count": 999,
            "balance": "99999.00",
            "is_verified": False,
            "late_cancellation_refund_percent": 50,
        },
        format="json",
    )

    assert response.status_code == 200
    tutor.refresh_from_db()
    assert str(tutor.rating) == "4.00"
    assert tutor.lessons_count == 3
    assert str(tutor.balance) == "500.00"
    assert tutor.is_verified is True
    assert tutor.late_cancellation_refund_percent == 0


def test_negative_hourly_rate_is_400():
    client, _ = tutor_client(hourly_rate=1000)

    response = client.patch(PROFILE_URL, {"hourly_rate": "-1.00"}, format="json")

    assert response.status_code == 400
    assert "hourly_rate" in response.json()


def test_put_is_405():
    # Only partial updates are exposed, matching /me.
    client, _ = tutor_client(hourly_rate=1000)

    assert client.put(PROFILE_URL, {"bio": "x"}, format="json").status_code == 405
