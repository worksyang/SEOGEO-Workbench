from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECTION_VERSION = "agent_observation_protocol_v1"
BRIEF_SCHEMA_VERSION = "agent_daily_brief_v1"
MAX_RECENT_ARTICLES = 100
MAX_RECENT_ARTICLES_IN_BRIEF = 20
MAX_ARTICLE_EVIDENCE = 100
MAX_EVENT_CANDIDATES = 24
MAX_ACTIVE_CLAIMS = 20
MAX_CONTENT_PREVIEW_CHARS = 2400
TERM_MAX_LENGTH = 36
KEYWORD_TREND_STABLE_DAYS = 7


def _project_root(project_root: Path | str | None = None) -> Path:
    if project_root is not None:
        return Path(project_root).resolve()
    return Path(__file__).resolve().parent.parent.parent


def _agent_dir(project_root: Path) -> Path:
    return project_root / "data" / "agent"


def _state_dir(project_root: Path) -> Path:
    return project_root / "data" / "state"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


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


def _file_digest(path: Path, length: int = 12) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()[:length]


def _clean_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s·•|｜:：,，。！？!?、\-—_（）()\[\]【】<>《》“”\"'‘’`]+", "", text)
    return text


def _confidence_value(level: Any) -> float:
    return {
        "high": 1.0,
        "medium": 0.7,
        "low": 0.4,
        "insufficient": 0.0,
    }.get(str(level or "").strip().lower(), 0.0)


def _load_keyword_registry_rows(project_root: Path) -> list[dict[str, Any]]:
    from app.repositories.keyword_registry_repo import KeywordRegistryRepository

    return KeywordRegistryRepository(
        project_root / "data" / "state" / "app.db"
    ).list_keywords(include_archived=False)


def _inclusive_calendar_days(started_at: datetime | None, as_of: datetime) -> int:
    if started_at is None:
        return 0
    return max((as_of.date() - started_at.date()).days + 1, 1)


def _keyword_observation_context(
    registry_item: dict[str, Any] | None,
    monitor_item: dict[str, Any] | None,
    as_of: datetime,
) -> dict[str, Any]:
    registry_item = registry_item or {}
    monitor_item = monitor_item or {}
    added_at = _parse_datetime(registry_item.get("created_at"))
    first_observed_at = _parse_datetime(registry_item.get("first_seen_at"))
    observation_started_at = max(
        (value for value in (added_at, first_observed_at) if value is not None),
        default=None,
    )
    calendar_days = _inclusive_calendar_days(observation_started_at, as_of)
    coverage_days = _integer(monitor_item.get("coverage_days"))
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
    keyword_rows = []
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
                "keyword": registry_item.get("keyword_text") or "",
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
                    "added_last_24h": bool(
                        added_at and added_at >= as_of - timedelta(hours=24)
                    ),
                    "added_previous_calendar_day": bool(
                        added_at and added_at.date() == previous_day
                    ),
                }
            )

    def sort_recent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (
                item.get("observation_started_at") or "",
                item.get("keyword") or "",
            ),
            reverse=True,
        )

    stable_count = sum(1 for item in keyword_rows if item["stable_for_trend"])
    added_last_24h = [item for item in early_observation if item["added_last_24h"]]
    added_previous_day = [
        item for item in early_observation if item["added_previous_calendar_day"]
    ]
    return {
        "current_keyword_count": len(keyword_rows),
        "trend_stable_after_days": KEYWORD_TREND_STABLE_DAYS,
        "stable_keyword_count": stable_count,
        "early_observation_keyword_count": len(keyword_rows) - stable_count,
        "added_last_24h_count": len(added_last_24h),
        "added_last_24h_keywords": [
            item["keyword"] for item in sort_recent(added_last_24h)[:40]
        ],
        "added_previous_calendar_day_count": len(added_previous_day),
        "added_previous_calendar_day_keywords": [
            item["keyword"] for item in sort_recent(added_previous_day)[:40]
        ],
        "early_observation_keywords": sort_recent(early_observation)[:40],
        "interpretation_note": (
            "关键词加入会扩大系统可见范围。观察期不足7天的词所带来的内容增量，"
            "首先说明监控口径发生变化；是否同时存在市场变化，需要结合稳定关键词、"
            "连续多日表现或独立行业事实判断。"
        ),
    }, context_by_id


