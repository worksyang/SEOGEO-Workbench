from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.ingest.common import kw_id
from app.repositories.keyword_discovery_repo import KeywordDiscoveryRepository
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.services.keyword_commercial_value_service import (
    apply_commercial_value_scores,
    score_keyword,
)
from app.services.keyword_discovery_service import (
    _activate_candidate,
    apply_keyword_lifecycle,
    evaluate_auto_archive,
    evaluate_candidate_validation,
    get_discovery_budget_status,
    ingest_probe_files,
    is_eligible_candidate_term,
    launch_next_discovery_batch,
    reconcile_candidate_validations,
    reconcile_probe_searches,
)


@pytest.fixture()
def workspace():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "data/config").mkdir(parents=True)
        (root / "data/state").mkdir(parents=True)
        (root / "data/runs").mkdir(parents=True)
        (root / "normalized").mkdir(parents=True)
        source_config = (
            Path(__file__).resolve().parent.parent
            / "data/config/keyword_refresh_policy.json"
        )
        (root / "data/config/keyword_refresh_policy.json").write_text(
            source_config.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for name in (
            "snapshots.json",
            "snapshot_terms.json",
            "ranking_hits.json",
            "keyword_read_deltas.json",
        ):
            (root / "normalized" / name).write_text("[]", encoding="utf-8")
        db = root / "data/state/app.db"
        keywords = KeywordRegistryRepository(db)
        discovery = KeywordDiscoveryRepository(db)
        group_id = keywords.create_group("测试组")["group_id"]
        yield root, keywords, discovery, group_id


def _probe(discovery: KeywordDiscoveryRepository, text: str = "港险监管新规"):
    return discovery.upsert_probe(
        brief_id="brief_001",
        source_article_id="article_001",
        source_title="监管文章",
        probe_text=text,
        probe_type="事件",
        source_quote="文章原文证据",
        warming_facts=["近三日出现"],
        proposed_at="2026-07-12T08:00:00",
    )


def _candidate(
    discovery: KeywordDiscoveryRepository,
    *,
    term_type: str = "related",
    text: str = "香港保险返佣入刑",
):
    probe = _probe(discovery)
    return discovery.upsert_candidate_evidence(
        candidate_text=text,
        probe_id=probe["probe_id"],
        snapshot_id="snapshot_001",
        evidence_date="2026-07-12",
        term_type=term_type,
        position=2,
        source_article_id="article_001",
        observed_at="2026-07-12T09:00:00",
        business_value_score=3,
        business_value_reason="测试",
    )


def _archive_policy():
    return {
        "auto_archive": {
            "enabled": True,
            "minimum_observation_days": 15,
            "maximum_commercial_value_score": 3,
            "required_refresh_frequency_days": 15,
            "maximum_steady_read_median": 10,
        }
    }


def _archive_keyword(**overrides):
    item = {
        "status": "active",
        "auto_archive_locked": False,
        "is_pinned": False,
        "refresh_frequency_source": "auto",
        "last_refresh_status": "success",
        "last_refresh_at": "2026-07-12T10:00:00",
        "snapshot_count": 20,
        "refresh_frequency_days": 15,
        "commercial_value_score": 3,
    }
    item.update(overrides)
    return item


def test_commercial_score_high_value_is_10():
    assert score_keyword("IUL保费融资投保门槛")["commercial_value_score"] == 10


def test_commercial_score_navigation_is_1():
    assert score_keyword("香港友邦保险公众号")["commercial_value_score"] == 1


def test_commercial_score_expired_month_is_1():
    assert score_keyword("港险6月优惠")["commercial_value_score"] == 1


@pytest.mark.parametrize(
    ("text", "expected_score"),
    [
        ("IUL计划书", 10),
        ("香港家族信托哪家好", 9),
        ("友邦财富盈活门槛", 8),
        ("香港保险投保", 7),
        ("香港保险养老金", 6),
        ("分红实现率怎么看", 5),
        ("香港重疾险", 4),
        ("香港保险靠谱吗", 3),
        ("AIA环宇盈活是什么产品", 2),
        ("港险6月优惠", 1),
    ],
)
def test_all_ten_commercial_value_levels_match_document(text, expected_score):
    assert score_keyword(text)["commercial_value_score"] == expected_score


def test_education_term_is_downgraded_without_specific_entity():
    result = score_keyword("香港保险靠谱吗")
    assert result["transaction_proximity"] == 1
    assert result["commercial_value_score"] <= 3


def test_manual_commercial_score_is_not_overwritten(workspace):
    _, keywords, _, group_id = workspace
    item = keywords.create_keyword(group_id, "香港保险养老金")
    keywords.set_commercial_value(
        item["keyword_id"],
        score=9,
        reason="人工判断",
        source="manual",
    )
    apply_commercial_value_scores(repository=keywords)
    refreshed = keywords.get(item["keyword_id"])
    assert refreshed["commercial_value_score"] == 9
    assert refreshed["commercial_value_source"] == "manual"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("香港友邦保险公众号", "navigation_or_noise"),
        ("国寿海外app", "navigation_or_noise"),
        ("国寿海外总资产", "navigation_or_noise"),
        ("澳门国寿保费融资", "outside_target_market"),
        ("港险6月优惠", "expired_time_term"),
        ("星河战队2联邦英雄", "navigation_or_noise"),
        ("今天吃什么", "outside_domain"),
        ("港险", ""),
    ],
)
def test_candidate_term_filters_are_transparent(text, reason):
    eligible, actual_reason = is_eligible_candidate_term(text)
    assert eligible is (reason == "")
    assert actual_reason == reason


