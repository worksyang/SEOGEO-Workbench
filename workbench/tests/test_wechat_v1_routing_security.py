from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from content_hub.adapters.wechat import WechatAdapter
from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.services.migration import WECHAT_HTTP_OPERATIONS, wechat_http_operation


def _fixture_settings(settings, tmp_path: Path):
    root = tmp_path / "isolated-freeze"
    normalized = root / "normalized"
    normalized.mkdir(parents=True)

    def write(name: str, value) -> None:
        (normalized / name).write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    write(
        "monitor-data.json",
        {
            "generated_at": "2026-07-16T00:00:00",
            "window_days": 1,
            "keywords": [
                {
                    "keyword_id": "kw_v1",
                    "keyword": "安全关键词",
                    "topic": "安全关键词",
                    "keyword_bucket": "测试",
                }
            ],
            "accounts": [
                {"account_id": "acct_v1", "canonical_name": "安全账号"}
            ],
        },
    )
    write(
        "accounts.json",
        [
            {
                "account_id": "acct_v1",
                "canonical_name": "安全账号",
                "first_seen_at": "2026-07-15T00:00:00",
            }
        ],
    )
    write(
        "articles.json",
        [
            {
                "article_id": "art_v1",
                "normalized_url": "https://mp.weixin.qq.com/s/v1-safe",
                "title": "安全文章",
                "account_id": "acct_v1",
                "published_at": "2026-07-15T10:00:00",
                "read_count": 8,
            }
        ],
    )
    write(
        "snapshots.json",
        [
            {
                "snapshot_id": "snap_v1",
                "keyword_id": "kw_v1",
                "captured_at": "2026-07-16T00:00:00",
                "result_count": 1,
            }
        ],
    )
    write(
        "snapshot_registry.json",
        {str(root / "safe.md"): {"keyword_text": "安全关键词"}},
    )
    write("snapshot_terms.json", [])
    write(
        "ranking_hits.json",
        [
            {
                "hit_id": "hit_v1",
                "snapshot_id": "snap_v1",
                "rank": 1,
                "article_id": "art_v1",
                "title_raw": "安全文章",
                "account_name_raw": "安全账号",
            }
        ],
    )
    write(
        "article_metric_observations.json",
        [
            {
                "observation_id": "obs_v1",
                "article_id": "art_v1",
                "observed_at": "2026-07-16T00:00:00",
                "read_count": 8,
            }
        ],
    )
    return replace(
        settings,
        wechat_source_url="http://127.0.0.1:8774",
        wechat_source_root=root,
    )


