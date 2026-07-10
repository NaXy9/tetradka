import pytest
from rest_framework.test import APIClient

from apps.bookings.models import Booking
from apps.bookings.tests.factories import (
    BookingFactory,
    ReviewFactory,
    SubjectFactory,
    TutorProfileFactory,
    UserFactory,
)
from apps.catalog.models import TutorSubject

TUTORS_URL = "/api/v1/tutors"


@pytest.fixture
def client():
    return APIClient()


def tutor_with(first_name="Anna", rating=0, hourly_rate=1500, subject=None, level="", **kwargs):
    profile = TutorProfileFactory(
        user=UserFactory(first_name=first_name),
        rating=rating,
        hourly_rate=hourly_rate,
        **kwargs,
    )
    if subject is not None:
        TutorSubject.objects.create(tutor=profile, subject=subject, level=level)
    return profile


@pytest.mark.django_db
def test_tutor_list_is_public(client):
    response = client.get(TUTORS_URL)
    assert response.status_code == 200


@pytest.mark.django_db
def test_list_hides_unfinished_profiles(client):
    tutor_with(first_name="Ready")
    tutor_with(first_name="Unfinished", hourly_rate=0)  # role=tutor sign-up, no onboarding yet

    results = client.get(TUTORS_URL).json()["results"]

    assert [t["first_name"] for t in results] == ["Ready"]


@pytest.mark.django_db
def test_list_card_shape(client):
    math = SubjectFactory(name="Math", slug="math")
    tutor_with(first_name="Anna", rating=4.5, subject=math, level="exam prep")

    card = client.get(TUTORS_URL).json()["results"][0]

    assert set(card) == {
        "id",
        "first_name",
        "last_name",
        "subjects",
        "hourly_rate",
        "rating",
        "lessons_count",
        "experience_years",
        "is_verified",
    }
    assert card["subjects"] == [{"name": "Math", "slug": "math", "level": "exam prep"}]
    assert "bio" not in card


@pytest.mark.django_db
def test_filter_by_subject_slug_without_duplicates(client):
    math = SubjectFactory(slug="math")
    physics = SubjectFactory(slug="physics")
    mathematician = tutor_with(first_name="Math", subject=math, level="school")
    # Same subject at a second level must not duplicate the tutor in the list.
    TutorSubject.objects.create(tutor=mathematician, subject=math, level="exam prep")
    tutor_with(first_name="Phys", subject=physics)

    results = client.get(TUTORS_URL, {"subject": "math"}).json()["results"]

    assert [t["first_name"] for t in results] == ["Math"]


@pytest.mark.django_db
def test_filter_by_price_range(client):
    tutor_with(first_name="Cheap", hourly_rate=800)
    tutor_with(first_name="Mid", hourly_rate=1500)
    tutor_with(first_name="Expensive", hourly_rate=3000)

    results = client.get(TUTORS_URL, {"price_min": 1000, "price_max": 2000}).json()["results"]

    assert [t["first_name"] for t in results] == ["Mid"]


@pytest.mark.django_db
def test_search_matches_name_and_bio(client):
    tutor_with(first_name="Ольга", bio="Готовлю к ЕГЭ по математике")
    tutor_with(first_name="Пётр", bio="Разговорный английский")

    by_name = client.get(TUTORS_URL, {"q": "Ольга"}).json()["results"]
    by_bio = client.get(TUTORS_URL, {"q": "ЕГЭ"}).json()["results"]

    assert [t["first_name"] for t in by_name] == ["Ольга"]
    assert [t["first_name"] for t in by_bio] == ["Ольга"]


@pytest.mark.django_db
def test_default_ordering_is_rating_desc(client):
    tutor_with(first_name="Low", rating=3.2)
    tutor_with(first_name="Top", rating=4.9)
    tutor_with(first_name="Mid", rating=4.1)

    results = client.get(TUTORS_URL).json()["results"]

    assert [t["first_name"] for t in results] == ["Top", "Mid", "Low"]