def test_suggestion_only_is_saved_but_cannot_enter_validation(workspace):
    root, keywords, discovery, _ = workspace
    candidate = _candidate(discovery, term_type="suggestion")
    probe = discovery.list_probes(limit=1)[0]
    discovery.mark_probe_searched(probe["probe_id"], searched_at="2026-07-12T09:00:00")
    assert candidate["suggestion_occurrence_count"] == 1
    assert candidate["related_occurrence_count"] == 0
    assert candidate["status"] == "suggestion_only"
    result = launch_next_discovery_batch(
        project_root=root,
        discovery_repository=discovery,
        keyword_repository=keywords,
        now=datetime(2026, 7, 12, 10, 0, 0),
    )
    assert result["launched"] is False
    assert result["reason"] == "no_pending_discovery_terms"


def test_related_term_can_be_queued_for_validation(workspace):
    root, keywords, discovery, _ = workspace
    candidate = _candidate(discovery, term_type="related")
    probe = discovery.list_probes(limit=1)[0]
    discovery.mark_probe_searched(probe["probe_id"], searched_at="2026-07-12T09:00:00")
    with patch(
        "app.services.refresh_service.start_batch_refresh",
        return_value={"batch_id": "discovery_001"},
    ):
        result = launch_next_discovery_batch(
            project_root=root,
            discovery_repository=discovery,
            keyword_repository=keywords,
            now=datetime(2026, 7, 12, 10, 0, 0),
        )
    assert result["launched"] is True
    assert discovery.get_candidate(candidate["candidate_id"])["status"] == "validation_queued"


@pytest.mark.parametrize(
    ("rank", "parents", "dates", "results", "accepted"),
    [
        (5, 1, 1, 10, True),
        (10, 1, 1, 5, True),
        (11, 2, 2, 10, True),
        (None, 1, 1, 10, False),
        (5, 1, 1, 0, False),
    ],
)
def test_candidate_reverse_validation_scenarios(rank, parents, dates, results, accepted):
    candidate = {
        "related_occurrence_count": 1,
        "related_parent_probe_count": parents,
        "related_date_count": dates,
        "historical_related_parent_count": parents,
        "historical_related_date_count": dates,
        "best_related_position": 2,
    }
    decision = evaluate_candidate_validation(
        candidate,
        source_article_best_rank=rank,
        validation_result_count=results,
    )
    assert decision["accepted"] is accepted


def test_candidate_activation_starts_15_day_daily_observation(workspace):
    _, keywords, discovery, _ = workspace
    candidate = _candidate(discovery)
    result = _activate_candidate(
        candidate=candidate,
        keyword_repository=keywords,
        discovery_repository=discovery,
        now=datetime(2026, 7, 12, 10, 0, 0),
    )
    item = keywords.get(result["keyword_id"])
    assert item["refresh_frequency_days"] == 1
    assert item["refresh_frequency_source"] == "auto"
    assert item["lifecycle_stage"] == "observing"
    assert item["observation_started_at"] == "2026-07-12T10:00:00"
    assert item["observation_deadline_at"] == "2026-07-27T10:00:00"