def _load_claim_ledger(project_root: Path) -> dict[str, Any]:
    path = _state_dir(project_root) / "agent_claims.json"
    payload = _read_json(path, default={}) or {}
    if not isinstance(payload, dict):
        payload = {}
    items = payload.get("items")
    if not isinstance(items, dict):
        items = {}
    return {
        "schema_version": "agent_claim_ledger_v1",
        "updated_at": payload.get("updated_at") or "",
        "items": items,
    }


def _load_article_content_preview(project_root: Path, content_path: Any) -> dict[str, Any]:
    relative_path = str(content_path or "").strip()
    if not relative_path:
        return {"available": False, "path": "", "preview": ""}

    root = project_root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        return {"available": False, "path": relative_path, "preview": ""}
    if candidate.suffix.lower() != ".md" or not candidate.exists():
        return {"available": False, "path": relative_path, "preview": ""}

    try:
        raw = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"available": False, "path": relative_path, "preview": ""}

    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {
        "available": True,
        "path": relative_path,
        "preview": _clip(text, MAX_CONTENT_PREVIEW_CHARS),
    }


def _latest_observation_time(snapshots: list[dict[str, Any]], fallback: Any) -> datetime:
    values = [_parse_datetime(item.get("captured_at")) for item in snapshots]
    latest = max((value for value in values if value is not None), default=None)
    return latest or _parse_datetime(fallback) or datetime.now()


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


