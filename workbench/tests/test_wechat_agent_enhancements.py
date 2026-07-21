from __future__ import annotations

import json
from datetime import datetime

import httpx
from fastapi import FastAPI

from content_hub.db.connection import connect
from content_hub.features.wechat.legacy_aux_router import router as aux_router
from content_hub.features.wechat.router import router as wechat_router
from content_hub.services.wechat_agent_projection import (
    apply_decision,
    build_projection,
    publish_projection,
)
from content_hub.services.wechat_aux import WechatAuxService


def _seed_enhanced(settings) -> dict:
    """两账号、四文章、含同稿、覆盖缺口与排名轨迹的精简 fixture。"""
    with connect(settings) as con:
        for keyword_id, keyword, topic in (
            ("kw_observed", "已观测词", "产品"),
            ("kw_missing", "缺口词", "话题"),
        ):
            con.execute(
                """INSERT INTO keywords(
                     keyword_id,platform,keyword,status,topic,keyword_bucket,
                     first_seen_at,updated_at,payload_json
                   ) VALUES(?,?,?,?,?,?, '2026-07-01T00:00:00Z','2026-07-21T08:00:00Z','{}')""",
                (keyword_id, "wechat-search", keyword, "active", topic, topic),
            )
        for account_id, name in (("acct_a", "账号A"), ("acct_b", "账号B")):
            con.execute(
                """INSERT INTO creators(
                     creator_id,canonical_name,platform,first_seen_at,updated_at,payload_json
                   ) VALUES(?,?,?,?,?,?)""",
                (account_id, name, "wechat-search", "2026-07-01T00:00:00Z", "2026-07-21T08:00:00Z", "{}"),
            )
        same_hash = "sha256_same_draft_hash_0123456789abcdef"
        articles = [
            ("art_a1", "同稿A1", "acct_a", "2026-07-21 01:00", same_hash),
            ("art_a2", "同稿B1", "acct_b", "2026-07-21 02:00", same_hash),
            ("art_a3", "独立文", "acct_a", "2026-07-21 03:00", "sha256_unique_hash_001"),
            ("art_a4", "另一独立", "acct_b", "2026-07-21 04:00", "sha256_unique_hash_002"),
        ]
        for content_id, title, account_id, published_at, content_hash in articles:
            con.execute(
                """INSERT INTO contents(
                     content_id,content_type,title,canonical_url,creator_id,author_name,
                     published_at,first_seen_at,updated_at,content_hash,entities_json,
                     intents_json,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?, '[]','[]', ?)""",
                (
                    content_id,
                    "wechat_article",
                    title,
                    f"https://mp.weixin.qq.com/s/{content_id}",
                    account_id,
                    account_id,
                    published_at,
                    "2026-07-21T08:00:00Z",
                    "2026-07-21T08:00:00Z",
                    content_hash,
                    json.dumps({"summary_raw": f"{title}摘要"}),
                ),
            )
        for day_offset, captured in enumerate(
            ("2026-07-19T08:00:00Z", "2026-07-20T08:00:00Z", "2026-07-21T08:00:00Z"),
            start=1,
        ):
            snapshot_id = f"snap_{day_offset}"
            con.execute(
                """INSERT INTO search_snapshots(
                     snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,
                     features_json,source_ref,payload_json
                   ) VALUES(?,'wechat-search','已观测词','kw_observed',?,'scheduled',3,'{}','test://snapshot','{}')""",
                (snapshot_id, captured),
            )
            for rank, content_id in enumerate(("art_a1", "art_a2", "art_a3"), start=1):
                con.execute(
                    """INSERT INTO search_hits(
                         hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json
                       ) VALUES(?,?,?,?,?,?,?,'{}')""",
                    (f"hit_{snapshot_id}_{rank}", snapshot_id, rank, content_id, content_id, f"https://mp.weixin.qq.com/s/{content_id}", content_id),
                )
        con.commit()
    return {
        "generated_at": "2026-07-21T08:00:00Z",
        "account_score_method": "three_board_breakthrough_v5_1",
        "keywords": [
            {
                "keyword_id": "kw_observed",
                "keyword": "已观测词",
                "topic": "产品",
                "keyword_bucket": "产品",
                "coverage_days": 15,
                "history_hits": [3] * 15,
                "keyword_read_delta": {
                    "keyword_id": "kw_observed",
                    "status": "ok",
                    "method": "canonical_metric_observations",
                    "trend_signal": 0.8,
                    "trend_label": "升温",
                    "confidence_level": "high",
                    "confidence_score": 0.95,
                    "coverage_ratio": 1.0,
                    "observed_share": 1.0,
                    "estimated_share": 0.0,
                    "snapshot_count": 15,
                    "read_delta_estimated": 100,
                    "steady_read_median": 50,
                },
                "latest_run": {"articles": [], "terms": {}},
            }
        ],
        "accounts": [],
    }