@pytest.mark.django_db
def test_explicit_ordering_by_price(client):
    tutor_with(first_name="Expensive", hourly_rate=3000)
    tutor_with(first_name="Cheap", hourly_rate=800)

    results = client.get(TUTORS_URL, {"ordering": "hourly_rate"}).json()["results"]

    assert [t["first_name"] for t in results] == ["Cheap", "Expensive"]


@pytest.mark.django_db
def test_list_avoids_n_plus_one(client, django_assert_num_queries):
    math = SubjectFactory(slug="math")
    for n in range(7):
        tutor_with(first_name=f"T{n}", subject=math)

    # COUNT for pagination + tutors page + one prefetch of subject rows.
    with django_assert_num_queries(3):
        response = client.get(TUTORS_URL)

    assert response.json()["count"] == 7


def completed_booking(**kwargs):
    return BookingFactory(status=Booking.Status.COMPLETED, **kwargs)


@pytest.mark.django_db
def test_detail_includes_profile_texts_and_reviews(client):
    tutor = tutor_with(first_name="Anna", bio="15 years of math", education="MSU")
    student = UserFactory(first_name="Boris")
    old = ReviewFactory(
        booking=completed_booking(tutor=tutor, student=student), rating=4, text="Good"
    )
    new = ReviewFactory(booking=completed_booking(tutor=tutor), rating=5, text="Great")
    ReviewFactory()  # another tutor's review must not leak in

    body = client.get(f"{TUTORS_URL}/{tutor.id}").json()

    assert body["bio"] == "15 years of math"
    assert body["education"] == "MSU"
    assert "late_cancellation_refund_percent" in body
    assert [r["text"] for r in body["reviews"]] == ["Great", "Good"]  # newest first
    assert body["reviews"][1]["student_first_name"] == "Boris"
    assert new.created_at > old.created_at


@pytest.mark.django_db
def test_detail_of_unfinished_profile_is_404(client):
    hidden = tutor_with(hourly_rate=0)

    response = client.get(f"{TUTORS_URL}/{hidden.id}")

    assert response.status_code == 404


@pytest.mark.django_db
def test_review_of_non_completed_booking_is_hidden(client):
    tutor = tutor_with()
    # A review may point at a booking that later got disputed/reverted;
    # the public page must only show reviews of completed lessons.
    ReviewFactory(booking=BookingFactory(tutor=tutor, status=Booking.Status.NO_SHOW))

    body = client.get(f"{TUTORS_URL}/{tutor.id}").json()

    assert body["reviews"] == []


@pytest.mark.django_db
def test_non_numeric_price_filter_is_rejected(client):
    # DRF's DjangoFilterBackend runs the filterset with raise_exception=True,
    # so a malformed value is an explicit 400, not a silently unfiltered list.
    tutor_with(first_name="Anna")

    response = client.get(TUTORS_URL, {"price_min": "abc"})

    assert response.status_code == 400
    assert "price_min" in response.json()


@pytest.mark.django_db
def test_combined_filters_work_together(client):
    math = SubjectFactory(slug="math")
    target = tutor_with(first_name="Anna", rating=4.0, hourly_rate=1500, subject=math)
    # Second level of the same subject: dedup must hold with search+ordering on.
    TutorSubject.objects.create(tutor=target, subject=math, level="exam prep")
    tutor_with(first_name="Anna2", rating=5.0, hourly_rate=1400, subject=math)
    tutor_with(first_name="Anna3", rating=4.5, hourly_rate=5000, subject=math)  # too expensive
    tutor_with(first_name="Boris", rating=5.0, hourly_rate=1500, subject=math)  # name mismatch

    response = client.get(
        TUTORS_URL,
        {"subject": "math", "price_max": 2000, "q": "Anna", "ordering": "-rating"},
    )

    assert [t["first_name"] for t in response.json()["results"]] == ["Anna2", "Anna"]


@pytest.mark.django_db
def test_catalog_is_read_only(client):
    tutor = tutor_with()

    assert client.post(TUTORS_URL, {}).status_code == 405
    assert client.delete(f"{TUTORS_URL}/{tutor.id}").status_code == 405
