from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect


def _fixture_settings(settings, tmp_path: Path):
    root = tmp_path / "legacy"
    (root / "normalized").mkdir(parents=True)
    def write(name, value):
        (root / "normalized" / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    write("monitor-data.json", {"generated_at": "2026-07-14T01:00:00", "window_days": 1, "keywords": [{"keyword_id": "kw_1", "keyword": "港险", "topic": "港险", "keyword_bucket": "主题"}], "accounts": [{"account_id": "acct_1", "canonical_name": "作者"}]})
    write("accounts.json", [{"account_id": "acct_1", "canonical_name": "作者", "first_seen_at": "2026-07-13T00:00:00"}])
    write("articles.json", [{"article_id": "art_1", "normalized_url": "https://mp.weixin.qq.com/s/1", "title": "标题", "account_id": "acct_1", "published_at": "2026-07-13T10:00:00", "read_count": 8}])
    write("snapshots.json", [{"snapshot_id": "snap_1", "keyword_id": "kw_1", "captured_at": "2026-07-14T00:00:00", "result_count": 1}])
    write("snapshot_registry.json", {str(root / "source.md"): {"keyword_text": "港险"}})
    write("snapshot_terms.json", [{"term_id": "term_1", "snapshot_id": "snap_1", "term_type": "suggestion", "position": 1, "term_text": "港险"}])
    write("ranking_hits.json", [{"hit_id": "hit_1", "snapshot_id": "snap_1", "rank": 1, "article_id": "art_1", "title_raw": "标题", "account_name_raw": "作者"}])
    write("article_metric_observations.json", [{"observation_id": "obs_1", "article_id": "art_1", "observed_at": "2026-07-14T00:00:00", "read_count": 8}])
    return replace(settings, wechat_source_url="http://127.0.0.1:1", wechat_source_root=root)


def test_wechat_degraded_bootstrap_and_idempotent_import(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                bootstrap = await client.get("/api/v1/wechat/bootstrap")
                assert bootstrap.status_code == 200
                assert bootstrap.json()["data"]["source_status"]["status"] == "degraded"
                assert bootstrap.json()["data"]["summary"]["keyword_count"] == 1
                dry = await client.post("/api/v1/wechat/import", json={"dry_run": True})
                assert dry.json()["data"]["counts"]["contents"] == 1
                payload = {"confirm": True, "idempotency_key": "route-import-1"}
                first = await client.post("/api/v1/wechat/import", json=payload)
                second = await client.post("/api/v1/wechat/import", json=payload)
                assert first.status_code == second.status_code == 200
                assert first.json()["data"]["batch_id"] == second.json()["data"]["batch_id"]
                with connect(configured, readonly=True) as connection:
                    assert connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
                    assert connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0] == 1
                    assert connection.execute(
                        "SELECT status FROM command_runs WHERE module_key='wechat-search' AND idempotency_key='route-import-1'"
                    ).fetchone()[0] == "succeeded"
                article = await client.get("/api/v1/wechat/articles/art_1")
                assert article.status_code == 200
                assert article.json()["data"]["article"]["title"] == "标题"
    asyncio.run(run())


def test_wechat_hub_bootstrap_uses_live_projection_contract(settings, monkeypatch):
    from content_hub.features.wechat.service import WechatService

    projection = {
        "generated_at": "2026-07-18T16:02:07.251850",
        "window_days": 15,
        "keywords": [
            {
                "keyword_id": "kw_visible",
                "keyword": "可见关键词",
                "status": "active",
                "topic": "主题",
                "keyword_bucket": "分组",
                "today_count": 3,
                "latest_run": {"id": "snap_latest", "date": "2026-07-18"},
            }
        ],
        "accounts": [{"account_id": "acct_hit", "name": "命中账号"}],
    }
    monkeypatch.setattr(
        "content_hub.features.wechat.service.WechatLegacyRepository.bootstrap",
        lambda self: projection,
    )
    payload = WechatService(settings)._bootstrap_hub()
    assert payload["source_status"] == {"status": "healthy", "source": "hub_db"}
    assert payload["summary"] == {
        "keyword_count": 1,
        "account_count": 1,
        "generated_at": "2026-07-18T16:02:07.251850",
        "window_days": 15,
    }
    assert payload["updated_at"] == "2026-07-18T16:02:07.251850"
    assert payload["keywords"][0]["today_count"] == 3
    assert payload["keywords"][0]["latest_run"]["id"] == "snap_latest"


def test_live_projection_keeps_utc_source_date_for_late_snapshot():
    from content_hub.services.wechat_live_projection import _iso_date, _legacy_local_iso

    value = "2026-07-18T18:15:12.947Z"
    assert _iso_date(value) == "2026-07-18"
    assert _legacy_local_iso(value) == "2026-07-18T18:15:12.947"


def test_wechat_full_import_route_requires_confirmation_and_idempotency_key(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)

    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (await client.post("/api/v1/wechat/import", json={})).status_code == 422
                assert (await client.post(
                    "/api/v1/wechat/import", json={"confirm": True}
                )).status_code == 422
                assert (await client.post(
                    "/api/v1/wechat/import",
                    json={"confirm": True, "idempotency_key": "formal-route"},
                )).status_code == 200

    asyncio.run(run())


def test_wechat_refresh_requires_confirmation_and_refuses_unavailable_source(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                assert (await client.post(
                    "/api/v1/wechat/import",
                    json={"confirm": True, "idempotency_key": "refresh-import-1"},
                )).status_code == 200
                missing = await client.post("/api/v1/wechat/keywords/kw_1/refresh", json={})
                assert missing.status_code == 422
                no_key = await client.post(
                    "/api/v1/wechat/keywords/kw_1/refresh",
                    json={"confirm": True},
                )
                assert no_key.status_code == 422
                refused = await client.post(
                    "/api/v1/wechat/keywords/kw_1/refresh",
                    json={"confirm": True, "idempotency_key": "disabled-v1"},
                )
                assert refused.status_code == 409
                assert refused.json()["ok"] is False
                assert refused.json()["data"]["status"] == "blocked"
                assert refused.json()["data"]["upstream_called"] is False
    asyncio.run(run())


def test_wechat_manifest_timezone_canonical_placeholder_and_closure(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.adapters.wechat import WechatAdapter
    adapter = WechatAdapter(configured)
    records, manifest, audit = adapter.import_records(limit=1)
    assert len(records["snapshots"]) == 1
    assert len(records["hits"]) == 1
    assert {x["article_id"] for x in records["articles"]} == {"art_1"}
    assert audit["registry_count"] == 1
    first = adapter.manifest_id(manifest)
    article_path = configured.wechat_source_root / "normalized/articles.json"
    article_path.write_text(article_path.read_text(encoding="utf-8").replace("标题", "标题2"), encoding="utf-8")
    _, changed_manifest, _ = adapter.import_records(limit=1)
    assert adapter.manifest_id(changed_manifest) != first
    from content_hub.features.wechat.service import _safe_url, _source_time
    assert _safe_url("https://mp.weixin.qq.com/s/1?utm_source=x") == "https://mp.weixin.qq.com/s/1"
    assert _safe_url("placeholder://作者/标题") is None
    assert _source_time("2026-07-14 10:00:00") == "2026-07-14T02:00:00Z"
    assert _source_time("26/07/10") == "2026-07-09T16:00:00Z"


def test_wechat_schema_invalid_and_audit_reconcile(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    bad = configured.wechat_source_root / "normalized/accounts.json"
    bad.write_text(json.dumps({"not": "rows"}), encoding="utf-8")
    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.post("/api/v1/wechat/import", json={"dry_run": True})
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "CONFLICT"
    asyncio.run(run())


def test_wechat_service_refresh_is_hub_only(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    from content_hub.services.wechat_refresh import FakeWechatRefreshProvider

    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-refresh-fixture")
    succeeded = service.refresh(
        "kw_1",
        True,
        idempotency_key="service-hub-success",
        provider=FakeWechatRefreshProvider(),
    )
    assert succeeded["status"] == "succeeded"
    blocked = service.refresh("kw_1", True, idempotency_key="service-hub-disabled")
    assert blocked["status"] == "blocked"
    assert blocked["upstream_called"] is False


def test_wechat_snapshot_slice_contract_and_term_features(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    root = configured.wechat_source_root / "normalized"
    (root / "snapshots.json").write_text(json.dumps([
        {"snapshot_id": "snap_1", "keyword_id": "kw_1", "captured_at": "2026-07-14T00:00:00", "result_count": 1, "trigger_type": "manual"},
        {"snapshot_id": "snap_2", "keyword_id": "kw_1", "captured_at": "2026-07-14T01:00:00", "result_count": 1, "trigger_type": "scheduled"},
    ]), encoding="utf-8")
    (root / "articles.json").write_text(json.dumps([
        {"article_id": "art_1", "normalized_url": "https://mp.weixin.qq.com/s/1", "title": "旧文章", "account_id": "acct_1"},
        {"article_id": "art_2", "normalized_url": "https://mp.weixin.qq.com/s/2", "title": "新文章", "account_id": "acct_1"},
    ]), encoding="utf-8")
    (root / "ranking_hits.json").write_text(json.dumps([
        {"hit_id": "hit_1", "snapshot_id": "snap_1", "rank": 2, "article_id": "art_1"},
        {"hit_id": "hit_2", "snapshot_id": "snap_2", "rank": 1, "article_id": "art_2"},
    ]), encoding="utf-8")
    (root / "snapshot_terms.json").write_text(json.dumps([
        {"term_id": "term_1", "snapshot_id": "snap_1", "term_type": "suggestion", "position": 1, "term_text": "旧词"},
        {"term_id": "term_2", "snapshot_id": "snap_2", "term_type": "related", "position": 1, "term_text": "新词"},
    ]), encoding="utf-8")
    (root / "article_metric_observations.json").write_text(json.dumps([
        {"observation_id": "obs_1", "article_id": "art_1", "source_snapshot_id": "snap_1", "observed_at": "2026-07-14T00:00:00", "read_count": 1},
        {"observation_id": "obs_2", "article_id": "art_2", "source_snapshot_id": "snap_2", "observed_at": "2026-07-14T01:00:00", "read_count": 2},
    ]), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    payload = WechatService(configured).keyword("kw_1")
    views = payload["snapshots"]
    assert [view["snapshot_id"] for view in views] == ["snap_1", "snap_2"]
    assert views[0]["hits"][0]["rank"] == 2 and views[1]["hits"][0]["rank"] == 1
    assert views[0]["articles"][0]["article_id"] == "art_1"
    assert views[1]["articles"][0]["article_id"] == "art_2"
    assert views[0]["features"]["suggestions"][0]["term"] == "旧词"
    assert views[1]["features"]["related"][0]["term"] == "新词"
    assert views[0]["observations"][0]["observation_id"] == "obs_1"
    assert views[1]["observations"][0]["observation_id"] == "obs_2"


def test_wechat_import_features_platform_and_zero_growth(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    from content_hub.db.migrations import migrate
    migrate(configured)
    service = WechatService(configured)
    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-import-features")
    with connect(configured, readonly=True) as con:
        before = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("keywords", "creators", "contents", "content_identifiers", "search_snapshots", "search_hits", "content_discoveries", "metric_definitions", "metric_observations", "ingestion_batches")}
        features = con.execute("SELECT features_json,platform FROM search_snapshots WHERE snapshot_id='snap_1'").fetchone()
        assert json.loads(features[0])["suggestions"][0]["term"] == "港险"
        assert features[1] == "wechat-search"
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat.snapshot_term'").fetchone()[0] == 0
    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-import-features")
    assert first["batch_id"] == second["batch_id"]
    with connect(configured, readonly=True) as con:
        after = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before


def test_wechat_limit_and_full_have_distinct_batches_and_consistency_gate(settings, tmp_path, monkeypatch):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    limited = service.import_history(dry_run=True, limit=1)
    full = service.import_history(dry_run=True, limit=None)
    assert limited["batch_id"] != full["batch_id"]
    from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
    adapter = WechatAdapter(configured)
    original = adapter.all_records
    def changing():
        result = original()
        path = configured.wechat_source_root / "normalized/articles.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return result
    monkeypatch.setattr(adapter, "all_records", changing)
    try:
        adapter.import_records(max_consistency_attempts=1)
    except WechatSourceError as exc:
        assert exc.kind == "source_changed_during_read"
    else:
        raise AssertionError("必须拒绝读中变更的源文件")


def test_wechat_reconciliation_verified_only_for_exact_full_match(settings, tmp_path, monkeypatch):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    from content_hub.db.migrations import migrate

    migrate(configured)
    service = WechatService(configured)
    matched = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-reconcile-matched")
    assert matched["audit"]["reconcile"]["verified"] is True
    assert matched["audit"]["reconcile"]["status"] == "matched"
    assert all(
        set(dimension) == {"source", "hub", "difference", "match"}
        for dimension in matched["audit"]["reconcile"]["dimensions"].values()
    )

    original = service._reconcile_summary

    def mismatched(records, **kwargs):
        result = original(records, **kwargs)
        result["hub"]["contents"] += 1
        result["difference"]["contents"] = 1
        result["match"]["contents"] = False
        result["dimensions"]["contents"] = {
            "source": result["source"]["contents"],
            "hub": result["hub"]["contents"],
            "difference": 1,
            "match": False,
        }
        result["status"] = "mismatch"
        result["verified"] = False
        return result

    monkeypatch.setattr(service, "_reconcile_summary", mismatched)
    mismatch = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-reconcile-mismatch")
    assert mismatch["audit"]["reconcile"]["verified"] is False
    assert mismatch["audit"]["reconcile"]["status"] == "mismatch"
    with connect(configured, readonly=True) as connection:
        status = connection.execute(
            "SELECT status, details_json FROM system_connections WHERE system_key='wechat-search'"
        ).fetchone()
        assert status[0] == "degraded"
        assert "reconciliation_mismatch" in status[1]


def test_wechat_reconciliation_limit_and_dry_run_are_not_verified(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService

    service = WechatService(configured)
    limited = service.import_history(dry_run=True, limit=1)
    assert limited["audit"]["reconcile"]["verified"] is False
    assert limited["audit"]["reconcile"]["status"] == "not_comparable"

    full_dry_run = service.import_history(dry_run=True, limit=None)
    assert full_dry_run["audit"]["reconcile"]["verified"] is False
    assert full_dry_run["audit"]["reconcile"]["status"] == "not_comparable"


def test_wechat_rejected_metrics_and_status_time(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    root = configured.wechat_source_root / "normalized/article_metric_observations.json"
    root.write_text(json.dumps([
        {"observation_id": "bad_1", "article_id": "art_1", "observed_at": "not-a-time", "read_count": 9},
        {"observation_id": "bad_2", "article_id": "art_1", "observed_at": "2026-07-14T00:00:00", "read_count": "not-a-number"},
    ]), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    result = WechatService(configured).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-invalid-observations")
    reasons = {item["reason"] for item in result["audit"]["rejected"]}
    assert "invalid_observed_at" in reasons and "invalid_numeric" in reasons
    with connect(configured, readonly=True) as con:
        row = con.execute("SELECT status,records_failed,records_written FROM ingestion_batches").fetchone()
        assert row[0] == "partial_failed" and row[1] >= 1
        status = con.execute("SELECT status,last_checked_at FROM system_connections WHERE system_key='wechat-search'").fetchone()
        assert status[0] == "degraded" and status[1]


def test_wechat_metric_collisions_are_canonical_audited_and_idempotent(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    metric_path = configured.wechat_source_root / "normalized/article_metric_observations.json"
    rows = [
        {"observation_id": "z_same", "article_id": "art_1", "source_snapshot_id": "missing", "observed_at": "2026-07-14T00:00:00", "observed_at_precision": "date", "observed_at_source": "filename", "raw_observed_at": "2026-07-14", "read_count": 3, "source_file_path": "history-z.md"},
        {"observation_id": "a_same", "article_id": "art_1", "source_snapshot_id": "also-missing", "observed_at": "2026-07-14T00:00:00", "observed_at_precision": "date", "observed_at_source": "filename", "raw_observed_at": "2026-07-14", "read_count": 3, "source_file_path": "history-a.md"},
        {"observation_id": "c_diff", "article_id": "art_1", "source_snapshot_id": "snap_1", "observed_at": "2026-07-14T01:00:00", "read_count": 6, "source_file_path": "history-c.md"},
        {"observation_id": "b_diff", "article_id": "art_1", "source_snapshot_id": "snap_1", "observed_at": "2026-07-14T01:00:00", "read_count": 5, "source_file_path": "history-b.md"},
    ]
    metric_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    from content_hub.db.migrations import migrate
    migrate(configured)
    service = WechatService(configured)
    dry = service.import_history(dry_run=True, limit=None)
    result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-collisions")
    assert dry["audit"]["metric_fact_count"] == result["audit"]["metric_fact_count"] == 4
    assert result["audit"]["metric_unique_count"] == 2
    assert result["audit"]["metric_collision_group_count"] == 2
    assert result["audit"]["metric_collision_extra_count"] == 2
    assert result["audit"]["metric_collision_same_value_count"] == 1
    assert result["audit"]["metric_collision_value_diff_count"] == 1
    assert result["audit"]["rejected"] == []
    collisions = result["audit"]["metric_collisions"]
    assert {c["same_value"] for c in collisions} == {True, False}
    assert {candidate["observation_id"] for c in collisions for candidate in c["candidates"]} == {
        "a_same:wechat.article.read_count", "z_same:wechat.article.read_count", "b_diff:wechat.article.read_count", "c_diff:wechat.article.read_count",
    }
    date_collision = next(c for c in collisions if c["same_value"])
    assert {candidate["observed_at_precision"] for candidate in date_collision["candidates"]} == {"date"}
    assert {candidate["observed_at_source"] for candidate in date_collision["candidates"]} == {"filename"}
    assert {candidate["raw_observed_at"] for candidate in date_collision["candidates"]} == {"2026-07-14"}
    with connect(configured, readonly=True) as con:
        values = con.execute("SELECT observation_id,numeric_value,snapshot_id FROM metric_observations ORDER BY observed_at,snapshot_id").fetchall()
        assert [(row[0], row[1], row[2]) for row in values] == [
            ("a_same:wechat.article.read_count", 3.0, None),
            ("b_diff:wechat.article.read_count", 5.0, "snap_1"),
        ]
        payload = con.execute("SELECT payload_json FROM ingestion_batches WHERE batch_id=?", (result["batch_id"],)).fetchone()[0]
        assert json.loads(payload)["metric_collisions"] == collisions
    repeated = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-collisions")
    assert repeated["batch_id"] == result["batch_id"]
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT observation_id,numeric_value,snapshot_id FROM metric_observations ORDER BY observed_at,snapshot_id").fetchall() == values


def test_wechat_metric_collision_order_does_not_change_winner(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    metric_path = configured.wechat_source_root / "normalized/article_metric_observations.json"
    rows = [
        {"observation_id": "winner", "article_id": "art_1", "observed_at": "2026-07-14", "read_count": 8},
        {"observation_id": "loser", "article_id": "art_1", "observed_at": "2026-07-14", "read_count": 9},
    ]
    metric_path.write_text(json.dumps(rows), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-order-first")
    metric_path.write_text(json.dumps(list(reversed(rows))), encoding="utf-8")
    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-order-second")
    assert first["audit"]["metric_collisions"][-1]["candidates"][0]["observation_id"] == "loser:wechat.article.read_count"
    with connect(configured, readonly=True) as con:
        row = con.execute("SELECT observation_id,numeric_value FROM metric_observations").fetchone()
        assert tuple(row) == ("loser:wechat.article.read_count", 9.0)
        assert second["audit"]["metric_collision_value_diff_count"] == 1


def test_wechat_metric_collision_same_or_missing_id_is_order_independent(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    metric_path = configured.wechat_source_root / "normalized/article_metric_observations.json"
    rows = [
        {"observation_id": "reused", "article_id": "art_1", "observed_at": "2026-07-14", "read_count": 9, "source_file_path": "b.md"},
        {"observation_id": "reused", "article_id": "art_1", "observed_at": "2026-07-14", "read_count": 8, "source_file_path": "a.md"},
        {"observation_id": "", "article_id": "art_1", "observed_at": "2026-07-14T01:00:00", "read_count": 7, "source_file_path": "d.md"},
        {"observation_id": "", "article_id": "art_1", "observed_at": "2026-07-14T01:00:00", "read_count": 6, "source_file_path": "c.md"},
    ]
    metric_path.write_text(json.dumps(rows), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-identity-first")
    with connect(configured, readonly=True) as con:
        first_rows = [tuple(row) for row in con.execute("SELECT observation_id,numeric_value,payload_json FROM metric_observations ORDER BY observed_at")]
    metric_path.write_text(json.dumps(list(reversed(rows))), encoding="utf-8")
    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-identity-second")
    with connect(configured, readonly=True) as con:
        second_rows = [tuple(row) for row in con.execute("SELECT observation_id,numeric_value,payload_json FROM metric_observations ORDER BY observed_at")]
    assert first["audit"]["metric_collisions"] == second["audit"]["metric_collisions"]
    assert first_rows == second_rows
    assert [(row[1]) for row in second_rows] == [8.0, 7.0]


def test_wechat_metric_collision_replaces_preexisting_noncanonical_row(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    metric_path = configured.wechat_source_root / "normalized/article_metric_observations.json"
    old = {"observation_id": "z_old", "article_id": "art_1", "observed_at": "2026-07-14", "read_count": 4}
    metric_path.write_text(json.dumps([old]), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-pruning-first")
    canonical = {**old, "observation_id": "a_new", "read_count": 4}
    metric_path.write_text(json.dumps([old, canonical]), encoding="utf-8")
    result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-metric-pruning-second")
    assert result["audit"]["rejected"] == []
    with connect(configured, readonly=True) as con:
        rows = con.execute("SELECT observation_id,numeric_value FROM metric_observations").fetchall()
        assert [tuple(row) for row in rows] == [("a_new:wechat.article.read_count", 4.0)]


def test_wechat_hit_pk_unique_and_snapshot_unique_conflicts(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-hit-unique-first")
    hits_path = configured.wechat_source_root / "normalized/ranking_hits.json"
    hits_path.write_text(json.dumps([{"hit_id": "hit_new", "snapshot_id": "snap_other", "rank": 1, "article_id": "art_1", "title_raw": "变化"}]), encoding="utf-8")
    snapshots_path = configured.wechat_source_root / "normalized/snapshots.json"
    snapshots_path.write_text(json.dumps([{"snapshot_id": "snap_other", "keyword_id": "kw_1", "captured_at": "2026-07-14T00:00:00", "result_count": 1}]), encoding="utf-8")
    result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-hit-unique-second")
    assert result["audit"]["rejected"] == []
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM search_snapshots").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] == 1
        row = con.execute("SELECT hit_id,title_raw FROM search_hits").fetchone()
        assert row[0] == "hit_new" and row[1] == "变化"


def test_wechat_refresh_route_preserves_hub_status_and_body(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.services.wechat_refresh import FakeWechatRefreshProvider

    async def run():
        app = create_app(configured)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                assert (await client.post(
                    "/api/v1/wechat/import",
                    json={"confirm": True, "idempotency_key": "refresh-route-import-1"},
                )).status_code == 200
                succeeded = await client.post(
                    "/api/v1/wechat/keywords/kw_1/refresh",
                    json={"confirm": True, "idempotency_key": "route-hub-success"},
                )
                assert succeeded.status_code == 200
                assert succeeded.json()["data"]["status"] == "succeeded"
                app.state.wechat_refresh_provider = None
                blocked = await client.post(
                    "/api/v1/wechat/keywords/kw_1/refresh",
                    json={"confirm": True, "idempotency_key": "route-hub-disabled"},
                )
                assert blocked.status_code == 409
                assert blocked.json()["data"]["status"] == "blocked"
                assert blocked.json()["data"]["upstream_called"] is False
    asyncio.run(run())


def test_wechat_cache_replaces_version_without_accumulating(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.adapters.wechat import WechatAdapter
    adapter = WechatAdapter(configured)
    first = adapter.local_json("normalized/articles.json")
    path = configured.wechat_source_root / "normalized/articles.json"
    path.write_text(json.dumps([{**first[0], "title": "版本二"}]), encoding="utf-8")
    second = adapter.local_json("normalized/articles.json")
    assert second[0]["title"] == "版本二"
    key = (str(configured.wechat_source_root), "normalized/articles.json")
    assert len([cache_key for cache_key in WechatAdapter._json_cache if cache_key == key]) == 1


@pytest.mark.parametrize(
    ("filename", "bad_value"),
    [
        ("accounts.json", [{}]),
        ("articles.json", [{}]),
        ("snapshots.json", [{}]),
        ("ranking_hits.json", [{}]),
        ("snapshot_terms.json", [{}]),
        ("article_metric_observations.json", [{}]),
    ],
)
def test_wechat_each_source_schema_enforces_required_fields(settings, tmp_path, filename, bad_value):
    configured = _fixture_settings(settings, tmp_path)
    path = configured.wechat_source_root / "normalized" / filename
    path.write_text(json.dumps(bad_value), encoding="utf-8")
    from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
    with pytest.raises(WechatSourceError, match="Schema"):
        WechatAdapter(configured).local_json(f"normalized/{filename}")


def test_wechat_markdown_audit_stream_limit_is_observable(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    root = configured.wechat_source_root
    markdown = root / "large.md"
    markdown.write_text(("正文\n" * 12000) + "#### 文章列表\n", encoding="utf-8")
    snapshots = root / "normalized/snapshots.json"
    snapshots.write_text(json.dumps([{"snapshot_id": "snap_1", "keyword_id": "kw_1", "captured_at": "2026-07-14T00:00:00", "source_file_path": "large.md"}]), encoding="utf-8")
    registry = root / "normalized/snapshot_registry.json"
    registry.write_text(json.dumps({str(markdown): {"keyword_text": "港险"}}), encoding="utf-8")
    from content_hub.adapters.wechat import WechatAdapter
    _, _, audit = WechatAdapter(configured).import_records(limit=None)
    assert audit["scan_limited_count"] == 1
    assert audit["markdown_missing_article_list_count"] == 0


def test_wechat_hub_article_snapshots_are_snapshot_objects(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-article-snapshot")
    payload = service.article("art_1")
    assert payload["snapshots"]
    assert "hits" in payload["snapshots"][0]
    assert payload["snapshots"][0]["snapshot_id"] == "snap_1"
    assert payload["snapshots"][0]["hits"][0]["hit_id"] == "hit_1"


def test_wechat_keyword_prefers_hub_after_import_and_keeps_slices(settings, tmp_path, monkeypatch):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    from content_hub.adapters.wechat import WechatAdapter, WechatSourceError

    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-keyword-hub")
    monkeypatch.setattr(WechatAdapter, "detail_records", lambda self: (_ for _ in ()).throw(AssertionError("不应读取 normalized 详情")))
    monkeypatch.setattr(WechatAdapter, "remote_keyword", lambda self, keyword_id: (_ for _ in ()).throw(WechatSourceError("旧服务不可用")))
    payload = service.keyword("kw_1")
    assert payload["source_status"]["source"] == "hub_db"
    assert payload["snapshots"][0]["snapshot_id"] == "snap_1"
    assert payload["snapshots"][0]["hits"][0]["hit_id"] == "hit_1"
    assert payload["snapshots"][0]["articles"][0]["article_id"] == "art_1"
    assert payload["snapshots"][0]["features"]["suggestions"][0]["term"] == "港险"


def test_wechat_keyword_summary_preserves_remote_score_fields(settings, tmp_path, monkeypatch):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.adapters.wechat import WechatAdapter
    from content_hub.features.wechat.service import WechatService

    remote_detail = {
        "keyword_id": "kw_1",
        "keyword": "港险",
        "history_best": [1, 2],
        "history_hits": [3, 4],
        "turnover_runs": [{"run_id": "run_1"}],
        "kw_score": 91.5,
    }
    monkeypatch.setattr(
        WechatAdapter,
        "remote_bootstrap",
        lambda self: {"keywords": [remote_detail], "accounts": []},
    )
    monkeypatch.setattr(
        WechatAdapter,
        "remote_keyword",
        lambda self, keyword_id: {**remote_detail, "keyword_id": keyword_id},
    )
    service = WechatService(configured)
    summary = service.bootstrap()["keywords"][0]
    assert "history_best" not in summary
    assert "history_hits" not in summary
    assert "turnover_runs" not in summary
    assert "kw_score" not in summary
    payload = service.keyword("kw_1")
    assert payload["keyword"]["history_best"] == [1, 2]
    assert payload["keyword"]["history_hits"] == [3, 4]
    assert payload["keyword"]["turnover_runs"] == [{"run_id": "run_1"}]
    assert payload["keyword"]["kw_score"] == 91.5


def test_wechat_article_hub_read_does_not_promote_offline_connection(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-article-offline")
    with connect(configured) as con:
        con.execute(
            "UPDATE system_connections SET status='offline',last_checked_at='2026-07-14T00:00:00Z' WHERE system_key='wechat-search'"
        )
        con.commit()
    payload = service.article("art_1")
    assert payload["source_status"]["source"] == "hub_db"
    with connect(configured, readonly=True) as con:
        assert con.execute(
            "SELECT status FROM system_connections WHERE system_key='wechat-search'"
        ).fetchone()[0] == "offline"


def test_wechat_import_releases_source_json_cache(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.adapters.wechat import WechatAdapter
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-cache-clear")
    root_key = str(configured.wechat_source_root)
    assert not [key for key in WechatAdapter._json_cache if key[0] == root_key]


def test_wechat_placeholder_url_is_warning_but_invalid_url_is_rejected(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    articles_path = configured.wechat_source_root / "normalized/articles.json"
    articles_path.write_text(
        json.dumps([
            {
                "article_id": "art_1",
                "normalized_url": "placeholder://作者/标题",
                "title": "标题",
                "account_id": "acct_1",
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    placeholder_result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-placeholder")
    assert placeholder_result["audit"]["rejected"] == []
    assert placeholder_result["audit"]["placeholder_count"] == 1
    assert placeholder_result["audit"]["placeholder_samples"][0]["value"] == "placeholder://作者/标题"
    with connect(configured, readonly=True) as con:
        content = con.execute(
            "SELECT canonical_url,payload_json FROM contents WHERE content_id='art_1'"
        ).fetchone()
        assert content[0] is None
        assert json.loads(content[1])["normalized_url"] == "placeholder://作者/标题"
        batch = con.execute(
            "SELECT status,records_failed FROM ingestion_batches WHERE batch_id=?",
            (placeholder_result["batch_id"],),
        ).fetchone()
        assert tuple(batch) == ("succeeded", 0)
        assert con.execute(
            "SELECT status FROM system_connections WHERE system_key='wechat-search'"
        ).fetchone()[0] == "healthy"

    articles_path.write_text(
        json.dumps([
            {
                "article_id": "art_1",
                "normalized_url": "这不是合法 URL",
                "title": "标题",
                "account_id": "acct_1",
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-invalid-url")
    assert invalid_result["audit"]["placeholder_count"] == 0
    assert any(
        item["reason"] == "invalid_url"
        for item in invalid_result["audit"]["rejected"]
    )
    with connect(configured, readonly=True) as con:
        batch = con.execute(
            "SELECT status,records_failed FROM ingestion_batches WHERE batch_id=?",
            (invalid_result["batch_id"],),
        ).fetchone()
        assert batch[0] == "partial_failed"
        assert batch[1] >= 1
        assert con.execute(
            "SELECT status FROM system_connections WHERE system_key='wechat-search'"
        ).fetchone()[0] == "degraded"


def test_wechat_full_sync_archives_keyword_missing_from_current_source(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    from content_hub.features.wechat.service import WechatService
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-full-sync-first")
    monitor = configured.wechat_source_root / "normalized/monitor-data.json"
    payload = json.loads(monitor.read_text(encoding="utf-8"))
    payload["keywords"] = []
    monitor.write_text(json.dumps(payload), encoding="utf-8")
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-full-sync-second")
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT status FROM keywords WHERE keyword_id='kw_1'").fetchone()[0] == "archived"


def test_wechat_keyword_read_delta_uses_canonical_metric_keys(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    path = configured.wechat_source_root / "normalized/keyword_read_deltas.json"
    path.write_text(json.dumps([{
        "keyword_id": "kw_1", "keyword": "港险", "window_end": "2026-07-15T00:00:00",
        "status": "ok", "read_delta_estimated": 12, "confidence_score": 0.8,
        "daily_read_delta_points": [{"date": "2026-07-14", "read_delta": 3}],
    }]), encoding="utf-8")
    from content_hub.features.wechat.service import WechatService
    result = WechatService(configured).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="wechat-keyword-delta")
    with connect(configured, readonly=True) as con:
        rows = con.execute(
            "SELECT subject_type,metric_key,numeric_value FROM metric_observations WHERE subject_id='kw_1' ORDER BY metric_key"
        ).fetchall()
    assert {(row[0], row[1], row[2]) for row in rows} == {
        ("keyword", "wechat.keyword.confidence_score", 0.8),
        ("keyword", "wechat.keyword.daily_read_delta", 3.0),
        ("keyword", "wechat.keyword.read_delta_estimated", 12.0),
    }
    assert result["audit"]["metric_compatibility"]["legacy_keys_preserved"] is True


def test_wechat_markdown_path_is_confined_and_missing_is_404(settings, tmp_path):
    configured = _fixture_settings(settings, tmp_path)
    article_path = configured.wechat_source_root / "正文.md"
    article_path.write_text("# 正文", encoding="utf-8")
    from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
    adapter = WechatAdapter(configured)
    assert adapter.read_markdown("正文.md") == "# 正文"
    with pytest.raises(WechatSourceError) as escaped:
        adapter.read_markdown("../outside.md")
    assert escaped.value.kind == "path_not_allowed"
    article_path.unlink()
    with pytest.raises(WechatSourceError) as missing:
        adapter.read_markdown("正文.md")
    assert missing.value.status == 404
