"""Migration operations shared across apps."""

from django.db import migrations


class RunPostgresOnlySQL(migrations.RunSQL):
    """RunSQL that is a no-op on non-PostgreSQL backends (e.g. local SQLite)."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)
