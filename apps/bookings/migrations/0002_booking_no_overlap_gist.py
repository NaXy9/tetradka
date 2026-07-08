# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Last line of defense against double booking (invariant §5, CLAUDE.md #1).

PostgreSQL-only: btree_gist extension + GiST exclusion constraint forbidding
overlapping [starts_at, ends_at) ranges per tutor while a booking is active
(pending/confirmed). On SQLite (local dev) this migration is a no-op; the
application-level check (select_for_update + overlap query) still applies.
"""

from django.db import migrations

from apps.common.migration_ops import RunPostgresOnlySQL


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0001_initial"),
    ]

    operations = [
        RunPostgresOnlySQL(
            sql="CREATE EXTENSION IF NOT EXISTS btree_gist;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        RunPostgresOnlySQL(
            sql="""
                ALTER TABLE bookings_booking
                ADD CONSTRAINT excl_booking_tutor_overlap
                EXCLUDE USING gist (
                    tutor_id WITH =,
                    tstzrange(starts_at, ends_at) WITH &&
                )
                WHERE (status IN ('pending', 'confirmed'));
            """,
            reverse_sql="""
                ALTER TABLE bookings_booking
                DROP CONSTRAINT IF EXISTS excl_booking_tutor_overlap;
            """,
        ),
    ]
