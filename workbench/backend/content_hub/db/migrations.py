from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from content_hub.config import Settings
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.ingestion.source_manifest_backfill import backfill_existing_manifests

MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


def _statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ValueError("迁移 SQL 最后一条语句不完整。")
    return statements


def _migration_files(directory: Path) -> list[tuple[int, str, Path]]:
    found: list[tuple[int, str, Path]] = []
    for path in directory.glob("*.sql"):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            raise ValueError(f"迁移文件名不符合规范：{path.name}")
        found.append((int(match["version"]), match["name"], path))
    found.sort()
    versions = [item[0] for item in found]
    if len(versions) != len(set(versions)):
        raise ValueError("迁移版本号重复。")
    return found


def _bootstrap(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def migrate(settings: Settings) -> list[int]:
    applied_now: list[int] = []
    with writer_lock(settings.lock_path):
        with connect(settings) as connection:
            _bootstrap(connection)
            applied = {
                row["version"]: row["checksum"]
                for row in connection.execute(
                    "SELECT version, checksum FROM schema_migrations ORDER BY version"
                )
            }
            for version, name, path in _migration_files(settings.migration_dir):
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise RuntimeError(
                            f"已应用迁移 {version:04d}_{name} 的校验和发生变化，禁止静默覆盖。"
                        )
                    continue
                with transaction(connection):
                    for statement in _statements(sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                        (version, name, checksum),
                    )
                if version == 7:
                    backfill_existing_manifests(connection, settings)
                    # 回填函数写入 source manifest/audit 事实；提交后才能开始
                    # 下一条迁移的 BEGIN IMMEDIATE。
                    connection.commit()
                applied_now.append(version)
    return applied_now
