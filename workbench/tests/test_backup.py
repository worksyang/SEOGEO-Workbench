from __future__ import annotations

from pathlib import Path

from content_hub.db.backup import create_backup, verify_database
from content_hub.db.connection import connect
from content_hub.services.backup import BackupService


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


def test_backup_service_reuses_verified_snapshot_and_writes_restore_report(settings) -> None:
    service = BackupService.from_settings(settings)
    first = service.snapshot(label="演练", reuse=False)
    second = service.snapshot(label="演练", reuse=True)

    assert first.name == second.name
    assert service.list_backups()[0].verifiable is True
    result = service.restore_drill(first.name, actor_id="test")
    assert result["ok"] is True
    assert result["runtime_database_unchanged"] is True
    assert result["integrity"] == "ok"
    assert (settings.database_path.parent / result["report"]).is_file()
    with connect(settings, readonly=True) as connection:
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM audit_log WHERE subject_type='backup'"
            ).fetchall()
        }
    assert {"backup.snapshot", "backup.restore_drill"} <= actions


def test_restore_drill_rejects_traversal_and_symlink(settings) -> None:
    service = BackupService.from_settings(settings)
    backup = service.snapshot(label="safe", reuse=False)

    import pytest

    with pytest.raises(ValueError):
        service.restore_drill(f"../{backup.name}")

    link = settings.database_path.parent / "backups" / "content_hub_link.sqlite"
    try:
        link.symlink_to(settings.database_path)
    except (OSError, NotImplementedError):
        pytest.skip("当前文件系统不支持软链接")
    with pytest.raises(ValueError):
        service.restore_drill(link.name)
