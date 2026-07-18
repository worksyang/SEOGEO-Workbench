from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime
from dataclasses import replace
from pathlib import Path

import httpx

from content_hub.app import create_app
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.features.wechat.service import WechatService
from content_hub.repositories.wechat_legacy import (
    WechatLegacyRepository,
    _compact_bootstrap_payload,
)
from content_hub.services.wechat_refresh import FakeWechatRefreshProvider, WechatRefreshService


def _fixture(settings, tmp_path: Path):
    root = tmp_path / "freeze-any-id"
    normalized = root / "normalized"
    normalized.mkdir(parents=True)
    files = {
        "monitor-data.json": {"generated_at": "2026-07-16T00:00:00Z", "window_days": 15, "window_start": "2026-07-02", "window_end": "2026-07-16", "keyword_read_delta_meta": {"available": True}, "keywords": [{"keyword_id": "kw_1", "keyword": "港险", "today_best": 1, "today_count": 1, "runs": [{"id": "run1", "date": "2026-07-16", "time": "10:00", "articles": [{"article_id": "art_1", "title": "标题", "account": "作者", "rank": 1}]}], "kw_score": {"has_heat": True, "heat": 1, "richness": 0}}], "accounts": [{"account_id": "acct_1", "name": "作者", "score": 88, "history": [0, 1], "topics": {}, "keywords": {}}]},
        "accounts.json": [{"account_id": "acct_1", "canonical_name": "作者"}],
        "articles.json": [{"article_id": "art_1", "normalized_url": "https://mp.weixin.qq.com/s/1", "title": "标题", "account_id": "acct_1", "content_file_path": "article.md", "read_count": 100, "like_count": 8, "published_at": "2026-07-16T10:00:00Z", "first_seen_at": "2026-07-16T10:00:00Z"}],
        "snapshots.json": [{"snapshot_id": "snap_1", "keyword_id": "kw_1", "captured_at": "2026-07-16T00:00:00Z", "snapshot_date": "2026-07-16", "snapshot_time": "00:00", "result_count": 1}],
        "snapshot_registry.json": {},
        "snapshot_terms.json": [],
        "ranking_hits.json": [{"hit_id": "hit_1", "snapshot_id": "snap_1", "rank": 1, "article_id": "art_1", "title_raw": "标题"}],
        "article_metric_observations.json": [],
    }
    for name, value in files.items():
        (normalized / name).write_text(json.dumps(value), encoding="utf-8")
    (root / "article.md").write_text("# 正文\n", encoding="utf-8")
    return replace(settings, wechat_source_root=root, wechat_source_url="http://127.0.0.1:1")


def test_compact_bootstrap_keeps_list_metrics_and_drops_heavy_detail_fields():
    payload = {
        "generated_at": "2026-07-18T16:00:00",
        "keywords": [{
            "keyword_id": "kw_1",
            "keyword": "港险",
            "today_best": 1,
            "payload_json": "{\"raw\": true}",
            "runs": [{"id": "run_1", "articles": [{"title": "正文"}]}],
            "latest_run": {
                "id": "run_1",
                "date": "2026-07-18",
                "time": "16:00",
                "run_at": "2026-07-18 16:00",
                "articles": [{"title": "正文"}],
            },
        }],
        "accounts": [{
            "account_id": "acct_1",
            "name": "账号",
            "score": 88,
            "day_scores": [1, 2, 3],
            "history": [
                {"_day_idx": 0, "rank": 4},
                {"_day_idx": 0, "rank": 2},
                {"_day_idx": 2, "rank": 1},
            ],
            "topics": {"盛利2": {"label": "盛利2"}},
            "keywords": {"港险": {}},
        }],
    }

    compact = _compact_bootstrap_payload(payload)

    assert compact["generated_at"] == payload["generated_at"]
    assert compact["keywords"][0]["today_best"] == 1
    assert compact["keywords"][0]["latest_run"] == {
        "id": "run_1",
        "date": "2026-07-18",
        "time": "16:00",
        "run_at": "2026-07-18 16:00",
    }
    assert "payload_json" not in compact["keywords"][0]
    assert "runs" not in compact["keywords"][0]
    assert compact["accounts"][0]["history"] == [2, 0, 1]
    assert compact["accounts"][0]["topic_names"] == ["盛利2"]
    assert compact["accounts"][0]["keyword_names"] == ["港险"]
    assert "topics" not in compact["accounts"][0]
    assert "keywords" not in compact["accounts"][0]


