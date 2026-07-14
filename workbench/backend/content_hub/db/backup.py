from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock


def verify_database(path: Path) -> str:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def create_backup(settings: Settings, destination: Path | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final_path = (
        destination.resolve()
        if destination is not None
        else (settings.database_path.parent / "backups" / f"content_hub_{timestamp}.sqlite")
    )
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")
    temporary_path.unlink(missing_ok=True)

    with writer_lock(settings.lock_path):
        with connect(settings) as source:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            with sqlite3.connect(temporary_path) as target:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                target.execute("PRAGMA optimize")
                target.commit()
        integrity = verify_database(temporary_path)
        if integrity != "ok":
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"备份完整性检查失败：{integrity}")
        os.replace(temporary_path, final_path)
    return final_path