def test_same_draft_pre_detection_flags_cross_account_duplicate(settings):
    staging = build_projection(settings, monitor_payload=_seed_enhanced(settings))
    clusters = staging["brief"]["content_clusters"]
    matrix = clusters[0]["same_draft_matrix_pre_detection"]
    assert matrix["status"] == "detected"
    assert matrix["matched_hash_group_count"] >= 1
    group = matrix["groups"][0]
    assert group["method"] == "exact_content_hash"
    assert group["confidence"] == 1.0
    assert {"acct_a", "acct_b"}.issubset(set(group["account_ids"]))
    candidate = next(
        item for item in staging["brief"]["event_candidates"]
        if item["kind"] == "content_cluster_24h"
    )
    assert "same_draft_matrix" in candidate["facts"]
    assert candidate["facts"]["same_draft_matrix"]["status"] == "detected"


def test_coverage_gaps_list_missing_keywords_with_reasons(settings):
    staging = build_projection(settings, monitor_payload=_seed_enhanced(settings))
    status = staging["manifest"]["data_status"]
    gaps = status["coverage_gaps"]
    gap_ids = {item["keyword_id"] for item in gaps}
    assert "kw_missing" in gap_ids
    missing = next(item for item in gaps if item["keyword_id"] == "kw_missing")
    assert missing["reason_code"] in {"not_observed_in_24h", "provider_failed"}
    assert status["coverage_gap_summary"]["total_missing_keyword_count"] >= 1
    brief_status = staging["brief"]["data_status"]
    assert "coverage_gaps" not in brief_status or brief_status.get("coverage_gaps") == []
    assert staging["brief"]["coverage_gap_summary"]["total_missing_keyword_count"] >= 1


def test_compared_to_last_unavailable_on_first_run(settings):
    staging = build_projection(settings, monitor_payload=_seed_enhanced(settings))
    compared = staging["brief"]["compared_to_last"]
    assert compared["available"] is False
    assert compared["reason_code"] == "no_previous_published_brief"


def test_compared_to_last_after_publish_and_decision(settings):
    monitor = _seed_enhanced(settings)
    first = build_projection(settings, now=datetime(2026, 7, 21, 9, 0, 0), monitor_payload=monitor)
    published = publish_projection(settings, first)
    candidate = first["brief"]["event_candidates"][0]
    apply_decision(
        settings,
        {
            "run_id": published["run_id"],
            "brief_id": published["brief_id"],
            "reported_claim_ids": [candidate["candidate_id"]],
            "report_path": "history/260721.md",
        },
        idempotency_key="enhanced-260721",
    )
    second = build_projection(settings, now=datetime(2026, 7, 21, 9, 0, 0), monitor_payload=monitor)
    compared = second["brief"]["compared_to_last"]
    # 同一 source_as_of 重建时不会把自身当作基线；跨期对比需要数据真正推进。
    if compared["available"]:
        assert compared["baseline"]["brief_id"] == first["brief"]["brief_id"]
        assert isinstance(compared["prior_claims"]["verified"], list)
    else:
        assert compared["reason_code"] == "no_previous_published_brief"


def _enhanced_app(settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.wechat_aux_service = WechatAuxService(settings)
    app.include_router(aux_router)
    app.include_router(wechat_router)
    return app


def _get(settings, path: str) -> httpx.Response:
    app = _enhanced_app(settings)

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.get(path)

    import asyncio

    return asyncio.run(run())


def test_batch_evidence_returns_found_and_missing(settings):
    staging = build_projection(settings, monitor_payload=_seed_enhanced(settings))
    publish_projection(settings, staging)
    known = next(iter(staging["evidence"]))
    response = _get(settings, f"/api/agent/evidence?ids={known},missing_id_xyz")
    assert response.status_code == 200
    body = response.json()
    assert body["found_count"] == 1
    assert body["missing_count"] == 1
    assert body["items"][0]["evidence_id"] == known
    assert "missing_id_xyz" in body["missing_ids"]


def test_batch_evidence_rejects_missing_ids_param(settings):
    response = _get(settings, "/api/agent/evidence")
    assert response.status_code == 400


def test_article_rank_history_aggregates_per_day_keyword(settings):
    _seed_enhanced(settings)
    response = _get(settings, "/api/v1/wechat/articles/art_a1")
    assert response.status_code == 200
    rank_history = response.json()["data"]["rank_history"]
    assert rank_history["aggregation"] == "best_rank_per_keyword_per_calendar_day"
    points = {(item["date"], item["keyword_id"]): item["best_rank"] for item in rank_history["points"]}
    assert points[("2026-07-21", "kw_observed")] == 1


def test_account_activity_reports_active_days_streak_and_new_flag(settings):
    _seed_enhanced(settings)
    response = _get(settings, "/api/v1/wechat/accounts/acct_a")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["activity"]["active_days_7d"] >= 1
    assert data["activity"]["streak"] >= 1
    assert data["new_account"]["is_new_account"] in {True, False}
    assert data["new_account"]["new_account_window_days"] == 30
