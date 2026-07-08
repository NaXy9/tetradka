"""Shared pytest configuration.

Tests marked @pytest.mark.postgres need PostgreSQL features (GiST exclusion constraints,
row-level locking) and are skipped automatically when the test DB is not PostgreSQL.
"""

import pytest
from django.db import connection


def pytest_collection_modifyitems(config, items):
    if connection.vendor == "postgresql":
        return
    skip_pg = pytest.mark.skip(reason="requires PostgreSQL (set TETRADKA_TEST_DATABASE_URL)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip_pg)
