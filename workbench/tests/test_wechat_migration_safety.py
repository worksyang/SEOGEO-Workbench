from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from content_hub.adapters.wechat import WechatAdapter
from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.services.migration import wechat_http_operation


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765",
        "http://127.0.0.1:8775",
        "https://127.0.0.1:8774",
        "http://0.0.0.0:8774",
        "http://example.com:8774",
        "http://user:pass@127.0.0.1:8774",
        "http://127.0.0.1:8774/path",
    ],
)
def test_wechat_reference_url_hard_guard_rejects_unsafe_values(monkeypatch, url: str) -> None:
    monkeypatch.setenv("HUB_WECHAT_SOURCE_URL", url)
    with pytest.raises(ValueError):
        Settings.load()


def test_wechat_reference_url_normalizes_loopback_aliases(monkeypatch) -> None:
    for value in ("http://localhost:8774/", "http://[::1]:8774"):
        monkeypatch.setenv("HUB_WECHAT_SOURCE_URL", value)
        assert Settings.load().wechat_source_url == "http://127.0.0.1:8774"


@pytest.mark.parametrize(
    "root",
    [
        "/tmp/wechat-freeze",
        "/Users/works14/.claude/监控/wechat-ybxhyyh-top3",
        "/Users/works14/.claude/监控/wechat-ybxhyyh-top3/normalized",
    ],
)
def test_wechat_freeze_root_hard_guard_rejects_external_or_real_source(
    monkeypatch, root: str
) -> None:
    monkeypatch.setenv("HUB_WECHAT_FREEZE_ROOT", root)
    with pytest.raises(ValueError):
        Settings.load()


def test_wechat_freeze_root_allows_project_isolated_temp_root(monkeypatch) -> None:
    base = Settings.load()
    root = base.project_root / "data/migration/wechat/test-freeze-safety"
    monkeypatch.setenv("HUB_WECHAT_FREEZE_ROOT", str(root))
    assert Settings.load().wechat_source_root == root.resolve()


def test_registered_v1_refresh_uses_persistent_hub_runtime_and_never_calls_adapter(
    settings, monkeypatch
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("v1 hub refresh attempted legacy HTTP")

    monkeypatch.setattr(WechatAdapter, "_request_response", fail_if_called)
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO keywords(
                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES('kw-1','wechat-search','安全样本','active',
                     '2026-07-16T00:00:00Z','2026-07-16T00:00:00Z','{}')
            """
        )

    async def scenario() -> None:
        app = create_app(settings)

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/api/v1/wechat/keywords/kw-1/refresh",
                    json={
                        "confirm": True,
                        "idempotency_key": "v1-refresh-disabled",
                        "keyword": "安全样本",
                    },
                )
                assert response.status_code == 409
                payload = response.json()
                assert payload["ok"] is False
                assert payload["data"]["status"] == "blocked"
                assert payload["data"]["upstream_called"] is False

    asyncio.run(scenario())
    assert wechat_http_operation(
        "POST", "/api/v1/wechat/keywords/kw-1/refresh"
    ) == {
        "method": "POST",
        "path": "/api/v1/wechat/keywords/{keyword_id}/refresh",
        "contract_key": "keywords-refresh",
        "kind": "write",
    }
    with connect(settings, readonly=True) as connection:
        row = connection.execute(
            "SELECT action,outcome,details_json FROM audit_log "
            "WHERE action='wechat.refresh' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM command_runs "
            "WHERE idempotency_key='v1-refresh-disabled'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM dual_write_receipts "
            "WHERE idempotency_key='v1-refresh-disabled'"
        ).fetchone()[0] == 1
    assert row["action"] == "wechat.refresh"
    assert row["outcome"] == "blocked"
    details = json.loads(row["details_json"])
    assert details["reason_code"] == "provider_disabled"
