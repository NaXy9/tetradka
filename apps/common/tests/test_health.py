import pytest
from django.test import Client


@pytest.mark.django_db
def test_healthz_ok():
    response = Client().get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
