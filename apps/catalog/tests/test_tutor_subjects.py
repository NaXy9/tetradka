"""CRUD for the subjects a tutor teaches, under /tutor/subjects.

Covers ownership scoping, the tutor-only permission, the duplicate
(subject, level) guard, and the profile-row lock that keeps a concurrent
duplicate from slipping past the check onto the unique constraint.
"""

import threading

import pytest
from django.db import connection
from rest_framework.test import APIClient

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    BookingFactory,
    SubjectFactory,
    TutorProfileFactory,
    UserFactory,
)
from apps.catalog.models import TutorSubject

SUBJECTS_URL = "/api/v1/tutor/subjects"

pytestmark = pytest.mark.django_db


def tutor_client():
    """An authenticated client whose user owns a tutor profile."""
    tutor = TutorProfileFactory()
    client = APIClient()
    client.force_authenticate(tutor.user)
    return client, tutor


# --- create & ownership -------------------------------------------------------


def test_create_subject_returns_201_owned_by_caller():
    client, tutor = tutor_client()
    subject = SubjectFactory()

    response = client.post(SUBJECTS_URL, {"subject": subject.id, "level": "A-level"})

    assert response.status_code == 201
    row = TutorSubject.objects.get(pk=response.json()["id"])
    assert row.tutor == tutor and row.subject == subject and row.level == "A-level"


def test_create_subject_defaults_level_to_blank():
    client, tutor = tutor_client()
    subject = SubjectFactory()

    response = client.post(SUBJECTS_URL, {"subject": subject.id})

    assert response.status_code == 201
    assert TutorSubject.objects.get(pk=response.json()["id"]).level == ""


def test_create_ignores_tutor_in_body():
    # `tutor` is not a serializer field; a spoofed id is dropped and the row is
    # owned by the authenticated caller.
    client, tutor = tutor_client()
    other = TutorProfileFactory()
    subject = SubjectFactory()

    response = client.post(SUBJECTS_URL, {"subject": subject.id, "tutor": other.id})

    assert response.status_code == 201
    assert TutorSubject.objects.get(pk=response.json()["id"]).tutor == tutor


def test_list_returns_only_callers_subjects_unpaginated():
    client, tutor = tutor_client()
    subject = SubjectFactory()
    mine = TutorSubject.objects.create(tutor=tutor, subject=subject)
    TutorSubject.objects.create(tutor=TutorProfileFactory(), subject=subject)

    body = client.get(SUBJECTS_URL).json()

    assert isinstance(body, list)
    assert [s["id"] for s in body] == [mine.id]


def test_other_tutors_subject_is_404():
    client, _ = tutor_client()
    foreign = TutorSubject.objects.create(tutor=TutorProfileFactory(), subject=SubjectFactory())

    assert client.get(f"{SUBJECTS_URL}/{foreign.id}").status_code == 404
    assert client.patch(f"{SUBJECTS_URL}/{foreign.id}", {"level": "x"}).status_code == 404
    assert client.delete(f"{SUBJECTS_URL}/{foreign.id}").status_code == 404


# --- duplicate guard ----------------------------------------------------------


def test_duplicate_subject_and_level_is_400():
    client, tutor = tutor_client()
    subject = SubjectFactory()
    TutorSubject.objects.create(tutor=tutor, subject=subject, level="A-level")

    response = client.post(SUBJECTS_URL, {"subject": subject.id, "level": "A-level"})

    assert response.status_code == 400


def test_same_subject_different_level_is_allowed():
    # The uniqueness is on (subject, level), so a tutor may teach one subject at
    # several levels.
    client, tutor = tutor_client()
    subject = SubjectFactory()
    TutorSubject.objects.create(tutor=tutor, subject=subject, level="A-level")

    response = client.post(SUBJECTS_URL, {"subject": subject.id, "level": "conversational"})

    assert response.status_code == 201


# --- update & delete ----------------------------------------------------------


def test_patch_updates_level():
    client, tutor = tutor_client()
    row = TutorSubject.objects.create(tutor=tutor, subject=SubjectFactory(), level="A-level")

    response = client.patch(f"{SUBJECTS_URL}/{row.id}", {"level": "GCSE"})

    assert response.status_code == 200
    row.refresh_from_db()
    assert row.level == "GCSE"


def test_delete_subject():
    client, tutor = tutor_client()
    row = TutorSubject.objects.create(tutor=tutor, subject=SubjectFactory())

    assert client.delete(f"{SUBJECTS_URL}/{row.id}").status_code == 204
    assert not TutorSubject.objects.filter(pk=row.id).exists()


def test_delete_subject_leaves_active_booking_intact():
    # Removing a subject the tutor teaches must not disturb existing bookings for
    # it: eligibility is checked only at booking creation, and Booking.subject is
    # a direct FK, not tied to the TutorSubject row being deleted.
    client, tutor = tutor_client()
    subject = SubjectFactory()
    row = TutorSubject.objects.create(tutor=tutor, subject=subject)
    booking = BookingFactory(tutor=tutor, subject=subject, status=Booking.Status.CONFIRMED)

    assert client.delete(f"{SUBJECTS_URL}/{row.id}").status_code == 204

    booking.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED


# --- permission: tutors only --------------------------------------------------


def test_non_tutor_is_403():
    client = APIClient()
    client.force_authenticate(UserFactory())
    subject = SubjectFactory()

    assert client.get(SUBJECTS_URL).status_code == 403
    assert client.post(SUBJECTS_URL, {"subject": subject.id}).status_code == 403
    assert client.delete(f"{SUBJECTS_URL}/1").status_code == 403


def test_anonymous_is_401():
    client = APIClient()

    assert client.get(SUBJECTS_URL).status_code == 401
    assert client.post(SUBJECTS_URL, {"subject": 1}).status_code == 401


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_subjects_keep_exactly_one():
    # The same (subject, level) posted at once: the profile-row lock serializes
    # the two, so the loser's duplicate check sees the committed row and the
    # unique constraint never has to reject anyone with a 500.
    tutor = TutorProfileFactory()
    subject = SubjectFactory()
    barrier = threading.Barrier(2)
    statuses = []

    def attempt():
        client = APIClient()
        client.force_authenticate(tutor.user)
        try:
            barrier.wait(timeout=10)
            statuses.append(
                client.post(SUBJECTS_URL, {"subject": subject.id, "level": "A-level"}).status_code
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [201, 400]
    assert TutorSubject.objects.filter(tutor=tutor).count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_patch_to_same_pair_keeps_exactly_one():
    # Two existing rows edited at once toward the same (subject, level): the
    # profile-row lock serializes the update path too, so the loser's duplicate
    # check sees the winner's committed row and only one row lands on the pair.
    tutor = TutorProfileFactory()
    subject = SubjectFactory()
    row_a = TutorSubject.objects.create(tutor=tutor, subject=subject, level="beginner")
    row_b = TutorSubject.objects.create(tutor=tutor, subject=subject, level="advanced")
    barrier = threading.Barrier(2)
    statuses = []

    def attempt(row_id):
        client = APIClient()
        client.force_authenticate(tutor.user)
        try:
            barrier.wait(timeout=10)
            response = client.patch(f"{SUBJECTS_URL}/{row_id}", {"level": "final"})
            statuses.append(response.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(r,)) for r in (row_a.id, row_b.id)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [200, 400]
    assert TutorSubject.objects.filter(tutor=tutor, subject=subject, level="final").count() == 1