def _force_hub_reads(settings) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE migration_switches
            SET data_mode='hub', enabled=1, updated_by='v1-security-test'
            WHERE module_key='wechat-search'
              AND contract_key IN (
                'bootstrap','keyword','article-hit-detail',
                'article-content','refresh-status'
              )
            """
        )


def _fail_all_adapter_http(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("v1 hub route attempted adapter HTTP")

    for name in (
        "_request_response",
        "remote_bootstrap",
        "remote_keyword",
        "remote_refresh",
        "remote_article_content",
        "remote_hit_detail",
        "remote_refresh_status",
    ):
        monkeypatch.setattr(WechatAdapter, name, fail)


def _fact_counts(settings) -> dict[str, int]:
    tables = (
        "keywords",
        "creators",
        "contents",
        "search_snapshots",
        "search_hits",
        "metric_observations",
    )
    with connect(settings, readonly=True) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_v1_hub_routes_import_refresh_status_and_restart_never_call_http(
    settings, tmp_path, monkeypatch
) -> None:
    configured = _fixture_settings(settings, tmp_path)
    _force_hub_reads(configured)
    _fail_all_adapter_http(monkeypatch)

    async def scenario() -> None:
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                dry_run = await client.post(
                    "/api/v1/wechat/import",
                    json={"dry_run": True},
                )
                assert dry_run.status_code == 200
                assert dry_run.json()["data"]["migration"] == {
                    "module_key": "wechat-search",
                    "contract_key": "history-import",
                    "data_mode": "hub",
                    "hub_only": True,
                    "resolver": False,
                }
                import_payload = {
                    "confirm": True,
                    "idempotency_key": "v1-full-import",
                }
                first_import = await client.post(
                    "/api/v1/wechat/import",
                    json=import_payload,
                )
                assert first_import.status_code == 200
                first_counts = _fact_counts(configured)
                second_import = await client.post(
                    "/api/v1/wechat/import",
                    json=import_payload,
                )
                assert second_import.status_code == 200
                assert _fact_counts(configured) == first_counts
                assert (
                    second_import.json()["data"]["batch_id"]
                    == first_import.json()["data"]["batch_id"]
                )

                bootstrap = await client.get("/api/v1/wechat/bootstrap")
                keyword = await client.get("/api/v1/wechat/keywords/kw_v1")
                article = await client.get("/api/v1/wechat/articles/art_v1")
                content = await client.get(
                    "/api/v1/wechat/articles/art_v1/content"
                )
                assert bootstrap.status_code == keyword.status_code == article.status_code == 200
                assert bootstrap.json()["data"]["migration"]["mode"] == "hub"
                assert keyword.json()["data"]["migration"]["mode"] == "hub"
                assert article.json()["data"]["migration"]["mode"] == "hub"
                assert content.status_code == 404
                assert content.json()["error"]["code"] == "NOT_FOUND"

                missing_confirmation = await client.post(
                    "/api/v1/wechat/keywords/kw_v1/refresh",
                    json={"idempotency_key": "v1-missing-confirm"},
                )
                missing_key = await client.post(
                    "/api/v1/wechat/keywords/kw_v1/refresh",
                    json={"confirm": True},
                )
                assert missing_confirmation.status_code == 422
                assert missing_key.status_code == 422

                before_refresh = _fact_counts(configured)
                first_refresh = await client.post(
                    "/api/v1/wechat/keywords/kw_v1/refresh",
                    json={
                        "confirm": True,
                        "keyword": "安全关键词",
                        "idempotency_key": "v1-disabled-refresh",
                    },
                )
                replay = await client.post(
                    "/api/v1/wechat/keywords/kw_v1/refresh",
                    json={
                        "confirm": True,
                        "keyword": "安全关键词",
                        "idempotency_key": "v1-disabled-refresh",
                    },
                )
                conflict = await client.post(
                    "/api/v1/wechat/keywords/kw_v1/refresh",
                    json={
                        "confirm": True,
                        "keyword": "不同输入",
                        "idempotency_key": "v1-disabled-refresh",
                    },
                )
                assert first_refresh.status_code == replay.status_code == 409
                assert first_refresh.json()["data"]["status"] == "blocked"
                assert first_refresh.json()["data"]["upstream_called"] is False
                assert (
                    first_refresh.json()["data"]["job_id"]
                    == replay.json()["data"]["job_id"]
                )
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "CONFLICT"
                assert _fact_counts(configured) == before_refresh

                job_id = first_refresh.json()["data"]["job_id"]
                known = await client.get(
                    f"/api/v1/wechat/refresh-status/{job_id}"
                )
                unknown = await client.get(
                    "/api/v1/wechat/refresh-status/srj_unknown"
                )
                assert known.status_code == 200
                assert known.json()["data"]["result"]["status"] == "blocked"
                assert known.json()["data"]["migration"]["mode"] == "hub"
                assert unknown.status_code == 404
                assert unknown.json()["error"]["code"] == "NOT_FOUND"

        restarted = create_app(configured)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://testserver",
            ) as client:
                recovered = await client.get(
                    f"/api/v1/wechat/refresh-status/{job_id}"
                )
                assert recovered.status_code == 200
                assert recovered.json()["data"]["result"]["status"] == "blocked"

    asyncio.run(scenario())
    with connect(configured, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM command_runs "
            "WHERE idempotency_key='v1-disabled-refresh'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM dual_write_receipts "
            "WHERE idempotency_key='v1-disabled-refresh'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='wechat.refresh' AND outcome='blocked'"
        ).fetchone()[0] == 1


def test_v1_refresh_status_requires_explicit_enabled_switch(
    settings, monkeypatch
) -> None:
    _fail_all_adapter_http(monkeypatch)
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE migration_switches
            SET enabled=0
            WHERE module_key='wechat-search' AND contract_key='refresh-status'
            """
        )

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.get(
                    "/api/v1/wechat/refresh-status/srj_missing"
                )
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "CONFLICT"
                assert "未明确启用" in response.json()["error"]["message"]

    asyncio.run(scenario())


def test_v1_refresh_middleware_requires_keywords_refresh_hub_mode(
    settings, monkeypatch
) -> None:
    _fail_all_adapter_http(monkeypatch)
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE migration_switches
            SET data_mode='legacy'
            WHERE module_key='wechat-search' AND contract_key='keywords-refresh'
            """
        )

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/v1/wechat/keywords/kw_missing/refresh",
                    json={
                        "confirm": True,
                        "idempotency_key": "must-not-run",
                    },
                )
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "CONFLICT"
        with connect(settings, readonly=True) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM command_runs "
                "WHERE idempotency_key='must-not-run'"
            ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_v1_registry_extends_but_does_not_change_legacy_43_operations() -> None:
    legacy = [
        item
        for item in WECHAT_HTTP_OPERATIONS
        if item["path"].startswith("/api/") and not item["path"].startswith("/api/v1/")
    ]
    assert len(legacy) == 43
    assert sum(item["kind"] == "read" for item in legacy) == 22
    assert sum(item["kind"] == "write" for item in legacy) == 21
    assert wechat_http_operation(
        "POST", "/api/v1/wechat/keywords/kw_v1/refresh"
    )["contract_key"] == "keywords-refresh"
    assert wechat_http_operation(
        "POST", "/api/v1/wechat/keywords/kw_v1/refresh"
    )["kind"] == "write"
    assert wechat_http_operation(
        "POST", "/api/v1/wechat/import"
    )["kind"] == "hub-only"
