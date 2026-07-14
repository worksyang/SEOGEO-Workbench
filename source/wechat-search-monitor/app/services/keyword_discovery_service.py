"""动态关键词发现闭环。

事实边界：
- Claude 晨报只提出 probe；
- 只有微信搜索快照中的 related term 可以进入候选验证；
- suggestion 只保存为辅助证据，不能单独晋级；
- 候选搜索通过后才进入 active，并强制每日观察 15 天；
- 归档只基于透明阈值，不删除历史事实。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import Config
from app.ingest.common import kw_id
from app.repositories.keyword_discovery_repo import KeywordDiscoveryRepository
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.services.keyword_commercial_value_service import (
    apply_commercial_value_scores,
    score_keyword,
)
from app.services.keyword_refresh_policy_service import (
    apply_auto_refresh_policies,
    build_refresh_metrics,
    load_policy_config,
)


DISCOVERY_GROUP_LABEL = "动态发现候选词"
MAX_TERM_LENGTH = 36
DOMAIN_SIGNAL = re.compile(
    r"(香港保险|港险|香港保单|储蓄险|分红险|重疾险|年金|终身寿|万用寿险|"
    r"家族信托|保险金信托|保费融资|杠杆寿|提领|保单贷款|友邦|安盛|"
    r"保诚|宏利|富卫|永明|万通|周大福|国寿|太保|中银|忠意|苏黎世|"
    r"安达|盛利|环宇|财富盈活|信守明天|星河|富饶|薪火传承|CRS|GN16|IUL)",
    re.IGNORECASE,
)
NOISE_SIGNAL = re.compile(
    r"(公众号|官网|客服电话|客服|地址|招聘|app下载|登录|小程序|"
    r"十大保险经纪|星河战队|荣誉2期|港险详析|港险干货|知乎|百度|"
    r"小红书|视频|保险公司介绍|保险公司排名|app$|"
    r"总资产|保费收入|保费规模|营收|年报|财报)",
    re.IGNORECASE,
)
EXPIRED_SIGNAL = re.compile(r"(202[0-5]年|6月优惠|六月优惠)", re.IGNORECASE)
OUTSIDE_MARKET_SIGNAL = re.compile(r"(澳门)", re.IGNORECASE)


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def is_eligible_candidate_term(term_text: str) -> tuple[bool, str]:
    text = str(term_text or "").strip()
    if len(text) < 2:
        return False, "too_short"
    if len(text) > MAX_TERM_LENGTH:
        return False, "too_long"
    if NOISE_SIGNAL.search(text):
        return False, "navigation_or_noise"
    if EXPIRED_SIGNAL.search(text):
        return False, "expired_time_term"
    if OUTSIDE_MARKET_SIGNAL.search(text):
        return False, "outside_target_market"
    if not DOMAIN_SIGNAL.search(text):
        return False, "outside_domain"
    return True, ""


def calculate_candidate_validation_score(
    *,
    related_parent_count: int,
    related_date_count: int,
    best_related_position: int | None,
    source_article_best_rank: int | None,
    validation_result_count: int,
) -> float:
    parent_score = min(max(related_parent_count, 0), 5) / 5 * 22
    date_score = min(max(related_date_count, 0), 5) / 5 * 18
    position = int(best_related_position or 99)
    position_score = max(0.0, min(10.0, (11 - position)))
    if source_article_best_rank is None:
        reverse_score = 0.0
    elif source_article_best_rank <= 5:
        reverse_score = 35.0
    elif source_article_best_rank <= 10:
        reverse_score = 25.0
    else:
        reverse_score = 0.0
    result_score = min(max(validation_result_count, 0), 10) / 10 * 15
    return round(
        min(100.0, parent_score + date_score + position_score + reverse_score + result_score),
        2,
    )


def evaluate_candidate_validation(
    candidate: dict[str, Any],
    *,
    source_article_best_rank: int | None,
    validation_result_count: int,
) -> dict[str, Any]:
    parent_count = max(
        int(candidate.get("related_parent_probe_count") or 0),
        int(candidate.get("historical_related_parent_count") or 0),
    )
    date_count = max(
        int(candidate.get("related_date_count") or 0),
        int(candidate.get("historical_related_date_count") or 0),
    )
    score = calculate_candidate_validation_score(
        related_parent_count=parent_count,
        related_date_count=date_count,
        best_related_position=candidate.get("best_related_position"),
        source_article_best_rank=source_article_best_rank,
        validation_result_count=validation_result_count,
    )
    has_related = (
        int(candidate.get("related_occurrence_count") or 0) > 0
        or int(candidate.get("historical_related_occurrence_count") or 0) > 0
    )
    has_reverse_hit = (
        source_article_best_rank is not None and source_article_best_rank <= 10
    )
    has_repeated_related = parent_count >= 2 and date_count >= 2
    accepted = (
        has_related
        and validation_result_count > 0
        and (has_reverse_hit or has_repeated_related)
        and score >= 35
    )
    if not has_related:
        reason = "没有关联词证据"
    elif validation_result_count <= 0:
        reason = "候选词搜索无结果"
    elif not has_reverse_hit and not has_repeated_related:
        reason = "未反向命中源文章，且关联词未跨2个父词和2天"
    elif score < 35:
        reason = f"验证分{score}低于35"
    else:
        reason = ""
    return {
        "accepted": accepted,
        "validation_score": score,
        "reason": reason,
        "related_parent_count": parent_count,
        "related_date_count": date_count,
    }


def evaluate_auto_archive(
    keyword: dict[str, Any],
    *,
    refresh_metric: dict[str, Any] | None,
    read_metric: dict[str, Any] | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    archive_policy = dict(policy.get("auto_archive") or {})
    reasons: list[str] = []
    observed_days = int((refresh_metric or {}).get("observation_days") or 0)
    steady_read = (
        float((read_metric or {}).get("steady_read_median"))
        if (read_metric or {}).get("steady_read_median") is not None
        else None
    )
    if not bool(archive_policy.get("enabled", True)):
        reasons.append("auto_archive_disabled")
    if keyword.get("status") != "active":
        reasons.append("not_active")
    if bool(keyword.get("auto_archive_locked")):
        reasons.append("manually_locked")
    if bool(keyword.get("is_pinned")):
        reasons.append("pinned")
    if keyword.get("refresh_frequency_source") == "manual":
        reasons.append("manual_refresh_policy")
    if keyword.get("last_refresh_status") == "failed":
        reasons.append("waiting_failure_retry")
    if not keyword.get("last_refresh_at") or int(keyword.get("snapshot_count") or 0) <= 0:
        reasons.append("never_successfully_observed")
    if observed_days < int(archive_policy.get("minimum_observation_days") or 15):
        reasons.append("insufficient_observation_days")
    if int(keyword.get("refresh_frequency_days") or 1) != int(
        archive_policy.get("required_refresh_frequency_days") or 15
    ):
        reasons.append("not_lowest_frequency")
    if int(keyword.get("commercial_value_score") or 5) > int(
        archive_policy.get("maximum_commercial_value_score") or 3
    ):
        reasons.append("commercial_value_above_threshold")
    if steady_read is None:
        reasons.append("read_metric_unavailable")
    elif steady_read >= float(archive_policy.get("maximum_steady_read_median") or 10):
        reasons.append("read_activity_above_threshold")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "observation_days": observed_days,
        "steady_read_median": steady_read,
    }


def ingest_probe_files(
    *,
    project_root: Path,
    discovery_repository: KeywordDiscoveryRepository,
) -> dict[str, Any]:
    directory = project_root / "data" / "agent" / "discovery"
    if not directory.exists():
        return {"files": 0, "probes_seen": 0, "probes_created_or_updated": 0}
    files = sorted(directory.glob("*_probe_candidates.json"))
    probes_seen = 0
    written = 0
    for path in files:
        payload = _read_json(path, {})
        if not isinstance(payload, dict):
            continue
        brief_id = str(payload.get("brief_id") or path.stem).strip()
        generated_at = str(payload.get("generated_at") or datetime.fromtimestamp(
            path.stat().st_mtime
        ).isoformat(timespec="seconds"))
        for item in payload.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            source_article_id = str(item.get("source_article_id") or "").strip()
            source_title = str(item.get("source_title") or "").strip()
            warming_facts = list(item.get("warming_facts") or [])
            for probe in item.get("probes") or []:
                if not isinstance(probe, dict):
                    continue
                probes_seen += 1
                text = str(probe.get("text") or "").strip()
                if not text:
                    continue
                discovery_repository.upsert_probe(
                    brief_id=brief_id,
                    source_article_id=source_article_id,
                    source_title=source_title,
                    probe_text=text,
                    probe_type=str(probe.get("type") or ""),
                    source_quote=str(probe.get("source_quote") or ""),
                    warming_facts=warming_facts,
                    proposed_at=generated_at,
                )
                written += 1
    return {
        "files": len(files),
        "probes_seen": probes_seen,
        "probes_created_or_updated": written,
    }


def _load_normalized(project_root: Path) -> dict[str, list[dict[str, Any]]]:
    normalized = project_root / "normalized"
    return {
        "snapshots": _read_json(normalized / "snapshots.json", []) or [],
        "terms": _read_json(normalized / "snapshot_terms.json", []) or [],
        "hits": _read_json(normalized / "ranking_hits.json", []) or [],
        "read_metrics": _read_json(normalized / "keyword_read_deltas.json", []) or [],
    }


def _historical_term_stats(
    *,
    candidate_text: str,
    snapshots: list[dict[str, Any]],
    terms: list[dict[str, Any]],
) -> dict[str, int]:
    target = _normalize(candidate_text)
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        if str(snapshot.get("status") or "success") != "success":
            continue
        keyword_id = str(snapshot.get("keyword_id") or "")
        date = str(snapshot.get("snapshot_date") or "")
        if not keyword_id or not date:
            continue
        key = (keyword_id, date)
        current = selected.get(key)
        if current is None or (
            bool(snapshot.get("is_primary")),
            str(snapshot.get("captured_at") or ""),
        ) > (
            bool(current.get("is_primary")),
            str(current.get("captured_at") or ""),
        ):
            selected[key] = snapshot
    snapshot_map = {
        str(item.get("snapshot_id")): item
        for item in selected.values()
        if item.get("snapshot_id")
    }
    related_occurrences = 0
    suggestion_occurrences = 0
    parents: set[str] = set()
    dates: set[str] = set()
    for term in terms:
        if _normalize(term.get("term_text")) != target:
            continue
        snapshot = snapshot_map.get(str(term.get("snapshot_id") or ""))
        if not snapshot:
            continue
        if str(term.get("term_type") or "") == "related":
            related_occurrences += 1
            parents.add(str(snapshot.get("keyword_id") or ""))
            dates.add(str(snapshot.get("snapshot_date") or ""))
        elif str(term.get("term_type") or "") == "suggestion":
            suggestion_occurrences += 1
    return {
        "related_occurrences": related_occurrences,
        "related_parents": len(parents - {""}),
        "related_dates": len(dates - {""}),
        "suggestion_occurrences": suggestion_occurrences,
    }


def _latest_snapshot_after(
    *,
    keyword_text: str,
    after: str | None,
    snapshots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    keyword_id = kw_id(keyword_text)
    after_dt = _parse_datetime(after)
    matches = []
    for snapshot in snapshots:
        if snapshot.get("keyword_id") != keyword_id:
            continue
        if str(snapshot.get("status") or "success") != "success":
            continue
        captured = _parse_datetime(snapshot.get("captured_at"))
        if after_dt and captured and captured < after_dt:
            continue
        matches.append(snapshot)
    if not matches:
        return None
    return max(matches, key=lambda item: str(item.get("captured_at") or ""))


def reconcile_probe_searches(
    *,
    project_root: Path,
    discovery_repository: KeywordDiscoveryRepository,
    keyword_repository: KeywordRegistryRepository,
    normalized: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    from app.services.refresh_service import get_batch_status

    snapshots = normalized["snapshots"]
    terms = normalized["terms"]
    terms_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for term in terms:
        terms_by_snapshot[str(term.get("snapshot_id") or "")].append(term)
    registry_by_normalized = {
        _normalize(item.get("keyword_text")): item
        for item in keyword_repository.list_keywords(include_archived=True)
    }
    searched = 0
    candidates_written = 0
    for probe in discovery_repository.list_probes(statuses=["queued"], limit=500):
        batch_id = str(probe.get("search_batch_id") or "")
        batch = get_batch_status(batch_id) if batch_id else None
        if not batch or not batch.get("is_finished"):
            continue
        snapshot = _latest_snapshot_after(
            keyword_text=probe["probe_text"],
            after=probe.get("queued_at"),
            snapshots=snapshots,
        )
        if snapshot is None:
            discovery_repository.reject_probe(
                probe["probe_id"],
                reason=f"探针批次结束但未找到成功快照：{batch.get('status')}",
                archived_at=_now_iso(now),
            )
            continue

        related_candidate_ids: list[str] = []
        for term in terms_by_snapshot.get(str(snapshot.get("snapshot_id") or ""), []):
            term_type = str(term.get("term_type") or "")
            if term_type not in {"related", "suggestion"}:
                continue
            term_text = str(term.get("term_text") or "").strip()
            eligible, _reason = is_eligible_candidate_term(term_text)
            if not eligible:
                continue
            existing = registry_by_normalized.get(_normalize(term_text))
            if existing and existing.get("status") == "active":
                continue
            if existing and existing.get("status") == "archived" and existing.get("source") not in {
                "observed",
                "discovery_candidate",
            }:
                continue
            business = score_keyword(term_text)
            candidate = discovery_repository.upsert_candidate_evidence(
                candidate_text=term_text,
                probe_id=probe["probe_id"],
                snapshot_id=str(snapshot["snapshot_id"]),
                evidence_date=str(snapshot.get("snapshot_date") or "")[:10],
                term_type=term_type,
                position=int(term.get("position") or 99),
                source_article_id=str(probe.get("source_article_id") or ""),
                observed_at=str(snapshot.get("captured_at") or _now_iso(now)),
                business_value_score=int(business["commercial_value_score"]),
                business_value_reason=str(business["commercial_value_reason"]),
            )
            historical = _historical_term_stats(
                candidate_text=term_text,
                snapshots=snapshots,
                terms=terms,
            )
            discovery_repository.set_candidate_historical_evidence(
                candidate["candidate_id"],
                **historical,
            )
            candidates_written += 1
            if term_type == "related":
                related_candidate_ids.append(candidate["candidate_id"])

        discovery_repository.mark_probe_searched(
            probe["probe_id"],
            searched_at=str(snapshot.get("captured_at") or _now_iso(now)),
        )
        if related_candidate_ids:
            discovery_repository.archive_probe_as_replaced(
                probe["probe_id"],
                candidate_ids=related_candidate_ids,
                archived_at=_now_iso(now),
            )
        else:
            discovery_repository.reject_probe(
                probe["probe_id"],
                reason="搜索结果没有合格关联词；仅下拉词不能晋级",
                archived_at=_now_iso(now),
            )
        searched += 1
    return {"probes_reconciled": searched, "candidate_evidence_written": candidates_written}


def _ensure_discovery_group(keyword_repository: KeywordRegistryRepository) -> str:
    payload = keyword_repository.load_payload(include_archived=True)
    for group in payload.get("groups", []):
        if group.get("label") == DISCOVERY_GROUP_LABEL:
            return str(group["group_id"])
    return str(keyword_repository.create_group(DISCOVERY_GROUP_LABEL)["group_id"])


def _activate_candidate(
    *,
    candidate: dict[str, Any],
    keyword_repository: KeywordRegistryRepository,
    discovery_repository: KeywordDiscoveryRepository,
    now: datetime,
) -> dict[str, Any]:
    existing = next(
        (
            item
            for item in keyword_repository.list_keywords(include_archived=True)
            if _normalize(item.get("keyword_text")) == _normalize(candidate["candidate_text"])
        ),
        None,
    )
    if existing and existing.get("status") == "archived" and existing.get("source") not in {
        "observed",
        "discovery_candidate",
    }:
        discovery_repository.archive_candidate(
            candidate["candidate_id"],
            archived_at=_now_iso(now),
            reason="该词曾被人工或业务规则归档，不自动恢复",
        )
        return {"activated": False, "reason": "manual_archive_protected"}

    if existing and existing.get("status") == "active":
        keyword = existing
    else:
        group_id = _ensure_discovery_group(keyword_repository)
        keyword = keyword_repository.create_keyword(
            group_id,
            candidate["candidate_text"],
            note="动态发现：由探针搜索的微信关联词验证后进入15天观察",
            source="discovery_candidate",
        )
    business = score_keyword(candidate["candidate_text"])
    keyword_repository.set_commercial_value(
        keyword["keyword_id"],
        score=int(business["commercial_value_score"]),
        reason=str(business["commercial_value_reason"]),
        source="auto",
    )
    keyword_repository.set_refresh_policy(keyword["keyword_id"], source="auto")
    started_at = _now_iso(now)
    deadline = _now_iso(now + timedelta(days=15))
    keyword_repository.set_discovery_lifecycle(
        keyword["keyword_id"],
        lifecycle_stage="observing",
        observation_started_at=started_at,
        observation_deadline_at=deadline,
        discovery_candidate_id=candidate["candidate_id"],
    )
    discovery_repository.mark_candidate_observing(
        candidate["candidate_id"],
        keyword_id=keyword["keyword_id"],
        observation_started_at=started_at,
        observation_deadline_at=deadline,
    )
    return {"activated": True, "keyword_id": keyword["keyword_id"]}


def reconcile_candidate_validations(
    *,
    project_root: Path,
    discovery_repository: KeywordDiscoveryRepository,
    keyword_repository: KeywordRegistryRepository,
    normalized: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    from app.services.refresh_service import get_batch_status

    snapshots = normalized["snapshots"]
    hits = normalized["hits"]
    hits_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        hits_by_snapshot[str(hit.get("snapshot_id") or "")].append(hit)
    validated = 0
    activated = 0
    for candidate in discovery_repository.list_candidates(
        statuses=["validation_queued"],
        limit=500,
    ):
        batch_id = str(candidate.get("validation_batch_id") or "")
        batch = get_batch_status(batch_id) if batch_id else None
        if not batch or not batch.get("is_finished"):
            continue
        snapshot = _latest_snapshot_after(
            keyword_text=candidate["candidate_text"],
            after=candidate.get("validation_queued_at"),
            snapshots=snapshots,
        )
        if snapshot is None:
            discovery_repository.mark_candidate_validated(
                candidate["candidate_id"],
                validated_at=_now_iso(now),
                source_article_best_rank=None,
                validation_result_count=0,
                validation_score=0,
                accepted=False,
                rejection_reason="候选验证批次结束但没有成功快照",
            )
            validated += 1
            continue
        source_articles = set(candidate.get("source_article_ids") or [])
        source_ranks = [
            int(hit.get("rank") or 99)
            for hit in hits_by_snapshot.get(str(snapshot.get("snapshot_id") or ""), [])
            if str(hit.get("article_id") or "") in source_articles
        ]
        source_rank = min(source_ranks) if source_ranks else None
        evaluation = evaluate_candidate_validation(
            candidate,
            source_article_best_rank=source_rank,
            validation_result_count=int(snapshot.get("result_count") or 0),
        )
        discovery_repository.mark_candidate_validated(
            candidate["candidate_id"],
            validated_at=str(snapshot.get("captured_at") or _now_iso(now)),
            source_article_best_rank=source_rank,
            validation_result_count=int(snapshot.get("result_count") or 0),
            validation_score=float(evaluation["validation_score"]),
            accepted=bool(evaluation["accepted"]),
            rejection_reason=str(evaluation["reason"]),
        )
        if evaluation["accepted"]:
            refreshed = discovery_repository.get_candidate(candidate["candidate_id"]) or candidate
            result = _activate_candidate(
                candidate=refreshed,
                keyword_repository=keyword_repository,
                discovery_repository=discovery_repository,
                now=now,
            )
            activated += 1 if result.get("activated") else 0
        validated += 1
    return {"candidates_validated": validated, "candidates_activated": activated}


def apply_keyword_lifecycle(
    *,
    project_root: Path,
    discovery_repository: KeywordDiscoveryRepository,
    keyword_repository: KeywordRegistryRepository,
    normalized: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    policy = load_policy_config(project_root)
    commercial_summary = apply_commercial_value_scores(repository=keyword_repository)
    refresh_summary = apply_auto_refresh_policies(
        repository=keyword_repository,
        normalized_dir=project_root / "normalized",
    )
    refresh_metrics = build_refresh_metrics(normalized_dir=project_root / "normalized")
    read_metrics = {
        str(item.get("keyword_id") or ""): item
        for item in normalized["read_metrics"]
        if item.get("keyword_id")
    }
    matured = 0
    archived = 0
    archive_audit: list[dict[str, Any]] = []
    for keyword in keyword_repository.list_keywords(include_archived=False):
        decision = evaluate_auto_archive(
            keyword,
            refresh_metric=refresh_metrics.get(keyword["keyword_id"]),
            read_metric=read_metrics.get(keyword["keyword_id"]),
            policy=policy,
        )
        deadline = _parse_datetime(keyword.get("observation_deadline_at"))
        observation_complete = bool(deadline and now >= deadline)
        if decision["eligible"]:
            detail = (
                f"自动归档：有效观察{decision['observation_days']}天，"
                f"商业价值{keyword['commercial_value_score']}/10，"
                f"刷新频率{keyword['refresh_frequency_days']}天，"
                f"日均阅读增量中位数{decision['steady_read_median']:.1f}"
            )
            keyword_repository.archive_keyword(
                keyword["keyword_id"],
                reason_code="low_value_low_activity",
                reason_detail=detail,
            )
            if keyword.get("discovery_candidate_id"):
                discovery_repository.archive_candidate(
                    keyword["discovery_candidate_id"],
                    archived_at=_now_iso(now),
                    reason=detail,
                )
            archived += 1
            archive_audit.append(
                {
                    "keyword_id": keyword["keyword_id"],
                    "keyword_text": keyword["keyword_text"],
                    "reason": detail,
                }
            )
            continue
        if keyword.get("lifecycle_stage") == "observing" and observation_complete:
            keyword_repository.set_discovery_lifecycle(
                keyword["keyword_id"],
                lifecycle_stage="established",
                observation_started_at=keyword.get("observation_started_at"),
                observation_deadline_at=keyword.get("observation_deadline_at"),
                discovery_candidate_id=keyword.get("discovery_candidate_id"),
            )
            if keyword.get("discovery_candidate_id"):
                discovery_repository.mark_candidate_matured(
                    keyword["discovery_candidate_id"],
                    matured_at=_now_iso(now),
                )
            matured += 1
    return {
        "commercial_value": commercial_summary,
        "refresh_policy": {
            key: value
            for key, value in refresh_summary.items()
            if key != "metrics"
        },
        "matured_count": matured,
        "archived_count": archived,
        "archive_audit": archive_audit,
    }


def _discovery_search_usage_today(project_root: Path, day: str) -> dict[str, int]:
    usage = {"total": 0, "probe": 0, "candidate": 0}
    runs_root = project_root / "data" / "runs"
    if not runs_root.exists():
        return usage
    for state_path in runs_root.glob("*/state.json"):
        state = _read_json(state_path, {})
        if not isinstance(state, dict):
            continue
        if not str(state.get("source") or "").startswith("discovery"):
            continue
        if str(state.get("started_at") or "")[:10] != day:
            continue
        total = int(state.get("total_keywords") or 0)
        usage["total"] += total
        usage["probe"] += int(
            state.get("discovery_probe_count")
            or state.get("probe_used_count")
            or state.get("probe_count")
            or 0
        )
        usage["candidate"] += int(
            state.get("discovery_candidate_count")
            or state.get("candidate_used_count")
            or state.get("candidate_count")
            or 0
        )
    return usage


def _discovery_searches_used_today(project_root: Path, day: str) -> int:
    return _discovery_search_usage_today(project_root, day)["total"]


def get_discovery_budget_status(
    *,
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回当天探针/候选轻量搜索额度，不把下拉/关联证据数误当搜索次数。"""
    root = Path(project_root or Config.PROJECT_ROOT)
    now = now or datetime.now()
    policy = load_policy_config(root)
    daily_limit = int(policy.get("discovery_daily_search_budget") or 30)
    usage = _discovery_search_usage_today(root, now.date().isoformat())
    used = usage["total"]
    probe_used = usage["probe"]
    candidate_used = usage["candidate"]
    return {
        "date": now.date().isoformat(),
        "daily_keyword_budget": daily_limit,
        "used_count": used,
        "remaining_count": max(0, daily_limit - used),
        "probe_used_count": probe_used,
        "candidate_used_count": candidate_used,
        "candidate_validation_used_count": candidate_used,
    }