def test_manually_archived_keyword_is_not_auto_restored(workspace):
    _, keywords, discovery, group_id = workspace
    item = keywords.create_keyword(group_id, "香港保险返佣入刑", source="manual")
    keywords.archive_keyword(item["keyword_id"], reason_code="manual")
    candidate = _candidate(discovery)
    result = _activate_candidate(
        candidate=candidate,
        keyword_repository=keywords,
        discovery_repository=discovery,
        now=datetime(2026, 7, 12, 10, 0, 0),
    )
    assert result == {"activated": False, "reason": "manual_archive_protected"}
    assert keywords.get(item["keyword_id"])["status"] == "archived"


@pytest.mark.parametrize(
    ("keyword_patch", "refresh_patch", "read_metric", "expected_reason"),
    [
        ({"status": "archived"}, {}, {"steady_read_median": 5}, "not_active"),
        ({"auto_archive_locked": True}, {}, {"steady_read_median": 5}, "manually_locked"),
        ({"is_pinned": True}, {}, {"steady_read_median": 5}, "pinned"),
        (
            {"refresh_frequency_source": "manual"},
            {},
            {"steady_read_median": 5},
            "manual_refresh_policy",
        ),
        (
            {"last_refresh_status": "failed"},
            {},
            {"steady_read_median": 5},
            "waiting_failure_retry",
        ),
        (
            {"last_refresh_at": None, "snapshot_count": 0},
            {},
            {"steady_read_median": 5},
            "never_successfully_observed",
        ),
        ({}, {"observation_days": 14}, {"steady_read_median": 5}, "insufficient_observation_days"),
        ({"refresh_frequency_days": 7}, {}, {"steady_read_median": 5}, "not_lowest_frequency"),
        ({"commercial_value_score": 4}, {}, {"steady_read_median": 5}, "commercial_value_above_threshold"),
        ({}, {}, None, "read_metric_unavailable"),
        ({}, {}, {"steady_read_median": 10}, "read_activity_above_threshold"),
    ],
)
def test_auto_archive_protection_scenarios(
    keyword_patch,
    refresh_patch,
    read_metric,
    expected_reason,
):
    refresh_metric = {"observation_days": 15, **refresh_patch}
    decision = evaluate_auto_archive(
        _archive_keyword(**keyword_patch),
        refresh_metric=refresh_metric,
        read_metric=read_metric,
        policy=_archive_policy(),
    )
    assert decision["eligible"] is False
    assert expected_reason in decision["reasons"]


def test_auto_archive_all_thresholds_pass():
    decision = evaluate_auto_archive(
        _archive_keyword(),
        refresh_metric={"observation_days": 15},
        read_metric={"steady_read_median": 9.99},
        policy=_archive_policy(),
    )
    assert decision["eligible"] is True
    assert decision["reasons"] == []


def test_archive_keeps_keyword_history_and_reason(workspace):
    _, keywords, _, group_id = workspace
    item = keywords.create_keyword(group_id, "低价值测试词")
    keywords.record_refresh_results(
        succeeded_keyword_ids=[item["keyword_id"]],
        failed_keyword_ids=[],
        refreshed_at="2026-07-12T10:00:00",
    )
    keywords.archive_keyword(
        item["keyword_id"],
        reason_code="low_value_low_activity",
        reason_detail="满足自动归档阈值",
    )
    archived = keywords.get(item["keyword_id"])
    assert archived["status"] == "archived"
    assert archived["last_refresh_at"] == "2026-07-12T10:00:00"
    assert archived["archive_reason_code"] == "low_value_low_activity"
    assert archived["archive_reason_detail"] == "满足自动归档阈值"


def test_discovery_budget_counts_only_discovery_batches(workspace):
    root, _, _, _ = workspace
    for batch_id, source, total in (
        ("a", "discovery", 7),
        ("b", "discovery_validation", 5),
        ("c", "scheduler", 200),
    ):
        run = root / "data/runs" / batch_id
        run.mkdir()
        (run / "state.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "started_at": "2026-07-12T10:00:00",
                    "total_keywords": total,
                }
            ),
            encoding="utf-8",
        )
    status = get_discovery_budget_status(
        project_root=root,
        now=datetime(2026, 7, 12, 12, 0, 0),
    )
    assert status["used_count"] == 12
    assert status["remaining_count"] == 18


