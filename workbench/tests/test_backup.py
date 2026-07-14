from __future__ import annotations

from pathlib import Path

from content_hub.db.backup import create_backup, verify_database
from content_hub.db.connection import connect


def test_online_backup_is_readable_and_complete(settings, tmp_path: Path) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO platforms(platform_key, canonical_name)
            VALUES ('wechat', '微信')
            """
        )
        connection.commit()

    backup = create_backup(settings, tmp_path / "backup.sqlite")
    assert backup.is_file()
    assert verify_database(backup) == "ok"

    uri = f"file:{backup}?mode=ro"
    import sqlite3

    with sqlite3.connect(uri, uri=True) as connection:
        assert connection.execute(
            "SELECT canonical_name FROM platforms WHERE platform_key='wechat'"
        ).fetchone()[0] == "微信"
