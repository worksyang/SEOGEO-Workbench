from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from content_hub.db.connection import connect
from content_hub.features.publishing.source_reader import read_legacy_accounts
from content_hub.services.publishing import PublishingService


def _service(settings, tmp_path: Path, source: Path) -> tuple[sqlite3.Connection, PublishingService]:
    configured = replace(
        settings,
        asset_store_path=tmp_path / "assets",
        publish_accounts_source=source,
    )
    configured.ensure_directories()
    connection = connect(configured)
    config = read_legacy_accounts(source)
    service = PublishingService(
        connection=connection,
        publish_root=configured.asset_store_path / "publish",
        accounts=[],
        legacy_config=config,
    )
    return connection, service


def test_legacy_accounts_are_projected_without_credentials(settings, tmp_path: Path) -> None:
    source = tmp_path / "accounts.json"
    source.write_text(json.dumps({
        "accounts": [
            {
                "id": i,
                "name": f"账号 {i}",
                "profile_dir": f"/private/profile/{i}",
                "cookie_file": f"/private/cookie/{i}",
                "token_file": f"/private/token/{i}",
                "status": "active",
            }
            for i in range(10)
        ]
    }), encoding="utf-8")
    connection, service = _service(settings, tmp_path, source)
    try:
        service.sync_runtime_accounts()
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM publish_accounts_runtime").fetchone()[0] == 10
        manifest = connection.execute(
            "SELECT manifest_id, payload_json FROM source_manifests WHERE system_key='publishing'"
        ).fetchone()
        assert manifest
        assert str(source) not in manifest["payload_json"]
        assert "private" not in manifest["payload_json"]
        payload = connection.execute(
            "SELECT payload_json FROM publish_accounts_runtime LIMIT 1"
        ).fetchone()[0]
        assert "cookie" not in payload.lower()
        assert "token" not in payload.lower()
        assert "profile" not in payload.lower()
    finally:
        connection.close()


def test_safe_queue_cancel_resume_has_command_audit_and_receipt(settings, tmp_path: Path) -> None:
    source = tmp_path / "accounts.json"
    source.write_text(json.dumps({"accounts": [{"id": 1, "name": "账号 1"}]}), encoding="utf-8")
    connection, service = _service(settings, tmp_path, source)
    try:
        service.sync_runtime_accounts()
        queued = service.enqueue(account_id="legacy-1", content_id="c1", body="# 内容", operator="tester")
        cancelled = service.set_queue_item_state(
            queue_item_id=queued["queue_item_id"], state="cancelled", operator="tester"
        )
        resumed = service.set_queue_item_state(
            queue_item_id=queued["queue_item_id"], state="queued", operator="tester"
        )
        connection.commit()
        assert cancelled["status"] == "cancelled"
        assert resumed["status"] == "queued"
        assert connection.execute(
            "SELECT status FROM publish_queue_items WHERE publish_queue_item_id=?",
            (queued["queue_item_id"],),
        ).fetchone()[0] == "queued"
        assert connection.execute(
            "SELECT COUNT(*) FROM command_runs WHERE module_key='publishing'"
        ).fetchone()[0] >= 3
        assert connection.execute(
            "SELECT COUNT(*) FROM dual_write_receipts WHERE module_key='publishing'"
        ).fetchone()[0] >= 3
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'publishing.%'"
        ).fetchone()[0] >= 3
    finally:
        connection.close()


def test_real_publish_stays_blocked_and_receipted(settings, tmp_path: Path) -> None:
    source = tmp_path / "accounts.json"
    source.write_text(json.dumps({"accounts": [{"id": 1, "name": "账号 1"}]}), encoding="utf-8")
    connection, service = _service(settings, tmp_path, source)
    try:
        service.sync_runtime_accounts()
        result = service.publish(
            account_id="legacy-1", content_id="c2", body="# 内容", confirm=True, operator="tester"
        )
        connection.commit()
        assert result["status"] == "blocked"
        assert result["reason_code"] == "publish.bridge_unavailable"
        assert result["dual_write_receipt_id"]
        assert connection.execute(
            "SELECT hub_status, reconcile_status FROM dual_write_receipts "
            "WHERE module_key='publishing' AND idempotency_key LIKE 'publish:%'"
        ).fetchone()[0] == "blocked"
        assert connection.execute(
            "SELECT COUNT(*) FROM publish_attempts WHERE status='blocked'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