def test_discovery_budget_advances_probe_and_candidate_queues_fairly(workspace):
    root, keywords, _, _ = workspace
    discovery = MagicMock()
    discovery.list_candidates.return_value = [
        {
            "candidate_id": f"candidate_{i}",
            "candidate_text": f"香港保险候选词{i}",
            "related_occurrence_count": 1,
        }
        for i in range(25)
    ]
    discovery.list_probes.return_value = [
        {"probe_id": f"probe_{i}", "probe_text": f"港险探针词{i}"}
        for i in range(25)
    ]
    with (
        patch("app.services.refresh_service.get_active_batch_status", return_value=None),
        patch(
            "app.services.refresh_service.start_batch_refresh",
            return_value={"batch_id": "discovery_fair_001"},
        ),
    ):
        result = launch_next_discovery_batch(
            project_root=root,
            discovery_repository=discovery,
            keyword_repository=keywords,
            now=datetime(2026, 7, 12, 10, 0, 0),
        )
    assert result["launched"] is True
    assert result["candidate_count"] == 20
    assert result["probe_count"] == 10


def test_discovery_daily_reserve_uses_prior_batches_and_partial_remaining_slots(workspace):
    root, keywords, _, _ = workspace
    prior = root / "data/runs/prior_discovery"
    prior.mkdir()
    (prior / "state.json").write_text(
        json.dumps(
            {
                "source": "discovery",
                "started_at": "2026-07-12T08:00:00",
                "total_keywords": 25,
                "discovery_candidate_count": 25,
                "discovery_probe_count": 0,
            }
        ),
        encoding="utf-8",
    )
    discovery = MagicMock()
    discovery.list_candidates.return_value = [
        {
            "candidate_id": f"candidate_{i}",
            "candidate_text": f"香港保险候选词{i}",
            "related_occurrence_count": 1,
        }
        for i in range(25)
    ]
    discovery.list_probes.return_value = [
        {"probe_id": f"probe_{i}", "probe_text": f"港险探针词{i}"}
        for i in range(25)
    ]
    policy_path = root / "data/config/keyword_refresh_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["discovery_daily_search_budget"] = 30
    policy["discovery_batch_limit"] = 30
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with (
        patch("app.services.refresh_service.get_active_batch_status", return_value=None),
        patch(
            "app.services.refresh_service.start_batch_refresh",
            return_value={"batch_id": "discovery_partial_001"},
        ),
    ):
        result = launch_next_discovery_batch(
            project_root=root,
            discovery_repository=discovery,
            keyword_repository=keywords,
            now=datetime(2026, 7, 12, 10, 0, 0),
        )
    # 候选保底已经在前一批兑现，剩余5个槽位只能先补探针保底，
    # 不能让候选词再次抢走尚未兑现的探针额度。
    assert result["total"] == 5
    assert result["candidate_count"] == 0
    assert result["probe_count"] == 5


