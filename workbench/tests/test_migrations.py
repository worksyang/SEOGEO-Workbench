from __future__ import annotations

from content_hub.db.connection import connect
from content_hub.db.migrations import migrate


def test_migrations_are_idempotent(settings) -> None:
    assert migrate(settings) == []
    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [(row["version"], row["name"]) for row in rows] == [
        (1, "initial"),
        (2, "views"),
        (3, "system_registry"),
    ]
