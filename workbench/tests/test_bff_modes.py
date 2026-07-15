from __future__ import annotations

from test_wechat import _fixture_settings

from content_hub.db.connection import connect
from content_hub.features.wechat.service import WechatService
from content_hub.features.xhs.service import XhsService


def _switch(settings, module: str, contract: str, mode: str) -> None:
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO migration_switches(
                switch_id,module_key,contract_key,data_mode,enabled,rollback_mode,
                updated_at,updated_by,reason
            ) VALUES(?,?,?,?,1,'legacy','2026-07-16T00:00:00Z','test','test')
            ON CONFLICT(module_key,contract_key) DO UPDATE SET data_mode=excluded.data_mode
            """,
            (f"sw_{module}_{contract}", module, contract, mode),
        )


def test_wechat_compare_records_real_contract_comparison(settings, tmp_path) -> None:
    configured = _fixture_settings(settings, tmp_path)
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None)
    _switch(configured, "wechat-search", "bootstrap", "compare")

    payload = service.bootstrap()

    assert payload["migration"]["mode"] == "compare"
    assert payload["migration"]["comparison"]["comparison_id"]
    with connect(configured, readonly=True) as con:
        row = con.execute(
            """
            SELECT status,legacy_hash,hub_hash
            FROM contract_comparisons
            WHERE module_key='wechat-search' AND contract_key='bootstrap'
            ORDER BY compared_at DESC LIMIT 1
            """
        ).fetchone()
    assert row["status"] in {"matched", "different", "hub_error", "legacy_error"}
    assert row["legacy_hash"] and row["hub_hash"]


def test_xhs_hub_refresh_is_explicitly_blocked_and_receipted(settings, tmp_path) -> None:
    configured = settings
    _switch(configured, "xhs-search", "refresh", "hub")
    with connect(configured) as con:
        con.execute(
            """
            INSERT INTO keywords(
                keyword_id,platform,keyword,first_seen_at,updated_at,payload_json
            ) VALUES('xhs_keyword_test','xiaohongshu','香港保险',
                     '2026-07-16T00:00:00Z','2026-07-16T00:00:00Z','{}')
            """
        )
    result = XhsService(configured).refresh("xhs_keyword_test", True, idempotency_key="bff-test-xhs")
    assert result["http_status"] == 409
    assert result["blocked"] is True
    with connect(configured, readonly=True) as con:
        receipt = con.execute(
            """
            SELECT legacy_status,hub_status,reconcile_status,details_json
            FROM dual_write_receipts
            WHERE module_key='xhs-search' AND idempotency_key='bff-test-xhs'
            """
        ).fetchone()
    assert tuple(receipt[:3]) == ("not_attempted", "not_implemented", "blocked")
    assert "xhs.hub_refresh_not_implemented" in receipt["details_json"]