def launch_next_discovery_batch(
    *,
    project_root: Path,
    discovery_repository: KeywordDiscoveryRepository,
    keyword_repository: KeywordRegistryRepository,
    now: datetime,
) -> dict[str, Any]:
    from app.services.refresh_service import (
        BatchAlreadyRunningError,
        get_active_batch_status,
        start_batch_refresh,
    )

    if get_active_batch_status():
        return {"launched": False, "reason": "batch_running"}
    policy = load_policy_config(project_root)
    daily_limit = int(policy.get("discovery_daily_search_budget") or 30)
    batch_limit = int(policy.get("discovery_batch_limit") or 30)
    usage = _discovery_search_usage_today(project_root, now.date().isoformat())
    used = usage["total"]
    probe_used = usage["probe"]
    candidate_used = usage["candidate"]
    remaining = max(0, daily_limit - used)
    limit = min(batch_limit, remaining)
    if limit <= 0:
        return {"launched": False, "reason": "daily_discovery_budget_exhausted", "used": used}

    pending_candidates = [
        item
        for item in discovery_repository.list_candidates(
            statuses=["discovered", "validation_failed"],
            limit=max(limit * 4, 100),
        )
        if int(item.get("related_occurrence_count") or 0) > 0
    ]
    pending_probes = discovery_repository.list_probes(
        statuses=["proposed"],
        limit=max(limit * 4, 100),
    )
    # 保底按当天累计消耗计算，而不是按当前批次重置：先补足各自日保底，
    # 再把剩余槽位借给另一侧；一侧已用满保底时，不会再次抢另一侧的额度。
    probe_target = int(policy.get("discovery_probe_reserve") or 10)
    candidate_target = int(policy.get("discovery_candidate_validation_reserve") or 20)
    probe_need = max(0, probe_target - probe_used)
    candidate_need = max(0, candidate_target - candidate_used)
    probe_reserve = min(probe_need, len(pending_probes), limit)
    candidate_reserve = min(
        candidate_need,
        len(pending_candidates),
        max(0, limit - probe_reserve),
    )
    remaining_slots = limit - probe_reserve - candidate_reserve
    selected_candidates = pending_candidates[:candidate_reserve]
    selected_probes = pending_probes[:probe_reserve]
    if remaining_slots:
        candidate_extra = pending_candidates[candidate_reserve:]
        extra = min(remaining_slots, len(candidate_extra))
        selected_candidates.extend(candidate_extra[:extra])
        remaining_slots -= extra
    if remaining_slots:
        probe_extra = pending_probes[probe_reserve:]
        selected_probes.extend(probe_extra[:remaining_slots])
    active_normalized = {
        _normalize(item.get("keyword_text"))
        for item in keyword_repository.list_keywords(include_archived=False)
    }
    unique_texts: set[str] = set()
    items: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    probe_ids: list[str] = []
    for candidate in selected_candidates:
        text = str(candidate.get("candidate_text") or "").strip()
        normalized_text = _normalize(text)
        if not text or normalized_text in unique_texts or normalized_text in active_normalized:
            continue
        unique_texts.add(normalized_text)
        candidate_ids.append(candidate["candidate_id"])
        items.append(
            {
                "group_id": "discovery_validation",
                "group_label": "候选词验真",
                "group_order": 0,
                "keyword_id": kw_id(text),
                "keyword_text": text,
            }
        )
    for probe in selected_probes:
        if len(items) >= limit:
            break
        text = str(probe.get("probe_text") or "").strip()
        normalized_text = _normalize(text)
        if not text or normalized_text in unique_texts or normalized_text in active_normalized:
            continue
        unique_texts.add(normalized_text)
        probe_ids.append(probe["probe_id"])
        items.append(
            {
                "group_id": "discovery_probe",
                "group_label": "文章探针词",
                "group_order": 1,
                "keyword_id": kw_id(text),
                "keyword_text": text,
            }
        )
    if not items:
        return {"launched": False, "reason": "no_pending_discovery_terms", "used": used}
    try:
        state = start_batch_refresh(
            items,
            source="discovery",
            fetch_depth=0,
            fetch_max_count=0,
            timeout=480,
            rebuild_every=0,
        )
    except BatchAlreadyRunningError:
        return {"launched": False, "reason": "batch_running"}
    batch_id = str(state.get("batch_id") or "")
    queued_at = _now_iso(now)
    discovery_repository.queue_candidate_validation(
        candidate_ids,
        batch_id=batch_id,
        queued_at=queued_at,
    )
    state_path = project_root / "data" / "runs" / batch_id / "state.json"
    batch_state = _read_json(state_path, {})
    if isinstance(batch_state, dict) and state_path.exists():
        batch_state["discovery_candidate_count"] = len(candidate_ids)
        batch_state["discovery_probe_count"] = len(probe_ids)
        state_path.write_text(
            json.dumps(batch_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    discovery_repository.mark_probes_queued(
        probe_ids,
        batch_id=batch_id,
        queued_at=queued_at,
    )
    return {
        "launched": True,
        "batch_id": batch_id,
        "candidate_count": len(candidate_ids),
        "probe_count": len(probe_ids),
        "total": len(items),
        "daily_used_before_launch": used,
        "daily_remaining_after_launch": max(0, remaining - len(items)),
    }


def run_discovery_cycle(
    *,
    project_root: Path | None = None,
    now: datetime | None = None,
    auto_launch: bool = False,
) -> dict[str, Any]:
    root = Path(project_root or Config.PROJECT_ROOT)
    now = now or datetime.now()
    keyword_repository = KeywordRegistryRepository(root / "data" / "state" / "app.db")
    discovery_repository = KeywordDiscoveryRepository(root / "data" / "state" / "app.db")
    normalized = _load_normalized(root)
    ingest = ingest_probe_files(
        project_root=root,
        discovery_repository=discovery_repository,
    )
    probe_reconcile = reconcile_probe_searches(
        project_root=root,
        discovery_repository=discovery_repository,
        keyword_repository=keyword_repository,
        normalized=normalized,
        now=now,
    )
    candidate_reconcile = reconcile_candidate_validations(
        project_root=root,
        discovery_repository=discovery_repository,
        keyword_repository=keyword_repository,
        normalized=normalized,
        now=now,
    )
    lifecycle = apply_keyword_lifecycle(
        project_root=root,
        discovery_repository=discovery_repository,
        keyword_repository=keyword_repository,
        normalized=normalized,
        now=now,
    )
    launch = (
        launch_next_discovery_batch(
            project_root=root,
            discovery_repository=discovery_repository,
            keyword_repository=keyword_repository,
            now=now,
        )
        if auto_launch
        else {"launched": False, "reason": "auto_launch_disabled"}
    )
    return {
        "generated_at": _now_iso(now),
        "ingest": ingest,
        "probe_reconcile": probe_reconcile,
        "candidate_reconcile": candidate_reconcile,
        "lifecycle": lifecycle,
        "launch": launch,
        "summary": discovery_repository.summary(),
    }
