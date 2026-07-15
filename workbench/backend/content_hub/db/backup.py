from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock


def verify_database(path: Path) -> str:
    requested = Path(path)
    if requested.is_symlink():
        raise ValueError("备份必须是普通文件，禁止使用软链接。")
    resolved = requested.resolve()
    if not resolved.is_file():
        raise ValueError("备份必须是普通文件，禁止使用软链接。")
    uri = f"file:{resolved}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def create_backup(settings: Settings, destination: Path | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    requested_path = (
        Path(destination)
        if destination is not None
        else (settings.database_path.parent / "backups" / f"content_hub_{timestamp}.sqlite")
    )
    if requested_path.is_symlink():
        raise ValueError("备份目标不能是软链接。")
    final_path = requested_path.resolve()
    if final_path == settings.database_path.resolve():
        raise ValueError("备份目标不能是运行库。")
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
