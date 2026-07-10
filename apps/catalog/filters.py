"""Query-param filters for the tutor catalog."""

import django_filters

from .models import TutorProfile


class TutorFilter(django_filters.FilterSet):
    """Filters of GET /tutors: ?subject=<slug>&price_min=&price_max=."""

    # distinct: a tutor teaching the same subject at several levels has
    # several TutorSubject rows and would appear in the list more than once.
    subject = django_filters.CharFilter(field_name="tutor_subjects__subject__slug", distinct=True)
    price_min = django_filters.NumberFilter(field_name="hourly_rate", lookup_expr="gte")
    price_max = django_filters.NumberFilter(field_name="hourly_rate", lookup_expr="lte")

    class Meta:
        model = TutorProfile
        fields = ["subject", "price_min", "price_max"]
