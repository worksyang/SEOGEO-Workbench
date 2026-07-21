"""Hub 原生微信 Agent 观察包投影。

本模块把老 top3 系统 ``agent_projection_service.py`` 的观察包算法迁到 Hub：
- 事实来源：Hub 规范化表 ``search_snapshots`` / ``search_hits`` / ``contents`` /
  ``creators`` / ``metric_observations``，以及由 ``WechatLegacyRepository`` 重建的
  兼容投影（关键词、账号三榜、阅读增量、关联词）。
- 纯算法（候选事件、证据包、blocked/cautious、claims 去重、manifest/brief）保持
  与旧实现等价，便于做 golden 对账。
- 持久化：run-scoped staging 校验后，持单写锁、单事务把 manifest、daily_brief、
  被引用 evidence 原子写入既有 ``wechat_aux_artifacts``，并推进 ``wechat_agent_*``
  状态表。失败保留上一份 last-known-good。

禁止依赖：旧 top3 目录、旧端口 8765、旧 normalized 文件、``source/**``。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock


PROJECTION_VERSION = "agent_observation_protocol_v1_2"
BRIEF_SCHEMA_VERSION = "agent_daily_brief_v1"
MANIFEST_SCHEMA_VERSION = "agent_manifest_v1"
EVIDENCE_SCHEMA_VERSION = "agent_evidence_v1"
CLAIM_LEDGER_SCHEMA_VERSION = "agent_claim_ledger_v1"

MAX_RECENT_ARTICLES = 100
MAX_RECENT_ARTICLES_IN_BRIEF = 20
MAX_ARTICLE_EVIDENCE = 100
MAX_EVENT_CANDIDATES = 24
MAX_ACTIVE_CLAIMS = 20
MAX_CONTENT_PREVIEW_CHARS = 2400
TERM_MAX_LENGTH = 36
KEYWORD_TREND_STABLE_DAYS = 7

AGENT_ARTIFACT_KINDS = {"manifest", "daily_brief", "metric_dictionary", "evidence"}
EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9_-]{3,180}")
_UTC = timezone.utc


class AgentProjectionError(Exception):
    """投影生成或发布失败的根异常。"""


class AgentProjectionValidation(AgentProjectionError):
    """staging 校验未通过，禁止发布。"""


# ---------------------------------------------------------------------------
# 纯函数工具：与旧实现保持一致的语义，便于 golden 对账。
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
            except ValueError:
                continue
            break
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_UTC).replace(tzinfo=None)
    return parsed


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _now_iso() -> str:
    return datetime.now(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip(value: Any, length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= length:
        return text
    return f"{text[: max(length - 1, 0)].rstrip()}…"


def _stable_hash(value: Any, length: int = 16) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _clean_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s·•|｜:：,，。！？!?、\--_（）()\[\]【】<>《》“”\"'‘’`]+", "", text)


def _confidence_value(level: Any) -> float:
    return {
        "high": 1.0,
        "medium": 0.7,
        "low": 0.4,
        "insufficient": 0.0,
    }.get(str(level or "").strip().lower(), 0.0)


def _inclusive_calendar_days(started_at: datetime | None, as_of: datetime) -> int:
    if started_at is None:
        return 0
    return max((as_of.date() - started_at.date()).days + 1, 1)


# ---------------------------------------------------------------------------
# Hub 事实适配：把 Hub 表和 live projection 转成旧算法所期望的内存 DTO。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HubFacts:
    """一次投影所需的全部 Hub 事实，生成后不可变。"""

    as_of: datetime
    snapshots: list[dict[str, Any]]
    ranking_hits: list[dict[str, Any]]
    articles: list[dict[str, Any]]
    accounts_by_id: dict[str, dict[str, Any]]
    monitor_accounts: list[dict[str, Any]]
    total_account_count: int
    monitor_keywords: dict[str, dict[str, Any]]
    deltas_by_id: dict[str, dict[str, Any]]
    snapshot_terms: list[dict[str, Any]]
    configured_keywords: set[str]
    keyword_registry_rows: list[dict[str, Any]]
    refresh_failures_by_keyword: dict[str, dict[str, Any]]
    monitor_generated_at: str
    account_score_method: str
    metric_meta_generated_at: str
    memory_text: str
    fingerprint: str


def _load_claim_ledger(settings: Any) -> dict[str, Any]:
    """读取 claims 账本；迁移自旧 ``data/state/agent_claims.json``，但来自 Hub 表。"""
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            """SELECT claim_id,claim_kind,subject_id,first_reported_at,last_reported_at,
                      last_direction,last_fingerprint,last_priority,last_state,
                      report_path,evidence_ids_json
               FROM wechat_agent_claims"""
        ).fetchall()
    items: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            evidence_ids = json.loads(row["evidence_ids_json"]) if row["evidence_ids_json"] else []
        except json.JSONDecodeError:
            evidence_ids = []
        items[row["claim_id"]] = {
            "candidate_id": row["claim_id"],
            "kind": row["claim_kind"],
            "subject": row["subject_id"],
            "first_reported_at": row["first_reported_at"],
            "last_reported_at": row["last_reported_at"],
            "last_direction": row["last_direction"],
            "last_fingerprint": row["last_fingerprint"],
            "last_priority": row["last_priority"],
            "last_state": row["last_state"],
            "report_path": row["report_path"],
            "evidence_ids": evidence_ids,
        }
    return {
        "schema_version": CLAIM_LEDGER_SCHEMA_VERSION,
        "updated_at": max((item["last_reported_at"] for item in items.values()), default=""),
        "items": items,
    }


def _keyword_observation_context(
    registry_item: dict[str, Any] | None,
    monitor_item: dict[str, Any] | None,
    as_of: datetime,
) -> dict[str, Any]:
    registry_item = registry_item or {}
    monitor_item = monitor_item or {}
    added_at = _parse_datetime(registry_item.get("added_at") or registry_item.get("created_at"))
    first_observed_at = _parse_datetime(registry_item.get("first_seen_at"))
    observation_started_at = max(
        (value for value in (added_at, first_observed_at) if value is not None),
        default=None,
    )
    calendar_days = _inclusive_calendar_days(observation_started_at, as_of)
    coverage_days = _integer(monitor_item.get("coverage_days") or monitor_item.get("observed_days"))
    observed_days = min(calendar_days, coverage_days) if calendar_days and coverage_days else 0
    if observed_days >= KEYWORD_TREND_STABLE_DAYS:
        stage = "stable"
    elif observed_days <= 1:
        stage = "new"
    else:
        stage = "maturing"
    return {
        "added_at": _iso(added_at),
        "first_observed_at": _iso(first_observed_at),
        "observation_started_at": _iso(observation_started_at),
        "calendar_days_in_scope": calendar_days,
        "coverage_days": coverage_days,
        "trend_observation_days": observed_days,
        "observation_stage": stage,
        "stable_after_days": KEYWORD_TREND_STABLE_DAYS,
        "stable_for_trend": observed_days >= KEYWORD_TREND_STABLE_DAYS,
    }


def _build_keyword_universe_context(
    registry_rows: list[dict[str, Any]],
    monitor_keywords: dict[str, dict[str, Any]],
    as_of: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    context_by_id: dict[str, dict[str, Any]] = {}
    keyword_rows: list[dict[str, Any]] = []
    for registry_item in registry_rows:
        keyword_id = str(registry_item.get("keyword_id") or "").strip()
        if not keyword_id:
            continue
        monitor_item = monitor_keywords.get(keyword_id) or {}
        observation = _keyword_observation_context(registry_item, monitor_item, as_of)
        context_by_id[keyword_id] = observation
        keyword_rows.append(
            {
                "keyword_id": keyword_id,
                "keyword": registry_item.get("keyword") or registry_item.get("keyword_text") or "",
                "topic": monitor_item.get("topic") or registry_item.get("topic") or "",
                **observation,
            }
        )

    previous_day = as_of.date() - timedelta(days=1)
    early_observation = []
    for item in keyword_rows:
        added_at = _parse_datetime(item.get("added_at"))
        if not item["stable_for_trend"]:
            early_observation.append(
                {
                    "keyword_id": item["keyword_id"],
                    "keyword": item["keyword"],
                    "added_at": item["added_at"],
                    "trend_observation_days": item["trend_observation_days"],
                    "observation_stage": item["observation_stage"],
                    "added_last_24h": bool(added_at and added_at >= as_of - timedelta(hours=24)),
                    "added_previous_calendar_day": bool(added_at and added_at.date() == previous_day),
                }
            )

    stable_count = sum(1 for item in keyword_rows if item["stable_for_trend"])
    added_last_24h = [item for item in early_observation if item["added_last_24h"]]
    added_previous_day = [item for item in early_observation if item["added_previous_calendar_day"]]
    return {
        "current_keyword_count": len(keyword_rows),
        "trend_stable_after_days": KEYWORD_TREND_STABLE_DAYS,
        "stable_keyword_count": stable_count,
        "early_observation_keyword_count": len(early_observation),
        "added_last_24h_count": len(added_last_24h),
        "added_previous_calendar_day_count": len(added_previous_day),
        "added_last_24h": added_last_24h,
    }, context_by_id


def _latest_observation_time(snapshots: list[dict[str, Any]], fallback: Any) -> datetime:
    values = [_parse_datetime(item.get("captured_at")) for item in snapshots]
    latest = max((value for value in values if value is not None), default=None)
    return latest or _parse_datetime(fallback) or datetime.now(_UTC).replace(tzinfo=None)


def _data_status(
    *,
    as_of: datetime,
    monitor_generated_at: Any,
    configured_keyword_count: int,
    stable_keyword_ids: set[str],
    snapshots: list[dict[str, Any]],
    keyword_registry_rows: list[dict[str, Any]],
    refresh_failures_by_keyword: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    recent_cutoff = as_of - timedelta(hours=24)
    latest_by_keyword: dict[str, datetime] = {}
    for snapshot in snapshots:
        keyword_id = str(snapshot.get("keyword_id") or "").strip()
        captured_at = _parse_datetime(snapshot.get("captured_at"))
        if not keyword_id or captured_at is None:
            continue
        prior = latest_by_keyword.get(keyword_id)
        if prior is None or captured_at > prior:
            latest_by_keyword[keyword_id] = captured_at

    observed_keyword_count = sum(1 for value in latest_by_keyword.values() if value >= recent_cutoff)
    observed_stable_keyword_count = sum(
        1
        for keyword_id in stable_keyword_ids
        if latest_by_keyword.get(keyword_id) is not None
        and latest_by_keyword[keyword_id] >= recent_cutoff
    )
    coverage_ratio = observed_keyword_count / configured_keyword_count if configured_keyword_count > 0 else 0
    stable_coverage_ratio = (
        observed_stable_keyword_count / len(stable_keyword_ids)
        if stable_keyword_ids
        else coverage_ratio
    )
    age_hours = max((now - as_of).total_seconds() / 3600, 0)
    if age_hours > 48 or stable_coverage_ratio < 0.7:
        mode = "blocked"
    elif age_hours > 24 or stable_coverage_ratio < 0.9:
        mode = "cautious"
    else:
        mode = "normal"

    warnings: list[str] = []
    if stable_coverage_ratio < 0.9:
        warnings.append("近24小时稳定监控词覆盖不足90%，请避免强结论。")
    elif coverage_ratio < 0.9:
        warnings.append(
            "总词表覆盖不足90%主要来自新加入关键词；稳定监控词覆盖完整，"
            "可判断稳定样本变化，但新词只用于建立基线。"
        )
    if age_hours > 24:
        warnings.append("最新快照超过24小时，禁止写成今日实时动向。")

    strong_conclusion_allowed = mode == "normal"
    registry_by_id = {
        str(row.get("keyword_id") or ""): row
        for row in keyword_registry_rows
        if row.get("keyword_id")
    }
    missing_ids = [
        keyword_id
        for keyword_id in registry_by_id
        if latest_by_keyword.get(keyword_id) is None
        or latest_by_keyword[keyword_id] < recent_cutoff
    ]
    missing_ids.sort(
        key=lambda keyword_id: (
            keyword_id not in stable_keyword_ids,
            str(registry_by_id[keyword_id].get("keyword") or ""),
        )
    )
    coverage_gaps = []
    for keyword_id in missing_ids[:100]:
        row = registry_by_id[keyword_id]
        failure = refresh_failures_by_keyword.get(keyword_id) or {}
        latest = latest_by_keyword.get(keyword_id)
        coverage_gaps.append(
            {
                "keyword_id": keyword_id,
                "keyword": row.get("keyword") or "",
                "topic": row.get("topic") or "",
                "keyword_bucket": row.get("keyword_bucket") or "",
                "stable_for_trend": keyword_id in stable_keyword_ids,
                "latest_observed_at": _iso(latest) if latest else None,
                "reason_code": failure.get("reason_code") or "not_observed_in_24h",
                "refresh_status": failure.get("status") or "missing",
                "error": failure.get("error") or "",
                "failure_occurred_at": failure.get("occurred_at") or "",
            }
        )
    missing_by_reason: dict[str, int] = {}
    for item in coverage_gaps:
        reason = str(item["reason_code"])
        missing_by_reason[reason] = missing_by_reason.get(reason, 0) + 1
    return {
        "mode": mode,
        "as_of": _iso(as_of),
        "monitor_generated_at": monitor_generated_at or "",
        "data_age_hours": round(age_hours, 2),
        "configured_keyword_count": configured_keyword_count,
        "recently_observed_keyword_count": observed_keyword_count,
        "recent_keyword_coverage_ratio": round(coverage_ratio, 4),
        "stable_keyword_count": len(stable_keyword_ids),
        "recently_observed_stable_keyword_count": observed_stable_keyword_count,
        "stable_keyword_coverage_ratio": round(stable_coverage_ratio, 4),
        "coverage_gaps": coverage_gaps,
        "coverage_gap_summary": {
            "total_missing_keyword_count": len(missing_ids),
            "returned_gap_count": len(coverage_gaps),
            "truncated": len(missing_ids) > len(coverage_gaps),
            "missing_by_reason": missing_by_reason,
        },
        "warnings": warnings,
        "strong_conclusion_allowed": strong_conclusion_allowed,
        "strong_conclusion_scope": (
            "stable_keywords_only"
            if strong_conclusion_allowed and coverage_ratio < 0.9
            else "all_observed_keywords"
            if strong_conclusion_allowed
            else "none"
        ),
    }


# 迁移自老投影的纯算法函数；不包含旧文件 I/O。

def _build_recent_hit_index(
    ranking_hits: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    monitor_keywords: dict[str, dict[str, Any]],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    snapshots_by_id = {
        item.get("snapshot_id"): item
        for item in snapshots
        if item.get("snapshot_id")
    }
    result: dict[str, dict[str, Any]] = {}

    for hit in ranking_hits:
        snapshot = snapshots_by_id.get(hit.get("snapshot_id"))
        captured_at = _parse_datetime(snapshot.get("captured_at") if snapshot else None)
        if captured_at is None or captured_at < cutoff:
            continue

        article_id = str(hit.get("article_id") or "").strip()
        if not article_id:
            continue
        keyword_id = str(snapshot.get("keyword_id") or "").strip() if snapshot else ""
        keyword_meta = monitor_keywords.get(keyword_id, {})
        keyword = str(keyword_meta.get("keyword") or "").strip()
        topic = str(keyword_meta.get("topic") or keyword).strip()
        rank = _integer(hit.get("rank"), default=99)

        entry = result.setdefault(
            article_id,
            {
                "snapshot_hit_count": 0,
                "best_rank": None,
                "latest_rank": None,
                "latest_seen_at": "",
                "keywords": {},
                "topics": Counter(),
            },
        )
        entry["snapshot_hit_count"] += 1
        if entry["best_rank"] is None or rank < entry["best_rank"]:
            entry["best_rank"] = rank
        if not entry["latest_seen_at"] or captured_at.isoformat() > entry["latest_seen_at"]:
            entry["latest_seen_at"] = _iso(captured_at)
            entry["latest_rank"] = rank

        keyword_entry = entry["keywords"].setdefault(
            keyword_id or keyword,
            {
                "keyword_id": keyword_id,
                "keyword": keyword,
                "topic": topic,
                "hit_count": 0,
                "best_rank": None,
                "latest_rank": None,
                "latest_seen_at": "",
            },
        )
        keyword_entry["hit_count"] += 1
        if keyword_entry["best_rank"] is None or rank < keyword_entry["best_rank"]:
            keyword_entry["best_rank"] = rank
        if not keyword_entry["latest_seen_at"] or captured_at.isoformat() > keyword_entry["latest_seen_at"]:
            keyword_entry["latest_seen_at"] = _iso(captured_at)
            keyword_entry["latest_rank"] = rank
        if topic:
            entry["topics"][topic] += 1

    normalized: dict[str, dict[str, Any]] = {}
    for article_id, entry in result.items():
        keyword_rows = sorted(
            entry["keywords"].values(),
            key=lambda item: (
                -_integer(item.get("hit_count")),
                _integer(item.get("best_rank"), default=99),
                item.get("keyword") or "",
            ),
        )
        topics = [
            topic
            for topic, _count in entry["topics"].most_common()
            if topic
        ]
        normalized[article_id] = {
            "snapshot_hit_count": entry["snapshot_hit_count"],
            "best_rank": entry["best_rank"],
            "latest_rank": entry["latest_rank"],
            "latest_seen_at": entry["latest_seen_at"],
            "keyword_count": len(keyword_rows),
            "keywords": keyword_rows[:6],
            "topics": topics[:6],
        }
    return normalized

def _article_fact(
    article: dict[str, Any],
    account: dict[str, Any],
    recent_footprint: dict[str, Any],
) -> dict[str, Any]:
    content_path = str(article.get("content_file_path") or "").strip()
    return {
        "article_id": article.get("article_id") or "",
        "title": article.get("title") or "",
        "account_id": article.get("account_id") or "",
        "account_name": account.get("canonical_name") or account.get("name") or "",
        "published_at": article.get("published_at") or "",
        "summary": _clip(article.get("summary"), 180),
        "content_hash": str(article.get("content_hash") or ""),
        "metrics": {
            "read_count": article.get("read_count"),
            "like_count": article.get("like_count"),
            "friends_follow_count": article.get("friends_follow_count"),
        },
        "search_footprint_24h": recent_footprint,
        "content": {
            "available": bool(content_path),
            "path": content_path,
        },
    }

def _article_brief_fact(article: dict[str, Any]) -> dict[str, Any]:
    footprint = article.get("search_footprint_24h") or {}
    return {
        "article_id": article.get("article_id") or "",
        "title": article.get("title") or "",
        "account_id": article.get("account_id") or "",
        "account_name": article.get("account_name") or "",
        "published_at": article.get("published_at") or "",
        "summary": _clip(article.get("summary"), 100),
        "metrics": article.get("metrics") or {},
        "search_footprint_24h": {
            "snapshot_hit_count": footprint.get("snapshot_hit_count"),
            "keyword_count": footprint.get("keyword_count"),
            "best_rank": footprint.get("best_rank"),
            "latest_rank": footprint.get("latest_rank"),
            "keywords": [
                item.get("keyword")
                for item in (footprint.get("keywords") or [])[:4]
                if item.get("keyword")
            ],
            "topics": (footprint.get("topics") or [])[:4],
        },
        "content": article.get("content") or {},
    }

def _build_recent_articles(
    articles: list[dict[str, Any]],
    accounts_by_id: dict[str, dict[str, Any]],
    recent_hit_index: dict[str, dict[str, Any]],
    cutoff: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    result: list[tuple[datetime, dict[str, Any]]] = []
    for article in articles:
        published_at = _parse_datetime(article.get("published_at"))
        if published_at is None or published_at < cutoff or published_at > as_of:
            continue
        article_id = str(article.get("article_id") or "").strip()
        footprint = recent_hit_index.get(
            article_id,
            {
                "snapshot_hit_count": 0,
                "best_rank": None,
                "latest_rank": None,
                "latest_seen_at": "",
                "keyword_count": 0,
                "keywords": [],
                "topics": [],
            },
        )
        result.append(
            (
                published_at,
                _article_fact(
                    article,
                    accounts_by_id.get(article.get("account_id"), {}),
                    footprint,
                ),
            )
        )

    result.sort(
        key=lambda item: (
            item[0],
            _integer(item[1]["search_footprint_24h"].get("snapshot_hit_count")),
            _integer(item[1]["metrics"].get("read_count")),
        ),
        reverse=True,
    )
    return [item for _published_at, item in result[:MAX_RECENT_ARTICLES]]

def _article_evidence_id(article_id: Any) -> str:
    return f"article_{str(article_id or '').replace('-', '_')}"

def _account_evidence_id(account_id: Any) -> str:
    return f"account_{str(account_id or '').replace('-', '_')}"

def _keyword_evidence_id(keyword_id: Any) -> str:
    return f"keyword_{str(keyword_id or '').replace('-', '_')}"

def _board_evidence(account: dict[str, Any], board: str) -> dict[str, Any]:
    keys = {
        "account": ("score", "score_delta", "account_score_hexagon"),
        "timeliness": ("timeliness_score", "timeliness_score_delta", "timeliness_score_hexagon"),
        "today": ("today_score", "today_score_delta", "today_score_hexagon"),
    }
    score_key, delta_key, hexagon_key = keys[board]
    hexagon = account.get(hexagon_key) or {}
    current = hexagon.get("current") or {}
    previous = hexagon.get("previous") or {}
    population = hexagon.get("population") or {}
    return {
        "label": hexagon.get("label") or score_key,
        "window": hexagon.get("window_label") or "",
        "score": account.get(score_key),
        "score_delta": account.get(delta_key),
        "benchmark_line": hexagon.get("benchmark_line"),
        "confidence": current.get("confidence"),
        "population": population.get("score") or {},
        "axes_current": current.get("axes") or {},
        "axes_delta": hexagon.get("delta") or {},
        "details_current": current.get("details") or {},
        "axes_previous": previous.get("axes") or {},
    }

def _account_observation_span(account: dict[str, Any]) -> int:
    hexagon = account.get("account_score_hexagon") or {}
    current = hexagon.get("current") or {}
    details = current.get("details") or {}
    return _integer(details.get("observation_span_days"))

def _account_evidence(account: dict[str, Any]) -> dict[str, Any]:
    observed_days = _account_observation_span(account)
    return {
        "schema_version": "agent_evidence_v1",
        "evidence_id": _account_evidence_id(account.get("account_id")),
        "kind": "account",
        "observed_scope": "当前监控关键词下的账号搜索排名表现。",
        "account": {
            "account_id": account.get("account_id") or "",
            "name": account.get("name") or "",
            "observation_span_days": observed_days,
            "identity_status": "观察期不足5天，不能据此认定为新号、改名号或矩阵号"
            if observed_days < 5
            else "已达到账号分的5天基础观察期",
            "boards": {
                "account": _board_evidence(account, "account"),
                "timeliness": _board_evidence(account, "timeliness"),
                "today": _board_evidence(account, "today"),
            },
        },
        "caveats": [
            "分数是相对监控样本的可复算表现，不是账号商业价值。",
            "涨跌只描述窗口内的结果变化；原因需要查证据，不能自动归因为断更或内容策略。",
        ],
    }

def _keyword_evidence(
    keyword: dict[str, Any],
    read_delta: dict[str, Any] | None,
) -> dict[str, Any]:
    latest_run = keyword.get("latest_run") or {}
    articles = []
    for article in (latest_run.get("articles") or [])[:10]:
        articles.append(
            {
                "rank": article.get("rank"),
                "title": article.get("title") or "",
                "account": article.get("account") or "",
                "article_id": article.get("article_id") or "",
                "published_at": article.get("published_at") or "",
                "read_count": article.get("read_count"),
                "like_count": article.get("like_count"),
                "content_path": article.get("content_path") or "",
            }
        )
    terms = latest_run.get("terms") or {}
    return {
        "schema_version": "agent_evidence_v1",
        "evidence_id": _keyword_evidence_id(keyword.get("keyword_id")),
        "kind": "keyword",
        "observed_scope": "当前监控关键词及其搜索结果快照。",
        "keyword": {
            "keyword_id": keyword.get("keyword_id") or "",
            "keyword": keyword.get("keyword") or "",
            "topic": keyword.get("topic") or "",
            "keyword_bucket": keyword.get("keyword_bucket") or "",
            "observation_context": keyword.get("observation_context") or {},
            "search_presence": {
                "today_best": keyword.get("today_best"),
                "today_result_count": keyword.get("today_count"),
                "coverage_days": keyword.get("coverage_days"),
                "tracked_accounts": keyword.get("tracked_accounts"),
                "article_count": keyword.get("article_count"),
                "history_hits": keyword.get("history_hits") or [],
            },
            "heat": keyword.get("heat_summary") or {},
            "heat_score": keyword.get("kw_score") or {},
            "reading_signal": _reading_signal_payload(read_delta),
            "latest_top_articles": articles,
            "latest_related_terms": {
                "suggestions": (terms.get("suggestions") or [])[:20],
                "related": (terms.get("related") or [])[:20],
            },
        },
        "caveats": [
            "趋势是近3天相对此前7天的代理信号，不是今天比昨天的真实搜索量。",
            "阅读增量是估算值，必须同时阅读数据质量字段。",
            "关联词只说明当前快照里出现过，不等于市场新增词。",
        ],
    }

def _reading_signal_payload(read_delta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(read_delta, dict):
        return {
            "status": "insufficient_data",
            "confidence_level": "insufficient",
        }
    return {
        "status": read_delta.get("status") or "unknown",
        "method": read_delta.get("method") or "",
        "window_start": read_delta.get("window_start") or "",
        "window_end": read_delta.get("window_end") or "",
        "window_days": read_delta.get("window_days"),
        "steady_read_median": read_delta.get("steady_read_median"),
        "read_delta_estimated": read_delta.get("read_delta_estimated"),
        "read_delta_raw": read_delta.get("read_delta_raw"),
        "trend_signal": read_delta.get("trend_signal"),
        "trend_label": read_delta.get("trend_label") or "观察中",
        "recent_vs_baseline_ratio": read_delta.get("recent_vs_baseline_ratio"),
        "term_momentum": read_delta.get("term_momentum"),
        "new_term_count": read_delta.get("new_term_count"),
        "rising_term_count": read_delta.get("rising_term_count"),
        "quality": {
            "confidence_score": read_delta.get("confidence_score"),
            "confidence_level": read_delta.get("confidence_level") or "insufficient",
            "coverage_ratio": read_delta.get("coverage_ratio"),
            "observed_share": read_delta.get("observed_share"),
            "estimated_share": read_delta.get("estimated_share"),
            "snapshot_count": read_delta.get("snapshot_count"),
        },
    }

def _account_brief(account: dict[str, Any]) -> dict[str, Any]:
    boards = {
        "account": _board_evidence(account, "account"),
        "timeliness": _board_evidence(account, "timeliness"),
        "today": _board_evidence(account, "today"),
    }
    return {
        "account_id": account.get("account_id") or "",
        "name": account.get("name") or "",
        "observation_span_days": _account_observation_span(account),
        "boards": {
            name: {
                "score": value.get("score"),
                "score_delta": value.get("score_delta"),
                "rank": (value.get("population") or {}).get("rank"),
                "percentile": (value.get("population") or {}).get("percentile"),
                "confidence": value.get("confidence"),
            }
            for name, value in boards.items()
        },
    }

def _account_movement_value(account: dict[str, Any]) -> tuple[str, int]:
    candidates = {
        "账号分": _integer(account.get("score_delta")),
        "时效分": _integer(account.get("timeliness_score_delta")),
        "当天分": _integer(account.get("today_score_delta")),
    }
    board, value = max(candidates.items(), key=lambda item: abs(item[1]))
    return board, value

def _make_candidate(
    *,
    kind: str,
    subject: str,
    direction: str,
    priority: int,
    facts: dict[str, Any],
    evidence_ids: list[str],
    signature: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = f"claim_{_stable_hash({'kind': kind, 'subject': subject})}"
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "subject": subject,
        "direction": direction,
        "report_priority": max(0, min(100, int(round(priority)))),
        "facts": facts,
        "evidence_ids": list(dict.fromkeys(item for item in evidence_ids if item)),
        "fingerprint": _stable_hash(signature),
        "scope": "监控样本内的可复算信号，不能直接外推为全行业事实。",
    }

def _attach_candidate_state(
    candidate: dict[str, Any],
    ledger_items: dict[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    prior = ledger_items.get(candidate["candidate_id"]) or {}
    last_reported_at = _parse_datetime(prior.get("last_reported_at"))
    hours_since_reported = (
        max((as_of - last_reported_at).total_seconds() / 3600, 0)
        if last_reported_at
        else None
    )

    if not prior:
        state = "emerging"
        eligible = True
    elif prior.get("last_direction") != candidate.get("direction"):
        state = "reversed"
        eligible = True
    elif prior.get("last_fingerprint") == candidate.get("fingerprint"):
        state = "persistent"
        eligible = hours_since_reported is None or hours_since_reported >= 72
    elif hours_since_reported is not None and hours_since_reported < 24:
        state = "updated_but_recently_reported"
        eligible = False
    elif hours_since_reported is not None and hours_since_reported >= 168:
        state = "reappeared"
        eligible = True
    else:
        state = "updated"
        eligible = True

    return {
        **candidate,
        "event_state": state,
        "report_eligible": eligible,
        "last_reported_at": prior.get("last_reported_at") or "",
        "last_report_path": prior.get("report_path") or "",
    }

def _build_account_outputs(accounts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    account_rows = [_account_brief(account) for account in accounts]
    board_rankings = {
        "account_score": sorted(
            account_rows,
            key=lambda item: (
                -_number(item["boards"]["account"].get("score")),
                item.get("name") or "",
            ),
        )[:8],
        "timeliness_score": sorted(
            account_rows,
            key=lambda item: (
                -_number(item["boards"]["timeliness"].get("score")),
                item.get("name") or "",
            ),
        )[:8],
        "today_score": sorted(
            account_rows,
            key=lambda item: (
                -_number(item["boards"]["today"].get("score")),
                item.get("name") or "",
            ),
        )[:8],
    }

    movement_rows = []
    for account in accounts:
        board, movement = _account_movement_value(account)
        if movement == 0:
            continue
        movement_rows.append((account, board, movement))

    qualified_rows = [
        item
        for item in movement_rows
        if _account_observation_span(item[0]) >= 3
    ]
    upward = sorted(
        (item for item in qualified_rows if item[2] > 0),
        key=lambda item: (
            item[2] * min(_account_observation_span(item[0]) / 5, 1),
            _number(item[0].get("timeliness_score")),
            _number(item[0].get("today_score")),
        ),
        reverse=True,
    )[:5]
    downward = sorted(
        (item for item in qualified_rows if item[2] < 0),
        key=lambda item: (
            item[2] * min(_account_observation_span(item[0]) / 5, 1),
            -_number(item[0].get("timeliness_score")),
            -_number(item[0].get("today_score")),
        ),
    )[:5]

    selected: dict[str, tuple[dict[str, Any], str, int]] = {}
    for account, board, movement in upward + downward:
        selected[str(account.get("account_id") or "")] = (account, board, movement)

    movements = []
    candidates = []
    for account, board, movement in selected.values():
        facts = _account_brief(account)
        facts["primary_change_board"] = board
        facts["primary_change"] = movement
        candidate_facts = {
            "name": facts["name"],
            "observation_span_days": facts["observation_span_days"],
            "primary_change_board": board,
            "primary_change": movement,
            "account_score": facts["boards"]["account"]["score"],
            "timeliness_score": facts["boards"]["timeliness"]["score"],
            "today_score": facts["boards"]["today"]["score"],
        }
        evidence_id = _account_evidence_id(account.get("account_id"))
        movements.append(facts)
        candidates.append(
            _make_candidate(
                kind="account_movement",
                subject=str(account.get("account_id") or account.get("name") or ""),
                direction="上升" if movement > 0 else "下降",
                priority=min(
                    100,
                    abs(movement) * 1.1 * min(_account_observation_span(account) / 5, 1) + 20,
                ),
                facts=candidate_facts,
                evidence_ids=[evidence_id],
                signature={
                    "board": board,
                    "direction": "up" if movement > 0 else "down",
                    "movement_band": int(abs(movement) // 3),
                    "account_observation_span_days": _account_observation_span(account),
                },
            )
        )

    return {
        "top_boards": board_rankings,
        "upward": [item for item in movements if item.get("primary_change", 0) > 0],
        "downward": [item for item in movements if item.get("primary_change", 0) < 0],
    }, candidates, set(selected)

def _keyword_signal_row(keyword: dict[str, Any], read_delta: dict[str, Any] | None) -> dict[str, Any]:
    reading = _reading_signal_payload(read_delta)
    observation = keyword.get("observation_context") or {}
    history_hits = keyword.get("history_hits") or []
    recent_three = sum(_integer(value) for value in history_hits[-3:])
    prior_seven = sum(_integer(value) for value in history_hits[-10:-3])
    return {
        "keyword_id": keyword.get("keyword_id") or "",
        "keyword": keyword.get("keyword") or "",
        "topic": keyword.get("topic") or "",
        "keyword_bucket": keyword.get("keyword_bucket") or "",
        "observation_context": {
            "added_at": observation.get("added_at") or "",
            "trend_observation_days": observation.get("trend_observation_days"),
            "observation_stage": observation.get("observation_stage") or "",
            "stable_for_trend": bool(observation.get("stable_for_trend")),
        },
        "trend": {
            "label": reading.get("trend_label"),
            "signal": reading.get("trend_signal"),
            "term_momentum": reading.get("term_momentum"),
            "new_term_count": reading.get("new_term_count"),
            "rising_term_count": reading.get("rising_term_count"),
        },
        "reading": {
            "steady_read_median": reading.get("steady_read_median"),
            "read_delta_estimated": reading.get("read_delta_estimated"),
            "confidence_level": (reading.get("quality") or {}).get("confidence_level"),
            "coverage_ratio": (reading.get("quality") or {}).get("coverage_ratio"),
            "observed_share": (reading.get("quality") or {}).get("observed_share"),
            "estimated_share": (reading.get("quality") or {}).get("estimated_share"),
            "snapshot_count": (reading.get("quality") or {}).get("snapshot_count"),
        },
        "search_presence": {
            "today_best": keyword.get("today_best"),
            "today_result_count": keyword.get("today_count"),
            "recent_3d_hit_count": recent_three,
            "prior_7d_hit_count": prior_seven,
            "coverage_days": keyword.get("coverage_days"),
            "tracked_accounts": keyword.get("tracked_accounts"),
            "article_count": keyword.get("article_count"),
        },
        "external_heat": {
            "wso_month_cover_count": (
                ((keyword.get("heat_summary") or {}).get("wso") or {}).get("month_cover_count")
            ),
            "dso_month_cover_count": (
                ((keyword.get("heat_summary") or {}).get("dso") or {}).get("month_cover_count")
            ),
            "wso_estimated": (keyword.get("kw_score") or {}).get("wso_estimated"),
        },
    }

def _build_keyword_outputs(
    monitor_keywords: list[dict[str, Any]],
    deltas_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for keyword in monitor_keywords:
        keyword_id = str(keyword.get("keyword_id") or "").strip()
        read_delta = deltas_by_id.get(keyword_id)
        if not isinstance(read_delta, dict) or read_delta.get("status") != "ok":
            continue
        row = _keyword_signal_row(keyword, read_delta)
        confidence = _confidence_value(row["reading"].get("confidence_level"))
        trend = _number(row["trend"].get("signal"))
        term_momentum = _number(row["trend"].get("term_momentum"))
        if confidence < 0.7:
            continue
        rows.append((row, keyword, read_delta))

    upward = sorted(
        (item for item in rows if _number(item[0]["trend"].get("signal")) > 0),
        key=lambda item: (
            _number(item[0]["trend"].get("signal")) * _confidence_value(item[0]["reading"].get("confidence_level")),
            _number(item[0]["trend"].get("term_momentum")),
        ),
        reverse=True,
    )[:8]
    downward = sorted(
        (item for item in rows if _number(item[0]["trend"].get("signal")) < 0),
        key=lambda item: (
            _number(item[0]["trend"].get("signal")) * _confidence_value(item[0]["reading"].get("confidence_level")),
            _number(item[0]["trend"].get("term_momentum")),
        ),
    )[:8]
    term_movers = sorted(
        rows,
        key=lambda item: (
            abs(_number(item[0]["trend"].get("term_momentum"))),
            _integer(item[0]["trend"].get("new_term_count")) + _integer(item[0]["trend"].get("rising_term_count")),
        ),
        reverse=True,
    )[:8]

    selected: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
    for row, keyword, read_delta in upward + downward + term_movers:
        selected[row["keyword_id"]] = (row, keyword, read_delta)

    candidates = []
    for row, keyword, read_delta in selected.values():
        trend = _number(row["trend"].get("signal"))
        term_momentum = _number(row["trend"].get("term_momentum"))
        direction = "上升" if trend > 0.03 else "下降" if trend < -0.03 else "关联词变化"
        confidence = _confidence_value(row["reading"].get("confidence_level"))
        priority = (
            abs(trend) * 42
            + abs(term_momentum) * 22
            + confidence * 24
            + min(
                _integer(row["trend"].get("new_term_count"))
                + _integer(row["trend"].get("rising_term_count")),
                8,
            )
            * 2
        )
        candidate_facts = {
            "keyword": row["keyword"],
            "trend_label": row["trend"]["label"],
            "trend_signal": row["trend"]["signal"],
            "term_momentum": row["trend"]["term_momentum"],
            "confidence_level": row["reading"]["confidence_level"],
            "read_delta_estimated": row["reading"]["read_delta_estimated"],
            "observed_share": row["reading"]["observed_share"],
            "estimated_share": row["reading"]["estimated_share"],
        }
        candidates.append(
            _make_candidate(
                kind="keyword_demand_signal",
                subject=row["keyword_id"],
                direction=direction,
                priority=priority,
                facts=candidate_facts,
                evidence_ids=[_keyword_evidence_id(row["keyword_id"])],
                signature={
                    "direction": direction,
                    "trend_band": round(trend, 1),
                    "term_band": round(term_momentum, 1),
                    "confidence": row["reading"].get("confidence_level"),
                },
            )
        )

    return {
        "rising": [row for row, _keyword, _delta in upward],
        "falling": [row for row, _keyword, _delta in downward],
        "term_movers": [row for row, _keyword, _delta in term_movers],
    }, candidates, set(selected)

def _same_draft_pre_detection(articles: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    hashed_ids: set[str] = set()
    all_ids: set[str] = set()
    for article in articles:
        article_id = str(article.get("article_id") or "").strip()
        if article_id:
            all_ids.add(article_id)
        content_hash = str(article.get("content_hash") or "").strip()
        if not content_hash or not article_id:
            continue
        hashed_ids.add(article_id)
        group = groups.setdefault(
            content_hash,
            {"article_ids": set(), "account_ids": set(), "articles": []},
        )
        group["article_ids"].add(article_id)
        account_id = str(article.get("account_id") or "").strip()
        if account_id:
            group["account_ids"].add(account_id)
        group["articles"].append(
            {
                "article_id": article_id,
                "title": article.get("title") or "",
                "account_id": account_id,
                "account_name": article.get("account_name") or "",
                "published_at": article.get("published_at") or "",
            }
        )
    matched = []
    for content_hash, group in groups.items():
        article_ids = sorted(group["article_ids"])
        account_ids = sorted(group["account_ids"])
        if len(article_ids) < 2 or len(account_ids) < 2:
            continue
        members = sorted(
            group["articles"],
            key=lambda item: (item["published_at"], item["article_id"]),
        )
        matched.append(
            {
                "content_hash": content_hash,
                "method": "exact_content_hash",
                "confidence": 1.0,
                "article_count": len(article_ids),
                "account_count": len(account_ids),
                "article_ids": article_ids[:12],
                "account_ids": account_ids[:12],
                "articles": members[:12],
            }
        )
    matched.sort(key=lambda item: (item["article_count"], item["account_count"]), reverse=True)
    if matched:
        status = "detected"
    elif hashed_ids:
        status = "not_detected"
    else:
        status = "insufficient_hash_coverage"
    return {
        "status": status,
        "method": "exact_content_hash_cross_account",
        "hashed_article_count": len(hashed_ids),
        "unhashed_article_count": max(0, len(all_ids) - len(hashed_ids)),
        "matched_hash_group_count": len(matched),
        "groups": matched[:3],
        "caveat": (
            "仅表示规范化正文内容哈希一致的跨账号重复；不证明账号归属、"
            "转载授权、协同行为或因果关系。"
        ),
    }


def _build_content_clusters(
    recent_articles: list[dict[str, Any]],
    keyword_context_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clusters: dict[str, dict[str, Any]] = {}
    for article in recent_articles:
        footprint = article.get("search_footprint_24h") or {}
        topics = footprint.get("topics") or []
        if not topics:
            topics = ["未归类监控词"]
        for topic in topics[:4]:
            cluster = clusters.setdefault(
                topic,
                {
                    "topic": topic,
                    "article_ids": set(),
                    "account_ids": set(),
                    "keywords": set(),
                    "keyword_contexts": {},
                    "snapshot_hit_count": 0,
                    "best_rank": None,
                    "top_articles": [],
                    "all_articles": [],
                },
            )
            cluster["article_ids"].add(article.get("article_id") or "")
            cluster["account_ids"].add(article.get("account_id") or "")
            cluster["snapshot_hit_count"] += _integer(footprint.get("snapshot_hit_count"))
            rank = footprint.get("best_rank")
            if rank is not None and (cluster["best_rank"] is None or rank < cluster["best_rank"]):
                cluster["best_rank"] = rank
            for keyword in footprint.get("keywords") or []:
                if keyword.get("keyword"):
                    cluster["keywords"].add(keyword["keyword"])
                keyword_id = str(keyword.get("keyword_id") or "").strip()
                if keyword_id and keyword_id in keyword_context_by_id:
                    cluster["keyword_contexts"][keyword_id] = {
                        "keyword_id": keyword_id,
                        "keyword": keyword.get("keyword") or "",
                        **keyword_context_by_id[keyword_id],
                    }
            cluster["all_articles"].append(article)
            cluster["top_articles"].append(
                {
                    "article_id": article.get("article_id") or "",
                    "title": article.get("title") or "",
                    "account_name": article.get("account_name") or "",
                    "published_at": article.get("published_at") or "",
                    "best_rank": footprint.get("best_rank"),
                    "read_count": (article.get("metrics") or {}).get("read_count"),
                }
            )

    rows = []
    for cluster in clusters.values():
        keyword_contexts = list(cluster["keyword_contexts"].values())
        stable_keywords = [
            item for item in keyword_contexts if item.get("stable_for_trend")
        ]
        early_keywords = [
            item for item in keyword_contexts if not item.get("stable_for_trend")
        ]
        early_share = (
            len(early_keywords) / len(keyword_contexts)
            if keyword_contexts
            else 0
        )
        articles = sorted(
            cluster["top_articles"],
            key=lambda item: (
                _integer(item.get("read_count")),
                -_integer(item.get("best_rank"), default=99),
                item.get("published_at") or "",
            ),
            reverse=True,
        )
        rows.append(
            {
                "topic": cluster["topic"],
                "article_count": len([item for item in cluster["article_ids"] if item]),
                "account_count": len([item for item in cluster["account_ids"] if item]),
                "keyword_count": len(cluster["keywords"]),
                "snapshot_hit_count": cluster["snapshot_hit_count"],
                "best_rank": cluster["best_rank"],
                "keywords": sorted(cluster["keywords"])[:12],
                "keyword_observation_mix": {
                    "observed_keyword_count": len(keyword_contexts),
                    "stable_keyword_count": len(stable_keywords),
                    "early_observation_keyword_count": len(early_keywords),
                    "early_observation_share": round(early_share, 4),
                    "all_observed_keywords_are_early": bool(keyword_contexts)
                    and not stable_keywords,
                    "early_observation_keywords": [
                        {
                            "keyword": item.get("keyword") or "",
                            "trend_observation_days": item.get("trend_observation_days"),
                            "observation_stage": item.get("observation_stage"),
                        }
                        for item in sorted(
                            early_keywords,
                            key=lambda item: (
                                item.get("trend_observation_days") or 0,
                                item.get("keyword") or "",
                            ),
                        )[:12]
                    ],
                },
                "_article_ids": cluster["article_ids"],
                "top_articles": articles[:3],
                "same_draft_matrix_pre_detection": _same_draft_pre_detection(
                    cluster["all_articles"]
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            item["article_count"],
            item["account_count"],
            item["snapshot_hit_count"],
        ),
        reverse=True,
    )
    selected_rows = []
    selected_article_ids: set[str] = set()
    for row in rows:
        article_ids = {item for item in row["_article_ids"] if item}
        overlap = len(article_ids & selected_article_ids) / max(len(article_ids), 1)
        if overlap >= 0.6:
            continue
        selected_rows.append(row)
        selected_article_ids.update(article_ids)
        if len(selected_rows) >= 8:
            break
    rows = selected_rows

    candidates = []
    for row in rows:
        if row["article_count"] < 2 and row["snapshot_hit_count"] < 4:
            continue
        evidence_ids = [_article_evidence_id(article.get("article_id")) for article in row["top_articles"]]
        candidate_facts = {
            "topic": row["topic"],
            "article_count": row["article_count"],
            "account_count": row["account_count"],
            "keyword_count": row["keyword_count"],
            "snapshot_hit_count": row["snapshot_hit_count"],
            "best_rank": row["best_rank"],
            "same_draft_matrix": {
                "status": row["same_draft_matrix_pre_detection"]["status"],
                "matched_hash_group_count": row["same_draft_matrix_pre_detection"][
                    "matched_hash_group_count"
                ],
                "hashed_article_count": row["same_draft_matrix_pre_detection"][
                    "hashed_article_count"
                ],
            },
            "keyword_observation_mix": {
                key: row["keyword_observation_mix"][key]
                for key in (
                    "observed_keyword_count",
                    "stable_keyword_count",
                    "early_observation_keyword_count",
                    "early_observation_share",
                    "all_observed_keywords_are_early",
                )
            },
        }
        candidates.append(
            _make_candidate(
                kind="content_cluster_24h",
                subject=row["topic"],
                direction="聚集",
                priority=(
                    row["article_count"] * 6
                    + row["account_count"] * 4
                    + min(row["snapshot_hit_count"], 25) * 1.3
                ),
                facts=candidate_facts,
                evidence_ids=evidence_ids,
                signature={
                    "article_band": min(row["article_count"], 8),
                    "account_band": min(row["account_count"], 6),
                    "best_rank": row["best_rank"],
                },
            )
        )
    return [
        {key: value for key, value in row.items() if key != "_article_ids"}
        for row in rows
    ], candidates

def _build_untracked_terms(
    snapshot_terms: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    configured_keywords: set[str],
    as_of: datetime,
    memory_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = as_of - timedelta(hours=24)
    snapshots_by_id = {
        item.get("snapshot_id"): item
        for item in snapshots
        if item.get("snapshot_id")
    }
    configured_normalized = {_clean_text(text) for text in configured_keywords}
    counters: dict[str, dict[str, Any]] = {}

    for item in snapshot_terms:
        snapshot = snapshots_by_id.get(item.get("snapshot_id"))
        captured_at = _parse_datetime(snapshot.get("captured_at") if snapshot else None)
        if captured_at is None or captured_at < cutoff:
            continue
        term = str(item.get("term_text") or "").strip()
        normalized = _clean_text(term)
        if not normalized or normalized in configured_normalized:
            continue
        if len(term) < 2 or len(term) > TERM_MAX_LENGTH:
            continue

        entry = counters.setdefault(
            term,
            {
                "term": term,
                "occurrence_count": 0,
                "source_keyword_ids": set(),
                "term_types": Counter(),
            },
        )
        entry["occurrence_count"] += 1
        if snapshot and snapshot.get("keyword_id"):
            entry["source_keyword_ids"].add(snapshot["keyword_id"])
        entry["term_types"][str(item.get("term_type") or "unknown")] += 1

    rows = []
    for entry in counters.values():
        source_count = len(entry["source_keyword_ids"])
        if entry["occurrence_count"] < 2 and source_count < 2:
            continue
        rows.append(
            {
                "term": entry["term"],
                "occurrence_count": entry["occurrence_count"],
                "source_keyword_count": source_count,
                "term_types": dict(entry["term_types"]),
                "memory_exact_match": entry["term"] in memory_text,
                "scope": "当前24小时监控词快照中的关联词/下拉词候选，不代表市场新增词。",
            }
        )
    rows.sort(
        key=lambda item: (
            item["source_keyword_count"],
            item["occurrence_count"],
            item["term"],
        ),
        reverse=True,
    )
    rows = rows[:12]

    gaps = [
        {
            "type": "untracked_related_term",
            "subject": row["term"],
            "reason": "词表外关联词在当前24小时快照中重复出现，且短期记忆未出现精确词条。",
            "evidence": {
                "occurrence_count": row["occurrence_count"],
                "source_keyword_count": row["source_keyword_count"],
            },
            "required_action": "准备采用该词前，先读取关联关键词证据或相关文章；不得直接宣称市场出现了新需求。",
        }
        for row in rows
        if not row["memory_exact_match"]
    ][:5]
    return rows, gaps

def _active_claims(ledger: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    active = []
    for candidate_id, item in (ledger.get("items") or {}).items():
        last_reported_at = _parse_datetime(item.get("last_reported_at"))
        if last_reported_at is None:
            continue
        if as_of - last_reported_at > timedelta(days=14):
            continue
        active.append(
            {
                "candidate_id": candidate_id,
                "kind": item.get("kind") or "",
                "subject": item.get("subject") or "",
                "last_direction": item.get("last_direction") or "",
                "last_reported_at": item.get("last_reported_at") or "",
                "report_path": item.get("report_path") or "",
                "last_state": item.get("last_state") or "",
            }
        )
    active.sort(key=lambda item: item["last_reported_at"], reverse=True)
    return active[:MAX_ACTIVE_CLAIMS]

def _build_brief_markdown(brief: dict[str, Any]) -> str:
    status = brief["data_status"]
    keyword_context = brief.get("keyword_universe_context") or {}
    lines = [
        "# 每日 Agent 观察包",
        "",
        f"- 生成时间：{brief['generated_at']}",
        f"- 数据截至：{status['as_of']}",
        f"- 数据状态：{status['mode']}",
        f"- 24 小时文章：{brief['summary']['recent_article_count']}",
        f"- 监控词：{brief['summary']['configured_keyword_count']}",
        f"- 近24小时新增监控词：{keyword_context.get('added_last_24h_count', 0)}",
        f"- 尚未满7天观察期：{keyword_context.get('early_observation_keyword_count', 0)}",
        f"- 账号：{brief['summary']['account_count']}",
        "",
        "## 使用规则",
        "",
        "- 先读 `daily_brief.json`，仅对准备写入日报的信号读取对应 `evidence_id`。",
        "- 候选信号只描述监控样本；必须区分事实、可计算信号与 Agent 判断。",
        "- `persistent` 且 `report_eligible=false` 的事件默认不重复推送。",
        "",
        "## 24小时内容聚集",
        "",
    ]
    for item in brief.get("content_clusters", [])[:8]:
        observation_mix = item.get("keyword_observation_mix") or {}
        lines.append(
            f"- {item['topic']}：{item['article_count']} 篇文章、{item['account_count']} 个账号、"
            f"{item['keyword_count']} 个关键词、最佳第 {item['best_rank'] or '—'} 名；"
            f"其中 {observation_mix.get('early_observation_keyword_count', 0)} 个词观察不足7天"
        )
    lines.extend(["", "## 候选事件", ""])
    for item in brief.get("event_candidates", [])[:12]:
        lines.append(
            f"- [{item['event_state']}] {item['kind']} / {item['subject']} / {item['direction']} "
            f"（优先级 {item['report_priority']}，可推送={item['report_eligible']}）"
        )
    lines.extend(["", "## 词表外关联词候选", ""])
    for item in brief.get("untracked_term_candidates", [])[:10]:
        lines.append(
            f"- {item['term']}：出现 {item['occurrence_count']} 次，来自 {item['source_keyword_count']} 个监控词"
        )
    return "\n".join(lines) + "\n"


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _latest_metrics(con: Any, article_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not article_ids:
        return {}
    result: dict[str, dict[str, Any]] = {item: {} for item in article_ids}
    metric_map = {
        "wechat.article.read_count": "read_count",
        "wechat.read_count": "read_count",
        "wechat.article.like_count": "like_count",
        "wechat.like_count": "like_count",
        "wechat.article.friends_follow_count": "friends_follow_count",
        "wechat.friends_follow_count": "friends_follow_count",
    }
    chunk_size = 400
    for start in range(0, len(article_ids), chunk_size):
        batch = article_ids[start : start + chunk_size]
        placeholders = ",".join("?" for _ in batch)
        rows = con.execute(
            f"""
            WITH ranked AS (
                SELECT subject_id,metric_key,numeric_value,observed_at,
                       ROW_NUMBER() OVER (
                         PARTITION BY subject_id,metric_key
                         ORDER BY observed_at DESC,observation_id DESC
                       ) AS rn
                FROM metric_observations
                WHERE subject_id IN ({placeholders})
                  AND metric_key IN ({','.join('?' for _ in metric_map)})
            )
            SELECT subject_id,metric_key,numeric_value FROM ranked WHERE rn=1
            """,
            [*batch, *metric_map],
        ).fetchall()
        for row in rows:
            key = metric_map[row["metric_key"]]
            # article.* 是新版规范 key，优先级高于兼容 key。
            current = result[row["subject_id"]].get(key)
            if current is None or str(row["metric_key"]).startswith("wechat.article."):
                result[row["subject_id"]][key] = row["numeric_value"]
    return result


def _asset_preview(settings: Any, relative_path: str) -> dict[str, Any]:
    path_text = str(relative_path or "").strip()
    if not path_text or Path(path_text).is_absolute() or ".." in Path(path_text).parts:
        return {"available": False, "path": path_text, "preview": ""}
    candidate = (Path(settings.asset_store_path).resolve() / path_text).resolve()
    root = Path(settings.asset_store_path).resolve()
    if root not in candidate.parents or candidate.suffix.lower() != ".md" or not candidate.is_file():
        return {"available": False, "path": path_text, "preview": ""}
    raw = candidate.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"available": True, "path": path_text, "preview": _clip(text, MAX_CONTENT_PREVIEW_CHARS)}


def _load_compact_monitor(settings: Any) -> dict[str, Any]:
    """只取早报需要的最新 keyword/account 投影，避免解码 200MB full projection。"""
    with connect(settings, readonly=True) as con:
        keyword_rows = con.execute(
            """
            WITH latest AS (
              SELECT subject_id,payload_json,updated_at,
                     ROW_NUMBER() OVER (PARTITION BY subject_id ORDER BY updated_at DESC,projection_id DESC) rn
              FROM wechat_legacy_projections WHERE projection_kind='keyword'
            )
            SELECT subject_id,updated_at,
                   json_extract(payload_json,'$.keyword_id') keyword_id,
                   json_extract(payload_json,'$.keyword') keyword,
                   json_extract(payload_json,'$.topic') topic,
                   json_extract(payload_json,'$.keyword_bucket') keyword_bucket,
                   json_extract(payload_json,'$.coverage_days') coverage_days,
                   json_extract(payload_json,'$.observed_days') observed_days,
                   json_extract(payload_json,'$.today_best') today_best,
                   json_extract(payload_json,'$.today_count') today_count,
                   json_extract(payload_json,'$.tracked_accounts') tracked_accounts,
                   json_extract(payload_json,'$.article_count') article_count,
                   json_extract(payload_json,'$.history_hits') history_hits,
                   json_extract(payload_json,'$.heat_summary') heat_summary,
                   json_extract(payload_json,'$.kw_score') kw_score,
                   json_extract(payload_json,'$.keyword_read_delta') keyword_read_delta,
                   json_extract(payload_json,'$.latest_run') latest_run
            FROM latest WHERE rn=1 ORDER BY subject_id
            """
        ).fetchall()
        account_rows = con.execute(
            """
            WITH latest AS (
              SELECT subject_id,payload_json,updated_at,
                     ROW_NUMBER() OVER (PARTITION BY subject_id ORDER BY updated_at DESC,projection_id DESC) latest_rn
              FROM wechat_legacy_projections WHERE projection_kind='account'
            ), scored AS (
              SELECT subject_id,payload_json,updated_at,
                     COALESCE(json_extract(payload_json,'$.score'),0) score,
                     COALESCE(json_extract(payload_json,'$.timeliness_score'),0) timeliness_score,
                     COALESCE(json_extract(payload_json,'$.today_score'),0) today_score,
                     MAX(
                       ABS(COALESCE(json_extract(payload_json,'$.score_delta'),0)),
                       ABS(COALESCE(json_extract(payload_json,'$.timeliness_score_delta'),0)),
                       ABS(COALESCE(json_extract(payload_json,'$.today_score_delta'),0))
                     ) movement,
                     ROW_NUMBER() OVER (ORDER BY COALESCE(json_extract(payload_json,'$.score'),0) DESC,subject_id) score_rn,
                     ROW_NUMBER() OVER (ORDER BY COALESCE(json_extract(payload_json,'$.timeliness_score'),0) DESC,subject_id) time_rn,
                     ROW_NUMBER() OVER (ORDER BY COALESCE(json_extract(payload_json,'$.today_score'),0) DESC,subject_id) today_rn,
                     ROW_NUMBER() OVER (ORDER BY MAX(
                       ABS(COALESCE(json_extract(payload_json,'$.score_delta'),0)),
                       ABS(COALESCE(json_extract(payload_json,'$.timeliness_score_delta'),0)),
                       ABS(COALESCE(json_extract(payload_json,'$.today_score_delta'),0))
                     ) DESC,subject_id) movement_rn
              FROM latest WHERE latest_rn=1
            )
            SELECT payload_json,updated_at FROM scored
            WHERE score_rn<=12 OR time_rn<=12 OR today_rn<=12 OR movement_rn<=40
            ORDER BY subject_id
            """
        ).fetchall()
        account_count = con.execute(
            """SELECT COUNT(DISTINCT subject_id) FROM wechat_legacy_projections
               WHERE projection_kind='account'"""
        ).fetchone()[0]

    keywords = []
    latest = ""
    json_fields = {"history_hits", "heat_summary", "kw_score", "keyword_read_delta", "latest_run"}
    for row in keyword_rows:
        item = {key: row[key] for key in row.keys() if key not in json_fields and key != "updated_at"}
        for key in json_fields:
            item[key] = _json_object(row[key]) if key not in {"history_hits"} else json.loads(row[key] or "[]")
        keywords.append(item)
        latest = max(latest, str(row["updated_at"] or ""))
    accounts = []
    for row in account_rows:
        payload = _json_object(row["payload_json"])
        if payload:
            accounts.append(payload)
        latest = max(latest, str(row["updated_at"] or ""))
    return {
        "generated_at": latest,
        "account_score_method": "three_board_breakthrough_v5_1",
        "keywords": keywords,
        "accounts": accounts,
        "account_population_count": int(account_count or 0),
    }


def load_hub_facts(
    settings: Any,
    *,
    memory_text: str = "",
    monitor_payload: dict[str, Any] | None = None,
) -> HubFacts:
    """从 Hub 一致事实水位构造投影输入，不触碰旧 source。"""
    monitor = monitor_payload or _load_compact_monitor(settings)
    monitor_keywords_raw = {
        str(item.get("keyword_id") or ""): dict(item)
        for item in monitor.get("keywords", [])
        if item.get("keyword_id")
    }
    monitor_accounts = monitor.get("accounts", []) or []

    with connect(settings, readonly=True) as con:
        snapshots = [
            dict(row)
            for row in con.execute(
                """SELECT snapshot_id,keyword_id,keyword,captured_at,trigger_type,result_count,
                          features_json,payload_json
                   FROM search_snapshots
                   WHERE platform='wechat-search'
                   ORDER BY captured_at,snapshot_id"""
            ).fetchall()
        ]
        as_of = _latest_observation_time(snapshots, monitor.get("generated_at"))
        recent_cutoff = _iso(as_of - timedelta(hours=24))
        hit_rows = con.execute(
            """SELECT h.snapshot_id,h.rank,h.content_id AS article_id,h.title_raw,
                      h.url_raw,h.creator_name_raw,h.payload_json
               FROM search_hits h
               JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
               WHERE s.platform='wechat-search' AND s.captured_at>=?
               ORDER BY s.captured_at,h.rank,h.hit_id""",
            (recent_cutoff,),
        ).fetchall()
        ranking_hits = [dict(row) for row in hit_rows]
        article_ids = sorted({str(row["article_id"]) for row in hit_rows if row["article_id"]})

        article_rows: list[Any] = []
        for offset in range(0, len(article_ids), 400):
            batch = article_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            article_rows.extend(
                con.execute(
                    f"""SELECT c.content_id AS article_id,c.title,c.creator_id AS account_id,
                               c.author_name,c.published_at,c.md_path AS content_file_path,
                               c.content_hash,c.payload_json,cr.canonical_name
                        FROM contents c
                        LEFT JOIN creators cr ON cr.creator_id=c.creator_id
                        WHERE c.content_id IN ({placeholders})""",
                    batch,
                ).fetchall()
            )
        metrics = _latest_metrics(con, article_ids)
        articles: list[dict[str, Any]] = []
        accounts_by_id: dict[str, dict[str, Any]] = {}
        for row in article_rows:
            payload = _json_object(row["payload_json"])
            account_id = str(row["account_id"] or "")
            accounts_by_id.setdefault(
                account_id,
                {"account_id": account_id, "canonical_name": row["canonical_name"] or row["author_name"] or ""},
            )
            articles.append(
                {
                    "article_id": row["article_id"],
                    "title": row["title"] or payload.get("title_raw") or "",
                    "account_id": account_id,
                    "published_at": row["published_at"] or "",
                    "summary": payload.get("summary_raw") or payload.get("summary") or "",
                    "content_file_path": row["content_file_path"] or "",
                    "content_hash": row["content_hash"] or payload.get("content_hash") or "",
                    **metrics.get(str(row["article_id"]), {}),
                }
            )

        registry_rows = [
            dict(row)
            for row in con.execute(
                """SELECT k.keyword_id,k.keyword,k.topic,k.keyword_bucket,k.status,k.first_seen_at,
                          k.updated_at,s.archived_at,s.payload_json AS setting_payload_json
                   FROM keywords k
                   LEFT JOIN search_keyword_settings s
                     ON s.keyword_id=k.keyword_id AND s.system_key='wechat-search'
                   WHERE k.platform='wechat-search'
                     AND k.status='active'
                     AND s.archived_at IS NULL
                   ORDER BY k.keyword,k.keyword_id"""
            ).fetchall()
        ]
        failure_cutoff = _iso(as_of - timedelta(hours=24))
        failure_rows = con.execute(
            """WITH ranked AS (
                   SELECT i.keyword_id,k.keyword,i.status,i.error_json,
                          COALESCE(i.finished_at,i.started_at,j.updated_at) AS occurred_at,
                          ROW_NUMBER() OVER (
                            PARTITION BY i.keyword_id
                            ORDER BY COALESCE(i.finished_at,i.started_at,j.updated_at) DESC,
                                     i.refresh_item_id DESC
                          ) AS rn
                   FROM search_refresh_items i
                   JOIN search_refresh_jobs j ON j.refresh_job_id=i.refresh_job_id
                   JOIN keywords k ON k.keyword_id=i.keyword_id
                   WHERE j.system_key='wechat-search'
                     AND i.status IN ('failed','blocked')
                     AND COALESCE(i.finished_at,i.started_at,j.updated_at)>=?
               )
               SELECT keyword_id,keyword,status,error_json,occurred_at
               FROM ranked WHERE rn=1""",
            (failure_cutoff,),
        ).fetchall()
        refresh_failures_by_keyword = {}
        for row in failure_rows:
            error = _json_object(row["error_json"])
            refresh_failures_by_keyword[str(row["keyword_id"])] = {
                "keyword_id": str(row["keyword_id"]),
                "keyword": str(row["keyword"] or ""),
                "status": str(row["status"]),
                "reason_code": str(error.get("reason_code") or row["status"]),
                "error": _clip(error.get("error"), 240),
                "occurred_at": str(row["occurred_at"] or ""),
            }

    snapshot_terms: list[dict[str, Any]] = []
    for snapshot in snapshots:
        features = _json_object(snapshot.get("features_json"))
        for term_type in ("suggestions", "related"):
            values = features.get(term_type, [])
            for item in values if isinstance(values, list) else []:
                if isinstance(item, dict):
                    term = item.get("term") or item.get("term_text")
                    position = item.get("position")
                else:
                    term, position = item, None
                if term:
                    snapshot_terms.append(
                        {
                            "snapshot_id": snapshot["snapshot_id"],
                            "term_type": term_type[:-1] if term_type.endswith("s") else term_type,
                            "term_text": str(term),
                            "position": position,
                        }
                    )

    as_of = _latest_observation_time(snapshots, monitor.get("generated_at"))
    _keyword_universe, contexts = _build_keyword_universe_context(registry_rows, monitor_keywords_raw, as_of)
    deltas_by_id: dict[str, dict[str, Any]] = {}
    monitor_keywords: dict[str, dict[str, Any]] = {}
    for keyword_id, item in monitor_keywords_raw.items():
        enriched = dict(item)
        enriched["observation_context"] = contexts.get(keyword_id) or {}
        monitor_keywords[keyword_id] = enriched
        delta = item.get("keyword_read_delta")
        if isinstance(delta, dict):
            deltas_by_id[keyword_id] = delta

    account_count = int(monitor.get("account_population_count") or len(monitor_accounts))
    fingerprint = _stable_hash(
        {
            "as_of": _iso(as_of),
            "snapshot_count": len(snapshots),
            "recent_hit_count": len(ranking_hits),
            "article_ids": article_ids,
            "monitor_generated_at": monitor.get("generated_at"),
            "account_count": account_count,
            "keyword_ids": sorted(monitor_keywords),
        },
        32,
    )
    return HubFacts(
        as_of=as_of,
        snapshots=snapshots,
        ranking_hits=ranking_hits,
        articles=articles,
        accounts_by_id=accounts_by_id,
        monitor_accounts=monitor_accounts,
        total_account_count=account_count,
        monitor_keywords=monitor_keywords,
        deltas_by_id=deltas_by_id,
        snapshot_terms=snapshot_terms,
        configured_keywords={str(row.get("keyword") or "").strip() for row in registry_rows if row.get("keyword")},
        keyword_registry_rows=registry_rows,
        refresh_failures_by_keyword=refresh_failures_by_keyword,
        monitor_generated_at=str(monitor.get("generated_at") or ""),
        account_score_method=str(monitor.get("account_score_method") or ""),
        metric_meta_generated_at=str(monitor.get("generated_at") or ""),
        memory_text=memory_text,
        fingerprint=fingerprint,
    )


def _article_evidence(settings: Any, article: dict[str, Any]) -> dict[str, Any]:
    content = _asset_preview(settings, str((article.get("content") or {}).get("path") or ""))
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_id": _article_evidence_id(article.get("article_id")),
        "kind": "recent_article",
        "observed_scope": "仅限当前监控关键词下的微信搜索命中与可抓取文章指标。",
        "article": article,
        "content_preview": content,
        "caveats": [
            "阅读、点赞和朋友在看是当前可观测文章指标，不等于推荐曝光。",
            "文章正文预览只用于理解文章明确写了什么，不可据此外推读者画像。",
        ],
    }


def _active_claims(ledger: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
    active = []
    for candidate_id, item in (ledger.get("items") or {}).items():
        last_reported_at = _parse_datetime(item.get("last_reported_at"))
        if last_reported_at is None or as_of - last_reported_at > timedelta(days=14):
            continue
        active.append(
            {
                "candidate_id": candidate_id,
                "kind": item.get("kind") or "",
                "subject": item.get("subject") or "",
                "last_direction": item.get("last_direction") or "",
                "last_reported_at": item.get("last_reported_at") or "",
                "report_path": item.get("report_path") or "",
                "last_state": item.get("last_state") or "",
            }
        )
    active.sort(key=lambda item: item["last_reported_at"], reverse=True)
    return active[:MAX_ACTIVE_CLAIMS]


def _load_previous_published_context(
    settings: Any,
    *,
    current_as_of: datetime,
) -> dict[str, Any] | None:
    with connect(settings, readonly=True) as con:
        run = con.execute(
            """SELECT run_id,brief_id,source_as_of,projection_version
               FROM wechat_agent_projection_runs
               WHERE status='published' AND source_as_of<?
               ORDER BY source_as_of DESC,published_at DESC LIMIT 1""",
            (_iso(current_as_of),),
        ).fetchone()
        if not run:
            return None
        brief_row = con.execute(
            """SELECT payload_json FROM wechat_aux_artifacts
               WHERE artifact_kind='daily_brief' AND subject_id=''
                 AND source_ref LIKE ?
               ORDER BY updated_at DESC LIMIT 1""",
            (f"%/{run['run_id']}/daily_brief/root",),
        ).fetchone()
        if not brief_row:
            return None
        decision = con.execute(
            """SELECT reported_claim_ids_json FROM wechat_agent_decisions
               WHERE run_id=? ORDER BY applied_at DESC LIMIT 1""",
            (run["run_id"],),
        ).fetchone()
    try:
        brief = json.loads(brief_row["payload_json"])
        reported = json.loads(decision["reported_claim_ids_json"]) if decision else []
    except (TypeError, json.JSONDecodeError):
        return None
    return {
        "run_id": str(run["run_id"]),
        "brief_id": str(run["brief_id"] or ""),
        "as_of": str(run["source_as_of"]),
        "projection_version": str(run["projection_version"]),
        "brief": brief if isinstance(brief, dict) else {},
        "reported_claim_ids": [str(item) for item in reported if str(item).strip()],
    }


def _recent_account_inventory(recent_articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for article in recent_articles:
        account_id = str(article.get("account_id") or "").strip()
        if not account_id:
            continue
        item = by_id.setdefault(
            account_id,
            {
                "account_id": account_id,
                "account_name": article.get("account_name") or "",
                "article_count": 0,
            },
        )
        item["article_count"] += 1
    return sorted(by_id.values(), key=lambda item: (-item["article_count"], item["account_id"]))


def _build_compared_to_last(
    previous: dict[str, Any] | None,
    *,
    current_status: dict[str, Any],
    current_articles: list[dict[str, Any]],
    current_accounts: list[dict[str, Any]],
    current_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not previous:
        return {"available": False, "reason_code": "no_previous_published_brief"}
    prior_brief = previous["brief"]
    prior_status = prior_brief.get("data_status") or {}
    eligible = bool(
        current_status.get("strong_conclusion_allowed")
        and prior_status.get("strong_conclusion_allowed")
    )
    caveats = []
    if not current_status.get("strong_conclusion_allowed"):
        caveats.append("current_coverage_not_comparable")
    if not prior_status.get("strong_conclusion_allowed"):
        caveats.append("previous_coverage_not_comparable")
    previous_accounts = {
        str(item.get("account_id") or ""): item
        for item in prior_brief.get("recent_accounts", [])
        if item.get("account_id")
    }
    current_by_account = {item["account_id"]: item for item in current_accounts}
    new_accounts = [current_by_account[key] for key in sorted(set(current_by_account) - set(previous_accounts))]
    disappeared = [
        {
            "account_id": key,
            "account_name": previous_accounts[key].get("account_name") or "",
            "previous_article_count": previous_accounts[key].get("article_count", 0),
        }
        for key in sorted(set(previous_accounts) - set(current_by_account))
    ]
    current_map = {str(item.get("candidate_id") or ""): item for item in current_candidates}
    prior_events = {
        str(item.get("candidate_id") or ""): item
        for item in prior_brief.get("event_candidates", [])
        if item.get("candidate_id")
    }
    assessments = {"verified": [], "refuted": [], "not_evaluable": []}
    for claim_id in previous.get("reported_claim_ids", [])[:20]:
        prior = prior_events.get(claim_id) or {}
        current = current_map.get(claim_id)
        base = {
            "claim_id": claim_id,
            "kind": prior.get("kind") or "",
            "subject": prior.get("subject") or "",
            "prior_direction": prior.get("direction") or "",
        }
        if not eligible:
            assessments["not_evaluable"].append({**base, "reason_code": "coverage_not_comparable"})
        elif current and current.get("direction") == prior.get("direction"):
            assessments["verified"].append(
                {
                    **base,
                    "current_direction": current.get("direction"),
                    "assessment": "consistent_observation",
                    "evidence_ids": current.get("evidence_ids", []),
                }
            )
        elif current:
            assessments["refuted"].append(
                {
                    **base,
                    "current_direction": current.get("direction"),
                    "assessment": "opposite_current_signal",
                    "evidence_ids": current.get("evidence_ids", []),
                }
            )
        else:
            assessments["not_evaluable"].append({**base, "reason_code": "claim_not_observed"})
    previous_count = int((prior_brief.get("summary") or {}).get("recent_article_count") or 0)
    current_count = len(current_articles)
    return {
        "available": True,
        "comparison_eligible": eligible,
        "baseline": {
            "run_id": previous["run_id"],
            "brief_id": previous["brief_id"],
            "as_of": previous["as_of"],
            "data_status_mode": prior_status.get("mode") or "",
        },
        "article_count": {
            "previous": previous_count,
            "current": current_count,
            "change": current_count - previous_count,
        },
        "accounts": {
            "previous_count": len(previous_accounts),
            "current_count": len(current_by_account),
            "new": new_accounts[:20],
            "disappeared": disappeared[:20] if eligible else [],
        },
        "prior_claims": {
            "evaluated_count": sum(len(items) for items in assessments.values()),
            **assessments,
        },
        "caveats": caveats,
    }


def build_projection(
    settings: Any,
    *,
    now: datetime | None = None,
    memory_text: str = "",
    monitor_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成内存 staging；无文件、数据库写入和通知副作用。"""
    generated_at = now or datetime.now(_UTC).replace(tzinfo=None)
    if generated_at.tzinfo:
        generated_at = generated_at.astimezone(_UTC).replace(tzinfo=None)
    facts = load_hub_facts(settings, memory_text=memory_text, monitor_payload=monitor_payload)
    as_of = facts.as_of
    cutoff = as_of - timedelta(hours=24)
    keyword_universe_context, keyword_context_by_id = _build_keyword_universe_context(
        facts.keyword_registry_rows, facts.monitor_keywords, as_of
    )
    monitor_keywords: dict[str, dict[str, Any]] = {}
    for keyword_id, item in facts.monitor_keywords.items():
        enriched = dict(item)
        enriched["observation_context"] = keyword_context_by_id.get(keyword_id) or {}
        monitor_keywords[keyword_id] = enriched

    ledger = _load_claim_ledger(settings)
    status = _data_status(
        as_of=as_of,
        monitor_generated_at=facts.monitor_generated_at,
        configured_keyword_count=len(facts.configured_keywords),
        stable_keyword_ids={
            keyword_id
            for keyword_id, context in keyword_context_by_id.items()
            if context.get("stable_for_trend")
        },
        snapshots=facts.snapshots,
        keyword_registry_rows=facts.keyword_registry_rows,
        refresh_failures_by_keyword=facts.refresh_failures_by_keyword,
        now=generated_at,
    )
    recent_hit_index = _build_recent_hit_index(
        facts.ranking_hits, facts.snapshots, monitor_keywords, cutoff
    )
    recent_articles = _build_recent_articles(
        facts.articles, facts.accounts_by_id, recent_hit_index, cutoff, as_of
    )
    content_clusters, cluster_candidates = _build_content_clusters(
        recent_articles, keyword_context_by_id
    )
    account_outputs, account_candidates, account_evidence_ids = _build_account_outputs(
        facts.monitor_accounts
    )
    keyword_outputs, keyword_candidates, keyword_evidence_ids = _build_keyword_outputs(
        list(monitor_keywords.values()), facts.deltas_by_id
    )
    untracked_terms, knowledge_gaps = _build_untracked_terms(
        facts.snapshot_terms,
        facts.snapshots,
        facts.configured_keywords,
        as_of,
        facts.memory_text,
    )

    grouped_candidates = {
        "content_cluster_24h": sorted(
            cluster_candidates, key=lambda item: item["report_priority"], reverse=True
        )[:8],
        "account_movement": sorted(
            account_candidates, key=lambda item: item["report_priority"], reverse=True
        )[:8],
        "keyword_demand_signal": sorted(
            keyword_candidates, key=lambda item: item["report_priority"], reverse=True
        )[:8],
    }
    candidate_events = [
        _attach_candidate_state(candidate, ledger["items"], as_of)
        for candidates in grouped_candidates.values()
        for candidate in candidates
    ]
    candidate_events.sort(
        key=lambda item: (
            not item["report_eligible"],
            -item["report_priority"],
            item["candidate_id"],
        )
    )
    candidate_events = candidate_events[:MAX_EVENT_CANDIDATES]
    recent_accounts = _recent_account_inventory(recent_articles)
    compared_to_last = _build_compared_to_last(
        _load_previous_published_context(settings, current_as_of=as_of),
        current_status=status,
        current_articles=recent_articles,
        current_accounts=recent_accounts,
        current_candidates=candidate_events,
    )

    selected_account_ids = {
        candidate["subject"]
        for candidate in candidate_events
        if candidate["kind"] == "account_movement"
    }
    selected_keyword_ids = {
        candidate["subject"]
        for candidate in candidate_events
        if candidate["kind"] == "keyword_demand_signal"
    }
    account_evidence_ids.update(selected_account_ids)
    keyword_evidence_ids.update(selected_keyword_ids)

    evidence: dict[str, dict[str, Any]] = {}
    for article in recent_articles[:MAX_ARTICLE_EVIDENCE]:
        item = _article_evidence(settings, article)
        evidence[item["evidence_id"]] = item
    accounts_by_monitor_id = {
        item.get("account_id"): item
        for item in facts.monitor_accounts
        if item.get("account_id")
    }
    for account_id in account_evidence_ids:
        account = accounts_by_monitor_id.get(account_id)
        if account:
            item = _account_evidence(account)
            evidence[item["evidence_id"]] = item
            if _account_observation_span(account) < 5:
                knowledge_gaps.append(
                    {
                        "type": "account_identity",
                        "subject": account.get("name") or account_id,
                        "reason": "账号基础观察期不足5天，身份与稳定性不能自动判断。",
                        "evidence_id": item["evidence_id"],
                        "required_action": "若准备作为黑马或新号案例写入报告，先读取该账号证据并使用谨慎措辞。",
                    }
                )
    for keyword_id in keyword_evidence_ids:
        keyword = monitor_keywords.get(keyword_id)
        if keyword:
            item = _keyword_evidence(keyword, facts.deltas_by_id.get(keyword_id))
            evidence[item["evidence_id"]] = item

    candidate_article_ids = {
        evidence_id
        for candidate in candidate_events
        for evidence_id in candidate.get("evidence_ids", [])
        if evidence_id.startswith("article_")
    }
    for article in recent_articles:
        evidence_id = _article_evidence_id(article.get("article_id"))
        if evidence_id not in candidate_article_ids:
            continue
        item = evidence.get(evidence_id) or {}
        preview = item.get("content_preview") or {}
        if article.get("summary") or not preview.get("available"):
            continue
        knowledge_gaps.append(
            {
                "type": "article_context",
                "subject": article.get("title") or article.get("article_id"),
                "reason": "标题和文章指标不足以理解内容事实，正文可读取。",
                "evidence_id": evidence_id,
                "required_action": "仅在准备采用该文章作为关键依据时，读取证据包中的正文预览。",
            }
        )

    run_id = f"run_{as_of.strftime('%Y%m%dT%H%M%S')}_{facts.fingerprint[:10]}_{_stable_hash(PROJECTION_VERSION, 4)}"
    brief_id = (
        f"brief_{as_of.strftime('%Y%m%d_%H%M%S')}_"
        f"{_stable_hash({'source': facts.fingerprint, 'version': PROJECTION_VERSION}, 8)}"
    )
    brief_status = {key: value for key, value in status.items() if key != "coverage_gaps"}
    brief = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "projection_version": PROJECTION_VERSION,
        "run_id": run_id,
        "brief_id": brief_id,
        "generated_at": _iso(generated_at),
        "data_status": brief_status,
        "observation_scope": {
            "search": "当前监控关键词下的微信搜索结果快照。",
            "articles": "被监控关键词命中的、且发布时间落在数据截至前24小时的文章。",
            "recommendation": "没有直接推荐曝光数据；只提供文章互动和内容聚集代理信号。",
            "industry": "所有信号仅覆盖监控样本，不能直接外推为行业全貌。",
        },
        "read_order": [
            "读取 manifest 并先核验 data_status 与 as_of。",
            "读取 metric dictionary。",
            "读取 daily brief。",
            "仅对准备写入报告的候选事件读取 evidence_id。",
            "读取晨报业务 MEMORY 与已推送判断账本去重。",
        ],
        "report_guardrails": [
            "每个核心判断都要附 evidence_id，并区分观察事实、可计算信号和Agent判断。",
            "不得由阅读、点赞推断年龄、资产、人群画像、推荐曝光或因果关系。",
            "persistent 且 report_eligible=false 的候选事件默认不重复推送。",
            "阅读估算必须同时交代 confidence_level、coverage_ratio、observed_share 与 estimated_share。",
        ],
        "metric_dictionary": {
            "api": "/api/agent/metric-dictionary",
            "schema_version": "agent_metric_dictionary_v1",
        },
        "algorithm_versions": {
            "account_score_method": facts.account_score_method,
            "keyword_read_method": next(
                (
                    str(item.get("method") or "")
                    for item in facts.deltas_by_id.values()
                    if item.get("method")
                ),
                "",
            ),
            "article_metric_meta_generated_at": facts.metric_meta_generated_at,
        },
        "summary": {
            "recent_article_count": len(recent_articles),
            "configured_keyword_count": len(facts.configured_keywords),
            "account_count": facts.total_account_count,
            "keyword_signal_count": len(keyword_outputs["rising"]) + len(keyword_outputs["falling"]),
            "active_claim_count": len(_active_claims(ledger, as_of)),
        },
        "keyword_universe_context": keyword_universe_context,
        "recent_articles": [
            _article_brief_fact(article)
            for article in recent_articles[:MAX_RECENT_ARTICLES_IN_BRIEF]
        ],
        "recent_accounts": recent_accounts,
        "compared_to_last": compared_to_last,
        "content_clusters": content_clusters,
        "keyword_signals": keyword_outputs,
        "account_boards": account_outputs["top_boards"],
        "account_movements": {
            "upward": account_outputs["upward"],
            "downward": account_outputs["downward"],
        },
        "untracked_term_candidates": untracked_terms,
        "coverage_gap_summary": status.get("coverage_gap_summary", {}),
        "knowledge_gaps": knowledge_gaps[:10],
        "active_claims": _active_claims(ledger, as_of),
        "event_candidates": candidate_events,
        "evidence_catalog": {
            "api_template": "/api/agent/evidence/<evidence_id>",
            "batch_api_template": "/api/agent/evidence?ids=<comma-separated-evidence_ids>",
            "article_evidence_count": sum(item.get("kind") == "recent_article" for item in evidence.values()),
            "account_evidence_count": sum(item.get("kind") == "account" for item in evidence.values()),
            "keyword_evidence_count": sum(item.get("kind") == "keyword" for item in evidence.values()),
        },
    }
    compact = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "projection_version": PROJECTION_VERSION,
        "run_id": run_id,
        "generated_at": _iso(generated_at),
        "source": {
            "system": "content-hub",
            "platform": "wechat-search",
            "source_fingerprint": facts.fingerprint,
            "as_of": _iso(as_of),
        },
        "brief": {
            "brief_id": brief_id,
            "api": "/api/agent/daily-brief",
            "bytes": len(compact),
            "sha256_12": hashlib.sha256(compact).hexdigest()[:12],
        },
        "data_status": status,
        "coverage_gaps": status.get("coverage_gaps", []),
        "required_endpoints": [
            "/api/agent/manifest",
            "/api/agent/metric-dictionary",
            "/api/agent/daily-brief",
        ],
        "evidence_api_template": "/api/agent/evidence/<evidence_id>",
        "claim_decision_command": (
            "python3 workbench/scripts/build_wechat_agent_daily_brief.py "
            "--apply-decision <decision.json>"
        ),
        "api": {
            "manifest": "/api/agent/manifest",
            "brief": "/api/agent/daily-brief",
            "metric_dictionary": "/api/agent/metric-dictionary",
            "evidence": "/api/agent/evidence/<evidence_id>",
            "evidence_batch": "/api/agent/evidence?ids=<comma-separated-evidence_ids>",
        },
    }
    staging = {
        "manifest": manifest,
        "brief": brief,
        "evidence": evidence,
        "facts": facts,
    }
    staging["validation"] = validate_staging(staging)
    return staging