def test_hub_legacy_read_shapes_etag_and_safe_content(settings, tmp_path):
    configured = _fixture(settings, tmp_path)
    service = WechatService(configured)
    service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="legacy-read-shapes")
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with transaction(con):
                for contract in ("bootstrap", "article-hit-detail", "article-content", "keyword"):
                    con.execute(
                            "INSERT INTO migration_switches(switch_id,module_key,contract_key,data_mode,updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(module_key,contract_key) DO UPDATE SET data_mode=excluded.data_mode,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                        (f"sw_{contract}", "wechat-search", contract, "hub", "2026-07-16T00:00:00Z", "test"),
                    )

    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/monitor-data/bootstrap", headers={"Accept-Encoding": "gzip"})
                assert response.status_code == 200
                assert response.headers["etag"]
                again = await client.get("/api/monitor-data/bootstrap", headers={"If-None-Match": response.headers["etag"]})
                assert again.status_code == 304
                article = await client.get("/api/article-hit-detail?article_id=art_1")
                assert article.status_code == 200
                assert article.json()["article"]["title"] == "标题"
                invalid = await client.get("/api/article-content?path=../secret")
                assert invalid.status_code == 400
                missing = await client.get("/api/monitor-data/keyword/missing")
                assert missing.status_code == 404

    asyncio.run(run())


def test_successful_refresh_overlays_frozen_projection_and_updates_article_library(settings, tmp_path):
    configured = _fixture(settings, tmp_path)
    WechatService(configured).import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="legacy-live-overlay",
    )
    provider = FakeWechatRefreshProvider(
        {
            "kw_1": {
                "captured_at": "2026-07-18T09:30:00",
                "result_count": 1,
                "hits": [
                    {
                        "rank": 1,
                        "title_raw": "7月18日新文章",
                        "url_raw": "https://mp.weixin.qq.com/s/live-overlay",
                        "creator_name_raw": "新公众号",
                        "published_at": "2026-07-18 08:00",
                        "markdown_body": "# 7月18日新文章\n\n正文",
                    }
                ],
                "source_ref": "remote:test:live-overlay",
            }
        }
    )
    result = WechatRefreshService(
        configured,
        provider=provider,
        max_attempts=1,
    ).refresh_one(
        keyword_id="kw_1",
        request_keyword="港险",
        key="legacy-live-overlay-refresh",
    )
    assert result["status"] == "completed"

    repo = WechatLegacyRepository(
        configured,
        clock=lambda: datetime(2026, 7, 18, 12),
    )
    bootstrap = repo.bootstrap()
    full = repo.full()
    keyword = repo.keyword("kw_1")
    assert bootstrap["generated_at"] == "2026-07-18T09:30:00"
    assert bootstrap["window_end"] == "2026-07-18"
    bootstrap_keyword = next(item for item in bootstrap["keywords"] if item["keyword_id"] == "kw_1")
    assert bootstrap_keyword["latest_run"]["run_at"] == "2026-07-18 09:30"
    full_keyword = next(item for item in full["keywords"] if item["keyword_id"] == "kw_1")
    assert full_keyword["runs"][0]["articles"][0]["title"] == "7月18日新文章"
    assert keyword["runs"][0]["id"] == full_keyword["runs"][0]["id"]

    articles = repo.articles(
        sort="todayReads",
        time_range=15,
        as_of=datetime(2026, 7, 18, 12),
    )
    new_article = next(item for item in articles["articles"] if item["title"] == "7月18日新文章")
    assert new_article["published_at"] == "2026-07-18 08:00"
    assert new_article["account_name"] == "新公众号"
    assert new_article["content_file_path"].startswith("wechat/")

    # 兼容 7 月 16–18 日已经落盘、但 relative_path 尚未回填的真实记录。
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with transaction(con):
                con.execute(
                    "UPDATE wechat_article_paths SET relative_path=NULL WHERE asset_path=?",
                    (new_article["content_file_path"],),
                )
                con.execute(
                    """INSERT INTO migration_switches(
                           switch_id,module_key,contract_key,data_mode,updated_at,updated_by
                       ) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(module_key,contract_key) DO UPDATE SET
                           data_mode=excluded.data_mode,
                           updated_at=excluded.updated_at,
                           updated_by=excluded.updated_by""",
                    (
                        "sw_live_article_content",
                        "wechat-search",
                        "article-content",
                        "hub",
                        "2026-07-18T12:00:00Z",
                        "test",
                    ),
                )
    record = repo.article_content(new_article["content_file_path"])
    assert record["relative_path"] == new_article["content_file_path"]
    assert repo.asset_content(record).startswith("# 7月18日新文章")

    async def read_live_article():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/article-content",
                    params={"path": new_article["content_file_path"]},
                )
                assert response.status_code == 200
                assert response.json()["markdown"].startswith("# 7月18日新文章")

    asyncio.run(read_live_article())


