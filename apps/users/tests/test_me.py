import pytest
from rest_framework.test import APIClient

from apps.bookings.tests.factories import TutorProfileFactory, UserFactory

ME_URL = "/api/v1/me"


@pytest.fixture
def user(db):
    return UserFactory(first_name="Bob", last_name="Stone", timezone="Europe/Berlin")


@pytest.fixture
def client(user):
    api_client = APIClient()
    api_client.force_authenticate(user)
    return api_client


@pytest.mark.django_db
def test_me_requires_authentication():
    response = APIClient().get(ME_URL)
    assert response.status_code == 401


@pytest.mark.django_db
def test_me_returns_own_profile(client, user):
    response = client.get(ME_URL)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": user.id,
        "email": user.email,
        "first_name": "Bob",
        "last_name": "Stone",
        "timezone": "Europe/Berlin",
        "push_token": "",
        "avatar_key": "",
        "is_tutor": False,
    }


@pytest.mark.django_db
def test_me_is_tutor_reflects_tutor_profile(client, user):
    TutorProfileFactory(user=user)

    response = client.get(ME_URL)

    assert response.json()["is_tutor"] is True


@pytest.mark.django_db
def test_patch_me_updates_allowed_fields(client, user):
    response = client.patch(
        ME_URL,
        {
            "first_name": "Robert",
            "timezone": "Asia/Yekaterinburg",
            "push_token": "ExponentPushToken[x]",
        },
        format="json",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.first_name == "Robert"
    assert user.timezone == "Asia/Yekaterinburg"
    assert user.push_token == "ExponentPushToken[x]"


@pytest.mark.django_db
def test_patch_me_ignores_read_only_fields(client, user):
    original_email = user.email

    response = client.patch(
        ME_URL, {"email": "hacker@example.com", "avatar_key": "evil/key"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.email == original_email
    assert user.avatar_key == ""


@pytest.mark.django_db
def test_patch_me_invalid_timezone_rejected(client, user):
    response = client.patch(ME_URL, {"timezone": "Not/AZone"}, format="json")

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.timezone == "Europe/Berlin"


@pytest.mark.django_db
def test_put_me_is_not_allowed(client):
    response = client.put(ME_URL, {"first_name": "X"}, format="json")

    assert response.status_code == 405
