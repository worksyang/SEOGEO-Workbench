"""本机备份、验证与隔离恢复演练服务。

这里故意只处理 SQLite 运行库，不接触七套原始系统，也不提供覆盖恢复能力。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from content_hub.config import Settings
from content_hub.db.backup import create_backup, verify_database
from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.services.audit import AuditService
from content_hub.validation.timestamps import utc_now_iso


@dataclass(frozen=True, slots=True)
class BackupRecord:
    name: str
    path: str
    size_bytes: int
    modified_at: str
    sha256: str
    integrity: str
    verifiable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "sha256": self.sha256,
            "integrity": self.integrity,
            "verifiable": self.verifiable,
        }


class BackupService:
    def __init__(
        self,
        database_path: Path,
        backup_dir: Path,
        *,
        lock_path: Path | None = None,
        report_dir: Path | None = None,
    ):
        self._db = Path(database_path).resolve()
        self._backup_dir = Path(backup_dir).resolve()
        self._lock_path = Path(lock_path or self._db.with_suffix(".lock")).resolve()
        self._report_dir = Path(report_dir or self._backup_dir.parent / "reports" / "backup").resolve()
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._report_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, settings: Settings) -> "BackupService":
        return cls(
            settings.database_path,
            settings.database_path.parent / "backups",
            lock_path=settings.lock_path,
            report_dir=settings.database_path.parent / "reports" / "backup",
        )

    def _inside(self, path: Path, root: Path) -> Path:
        if path.is_symlink():
            raise ValueError("禁止使用软链接路径。")
        candidate = path.resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("路径必须位于工作台备份目录内。") from exc
        return candidate

    def _backup_path(self, name: str) -> Path:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("备份名称无效，禁止路径穿越。")
        path = self._inside(self._backup_dir / name, self._backup_dir)
        if path == self._db or path.is_symlink():
            raise ValueError("备份不能指向运行库或软链接。")
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _record(self, path: Path) -> BackupRecord:
        path = self._inside(path, self._backup_dir)
        if path == self._db or path.is_symlink() or not path.is_file():
            raise ValueError("备份必须是备份目录内的普通文件。")
        try:
            integrity = verify_database(path)
            verifiable = integrity == "ok"
        except (OSError, sqlite3.Error, ValueError) as exc:
            integrity = f"error:{type(exc).__name__}"
            verifiable = False
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        return BackupRecord(
            name=path.name,
            path=str(path.relative_to(self._backup_dir)),
            size_bytes=stat.st_size,
            modified_at=modified,
            sha256=self._sha256(path),
            integrity=integrity,
            verifiable=verifiable,
        )

    def list_backups(self) -> list[BackupRecord]:
        records: list[BackupRecord] = []
        for path in sorted(self._backup_dir.glob("content_hub_*.sqlite")):
            try:
                records.append(self._record(path))
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda item: item.modified_at, reverse=True)

    def snapshot(
        self,
        *,
        label: str = "auto",
        reuse: bool = True,
        actor_id: str = "user",
    ) -> BackupRecord:
        safe_label = "".join(ch for ch in label if ch.isalnum() or ch in "-_")[:32] or "auto"
        if reuse:
            for record in self.list_backups():
                if record.verifiable and record.name.endswith(f"_{safe_label}.sqlite"):
                    self._audit(
                        action="backup.snapshot",
                        subject_id=record.name,
                        actor_id=actor_id,
                        details={"backup": record.name, "reused": True, "integrity": record.integrity},
                    )
                    return record
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._backup_dir / f"content_hub_{timestamp}_{safe_label}.sqlite"
        created = create_backup(Settings.load().with_database(self._db), target)
        record = self._record(created)
        self._audit(
            action="backup.snapshot",
            subject_id=record.name,
            actor_id=actor_id,
            details={"backup": record.name, "reused": False, "integrity": record.integrity},
        )
        return record

    def restore_drill(self, backup_name: str, *, actor_id: str = "user") -> dict[str, Any]:
        source = self._backup_path(backup_name)
        record = self._record(source)
        if not record.verifiable:
            raise ValueError("备份未通过完整性验证，不能进行恢复演练。")

        drill_id = f"restore_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        drill_dir = self._inside(self._backup_dir / "restore-drills", self._backup_dir)
        drill_dir.mkdir(parents=True, exist_ok=True)
        target = self._inside(drill_dir / f"{drill_id}.sqlite", drill_dir)
        if target == self._db or target.exists() or target.is_symlink():
            raise ValueError("恢复演练目标不安全。")

        temporary = target.with_suffix(".sqlite.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn:
                with sqlite3.connect(temporary) as target_conn:
                    source_conn.backup(target_conn)
                    target_conn.execute("PRAGMA journal_mode=DELETE")
                    target_conn.commit()
            integrity = verify_database(temporary)
            if integrity != "ok":
                raise RuntimeError(f"恢复演练完整性检查失败：{integrity}")
            os.replace(temporary, target)
            with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as connection:
                table_count = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            result: dict[str, Any] = {
                "ok": True,
                "drill_id": drill_id,
                "source": record.to_dict(),
                "target": str(target.relative_to(self._backup_dir)),
                "integrity": integrity,
                "table_count": table_count,
                "runtime_database_unchanged": True,
                "completed_at": utc_now_iso(),
            }
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

        report_path = self._report_dir / f"{drill_id}.json"
        result["report"] = str(report_path.relative_to(self._db.parent))
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self._audit(
            action="backup.restore_drill",
            subject_id=drill_id,
            actor_id=actor_id,
            details={
                "source": record.name,
                "target": result["target"],
                "integrity": integrity,
                "runtime_database_unchanged": True,
            },
        )
        return result

    def _audit(self, *, action: str, subject_id: str, actor_id: str, details: dict[str, Any]) -> None:
        with writer_lock(self._lock_path):
            with connect(self._settings_for_db(), readonly=False) as connection:
                AuditService(connection).record(
                    action=action,
                    subject_type="backup",
                    subject_id=subject_id,
                    actor_id=actor_id,
                    details=details,
                )

    def _settings_for_db(self) -> Settings:
        return Settings.load().with_database(self._db)