def test_golden_legacy_read_routes_and_article_contract(settings, tmp_path):
    configured = _fixture(settings, tmp_path)
    WechatService(configured).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="legacy-read-golden")
    contracts = (
        "monitor-data", "bootstrap", "keyword", "account", "article-content",
        "article-hit-detail", "keyword-manage", "keyword-discovery",
        "refresh-status", "refresh-all-status", "refresh-all-history",
        "scheduler-status", "articles", "articles-accounts",
    )
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with transaction(con):
                for index, contract in enumerate(contracts):
                    con.execute(
                            "INSERT INTO migration_switches(switch_id,module_key,contract_key,data_mode,updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(module_key,contract_key) DO UPDATE SET data_mode=excluded.data_mode,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                        (f"gold_{index}", "wechat-search", contract, "hub", "2026-07-16T00:00:00Z", "test"),
                    )

    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                full = await client.get("/api/monitor-data")
                bootstrap = await client.get("/api/monitor-data/bootstrap")
                assert full.status_code == bootstrap.status_code == 200
                assert "runs" in full.json()["keywords"][0]
                assert "runs" not in bootstrap.json()["keywords"][0]
                assert full.json()["keyword_bucket_options"]
                keyword = await client.get("/api/monitor-data/keyword/kw_1")
                account = await client.get("/api/monitor-data/account/acct_1")
                assert keyword.json()["keyword_id"] == "kw_1"
                assert account.json()["account_id"] == "acct_1"
                detail = await client.get("/api/article-hit-detail?article_id=art_1")
                assert {"article", "account", "url_profile", "keyword_groups", "keyword_cloud", "metric_points", "timeline_events"}.issubset(detail.json())
                content = await client.get("/api/article-content?path=article.md")
                assert content.json() == {"path": "article.md", "markdown": "# 正文\n"}
                manage = await client.get("/api/keyword-manage")
                assert {"groups", "total", "ranked_total", "not_ranked_total", "updated_at"} <= set(manage.json())
                discovery = await client.get("/api/keyword-discovery?probe_status=proposed&probe_status=searched&candidate_status=discovered&limit=1")
                assert set(discovery.json()) == {"summary", "candidates", "probes"}
                articles = await client.get("/api/articles?page=1&page_size=50&sort=reads&time_range=0&min_hits=0")
                assert articles.json()["articles"][0]["article_id"] == "art_1"
                accounts = await client.get("/api/articles/accounts")
                assert list(accounts.json()) == ["accounts"]
                assert (await client.get("/api/article-content")).status_code == 404
                assert (await client.get("/api/article-content?path=article.txt")).status_code == 400
                clamped = await client.get("/api/articles?page=0&page_size=9999&sort=unknown&min_hits=-3&time_range=-1")
                assert clamped.status_code == 200
                assert clamped.json()["page"] == 1
                assert clamped.json()["page_size"] == 50

    asyncio.run(run())


