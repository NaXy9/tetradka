import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.catalog.models import TutorProfile
from apps.users.models import User

REGISTER_URL = "/api/v1/auth/register"

VALID_PAYLOAD = {
    "email": "alice@example.com",
    "password": "korovamoloko7",
    "role": "student",
    "first_name": "Alice",
    "last_name": "Smith",
    "timezone": "Europe/Berlin",
}


@pytest.fixture(autouse=True)
def _reset_throttle_counters():
    # Registration throttle counters live in the (locmem) cache and would leak
    # between tests of the same process otherwise.
    cache.clear()


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_register_student_creates_user_and_returns_tokens(client):
    response = client.post(REGISTER_URL, VALID_PAYLOAD, format="json")

    assert response.status_code == 201
    body = response.json()
    assert body["access"] and body["refresh"]
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["is_tutor"] is False
    assert body["user"]["timezone"] == "Europe/Berlin"

    user = User.objects.get(email="alice@example.com")
    assert user.check_password(VALID_PAYLOAD["password"])
    assert not TutorProfile.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_register_tutor_creates_empty_tutor_profile(client):
    payload = {**VALID_PAYLOAD, "role": "tutor"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 201
    assert response.json()["user"]["is_tutor"] is True
    profile = TutorProfile.objects.get(user__email="alice@example.com")
    assert profile.hourly_rate == 0
    assert profile.is_verified is False


@pytest.mark.django_db
def test_register_access_token_authenticates(client):
    response = client.post(REGISTER_URL, VALID_PAYLOAD, format="json")

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


@pytest.mark.django_db
def test_register_duplicate_email_rejected(client):
    User.objects.create_user(email="alice@example.com", password="korovamoloko7")

    response = client.post(REGISTER_URL, VALID_PAYLOAD, format="json")

    assert response.status_code == 400
    # The error must not confirm that an account exists (email enumeration).
    assert response.json()["email"] == ["Unable to register with this email address."]
    assert "exists" not in response.content.decode()
    assert User.objects.count() == 1


@pytest.mark.django_db
def test_register_weak_password_rejected(client):
    payload = {**VALID_PAYLOAD, "password": "12345678"}  # fails NumericPasswordValidator

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 400
    assert not User.objects.exists()


@pytest.mark.django_db
def test_register_password_similar_to_email_rejected(client):
    payload = {**VALID_PAYLOAD, "password": "alice@example.com"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 400


@pytest.mark.django_db
def test_register_invalid_timezone_rejected(client):
    payload = {**VALID_PAYLOAD, "timezone": "Mars/Olympus_Mons"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 400
    assert "timezone" in response.json()


@pytest.mark.django_db
def test_register_invalid_role_rejected(client):
    payload = {**VALID_PAYLOAD, "role": "admin"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 400
    assert "role" in response.json()


@pytest.mark.django_db
def test_register_role_is_required(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "role"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 400
    assert "role" in response.json()


@pytest.mark.django_db
def test_register_timezone_defaults_to_utc(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "timezone"}

    response = client.post(REGISTER_URL, payload, format="json")

    assert response.status_code == 201
    assert response.json()["user"]["timezone"] == "UTC"


@pytest.mark.django_db
def test_register_does_not_leak_password_in_response(client):
    response = client.post(REGISTER_URL, VALID_PAYLOAD, format="json")

    assert VALID_PAYLOAD["password"] not in response.content.decode()


@pytest.mark.django_db
def test_register_throttled_after_rate_exceeded(client, monkeypatch):
    # DRF binds THROTTLE_RATES to the class at import time, so override_settings
    # on REST_FRAMEWORK would only work if this test ran first; patch the class
    # attribute instead to stay independent of test ordering.
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"auth.register": "3/hour"})
    # The throttle counts every request from the IP, valid or not, so real
    # sign-ups and probing attempts share the same budget.
    for n in range(3):
        payload = {**VALID_PAYLOAD, "email": f"user{n}@example.com"}
        assert client.post(REGISTER_URL, payload, format="json").status_code == 201

    response = client.post(REGISTER_URL, {**VALID_PAYLOAD, "email": "x@example.com"}, format="json")

    assert response.status_code == 429
    assert not User.objects.filter(email="x@example.com").exists()