def test_end_to_end_probe_related_validation_observation_and_archive(workspace):
    root, keywords, discovery, _ = workspace
    probe_dir = root / "data/agent/discovery"
    probe_dir.mkdir(parents=True)
    (probe_dir / "260712_probe_candidates.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-12T08:00:00",
                "brief_id": "brief_e2e",
                "candidates": [
                    {
                        "source_article_id": "article_source",
                        "source_title": "香港保险争议文章",
                        "warming_facts": ["多账号集中跟进"],
                        "probes": [
                            {
                                "text": "港险争议",
                                "type": "question_core",
                                "source_quote": "原文出现港险争议",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert ingest_probe_files(
        project_root=root,
        discovery_repository=discovery,
    )["probes_created_or_updated"] == 1
    probe = discovery.list_probes(statuses=["proposed"])[0]
    discovery.mark_probes_queued(
        [probe["probe_id"]],
        batch_id="probe_batch",
        queued_at="2026-07-12T08:30:00",
    )

    probe_snapshot = {
        "snapshot_id": "probe_snapshot",
        "keyword_id": kw_id("港险争议"),
        "snapshot_date": "2026-07-12",
        "captured_at": "2026-07-12T09:00:00",
        "status": "success",
        "is_primary": True,
        "result_count": 10,
    }
    normalized = {
        "snapshots": [probe_snapshot],
        "terms": [
            {
                "snapshot_id": "probe_snapshot",
                "term_text": "香港保险靠谱吗",
                "term_type": "related",
                "position": 1,
            },
            {
                "snapshot_id": "probe_snapshot",
                "term_text": "香港保险好不好",
                "term_type": "suggestion",
                "position": 1,
            },
        ],
        "hits": [],
        "read_metrics": [],
    }
    with patch(
        "app.services.refresh_service.get_batch_status",
        return_value={"is_finished": True, "status": "completed"},
    ):
        probe_result = reconcile_probe_searches(
            project_root=root,
            discovery_repository=discovery,
            keyword_repository=keywords,
            normalized=normalized,
            now=datetime(2026, 7, 12, 9, 5, 0),
        )
    assert probe_result["probes_reconciled"] == 1
    assert discovery.get_probe(probe["probe_id"])["status"] == "replaced"
    candidate = discovery.get_candidate_by_text("香港保险靠谱吗")
    assert candidate is not None
    assert candidate["status"] == "discovered"
    suggestion = discovery.get_candidate_by_text("香港保险好不好")
    assert suggestion is not None
    assert suggestion["status"] == "suggestion_only"

    discovery.queue_candidate_validation(
        [candidate["candidate_id"]],
        batch_id="candidate_batch",
        queued_at="2026-07-12T09:10:00",
    )
    validation_snapshot = {
        "snapshot_id": "validation_snapshot",
        "keyword_id": kw_id("香港保险靠谱吗"),
        "snapshot_date": "2026-07-12",
        "captured_at": "2026-07-12T10:00:00",
        "status": "success",
        "is_primary": True,
        "result_count": 10,
    }
    validation_normalized = {
        "snapshots": [probe_snapshot, validation_snapshot],
        "terms": normalized["terms"],
        "hits": [
            {
                "snapshot_id": "validation_snapshot",
                "article_id": "article_source",
                "rank": 3,
            }
        ],
        "read_metrics": [],
    }
    with patch(
        "app.services.refresh_service.get_batch_status",
        return_value={"is_finished": True, "status": "completed"},
    ):
        validation_result = reconcile_candidate_validations(
            project_root=root,
            discovery_repository=discovery,
            keyword_repository=keywords,
            normalized=validation_normalized,
            now=datetime(2026, 7, 12, 10, 5, 0),
        )
    assert validation_result == {
        "candidates_validated": 1,
        "candidates_activated": 1,
    }
    active = next(
        item
        for item in keywords.list_keywords(include_archived=False)
        if item["keyword_text"] == "香港保险靠谱吗"
    )
    assert active["lifecycle_stage"] == "observing"
    assert active["refresh_frequency_days"] == 1
    assert active["observation_deadline_at"] == "2026-07-27T10:05:00"

    daily_snapshots = []
    daily_hits = []
    for day in range(1, 16):
        date = f"2026-07-{day + 11:02d}"
        snapshot_id = f"observation_{day:02d}"
        daily_snapshots.append(
            {
                "snapshot_id": snapshot_id,
                "keyword_id": active["keyword_id"],
                "snapshot_date": date,
                "captured_at": f"{date}T10:00:00",
                "status": "success",
                "is_primary": True,
                "result_count": 1,
            }
        )
        daily_hits.append(
            {
                "snapshot_id": snapshot_id,
                "article_id": "stable_article",
                "rank": 1,
            }
        )
    (root / "normalized/snapshots.json").write_text(
        json.dumps(daily_snapshots),
        encoding="utf-8",
    )
    (root / "normalized/ranking_hits.json").write_text(
        json.dumps(daily_hits),
        encoding="utf-8",
    )
    read_metric = {
        "keyword_id": active["keyword_id"],
        "steady_read_median": 5,
    }
    (root / "normalized/keyword_read_deltas.json").write_text(
        json.dumps([read_metric]),
        encoding="utf-8",
    )
    keywords.sync_observations(
        [
            {
                "keyword_id": active["keyword_id"],
                "keyword_text": active["keyword_text"],
                "first_seen_at": "2026-07-12T10:00:00",
                "last_seen_at": "2026-07-26T10:00:00",
                "snapshot_count": 15,
            }
        ]
    )
    keywords.record_refresh_results(
        succeeded_keyword_ids=[active["keyword_id"]],
        failed_keyword_ids=[],
        refreshed_at="2026-07-26T10:00:00",
    )
    lifecycle = apply_keyword_lifecycle(
        project_root=root,
        discovery_repository=discovery,
        keyword_repository=keywords,
        normalized={
            "snapshots": daily_snapshots,
            "terms": [],
            "hits": daily_hits,
            "read_metrics": [read_metric],
        },
        now=datetime(2026, 7, 28, 10, 0, 0),
    )
    archived = keywords.get(active["keyword_id"])
    assert lifecycle["archived_count"] == 1
    assert archived["status"] == "archived"
    assert archived["refresh_frequency_days"] == 15
    assert archived["commercial_value_score"] == 3
    assert archived["archive_reason_code"] == "low_value_low_activity"
    assert discovery.get_candidate(candidate["candidate_id"])["status"] == "archived"
