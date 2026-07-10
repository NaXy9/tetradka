import pytest
from rest_framework.test import APIClient

from apps.bookings.tests.factories import SubjectFactory

SUBJECTS_URL = "/api/v1/subjects"


@pytest.mark.django_db
def test_subjects_are_public():
    response = APIClient().get(SUBJECTS_URL)
    assert response.status_code == 200


@pytest.mark.django_db
def test_subjects_listed_alphabetically_without_pagination():
    SubjectFactory(name="Physics", slug="physics")
    SubjectFactory(name="Algebra", slug="algebra")
    SubjectFactory(name="Math", slug="math")

    body = APIClient().get(SUBJECTS_URL).json()

    # Plain list (no pagination envelope): the client renders all chips at once.
    assert [s["name"] for s in body] == ["Algebra", "Math", "Physics"]
    assert set(body[0]) == {"id", "name", "slug"}
