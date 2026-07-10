"""Factories for the booking domain. All datetimes are aware UTC."""

import datetime as dt

import factory
from django.utils import timezone

from apps.bookings.models import Booking, Review
from apps.catalog.models import Subject, TutorProfile
from apps.users.models import User


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    timezone = "UTC"


class TutorProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TutorProfile

    user = factory.SubFactory(UserFactory)
    hourly_rate = 1500


class SubjectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Subject
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Subject {n}")
    slug = factory.Sequence(lambda n: f"subject-{n}")


class BookingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Booking

    student = factory.SubFactory(UserFactory)
    tutor = factory.SubFactory(TutorProfileFactory)
    subject = factory.SubFactory(SubjectFactory)
    starts_at = factory.LazyFunction(
        lambda: (timezone.now() + dt.timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    )
    ends_at = factory.LazyAttribute(lambda o: o.starts_at + dt.timedelta(hours=1))
    price = 1500


class ReviewFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Review

    # Reviews only exist for completed lessons; status is set directly because
    # walking the pending→confirmed→completed machine is irrelevant here.
    booking = factory.SubFactory(BookingFactory, status=Booking.Status.COMPLETED)
    rating = 5
    text = factory.Sequence(lambda n: f"Review text {n}")
