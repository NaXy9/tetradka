import pytest

from apps.users.models import User


@pytest.mark.django_db
def test_create_user_normalizes_email_domain():
    user = User.objects.create_user(email="Student@Example.COM", password="pass12345")
    assert user.email == "Student@example.com"  # only the domain is normalized
    assert user.check_password("pass12345")
    assert user.timezone == "UTC"
    assert user.is_staff is False
    assert user.is_superuser is False


@pytest.mark.django_db
def test_create_superuser():
    admin = User.objects.create_superuser(email="admin@example.com", password="pass12345")
    assert admin.is_staff is True
    assert admin.is_superuser is True


@pytest.mark.django_db
def test_email_is_required():
    with pytest.raises(ValueError, match="email address must be set"):
        User.objects.create_user(email="", password="whatever")