def validate_staging(staging: dict[str, Any]) -> dict[str, Any]:
    manifest = staging.get("manifest") or {}
    brief = staging.get("brief") or {}
    evidence = staging.get("evidence") or {}
    errors: list[str] = []
    if manifest.get("brief", {}).get("brief_id") != brief.get("brief_id"):
        errors.append("manifest brief_id does not match daily brief")
    if not manifest.get("run_id") or manifest.get("run_id") != brief.get("run_id"):
        errors.append("manifest run_id does not match daily brief")
    compact = json.dumps(brief, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(compact) > 100_000:
        errors.append("daily brief exceeds 100KB compact-context budget")
    if not (brief.get("data_status") or {}).get("as_of"):
        errors.append("daily brief is missing data as_of")
    for evidence_id, payload in evidence.items():
        if not EVIDENCE_ID_RE.fullmatch(evidence_id):
            errors.append(f"invalid evidence id: {evidence_id}")
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > 256_000:
            errors.append(f"evidence exceeds 256KB budget: {evidence_id}")
    for candidate in brief.get("event_candidates", []):
        ids = candidate.get("evidence_ids") or []
        if not ids:
            errors.append(f"candidate has no evidence: {candidate.get('candidate_id')}")
        for evidence_id in ids:
            if evidence_id not in evidence:
                errors.append(f"missing evidence: {evidence_id}")
    return {
        "valid": not errors,
        "errors": errors,
        "brief_id": brief.get("brief_id"),
        "compact_brief_bytes": len(compact),
        "event_candidate_count": len(brief.get("event_candidates", [])),
        "evidence_count": len(evidence),
        "recent_article_total": int((brief.get("summary") or {}).get("recent_article_count") or 0),
        "recent_article_in_brief": len(brief.get("recent_articles", [])),
        "data_mode": (brief.get("data_status") or {}).get("mode"),
    }


def _artifact_row(
    *,
    kind: str,
    subject_id: str,
    payload: dict[str, Any],
    run_id: str,
    updated_at: str,
) -> tuple[str, str, str, str, str, str, str, str]:
    packed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(packed.encode("utf-8")).hexdigest()
    artifact_id = f"agent_{run_id}_{kind}_{_stable_hash(subject_id or 'root', 10)}"
    return (
        artifact_id,
        kind,
        subject_id,
        packed,
        digest,
        f"canonical://wechat-search/agent-projection/{run_id}/{kind}/{subject_id or 'root'}",
        PROJECTION_VERSION,
        updated_at,
    )


def publish_projection(settings: Any, staging: dict[str, Any]) -> dict[str, Any]:
    """校验并原子发布 staging；失败时线上继续读取上一版本。"""
    validation = validate_staging(staging)
    if not validation["valid"]:
        raise AgentProjectionValidation("; ".join(validation["errors"]))
    facts: HubFacts = staging["facts"]
    run_id = str(staging["manifest"].get("run_id") or "")
    if not run_id or staging["brief"].get("run_id") != run_id:
        raise AgentProjectionValidation("manifest/brief run_id mismatch")
    started_at = _now_iso()
    rows = [
        _artifact_row(
            kind="daily_brief",
            subject_id="",
            payload=staging["brief"],
            run_id=run_id,
            updated_at=started_at,
        )
    ]
    for evidence_id, payload in sorted(staging["evidence"].items()):
        rows.append(
            _artifact_row(
                kind="evidence",
                subject_id=evidence_id,
                payload=payload,
                run_id=run_id,
                updated_at=started_at,
            )
        )
    # manifest 与其引用的 brief/evidence 同事务提交；插入顺序仅帮助人工审计。
    rows.append(
        _artifact_row(
            kind="manifest",
            subject_id="",
            payload=staging["manifest"],
            run_id=run_id,
            updated_at=started_at,
        )
    )

    with writer_lock(settings.lock_path):
        with connect(settings) as con, transaction(con):
            existing = con.execute(
                """SELECT run_id,status,brief_id,artifact_count,evidence_count,validation_json,
                          source_as_of,source_fingerprint,published_at
                   FROM wechat_agent_projection_runs
                   WHERE source_fingerprint=? AND projection_version=?""",
                (facts.fingerprint, PROJECTION_VERSION),
            ).fetchone()
            if existing and existing["status"] == "published":
                return {
                    "run_id": existing["run_id"],
                    "status": "published",
                    "replayed": True,
                    "brief_id": existing["brief_id"],
                    "artifact_count": existing["artifact_count"],
                    "evidence_count": existing["evidence_count"],
                    "source_as_of": existing["source_as_of"],
                    "source_fingerprint": existing["source_fingerprint"],
                    "published_at": existing["published_at"],
                    "validation": json.loads(existing["validation_json"]),
                }
            if existing:
                run_id = str(existing["run_id"])
                con.execute(
                    """UPDATE wechat_agent_projection_runs
                       SET status='validated',brief_id=?,artifact_count=?,evidence_count=?,
                           validation_json=?,error_json=NULL,completed_at=?
                       WHERE run_id=?""",
                    (
                        staging["brief"]["brief_id"],
                        len(rows),
                        len(staging["evidence"]),
                        json.dumps(validation, ensure_ascii=False, sort_keys=True),
                        started_at,
                        run_id,
                    ),
                )
                # artifact IDs were made from the deterministic run_id above. When a prior
                # rejected run exists, rebuild rows using the persisted ID.
                rows = [
                    _artifact_row(
                        kind=kind,
                        subject_id=subject,
                        payload=payload,
                        run_id=run_id,
                        updated_at=started_at,
                    )
                    for kind, subject, payload in [
                        ("daily_brief", "", staging["brief"]),
                        *[("evidence", eid, value) for eid, value in sorted(staging["evidence"].items())],
                        ("manifest", "", staging["manifest"]),
                    ]
                ]
            else:
                con.execute(
                    """INSERT INTO wechat_agent_projection_runs(
                         run_id,status,source_as_of,source_fingerprint,projection_version,
                         brief_id,artifact_count,evidence_count,validation_json,started_at,completed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        "validated",
                        _iso(facts.as_of),
                        facts.fingerprint,
                        PROJECTION_VERSION,
                        staging["brief"]["brief_id"],
                        len(rows),
                        len(staging["evidence"]),
                        json.dumps(validation, ensure_ascii=False, sort_keys=True),
                        started_at,
                        started_at,
                    ),
                )
            con.executemany(
                """INSERT INTO wechat_aux_artifacts(
                     artifact_id,artifact_kind,subject_id,payload_json,source_hash,
                     source_ref,model_version,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(artifact_kind,subject_id,source_hash) DO UPDATE SET
                     payload_json=excluded.payload_json,source_ref=excluded.source_ref,
                     model_version=excluded.model_version,updated_at=excluded.updated_at""",
                rows,
            )
            con.execute(
                """UPDATE wechat_agent_projection_runs
                   SET status='published',published_at=?,completed_at=?
                   WHERE run_id=?""",
                (started_at, started_at, run_id),
            )

    return {
        "run_id": run_id,
        "status": "published",
        "replayed": False,
        "brief_id": staging["brief"]["brief_id"],
        "artifact_count": len(rows),
        "evidence_count": len(staging["evidence"]),
        "source_as_of": _iso(facts.as_of),
        "source_fingerprint": facts.fingerprint,
        "published_at": started_at,
        "validation": validation,
    }


def apply_decision(settings: Any, decision: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    """幂等应用已推送判断，推进 claims；不发送通知。"""
    key = str(idempotency_key or "").strip()
    if not key:
        raise AgentProjectionValidation("decision requires idempotency_key")
    brief_id = str(decision.get("brief_id") or "").strip()
    run_id = str(decision.get("run_id") or "").strip()
    raw_reported_ids = decision.get("reported_claim_ids")
    if raw_reported_ids is None:
        raw_reported_ids = decision.get("claim_ids", [])
    reported_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in raw_reported_ids
            if str(item).strip()
        )
    )
    report_path = str(
        decision.get("report_path") or decision.get("formal_body_path") or ""
    ).strip()
    if str(decision.get("decision") or "").strip().lower() == "publish" and not reported_ids:
        raise AgentProjectionValidation("publish decision requires claim ids")
    applied_at = _now_iso()

    with writer_lock(settings.lock_path):
        with connect(settings) as con, transaction(con):
            prior = con.execute(
                "SELECT decision_id,reported_claim_ids_json,applied_at FROM wechat_agent_decisions WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if prior:
                return {
                    "decision_id": prior["decision_id"],
                    "replayed": True,
                    "reported_claim_ids": json.loads(prior["reported_claim_ids_json"]),
                    "applied_at": prior["applied_at"],
                }
            run = con.execute(
                "SELECT run_id,brief_id,status FROM wechat_agent_projection_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run or run["status"] != "published" or run["brief_id"] != brief_id:
                raise AgentProjectionValidation("decision run/brief does not match a published projection")
            brief_row = con.execute(
                """SELECT payload_json FROM wechat_aux_artifacts
                   WHERE artifact_kind='daily_brief' AND subject_id=''
                     AND source_ref LIKE ? ORDER BY updated_at DESC,artifact_id DESC LIMIT 1""",
                (f"%/{run_id}/daily_brief/root",),
            ).fetchone()
            if not brief_row:
                raise AgentProjectionValidation("published daily brief is missing")
            brief = json.loads(brief_row["payload_json"])
            candidate_by_id = {
                item.get("candidate_id"): item
                for item in brief.get("event_candidates", [])
                if item.get("candidate_id")
            }
            unknown = [item for item in reported_ids if item not in candidate_by_id]
            if unknown:
                raise AgentProjectionValidation(f"unknown claim ids: {', '.join(unknown)}")
            for claim_id in reported_ids:
                candidate = candidate_by_id[claim_id]
                con.execute(
                    """INSERT INTO wechat_agent_claims(
                         claim_id,claim_kind,subject_id,first_reported_at,last_reported_at,
                         last_direction,last_fingerprint,last_priority,last_state,report_path,
                         evidence_ids_json,source_run_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(claim_id) DO UPDATE SET
                         last_reported_at=excluded.last_reported_at,
                         last_direction=excluded.last_direction,
                         last_fingerprint=excluded.last_fingerprint,
                         last_priority=excluded.last_priority,
                         last_state=excluded.last_state,
                         report_path=excluded.report_path,
                         evidence_ids_json=excluded.evidence_ids_json,
                         source_run_id=excluded.source_run_id,
                         updated_at=excluded.updated_at""",
                    (
                        claim_id,
                        candidate.get("kind") or "",
                        candidate.get("subject") or "",
                        applied_at,
                        applied_at,
                        candidate.get("direction") or "",
                        candidate.get("fingerprint") or "",
                        candidate.get("report_priority"),
                        candidate.get("event_state") or "",
                        report_path,
                        json.dumps(candidate.get("evidence_ids") or [], ensure_ascii=False),
                        run_id,
                        applied_at,
                    ),
                )
            decision_id = f"decision_{uuid.uuid4().hex}"
            con.execute(
                """INSERT INTO wechat_agent_decisions(
                     decision_id,idempotency_key,run_id,brief_id,decision_json,
                     reported_claim_ids_json,report_path,applied_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    key,
                    run_id,
                    brief_id,
                    json.dumps(decision, ensure_ascii=False, sort_keys=True),
                    json.dumps(reported_ids, ensure_ascii=False),
                    report_path,
                    applied_at,
                ),
            )
    return {
        "decision_id": decision_id,
        "replayed": False,
        "run_id": run_id,
        "brief_id": brief_id,
        "reported_count": len(reported_ids),
        "reported_claim_ids": reported_ids,
        "applied_at": applied_at,
    }


def import_claim_ledger(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """一次性幂等导入旧 claims；保持稳定 ID 和历史报告时间。"""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        raise AgentProjectionValidation("claim ledger must contain an items object")
    imported = 0
    now = _now_iso()
    with writer_lock(settings.lock_path):
        with connect(settings) as con, transaction(con):
            for claim_id, item in items.items():
                if not isinstance(item, dict) or not str(claim_id).strip():
                    continue
                first = str(item.get("first_reported_at") or item.get("last_reported_at") or now)
                last = str(item.get("last_reported_at") or first)
                con.execute(
                    """INSERT INTO wechat_agent_claims(
                         claim_id,claim_kind,subject_id,first_reported_at,last_reported_at,
                         last_direction,last_fingerprint,last_priority,last_state,report_path,
                         evidence_ids_json,source_run_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                       ON CONFLICT(claim_id) DO UPDATE SET
                         first_reported_at=excluded.first_reported_at,
                         last_reported_at=excluded.last_reported_at,
                         last_direction=excluded.last_direction,
                         last_fingerprint=excluded.last_fingerprint,
                         last_priority=excluded.last_priority,
                         last_state=excluded.last_state,
                         report_path=excluded.report_path,
                         evidence_ids_json=excluded.evidence_ids_json,
                         updated_at=excluded.updated_at""",
                    (
                        str(claim_id),
                        str(item.get("kind") or ""),
                        str(item.get("subject") or ""),
                        first,
                        last,
                        str(item.get("last_direction") or ""),
                        str(item.get("last_fingerprint") or ""),
                        item.get("last_priority"),
                        str(item.get("last_state") or ""),
                        str(item.get("report_path") or ""),
                        json.dumps(item.get("evidence_ids") or [], ensure_ascii=False),
                        now,
                    ),
                )
                imported += 1
    return {"imported": imported, "total": len(items), "updated_at": now}


def build_brief_markdown(brief: dict[str, Any]) -> str:
    return _build_brief_markdown(brief)


__all__ = [
    "AgentProjectionError",
    "AgentProjectionValidation",
    "HubFacts",
    "apply_decision",
    "build_brief_markdown",
    "build_projection",
    "import_claim_ledger",
    "load_hub_facts",
    "publish_projection",
    "validate_staging",
]