def test_same_freeze_content_in_different_root_is_idempotent(settings, tmp_path):
    first_root = _fixture(settings, tmp_path).wechat_source_root
    second_root = tmp_path / "another-freeze-id"
    shutil.copytree(first_root, second_root)
    first = replace(settings, wechat_source_root=first_root, wechat_source_url="http://127.0.0.1:1")
    second = replace(settings, wechat_source_root=second_root, wechat_source_url="http://127.0.0.1:1")
    from content_hub.adapters.wechat import WechatAdapter
    first_adapter, second_adapter = WechatAdapter(first), WechatAdapter(second)
    _, first_manifest, _ = first_adapter.import_records()
    _, second_manifest, _ = second_adapter.import_records()
    assert first_adapter.manifest_id(first_manifest) == second_adapter.manifest_id(second_manifest)
    first_batch = WechatService(first).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="legacy-read-first-db")
    second_batch = WechatService(second).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="legacy-read-second-db")
    assert first_batch["batch_id"] == second_batch["batch_id"]


def test_discovery_repeated_statuses_platform_isolation_and_fixed_clock(settings, tmp_path):
    configured = _fixture(settings, tmp_path)
    WechatService(configured).import_history(dry_run=False, limit=None, confirm=True, idempotency_key="legacy-read-isolation")
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with transaction(con):
                con.execute(
                    "INSERT INTO wechat_discovery_probes(probe_id,probe_text,status,payload_json,updated_at) VALUES(?,?,?,?,?)",
                    ("p1", "候选词", "proposed", '{"evidence":"p1"}', "2026-07-16T01:00:00Z"),
                )
                con.execute(
                    "INSERT INTO wechat_discovery_probes(probe_id,probe_text,status,payload_json,updated_at) VALUES(?,?,?,?,?)",
                    ("p2", "候选词2", "searched", '{"evidence":"p2"}', "2026-07-16T02:00:00Z"),
                )
                con.execute(
                    "INSERT INTO wechat_discovery_candidates(candidate_id,candidate_text,status,payload_json,updated_at) VALUES(?,?,?,?,?)",
                    ("c1", "候选词", "discovered", '{"candidate":"c1"}', "2026-07-16T01:00:00Z"),
                )
                con.execute(
                    "INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                    ("xhs_acct", "XHS", "xhs", "xhs_acct", "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z", "{}"),
                )
                con.execute(
                    "INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    ("xhs_article", "external_article", "XHS only", "https://xhs.example/a", "xhs_acct", "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z", '{"read_count":999}'),
                )
                con.execute(
                    "INSERT INTO search_snapshots(snapshot_id,platform,keyword,keyword_id,captured_at,result_count) VALUES(?,?,?,?,?,?)",
                    ("xhs_snap", "xhs", "港险", "kw_1", "2026-07-16T03:00:00Z", 1),
                )
                con.execute(
                    "INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,title_raw) VALUES(?,?,?,?,?)",
                    ("xhs_hit", "xhs_snap", 1, "xhs_article", "XHS only"),
                )
                for contract in ("keyword-discovery", "articles", "articles-accounts"):
                    con.execute(
                            "INSERT INTO migration_switches(switch_id,module_key,contract_key,data_mode,updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(module_key,contract_key) DO UPDATE SET data_mode=excluded.data_mode,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                        (f"iso_{contract}", "wechat-search", contract, "hub", "2026-07-16T00:00:00Z", "test"),
                    )
    repo = WechatLegacyRepository(configured, clock=lambda: datetime(2026, 7, 16, 12))
    assert repo.discovery(
        ["proposed", "searched"], 500, ["discovered"]
    )["summary"] == {
        "probes": {"proposed": 1, "searched": 1},
        "candidates": {"discovered": 1},
    }
    articles = repo.articles(time_range=0, as_of=datetime(2026, 7, 16, 12))
    assert [x["article_id"] for x in articles["articles"]] == ["art_1"]
    assert articles["articles"][0]["on_rank_days"] == 1
    assert [x["account_id"] for x in repo.article_accounts()["accounts"]] == ["acct_1"]
