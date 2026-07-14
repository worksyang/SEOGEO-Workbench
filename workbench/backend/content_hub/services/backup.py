"""备份服务：对 dev-plan §4.7 备份协议的薄封装。"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BackupService:
    def __init__(self, database_path: Path, backup_dir: Path):
        self._db = Path(database_path).resolve()
        self._backup_dir = Path(backup_dir).resolve()
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, *, label: str = "auto") -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self._backup_dir / f"content_hub_{timestamp}_{label}.sqlite"
        # 在线 SQLite backup，避免只复制在写主文件。
        source = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
        try:
            destination = sqlite3.connect(str(target))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        # 校验
        integrity = sqlite3.connect(str(target))
        try:
            integrity.execute("PRAGMA integrity_check").fetchone()
        finally:
            integrity.close()
        return target

    def list_backups(self) -> list[Path]:
        return sorted(self._backup_dir.glob("content_hub_*.sqlite"))