def _article_evidence(
    project_root: Path,
    article: dict[str, Any],
) -> dict[str, Any]:
    content = _load_article_content_preview(project_root, article.get("content", {}).get("path"))
    return {
        "schema_version": "agent_evidence_v1",
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


def _data_status(
    *,
    as_of: datetime,
    generated_at: Any,
    configured_keyword_count: int,
    stable_keyword_ids: set[str],
    snapshots: list[dict[str, Any]],
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
    coverage_ratio = (
        observed_keyword_count / configured_keyword_count
        if configured_keyword_count > 0
        else 0
    )
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

    warnings = []
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
    return {
        "mode": mode,
        "as_of": _iso(as_of),
        "monitor_generated_at": generated_at or "",
        "data_age_hours": round(age_hours, 2),
        "configured_keyword_count": configured_keyword_count,
        "recently_observed_keyword_count": observed_keyword_count,
        "recent_keyword_coverage_ratio": round(coverage_ratio, 4),
        "stable_keyword_count": len(stable_keyword_ids),
        "recently_observed_stable_keyword_count": observed_stable_keyword_count,
        "stable_keyword_coverage_ratio": round(stable_coverage_ratio, 4),
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


def build_agent_projection(project_root: Path | str | None = None, now: datetime | None = None) -> dict[str, Any]:
    root = _project_root(project_root)
    normalized_dir = root / "normalized"
    agent_dir = _agent_dir(root)
    evidence_dir = agent_dir / "evidence"
    monitor = _read_json(normalized_dir / "monitor-data.json")
    if not isinstance(monitor, dict):
        raise FileNotFoundError(f"monitor data not found: {normalized_dir / 'monitor-data.json'}")

    articles = _read_json(normalized_dir / "articles.json", default=[]) or []
    accounts = _read_json(normalized_dir / "accounts.json", default=[]) or []
    snapshots = _read_json(normalized_dir / "snapshots.json", default=[]) or []
    ranking_hits = _read_json(normalized_dir / "ranking_hits.json", default=[]) or []
    snapshot_terms = _read_json(normalized_dir / "snapshot_terms.json", default=[]) or []
    read_deltas = _read_json(normalized_dir / "keyword_read_deltas.json", default=[]) or []
    metric_meta = _read_json(normalized_dir / "article_metric_observations_meta.json", default={}) or {}
    dictionary_path = root / "data" / "config" / "agent_metric_dictionary.json"
    metric_dictionary = _read_json(dictionary_path)
    if not isinstance(metric_dictionary, dict):
        raise FileNotFoundError(f"metric dictionary not found: {dictionary_path}")

    generated_at = datetime.now() if now is None else now
    as_of = _latest_observation_time(snapshots, monitor.get("generated_at"))
    cutoff = as_of - timedelta(hours=24)
    keyword_registry_rows = _load_keyword_registry_rows(root)
    configured_keywords = {
        str(item.get("keyword_text") or "").strip()
        for item in keyword_registry_rows
        if str(item.get("keyword_text") or "").strip()
    }
    accounts_by_id = {
        item.get("account_id"): item
        for item in accounts
        if item.get("account_id")
    }
    raw_monitor_keywords = {
        item.get("keyword_id"): item
        for item in monitor.get("keywords", [])
        if item.get("keyword_id")
    }
    keyword_universe_context, keyword_context_by_id = _build_keyword_universe_context(
        keyword_registry_rows,
        raw_monitor_keywords,
        as_of,
    )
    monitor_keywords = {}
    for keyword_id, item in raw_monitor_keywords.items():
        enriched = dict(item)
        enriched["observation_context"] = keyword_context_by_id.get(keyword_id) or {}
        monitor_keywords[keyword_id] = enriched
    deltas_by_id = {
        item.get("keyword_id"): item
        for item in read_deltas
        if isinstance(item, dict) and item.get("keyword_id")
    }
    ledger = _load_claim_ledger(root)
    memory_path = root / "MEMORY.md"
    memory_text = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    status = _data_status(
        as_of=as_of,
        generated_at=monitor.get("generated_at"),
        configured_keyword_count=len(configured_keywords),
        stable_keyword_ids={
            keyword_id
            for keyword_id, context in keyword_context_by_id.items()
            if context.get("stable_for_trend")
        },
        snapshots=snapshots,
        now=generated_at,
    )
    recent_hit_index = _build_recent_hit_index(
        ranking_hits,
        snapshots,
        monitor_keywords,
        cutoff,
    )
    recent_articles = _build_recent_articles(
        articles,
        accounts_by_id,
        recent_hit_index,
        cutoff,
        as_of,
    )
    content_clusters, cluster_candidates = _build_content_clusters(
        recent_articles,
        keyword_context_by_id,
    )
    account_outputs, account_candidates, account_evidence_ids = _build_account_outputs(monitor.get("accounts", []))
    keyword_outputs, keyword_candidates, keyword_evidence_ids = _build_keyword_outputs(
        list(monitor_keywords.values()),
        deltas_by_id,
    )
    untracked_terms, knowledge_gaps = _build_untracked_terms(
        snapshot_terms,
        snapshots,
        configured_keywords,
        as_of,
        memory_text,
    )

    grouped_candidates = {
        "content_cluster_24h": sorted(
            cluster_candidates,
            key=lambda item: item["report_priority"],
            reverse=True,
        )[:8],
        "account_movement": sorted(
            account_candidates,
            key=lambda item: item["report_priority"],
            reverse=True,
        )[:8],
        "keyword_demand_signal": sorted(
            keyword_candidates,
            key=lambda item: item["report_priority"],
            reverse=True,
        )[:8],
    }
    candidate_events = [
        candidate
        for candidates in grouped_candidates.values()
        for candidate in candidates
    ]
    candidate_events = [
        _attach_candidate_state(candidate, ledger["items"], as_of)
        for candidate in candidate_events
    ]
    candidate_events.sort(
        key=lambda item: (
            not item["report_eligible"],
            -item["report_priority"],
            item["candidate_id"],
        )
    )
    candidate_events = candidate_events[:MAX_EVENT_CANDIDATES]

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

    for article in recent_articles[:MAX_ARTICLE_EVIDENCE]:
        evidence = _article_evidence(root, article)
        _write_json(evidence_dir / f"{evidence['evidence_id']}.json", evidence)
    accounts_by_monitor_id = {
        item.get("account_id"): item
        for item in monitor.get("accounts", [])
        if item.get("account_id")
    }
    for account_id in account_evidence_ids:
        account = accounts_by_monitor_id.get(account_id)
        if account:
            evidence = _account_evidence(account)
            _write_json(evidence_dir / f"{evidence['evidence_id']}.json", evidence)
            if _account_observation_span(account) < 5:
                knowledge_gaps.append(
                    {
                        "type": "account_identity",
                        "subject": account.get("name") or account_id,
                        "reason": "账号基础观察期不足5天，身份与稳定性不能自动判断。",
                        "evidence_id": evidence["evidence_id"],
                        "required_action": "若准备作为黑马或新号案例写入报告，先读取该账号证据并使用谨慎措辞。",
                    }
                )
    for keyword_id in keyword_evidence_ids:
        keyword = monitor_keywords.get(keyword_id)
        if keyword:
            evidence = _keyword_evidence(keyword, deltas_by_id.get(keyword_id))
            _write_json(evidence_dir / f"{evidence['evidence_id']}.json", evidence)

    candidate_article_evidence_ids = {
        evidence_id
        for candidate in candidate_events
        for evidence_id in candidate.get("evidence_ids", [])
        if evidence_id.startswith("article_")
    }
    for article in recent_articles:
        if _article_evidence_id(article.get("article_id")) not in candidate_article_evidence_ids:
            continue
        if article.get("summary") or not article.get("content", {}).get("available"):
            continue
        knowledge_gaps.append(
            {
                "type": "article_context",
                "subject": article.get("title") or article.get("article_id"),
                "reason": "标题和文章指标不足以理解内容事实，正文可读取。",
                "evidence_id": _article_evidence_id(article.get("article_id")),
                "required_action": "仅在准备采用该文章作为关键依据时，读取证据包中的正文预览或原始 Markdown。",
            }
        )

    brief_id = f"brief_{as_of.strftime('%Y%m%d_%H%M%S')}_{_stable_hash({'generated': _iso(generated_at), 'as_of': _iso(as_of)}, 8)}"
    brief = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "projection_version": PROJECTION_VERSION,
        "brief_id": brief_id,
        "generated_at": _iso(generated_at),
        "data_status": status,
        "observation_scope": {
            "search": "当前监控关键词下的微信搜索结果快照。",
            "articles": "被监控关键词命中的、且发布时间落在数据截至前24小时的文章。",
            "recommendation": "没有直接推荐曝光数据；只提供文章互动和内容聚集代理信号。",
            "industry": "所有信号仅覆盖监控样本，不能直接外推为行业全貌。",
        },
        "read_order": [
            "AGENTS.md",
            "data/agent/manifest.json",
            "data/config/agent_metric_dictionary.json",
            "data/agent/daily_brief.json",
            "MEMORY.md",
            "仅对准备写入报告的候选事件读取 evidence_id。",
        ],
        "report_guardrails": [
            "每个核心判断都要附 evidence_id，并区分观察事实、可计算信号和Agent判断。",
            "不得由阅读、点赞推断年龄、资产、人群画像、推荐曝光或因果关系。",
            "persistent 且 report_eligible=false 的候选事件默认不重复推送。",
            "阅读估算必须同时交代 confidence_level、coverage_ratio、observed_share 与 estimated_share。",
        ],
        "metric_dictionary": {
            "path": str(dictionary_path.relative_to(root)),
            "schema_version": metric_dictionary.get("schema_version") or "",
            "metrics": [item.get("metric_id") for item in metric_dictionary.get("metrics", [])],
        },
        "algorithm_versions": {
            "account_score_method": monitor.get("account_score_method") or "",
            "keyword_read_method": (
                next(
                    (
                        item.get("method")
                        for item in read_deltas
                        if isinstance(item, dict) and item.get("method")
                    ),
                    "",
                )
            ),
            "article_metric_meta_generated_at": metric_meta.get("generated_at") or "",
            "wso_fit": monitor.get("wso_fit_meta"),
        },
        "summary": {
            "recent_article_count": len(recent_articles),
            "configured_keyword_count": len(configured_keywords),
            "account_count": len(monitor.get("accounts", [])),
            "keyword_signal_count": len(keyword_outputs["rising"]) + len(keyword_outputs["falling"]),
            "active_claim_count": len(_active_claims(ledger, as_of)),
        },
        "keyword_universe_context": keyword_universe_context,
        "recent_articles": [
            _article_brief_fact(article)
            for article in recent_articles[:MAX_RECENT_ARTICLES_IN_BRIEF]
        ],
        "content_clusters": content_clusters,
        "keyword_signals": keyword_outputs,
        "account_boards": account_outputs["top_boards"],
        "account_movements": {
            "upward": account_outputs["upward"],
            "downward": account_outputs["downward"],
        },
        "untracked_term_candidates": untracked_terms,
        "knowledge_gaps": knowledge_gaps[:10],
        "active_claims": _active_claims(ledger, as_of),
        "event_candidates": candidate_events,
        "evidence_catalog": {
            "directory": str(evidence_dir.relative_to(root)),
            "article_evidence_count": len(recent_articles[:MAX_ARTICLE_EVIDENCE]),
            "account_evidence_count": len(account_evidence_ids),
            "keyword_evidence_count": len(keyword_evidence_ids),
        },
    }
    brief_path = agent_dir / "daily_brief.json"
    _write_json(brief_path, brief)
    _write_text(agent_dir / "daily_brief.md", _build_brief_markdown(brief))

    manifest = {
        "schema_version": "agent_manifest_v1",
        "projection_version": PROJECTION_VERSION,
        "generated_at": _iso(generated_at),
        "brief": {
            "brief_id": brief_id,
            "path": str(brief_path.relative_to(root)),
            "bytes": brief_path.stat().st_size,
            "sha256_12": _file_digest(brief_path),
        },
        "data_status": status,
        "required_files": [
            "AGENTS.md",
            "MEMORY.md",
            str(dictionary_path.relative_to(root)),
            str(brief_path.relative_to(root)),
        ],
        "evidence_directory": str(evidence_dir.relative_to(root)),
        "claim_decision_command": (
            "python3 scripts/agent_claims.py apply "
            "--decision-file data/agent/decisions/<YYMMDD_HHMM>.json"
        ),
        "api": {
            "manifest": "/api/agent/manifest",
            "brief": "/api/agent/daily-brief",
            "metric_dictionary": "/api/agent/metric-dictionary",
            "evidence": "/api/agent/evidence/<evidence_id>",
        },
    }
    manifest_path = agent_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    return {
        "manifest": manifest,
        "brief": brief,
        "paths": {
            "manifest": manifest_path,
            "brief": brief_path,
            "markdown": agent_dir / "daily_brief.md",
            "evidence_dir": evidence_dir,
        },
    }


def load_agent_artifact(project_root: Path | str | None, name: str) -> dict[str, Any]:
    root = _project_root(project_root)
    allowed = {"manifest.json", "daily_brief.json"}
    if name not in allowed:
        raise ValueError("unsupported agent artifact")
    path = _agent_dir(root) / name
    payload = _read_json(path)
    if not isinstance(payload, dict):
        build_agent_projection(root)
        payload = _read_json(path)
    if not isinstance(payload, dict):
        raise FileNotFoundError(f"agent artifact not found: {path}")
    return payload


def load_metric_dictionary(project_root: Path | str | None = None) -> dict[str, Any]:
    root = _project_root(project_root)
    path = root / "data" / "config" / "agent_metric_dictionary.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise FileNotFoundError(f"metric dictionary not found: {path}")
    return payload


def load_agent_evidence(project_root: Path | str | None, evidence_id: str) -> dict[str, Any]:
    root = _project_root(project_root)
    cleaned = str(evidence_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,180}", cleaned):
        raise ValueError("invalid evidence id")
    path = _agent_dir(root) / "evidence" / f"{cleaned}.json"
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise FileNotFoundError(f"evidence not found: {cleaned}")
    return payload


def apply_claim_decision(project_root: Path | str | None, decision_path: Path | str) -> dict[str, Any]:
    root = _project_root(project_root)
    decisions_root = (_agent_dir(root) / "decisions").resolve()
    requested = Path(decision_path)
    if requested.is_absolute():
        candidate_path = requested.resolve()
    else:
        candidate_path = (root / requested).resolve()
    if decisions_root not in candidate_path.parents:
        raise ValueError("decision file must be inside data/agent/decisions")
    decision = _read_json(candidate_path)
    if not isinstance(decision, dict):
        raise FileNotFoundError(f"decision file not found: {candidate_path}")

    brief = load_agent_artifact(root, "daily_brief.json")
    if decision.get("brief_id") != brief.get("brief_id"):
        raise ValueError("decision brief_id does not match the current daily brief")

    candidate_by_id = {
        item.get("candidate_id"): item
        for item in brief.get("event_candidates", [])
        if item.get("candidate_id")
    }
    reported_ids = list(dict.fromkeys(str(item).strip() for item in decision.get("reported_claim_ids", []) if str(item).strip()))
    unknown_ids = [item for item in reported_ids if item not in candidate_by_id]
    if unknown_ids:
        raise ValueError(f"unknown claim ids: {', '.join(unknown_ids)}")

    ledger = _load_claim_ledger(root)
    now = _iso(datetime.now())
    report_path = str(decision.get("report_path") or "").strip()
    for candidate_id in reported_ids:
        candidate = candidate_by_id[candidate_id]
        existing = ledger["items"].get(candidate_id) or {}
        ledger["items"][candidate_id] = {
            "candidate_id": candidate_id,
            "kind": candidate.get("kind") or "",
            "subject": candidate.get("subject") or "",
            "first_reported_at": existing.get("first_reported_at") or now,
            "last_reported_at": now,
            "last_direction": candidate.get("direction") or "",
            "last_fingerprint": candidate.get("fingerprint") or "",
            "last_priority": candidate.get("report_priority"),
            "last_state": candidate.get("event_state") or "",
            "report_path": report_path,
            "evidence_ids": candidate.get("evidence_ids") or [],
        }
    ledger["updated_at"] = now
    _write_json(_state_dir(root) / "agent_claims.json", ledger)

    return {
        "brief_id": brief.get("brief_id"),
        "reported_count": len(reported_ids),
        "reported_claim_ids": reported_ids,
        "ledger_path": str((_state_dir(root) / "agent_claims.json").relative_to(root)),
    }


def validate_agent_projection(project_root: Path | str | None = None) -> dict[str, Any]:
    root = _project_root(project_root)
    manifest = load_agent_artifact(root, "manifest.json")
    brief = load_agent_artifact(root, "daily_brief.json")
    errors = []
    brief_path = _agent_dir(root) / "daily_brief.json"
    if manifest.get("brief", {}).get("brief_id") != brief.get("brief_id"):
        errors.append("manifest brief_id does not match daily brief")
    compact_brief_bytes = len(
        json.dumps(brief, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if compact_brief_bytes > 100_000:
        errors.append("daily brief exceeds 100KB compact-context budget")
    evidence_dir = _agent_dir(root) / "evidence"
    for candidate in brief.get("event_candidates", []):
        for evidence_id in candidate.get("evidence_ids", []):
            if not (evidence_dir / f"{evidence_id}.json").exists():
                errors.append(f"missing evidence: {evidence_id}")
    return {
        "valid": not errors,
        "errors": errors,
        "brief_id": brief.get("brief_id"),
        "brief_bytes": brief_path.stat().st_size,
        "compact_brief_bytes": compact_brief_bytes,
        "event_candidate_count": len(brief.get("event_candidates", [])),
        "recent_article_count": len(brief.get("recent_articles", [])),
        "data_mode": (brief.get("data_status") or {}).get("mode"),
    }
