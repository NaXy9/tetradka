"""Operational endpoints."""

from django.db import connection
from django.http import HttpRequest, JsonResponse


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe: the process is up and the database answers."""
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — any DB error means "not ready"
        db_ok = False
    return JsonResponse(
        {"status": "ok" if db_ok else "degraded", "database": db_ok},
        status=200 if db_ok else 503,
    )
