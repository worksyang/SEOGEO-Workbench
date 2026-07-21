from __future__ import annotations

import json
from datetime import datetime

import pytest

from content_hub.db.connection import connect
from content_hub.services.wechat_agent_projection import (
    AgentProjectionValidation,
    apply_decision,
    build_projection,
    import_claim_ledger,
    publish_projection,
    validate_staging,
)
from content_hub.services.wechat_aux import WechatAuxService


def _seed(settings) -> dict:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO keywords(
                 keyword_id,platform,keyword,status,topic,keyword_bucket,first_seen_at,updated_at,payload_json
               ) VALUES('kw_1','wechat-search','保费融资','active','保费融资','产品','2026-07-01T00:00:00Z','2026-07-21T08:00:00Z','{}')"""
        )
        con.execute(
            """INSERT INTO creators(
                 creator_id,canonical_name,platform,first_seen_at,updated_at,payload_json
               ) VALUES('acct_1','港险观察','wechat-search','2026-07-01T00:00:00Z','2026-07-21T08:00:00Z','{}')"""
        )
        for index, title in enumerate(("保费融资新变化", "银行收紧后的保单融资"), start=1):
            content_id = f"article_{index}"
            con.execute(
                """INSERT INTO contents(
                     content_id,content_type,title,canonical_url,creator_id,author_name,published_at,
                     first_seen_at,updated_at,entities_json,intents_json,payload_json
                   ) VALUES(?,?,?,?,?,?,?,'2026-07-21T08:00:00Z','2026-07-21T08:00:00Z','[]','[]',?)""",
                (
                    content_id,
                    "wechat_article",
                    title,
                    f"https://mp.weixin.qq.com/s/{index}",
                    "acct_1",
                    "港险观察",
                    f"2026-07-21 0{index}:00",
                    json.dumps({"summary_raw": f"{title}摘要", "read_count": index * 100}),
                ),
            )
        for offset, captured in enumerate(("2026-07-21T07:00:00Z", "2026-07-21T08:00:00Z"), start=1):
            snapshot_id = f"snap_{offset}"
            con.execute(
                """INSERT INTO search_snapshots(
                     snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,
                     features_json,source_ref,payload_json
                   ) VALUES(?,'wechat-search','保费融资','kw_1',?,'scheduled',2,?,'test://snapshot','{}')""",
                (
                    snapshot_id,
                    captured,
                    json.dumps(
                        {
                            "related": [{"term": "保单融资利率", "position": 1}],
                            "suggestions": [{"term": "保费融资风险", "position": 1}],
                        }
                    ),
                ),
            )
            for rank in (1, 2):
                con.execute(
                    """INSERT INTO search_hits(
                         hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json
                       ) VALUES(?,?,?,?,?,?,?,'{}')""",
                    (
                        f"hit_{offset}_{rank}",
                        snapshot_id,
                        rank,
                        f"article_{rank}",
                        f"文章{rank}",
                        f"https://mp.weixin.qq.com/s/{rank}",
                        "港险观察",
                    ),
                )
        con.commit()

    return {
        "generated_at": "2026-07-21T08:00:00Z",
        "account_score_method": "three_board_breakthrough_v5_1",
        "keywords": [
            {
                "keyword_id": "kw_1",
                "keyword": "保费融资",
                "topic": "保费融资",
                "keyword_bucket": "产品",
                "coverage_days": 15,
                "history_hits": [2] * 15,
                "keyword_read_delta": {
                    "keyword_id": "kw_1",
                    "status": "ok",
                    "method": "canonical_metric_observations",
                    "trend_signal": 0.8,
                    "trend_label": "升温",
                    "term_momentum": 0.5,
                    "new_term_count": 2,
                    "rising_term_count": 1,
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


def test_projection_builds_from_hub_and_publishes_atomically(settings):
    monitor = _seed(settings)
    staging = build_projection(
        settings,
        now=datetime(2026, 7, 21, 9, 0, 0),
        monitor_payload=monitor,
    )
    validation = validate_staging(staging)
    assert validation["valid"] is True
    assert staging["brief"]["data_status"]["as_of"] == "2026-07-21T08:00:00"
    assert staging["brief"]["summary"]["recent_article_count"] == 2
    assert staging["brief"]["event_candidates"]
    assert all(
        evidence_id in staging["evidence"]
        for candidate in staging["brief"]["event_candidates"]
        for evidence_id in candidate["evidence_ids"]
    )

    published = publish_projection(settings, staging)
    assert published["status"] == "published"
    assert published["replayed"] is False
    replayed = publish_projection(settings, staging)
    assert replayed["replayed"] is True

    service = WechatAuxService(settings)
    assert service.artifact("manifest")["brief"]["brief_id"] == staging["brief"]["brief_id"]
    assert service.artifact("daily_brief")["brief_id"] == staging["brief"]["brief_id"]
    first_evidence = next(iter(staging["evidence"]))
    assert service.artifact("evidence", first_evidence)["evidence_id"] == first_evidence
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_projection_runs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM wechat_aux_artifacts").fetchone()[0] == published["artifact_count"]


def test_invalid_staging_cannot_replace_last_known_good(settings):
    staging = build_projection(
        settings,
        now=datetime(2026, 7, 21, 9, 0, 0),
        monitor_payload=_seed(settings),
    )
    publish_projection(settings, staging)
    original = WechatAuxService(settings).artifact("daily_brief")["brief_id"]
    broken = dict(staging)
    broken["brief"] = {**staging["brief"], "event_candidates": [{"candidate_id": "bad", "evidence_ids": ["missing"]}]}
    with pytest.raises(AgentProjectionValidation):
        publish_projection(settings, broken)
    assert WechatAuxService(settings).artifact("daily_brief")["brief_id"] == original


def test_decision_is_idempotent_and_advances_claims(settings):
    staging = build_projection(
        settings,
        now=datetime(2026, 7, 21, 9, 0, 0),
        monitor_payload=_seed(settings),
    )
    published = publish_projection(settings, staging)
    candidate = staging["brief"]["event_candidates"][0]
    decision = {
        "run_id": published["run_id"],
        "brief_id": published["brief_id"],
        "reported_claim_ids": [candidate["candidate_id"]],
        "report_path": "history/260721.md",
    }
    first = apply_decision(settings, decision, idempotency_key="brief-260721")
    second = apply_decision(settings, decision, idempotency_key="brief-260721")
    assert first["replayed"] is False
    assert second["replayed"] is True
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_claims").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_decisions").fetchone()[0] == 1


def test_unchanged_evidence_is_reused_across_projection_versions(settings, monkeypatch):
    monitor = _seed(settings)
    first_staging = build_projection(settings, monitor_payload=monitor)
    publish_projection(settings, first_staging)
    import content_hub.services.wechat_agent_projection as module

    monkeypatch.setattr(module, "PROJECTION_VERSION", "agent_observation_protocol_test_next")
    second_staging = build_projection(settings, monitor_payload=monitor)
    publish_projection(settings, second_staging)
    with connect(settings, readonly=True) as con:
        # manifest/brief 各有两个版本；相同内容的 evidence 只保留不可变内容行。
        evidence_count = con.execute(
            "SELECT COUNT(*) FROM wechat_aux_artifacts WHERE artifact_kind='evidence'"
        ).fetchone()[0]
        assert evidence_count == len(first_staging["evidence"])
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_projection_runs").fetchone()[0] == 2


def test_decision_accepts_agent_field_aliases_and_rejects_empty_publish(settings):
    staging = build_projection(settings, monitor_payload=_seed(settings))
    published = publish_projection(settings, staging)
    candidate = staging["brief"]["event_candidates"][0]
    result = apply_decision(
        settings,
        {
            "decision": "publish",
            "run_id": published["run_id"],
            "brief_id": published["brief_id"],
            "claim_ids": [candidate["candidate_id"]],
            "formal_body_path": "历史记录/revision.md",
        },
        idempotency_key="alias-fields",
    )
    assert result["reported_count"] == 1
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT report_path FROM wechat_agent_claims WHERE claim_id=?",
            (candidate["candidate_id"],),
        ).fetchone()
        assert row["report_path"] == "历史记录/revision.md"
    with pytest.raises(AgentProjectionValidation, match="requires claim ids"):
        apply_decision(
            settings,
            {
                "decision": "publish",
                "run_id": published["run_id"],
                "brief_id": published["brief_id"],
                "claim_ids": [],
            },
            idempotency_key="empty-publish",
        )


def test_validation_distinguishes_total_and_embedded_articles(settings):
    staging = build_projection(settings, monitor_payload=_seed(settings))
    validation = staging["validation"]
    assert validation["recent_article_total"] == staging["brief"]["summary"]["recent_article_count"]
    assert validation["recent_article_in_brief"] == len(staging["brief"]["recent_articles"])


def test_claim_ledger_import_is_idempotent(settings):
    payload = {
        "items": {
            "claim_legacy": {
                "kind": "keyword_demand_signal",
                "subject": "kw_1",
                "first_reported_at": "2026-07-14T08:00:00",
                "last_reported_at": "2026-07-17T08:00:00",
                "last_direction": "上升",
                "last_fingerprint": "abc",
                "last_priority": 80,
                "last_state": "updated",
                "report_path": "历史记录/260717.md",
                "evidence_ids": ["keyword_kw_1"],
            }
        }
    }
    assert import_claim_ledger(settings, payload)["imported"] == 1
    assert import_claim_ledger(settings, payload)["imported"] == 1
    with connect(settings, readonly=True) as con:
        row = con.execute("SELECT * FROM wechat_agent_claims WHERE claim_id='claim_legacy'").fetchone()
        assert row["last_reported_at"] == "2026-07-17T08:00:00"
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_claims").fetchone()[0] == 1


def test_blocked_mode_is_preserved(settings):
    monitor = _seed(settings)
    staging = build_projection(
        settings,
        now=datetime(2026, 7, 24, 9, 0, 0),
        monitor_payload=monitor,
    )
    assert staging["brief"]["data_status"]["mode"] == "blocked"
    assert staging["brief"]["data_status"]["strong_conclusion_allowed"] is False
