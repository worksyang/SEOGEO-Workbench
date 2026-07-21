"""Rebuild the legacy WeChat read projections from canonical Hub facts.

This module is intentionally standalone: the integration point is owned by the
main agent, so the builder only accepts an existing SQLite connection.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


WINDOW_DAYS = 15
PLATFORM = "wechat-search"
PROJECTION_KINDS = ("bootstrap", "full", "keyword", "account")
SCORE_BENCHMARK_PERCENTILE = 0.99
SCORE_OVERFLOW_LOG_SCALE = 40.0
RECENT_WINDOW_DAYS = 7
TIMELINESS_WINDOW_DAYS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")

BOARD_CONFIGS = {
    "account": {
        "label": "账号分", "window_label": "滚动15天",
        "axes_meta": [
            {"key": "history_coverage", "label": "历史覆盖", "desc": "滚动15天不同关键词、文章与主题覆盖"},
            {"key": "recent_coverage", "label": "近期覆盖", "desc": "最近7天仍然有效的关键词与文章覆盖"},
            {"key": "classic_articles", "label": "经典文章", "desc": "同文同词位于第4–10名至少3个不同日期"},
            {"key": "continuity", "label": "持续经营", "desc": "近7天在榜、15天在榜与当前连续命中"},
            {"key": "content_matrix", "label": "内容矩阵", "desc": "多篇文章共同贡献，降低单篇文章集中度"},
            {"key": "battle_breadth", "label": "战场广度", "desc": "不同产品 topic 与搜索意图类别覆盖"},
        ],
        "weights": {"history_coverage": .15, "recent_coverage": .15, "classic_articles": .30,
                    "continuity": .20, "content_matrix": .10, "battle_breadth": .10},
        "required_breakthrough_axes": {"classic_articles"},
    },
    "timeliness": {
        "label": "时效分", "window_label": "最近3天",
        "axes_meta": [
            {"key": "top3_volume", "label": "Top3规模", "desc": "最近3天进入前三的文章×关键词命中规模"},
            {"key": "top3_breadth", "label": "Top3广度", "desc": "前三覆盖的关键词、产品与搜索意图类别"},
            {"key": "new_top3", "label": "新进Top3", "desc": "最近3天进入前三、此前7天未进前三的文章×关键词"},
            {"key": "fresh_top3", "label": "新文冲榜", "desc": "发布21天内文章进入前三的数量"},
            {"key": "top3_continuity", "label": "连续冲榜", "desc": "最近3天中实际出现Top3命中的天数"},
            {"key": "upward_momentum", "label": "上升动能", "desc": "今天相对昨天的新进关键词与排名上升"},
        ],
        "weights": {"top3_volume": .28, "top3_breadth": .20, "new_top3": .22,
                    "fresh_top3": .12, "top3_continuity": .10, "upward_momentum": .08},
        "required_breakthrough_axes": {"top3_volume", "new_top3"},
    },
    "today": {
        "label": "当天分", "window_label": "今天",
        "axes_meta": [
            {"key": "today_top3", "label": "今日Top3", "desc": "今天进入前三的文章×关键词命中数量"},
            {"key": "today_keywords", "label": "今日关键词", "desc": "今天覆盖的不同监控关键词数量"},
            {"key": "today_articles", "label": "今日文章", "desc": "今天仍在榜的不同文章数量"},
            {"key": "today_themes", "label": "今日主题", "desc": "今天覆盖的产品 topic 与搜索意图类别"},
            {"key": "today_rank_quality", "label": "排名质量", "desc": "今天全部命中的平均排名质量，第一名权重最高"},
            {"key": "today_growth", "label": "今日增长", "desc": "今天相对昨天的新进关键词与排名上升"},
        ],
        "weights": {"today_top3": .30, "today_keywords": .25, "today_articles": .18,
                    "today_themes": .10, "today_rank_quality": .10, "today_growth": .07},
        "required_breakthrough_axes": {"today_top3", "today_keywords"},
    },
}


def _json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def _number(value: Any) -> int | float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _local_datetime(value: Any) -> datetime | None:
    """Put legacy naive and new UTC timestamps onto one local timeline."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _source_datetime(value: Any) -> datetime | None:
    """Return the source-date representation used by public legacy labels.

    Canonical Hub snapshots with a ``Z`` suffix are UTC facts.  They must keep
    their UTC calendar date when rendered: ``2026-07-18T18:15Z`` is a July 18
    source snapshot, not July 19 merely because the workstation is in China.
    Naive legacy values are already date-labelled and are left naive.
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _timestamp_key(value: Any) -> float:
    parsed = _local_datetime(value)
    return parsed.timestamp() if parsed is not None else float("-inf")


def _legacy_local_iso(value: Any) -> str:
    parsed = _source_datetime(value)
    if parsed is None:
        return str(value or "")
    return (
        parsed.replace(tzinfo=None)
        .isoformat(timespec="microseconds")
        .rstrip("0")
        .rstrip(".")
    )


def _iso_date(value: Any) -> str:
    parsed = _source_datetime(value)
    return parsed.date().isoformat() if parsed is not None else str(value or "")[:10]


def _parse_date(value: str) -> date | None:
    parsed = _source_datetime(value)
    return parsed.date() if parsed is not None else None


def _old_payload(raw: Any) -> dict[str, Any]:
    value = _json(raw, {})
    if value.get("__compressed_json__") != "zlib+base64":
        return value
    try:
        return _json(zlib.decompress(base64.b64decode(value["data"])), {})
    except (KeyError, TypeError, ValueError, zlib.error):
        return {}


def _stored_projection_json(payload: dict[str, Any]) -> str:
    """Keep durable live projections bounded without changing their contract.

    Readers already understand this envelope (the same format is used by the
    legacy repository for article details), so large full/bootstrap rows do not
    require materialising a second uncompressed copy on disk.
    """
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= (1 << 20):
        return encoded
    compressed = zlib.compress(encoded.encode("utf-8"), 6)
    return json.dumps({
        "__compressed_json__": "zlib+base64",
        "data": base64.b64encode(compressed).decode("ascii"),
    }, ensure_ascii=False, separators=(",", ":"))


def _keyword_text(source: dict[str, Any], seed: dict[str, Any]) -> str:
    """Choose display text without exposing an internal ``kw_*`` identifier."""
    setting_payload = _json(source.get("setting_payload_json"), {})
    candidates = (
        setting_payload.get("keyword_text"),
        _json(source.get("payload_json"), {}).get("keyword_text"),
        source.get("keyword"),
        seed.get("keyword_text"),
        seed.get("keyword"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text != str(source.get("keyword_id") or "").strip() and not text.startswith("kw_"):
            return text
    return ""


def _rank_weight(rank: int) -> float:
    return {1: 10.0, 2: 8.2, 3: 6.8, 4: 5.6, 5: 4.6,
            6: 3.7, 7: 3.0, 8: 2.4, 9: 1.9, 10: 1.5}.get(rank, 0.0)


def _percentile(values: list[float], q: float) -> float:
    nums = sorted(v for v in values if v > 0)
    if not nums:
        return 1.0
    if len(nums) == 1:
        return nums[0]
    pos = (len(nums) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return nums[lo] if lo == hi else nums[lo] * (hi - pos) + nums[hi] * (pos - lo)


def _axis_score(raw_value: float, benchmark: float) -> float:
    raw, base = max(float(raw_value or 0), 0), max(float(benchmark or 0), 1e-9)
    if raw <= 0:
        return 0.0
    if raw <= base:
        return round(100 * raw / base, 4)
    return round(100 + SCORE_OVERFLOW_LOG_SCALE * math.log2(raw / base), 4)


def _score_board_snapshot(raw_snapshot: dict[str, Any], board: str,
                          benchmarks: dict[str, float]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = BOARD_CONFIGS[board]
    axis_values = {
        key: _axis_score(raw_snapshot["raw_axes"].get(key, 0), benchmarks.get(key, 1))
        for key in config["weights"]
    }
    confidence = float(raw_snapshot.get("confidence") or 0)
    base_score = sum(min(axis_values[k], 100) * w for k, w in config["weights"].items()) * confidence
    breakthrough_energy = sum(max(axis_values[k] - 100, 0) * w for k, w in config["weights"].items()) * confidence
    over_axes = [k for k, v in axis_values.items() if v > 100.0001]
    gate = (
        confidence >= 0.999 and base_score >= 85 and len(over_axes) >= 2
        and any(k in config["required_breakthrough_axes"] for k in over_axes)
    )
    score_raw = base_score + breakthrough_energy if gate else min(base_score, 100)
    normalized = {
        **raw_snapshot,
        "axes": {k: max(0, int(round(v))) for k, v in axis_values.items()},
        "axis_values": {k: round(v, 4) for k, v in axis_values.items()},
    }
    return normalized, {
        "score": max(0, int(round(score_raw))), "score_raw": round(score_raw, 4),
        "base_score": round(base_score, 4), "breakthrough_energy": round(max(score_raw - 100, 0), 4),
        "confidence": round(confidence, 4), "breakthrough": score_raw > 100,
        "breakthrough_gate": gate, "over_axes": over_axes,
    }


def _score_level(score: int) -> str:
    if score >= 130:
        return "extreme_breakthrough"
    if score >= 110:
        return "strong_breakthrough"
    if score > 100:
        return "breakthrough"
    if score == 100:
        return "benchmark"
    return "within_benchmark"


def _population_stat(values: list[float], value: float) -> dict[str, int | float]:
    total = len(values)
    better = sum(v > value + 1e-6 for v in values)
    lower = sum(v < value - 1e-6 for v in values)
    return {"rank": better + 1, "total": total, "tie_count": total - better - lower,
            "percentile": round(100 * lower / total, 1) if total else 0.0}


def _hexagon(board: str, current: dict[str, Any], previous: dict[str, Any],
             benchmarks: dict[str, float]) -> dict[str, Any]:
    config = BOARD_CONFIGS[board]
    return {
        "board": board, "label": config["label"], "window_label": config["window_label"],
        "benchmark_line": 100, "benchmark_percentile": SCORE_BENCHMARK_PERCENTILE,
        "overflow_log_scale": SCORE_OVERFLOW_LOG_SCALE, "axes_meta": config["axes_meta"],
        "weights": config["weights"], "benchmarks": benchmarks, "current": current,
        "previous": previous,
        "delta": {k: current["axes"].get(k, 0) - previous["axes"].get(k, 0) for k in current["axes"]},
    }


def _log_count(value: int | float) -> float:
    return math.log1p(max(float(value or 0), 0))


def _events_window(events: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    return [e for e in events if start <= int(e["_day_idx"]) <= end]


def _event_sets(events: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str], set[str]]:
    return (
        {str(e.get("keyword") or "") for e in events},
        {str(e.get("article_key") or "") for e in events},
        {str(e.get("topic") or "") for e in events},
        {str(e.get("bucket") or "") for e in events},
    )


def _best_ranks(events: list[dict[str, Any]], day_idx: int) -> dict[str, int]:
    result: dict[str, int] = {}
    for event in events:
        if int(event["_day_idx"]) != day_idx:
            continue
        key, rank = str(event.get("keyword") or ""), int(event.get("rank") or 0)
        if rank and (key not in result or rank < result[key]):
            result[key] = rank
    return result


def _moves(events: list[dict[str, Any]], end_idx: int) -> dict[str, int]:
    current, previous = _best_ranks(events, end_idx), _best_ranks(events, end_idx - 1)
    result = {"new_count": 0, "up_count": 0, "down_count": 0, "flat_count": 0}
    for keyword, rank in current.items():
        prior = previous.get(keyword)
        if prior is None:
            result["new_count"] += 1
        elif rank < prior:
            result["up_count"] += 1
        elif rank > prior:
            result["down_count"] += 1
        else:
            result["flat_count"] += 1
    return result


def _raw_boards(events: list[dict[str, Any]], end_idx: int, end_day: date) -> dict[str, dict[str, Any]]:
    history = _events_window(events, end_idx - WINDOW_DAYS + 1, end_idx)
    recent = _events_window(events, end_idx - RECENT_WINDOW_DAYS + 1, end_idx)
    hkw, hart, htopic, hbucket = _event_sets(history)
    rkw, rart, rtopic, rbucket = _event_sets(recent)
    classic_pairs: dict[tuple[str, str], set[int]] = defaultdict(set)
    for e in history:
        if 4 <= int(e.get("rank") or 0) <= 10:
            classic_pairs[(str(e.get("keyword") or ""), str(e.get("article_key") or ""))].add(int(e["_day_idx"]))
    classic_pairs = {k: v for k, v in classic_pairs.items() if len(v) >= 3}
    classic_articles = {p[1] for p in classic_pairs}
    classic_keywords = {p[0] for p in classic_pairs}
    history_days = {int(e["_day_idx"]) for e in history}
    recent_days = {int(e["_day_idx"]) for e in recent}
    active_days = sorted(history_days)
    streak = 0
    for idx in range(end_idx, end_idx - WINDOW_DAYS, -1):
        if idx not in history_days:
            break
        streak += 1
    article_counts: dict[str, int] = defaultdict(int)
    for e in history:
        article_counts[str(e.get("article_key") or "")] += 1
    total = sum(article_counts.values()) or 1
    concentration = max(article_counts.values(), default=0) / total
    effective = math.exp(-sum((n / total) * math.log(n / total) for n in article_counts.values() if n))
    continuity = .50 * len(recent_days) / 7 + .30 * len(history_days) / 15 + .20 * streak / 15
    classic_raw = (
        .35 * _log_count(len(classic_pairs)) + .25 * _log_count(len(classic_articles))
        + .15 * _log_count(len(classic_keywords)) + .25 * _log_count(sum(map(len, classic_pairs.values())))
    )
    breadth_raw = .50 * _log_count(len(hkw)) + .30 * _log_count(len(htopic)) + .20 * _log_count(len(hbucket))
    observation_span = min(end_idx - min(active_days) + 1, WINDOW_DAYS) if active_days else 0
    confidence_account = {0: 0, 1: .38, 2: .55, 3: .74, 4: .88}.get(observation_span, 1.0)
    account = {
        "end_idx": end_idx,
        "raw_axes": {
            "history_coverage": .55 * _log_count(len(hkw)) + .30 * _log_count(len(hart)) + .15 * _log_count(len(htopic) + len(hbucket)),
            "recent_coverage": .55 * _log_count(len(rkw)) + .30 * _log_count(len(rart)) + .15 * _log_count(len(rtopic) + len(rbucket)),
            "classic_articles": classic_raw, "continuity": continuity,
            "content_matrix": _log_count(effective) * (1 - .35 * concentration),
            "battle_breadth": breadth_raw,
        },
        "confidence": confidence_account,
        "details": {"recent_article_count": len(rart), "classic_article_count": len(classic_articles),
                    "classic_pair_count": len(classic_pairs), "classic_rank_days": sum(map(len, classic_pairs.values())),
                    "recent_topic_count": len(rtopic), "recent_bucket_count": len(rbucket),
                    "current_streak": streak},
    }
    recent3 = _events_window(events, end_idx - 2, end_idx)
    top3 = [e for e in recent3 if int(e.get("rank") or 0) <= 3]
    tkw, tart, ttopic, tbucket = _event_sets(top3)
    prior = _events_window(events, end_idx - 9, end_idx - 3)
    prior_pairs = {(str(e.get("keyword") or ""), str(e.get("article_key") or "")) for e in prior if int(e.get("rank") or 0) <= 3}
    top3_pairs = {(str(e.get("keyword") or ""), str(e.get("article_key") or "")) for e in top3}
    fresh = {str(e.get("article_key") or "") for e in top3
             if (_parse_date(str(e.get("published_at") or "")) and
                 0 <= (end_day - _parse_date(str(e.get("published_at")))).days <= 21)}
    top3_days = {int(e["_day_idx"]) for e in top3}
    moves = _moves(events, end_idx)
    timeliness = {
        "end_idx": end_idx,
        "raw_axes": {
            "top3_volume": _log_count(len(top3)),
            "top3_breadth": .55 * _log_count(len(tkw)) + .25 * _log_count(len(ttopic)) + .20 * _log_count(len(tbucket)),
            "new_top3": _log_count(len(top3_pairs - prior_pairs)), "fresh_top3": _log_count(len(fresh)),
            "top3_continuity": len(top3_days) / 3, "upward_momentum": _log_count(moves["new_count"] + .6 * moves["up_count"]),
        },
        "confidence": {0: 0, 1: .68, 2: .88}.get(min(3, len({int(e["_day_idx"]) for e in top3})), 1.0),
        "details": {"top3_hit_count": len(top3), "new_top3_pair_count": len(top3_pairs - prior_pairs), **moves},
    }
    today = _events_window(events, end_idx, end_idx)
    today_top3 = [e for e in today if int(e.get("rank") or 0) <= 3]
    dkw, dart, dtopic, dbucket = _event_sets(today)
    rank_quality = sum((11 - int(e["rank"])) / 10 for e in today) / len(today) if today else 0
    today_board = {
        "end_idx": end_idx, "raw_axes": {
            "today_top3": _log_count(len(today_top3)), "today_keywords": _log_count(len(dkw)),
            "today_articles": _log_count(len(dart)), "today_themes": _log_count(len(dtopic) + .5 * len(dbucket)),
            "today_rank_quality": rank_quality, "today_growth": _log_count(moves["new_count"] + .6 * moves["up_count"]),
        }, "confidence": 1.0,
        "details": {"today_hit_count": len(today), "today_top3_count": len(today_top3), **moves},
    }
    return {"account": account, "timeliness": timeliness, "today": today_board}


def _safe_key(row: dict[str, Any]) -> str:
    return str(
        row.get("article_id")
        or row.get("content_id")
        or row.get("url")
        or row.get("url_raw")
        or row.get("title")
        or row.get("title_raw")
        or ""
    )


def _latest_metrics(connection: Any, content_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not content_ids:
        return {}
    marks = ",".join("?" for _ in content_ids)
    rows = connection.execute(
        f"""SELECT subject_id,metric_key,numeric_value,observed_at,payload_json
            FROM metric_observations
            WHERE subject_type='content' AND subject_id IN ({marks})
              AND metric_key IN ('wechat.read_count','wechat.like_count',
                                 'wechat.article.read_count','wechat.article.like_count')
            ORDER BY observed_at DESC, observation_id DESC""",
        tuple(sorted(content_ids)),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = result.setdefault(str(row["subject_id"]), {})
        short = str(row["metric_key"]).rsplit(".", 1)[-1]
        item.setdefault(short, row["numeric_value"])
    return result


def _read_delta(connection: Any, keyword_id: str, start: str, end: str) -> dict[str, Any] | None:
    legacy_seed: dict[str, Any] = {}
    seed_row = connection.execute(
        """SELECT payload_json
           FROM wechat_legacy_projections
           WHERE projection_kind='keyword' AND subject_id=?
             AND source_manifest_id NOT LIKE 'wechat-live-projection-%'
           ORDER BY updated_at DESC, projection_id DESC
           LIMIT 1""",
        (keyword_id,),
    ).fetchone()
    if seed_row:
        seed_payload = _old_payload(seed_row["payload_json"])
        if isinstance(seed_payload.get("keyword_read_delta"), dict):
            legacy_seed = dict(seed_payload["keyword_read_delta"])

    rows = connection.execute(
        """SELECT metric_key,numeric_value,observed_at,payload_json
           FROM metric_observations
           WHERE subject_type='keyword' AND subject_id=?
             AND metric_key IN ('wechat.keyword.daily_read_delta',
                                'wechat.keyword.read_delta_raw',
                                'wechat.keyword.read_delta_estimated',
                                'wechat.keyword.steady_read_median',
                                'wechat.keyword.confidence_score',
                                'wechat.keyword.trend_signal')
           ORDER BY observed_at DESC, observation_id DESC""",
        (keyword_id,),
    ).fetchall()
    if not rows:
        # Some imports retain the normalized delta facts in a dedicated table
        # rather than materializing them as observations.  Treat that table as
        # optional so small Hub fixtures and older databases remain supported.
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='keyword_read_deltas'"
        ).fetchone()
        if exists:
            columns = {
                str(r["name"])
                for r in connection.execute("PRAGMA table_info(keyword_read_deltas)")
            }
            id_column = "keyword_id" if "keyword_id" in columns else "subject_id" if "subject_id" in columns else None
            if id_column:
                candidates = connection.execute(
                    f"SELECT * FROM keyword_read_deltas WHERE {id_column}=?",
                    (keyword_id,),
                ).fetchall()
                for candidate in candidates:
                    item = dict(candidate)
                    observed = str(item.get("window_end") or item.get("observed_at") or "")
                    if observed and _iso_date(observed) < _iso_date(start):
                        continue
                    estimated = _number(item.get("read_delta_estimated"))
                    raw = _number(item.get("read_delta_raw"))
                    status = "ok" if estimated is not None else "insufficient_data"
                    confidence_score = _number(item.get("confidence_score"))
                    trend_signal = _number(item.get("trend_signal"))
                    confidence_level = (
                        item.get("confidence_level")
                        if status == "ok"
                        else "insufficient"
                    )
                    if status == "ok" and not confidence_level and confidence_score is not None:
                        confidence_level = (
                            "high" if confidence_score >= 0.72
                            else "medium" if confidence_score >= 0.50
                            else "low"
                        )
                    trend_label = (
                        item.get("trend_label")
                        if status == "ok"
                        else "观察中"
                    )
                    if status == "ok" and not trend_label and trend_signal is not None:
                        trend_label = (
                            "上升" if trend_signal >= 0.20
                            else "下降" if trend_signal <= -0.20
                            else "平稳"
                        )
                    return {
                        "keyword_id": keyword_id, "window_start": start,
                        "window_end": end, "window_days": WINDOW_DAYS,
                        "method": "canonical_keyword_read_deltas",
                        "status": status,
                        "read_delta_estimated": estimated,
                        "read_delta_raw": raw,
                        "steady_read_median": _number(item.get("steady_read_median")),
                        "confidence_score": confidence_score,
                        "confidence_level": confidence_level or (
                            "low" if status == "ok" else "insufficient"
                        ),
                        "trend_signal": trend_signal,
                        "trend_label": trend_label or ("平稳" if status == "ok" else "观察中"),
                        "recent_vs_baseline_ratio": _number(item.get("recent_vs_baseline_ratio")),
                        "provisional_steady_read_median": _number(item.get("provisional_steady_read_median")),
                        "provisional_read_delta_estimated": _number(item.get("provisional_read_delta_estimated")),
                        "provisional_sample_count": _number(item.get("provisional_sample_count")),
                        "provisional_status": item.get("provisional_status"),
                        "insufficient_reason": (
                            item.get("insufficient_reason")
                            if status != "ok"
                            else None
                        ),
                    }
        if legacy_seed and (
            not _iso_date(legacy_seed.get("window_end"))
            or _iso_date(legacy_seed.get("window_end")) >= start
        ):
            result = dict(legacy_seed)
            result.update({
                "keyword_id": keyword_id,
                "window_start": start,
                "window_end": end,
                "window_days": WINDOW_DAYS,
                "status": (
                    "ok"
                    if result.get("read_delta_estimated") is not None
                    else "insufficient_data"
                ),
            })
            result.setdefault(
                "confidence_level",
                "low" if result["status"] == "ok" else "insufficient",
            )
            result.setdefault(
                "trend_label",
                "平稳" if result["status"] == "ok" else "观察中",
            )
            result.setdefault(
                "insufficient_reason",
                None if result["status"] == "ok" else "legacy_import_only",
            )
            return result
        return None
    values: dict[str, Any] = {}
    points: list[dict[str, Any]] = []
    for row in rows:
        observed_date = _iso_date(row["observed_at"])
        if observed_date < start or observed_date > end:
            continue
        key = str(row["metric_key"]).split(".")[-1]
        values.setdefault(key, row["numeric_value"])
        payload = _json(row["payload_json"], {})
        for field in (
            "confidence_level", "trend_label", "recent_vs_baseline_ratio",
            "observed_share", "estimated_share", "slot_coverage_ratio",
            "observed_days", "missing_days", "snapshot_count",
        ):
            if field not in values and field in payload:
                values[field] = payload[field]
        if key == "daily_read_delta":
            daily_point = payload.get("daily_point")
            if not isinstance(daily_point, dict):
                daily_point = {}
            point = {
                **daily_point,
                "date": daily_point.get("date") or _iso_date(row["observed_at"]),
                "read_delta": _number(
                    daily_point.get("read_delta")
                    if daily_point.get("read_delta") is not None
                    else row["numeric_value"]
                ),
            }
            points.append(point)
    raw = values.get("read_delta_raw")
    estimated = values.get("read_delta_estimated")
    # The legacy normalized import contains historical model fields that were
    # intentionally split into canonical metric observations.  During the
    # migration window, keep those non-numeric fields (and any mature value
    # absent from the new observations) as a bounded fallback.  This prevents
    # a schema migration from turning every historical keyword into
    # “insufficient” until fifteen new snapshots have accumulated.
    if legacy_seed:
        seed_window_end = _iso_date(legacy_seed.get("window_end"))
        if not seed_window_end or seed_window_end >= start:
            for field in (
                "read_delta_estimated", "read_delta_raw", "steady_read_median",
                "confidence_score", "confidence_level", "trend_signal",
                "trend_label", "recent_vs_baseline_ratio", "observed_share",
                "estimated_share", "slot_coverage_ratio", "observed_days",
                "missing_days", "snapshot_count", "hit_articles",
                "articles_with_metric", "coverage_ratio", "provisional_steady_read_median",
                "provisional_read_delta_estimated", "provisional_sample_count",
                "provisional_status", "new_term_count", "rising_term_count",
            ):
                if values.get(field) is None and legacy_seed.get(field) is not None:
                    values[field] = legacy_seed[field]
            if not points and isinstance(legacy_seed.get("daily_read_delta_points"), list):
                points = [
                    dict(point) for point in legacy_seed["daily_read_delta_points"]
                    if isinstance(point, dict)
                ]
    estimated = values.get("read_delta_estimated")
    status = "ok" if estimated is not None else "insufficient_data"
    confidence_score = _number(values.get("confidence_score"))
    confidence_level = values.get("confidence_level") if status == "ok" else "insufficient"
    if status == "ok" and not confidence_level and confidence_score is not None:
        confidence_level = (
            "high" if confidence_score >= 0.72
            else "medium" if confidence_score >= 0.50
            else "low"
        )
    trend_signal = _number(values.get("trend_signal"))
    trend_label = values.get("trend_label") if status == "ok" else "观察中"
    if status == "ok" and not trend_label and trend_signal is not None:
        trend_label = (
            "上升" if trend_signal >= 0.20
            else "下降" if trend_signal <= -0.20
            else "平稳"
        )
    numeric_points = [
        point for point in points
        if _number(point.get("read_delta")) is not None
    ]
    if numeric_points:
        total_value = sum(float(point["read_delta"]) for point in numeric_points)
        direct_value = sum(
            float(_number(point.get("observed_component")) or 0)
            for point in numeric_points
        )
        if values.get("observed_share") is None and total_value > 0:
            values["observed_share"] = min(1.0, direct_value / total_value)
        if values.get("estimated_share") is None and values.get("observed_share") is not None:
            values["estimated_share"] = max(0.0, 1.0 - float(values["observed_share"]))
        observed_points = [
            point for point in numeric_points
            if _number(point.get("slot_coverage_ratio")) is not None
            and _number(point.get("snapshot_count")) is not None
            and _number(point.get("snapshot_count")) > 0
        ]
        if values.get("slot_coverage_ratio") is None and observed_points:
            values["slot_coverage_ratio"] = sum(
                float(point["slot_coverage_ratio"])
                for point in observed_points
            ) / len(observed_points)
        if values.get("observed_days") is None:
            values["observed_days"] = len(observed_points)
        if values.get("missing_days") is None:
            values["missing_days"] = max(0, WINDOW_DAYS - len(observed_points))
        if values.get("snapshot_count") is None:
            values["snapshot_count"] = sum(
                int(_number(point.get("snapshot_count")) or 0)
                for point in numeric_points
            )
        recent_dates = {
            (datetime.fromisoformat(end).date() - timedelta(days=offset)).isoformat()
            for offset in range(3)
        }
        baseline_dates = {
            (datetime.fromisoformat(end).date() - timedelta(days=offset)).isoformat()
            for offset in range(3, 10)
        }
        def median(items: list[float]) -> float | None:
            if not items:
                return None
            ordered = sorted(items)
            middle = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[middle]
            return (ordered[middle - 1] + ordered[middle]) / 2
        recent_value = median([
            float(point["read_delta"]) for point in numeric_points
            if str(point.get("date")) in recent_dates
        ])
        baseline_value = median([
            float(point["read_delta"]) for point in numeric_points
            if str(point.get("date")) in baseline_dates
        ])
        if values.get("recent_vs_baseline_ratio") is None and recent_value is not None:
            # 近期值缺失时不能等同于 0，否则会把"没数据"误算成"-100% 暴跌"。
            # 只有近期有真实观测时才计算对比率；基线缺失时用 1.0 兜底避免除零。
            values["recent_vs_baseline_ratio"] = (
                (recent_value - baseline_value) / baseline_value
                if baseline_value is not None and baseline_value != 0
                else recent_value / 1.0
            )
        if (
            status != "ok"
            and values.get("provisional_steady_read_median") is None
            and len(numeric_points) >= 2
        ):
            ordered = sorted(float(point["read_delta"]) for point in numeric_points)
            middle = len(ordered) // 2
            provisional_steady = (
                ordered[middle]
                if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2
            )
            values["provisional_steady_read_median"] = round(provisional_steady)
            values["provisional_read_delta_estimated"] = round(
                provisional_steady * WINDOW_DAYS
            )
            values["provisional_sample_count"] = int(values.get("snapshot_count") or len(numeric_points))
            values["provisional_status"] = "provisional"
    return {
        "keyword_id": keyword_id, "window_start": start, "window_end": end,
        "window_days": WINDOW_DAYS, "method": "canonical_metric_observations",
        "status": status, "read_delta_estimated": estimated,
        "read_delta_raw": raw, "steady_read_median": values.get("steady_read_median"),
        "confidence_score": confidence_score,
        "confidence_level": confidence_level or (
            "low" if status == "ok" else "insufficient"
        ),
        "trend_signal": trend_signal,
        "trend_label": trend_label or ("平稳" if status == "ok" else "观察中"),
        "recent_vs_baseline_ratio": _number(values.get("recent_vs_baseline_ratio")),
        "provisional_steady_read_median": _number(values.get("provisional_steady_read_median")),
        "provisional_read_delta_estimated": _number(values.get("provisional_read_delta_estimated")),
        "provisional_sample_count": int(_number(values["provisional_sample_count"]) or 0)
        if values.get("provisional_sample_count") is not None else None,
        "provisional_status": values.get("provisional_status"),
        "observed_share": _number(values.get("observed_share")),
        "estimated_share": _number(values.get("estimated_share")),
        "slot_coverage_ratio": _number(values.get("slot_coverage_ratio")),
        "observed_days": int(_number(values["observed_days"]) or 0)
        if values.get("observed_days") is not None else None,
        "missing_days": int(_number(values["missing_days"]) or 0)
        if values.get("missing_days") is not None else None,
        "snapshot_count": int(_number(values["snapshot_count"]) or 0)
        if values.get("snapshot_count") is not None else None,
        "daily_read_delta_points": points,
        "insufficient_reason": None if status == "ok" else "no_numeric_read_delta",
    }


def rebuild(connection: Any, *, window_days: int = WINDOW_DAYS) -> dict[str, dict[str, Any]]:
    """Return fresh ``bootstrap/full/keyword/account`` payloads.

    ``generated_at`` is the latest canonical snapshot timestamp.  It is never
    taken from ``wechat_legacy_projections.updated_at``.
    """
    snapshots = [dict(r) for r in connection.execute(
        "SELECT * FROM search_snapshots WHERE platform=? ORDER BY captured_at,snapshot_id",
        (PLATFORM,),
    ).fetchall()]
    if not snapshots:
        return {}
    latest_row = max(
        snapshots,
        key=lambda row: (
            _timestamp_key(row.get("captured_at")),
            str(row.get("snapshot_id") or ""),
        ),
    )
    latest_raw = str(latest_row.get("captured_at") or "")
    latest = _legacy_local_iso(latest_raw)
    end_day = _parse_date(latest_raw) or date.today()
    start_day = end_day - timedelta(days=max(1, window_days) - 1)
    start, end = start_day.isoformat(), end_day.isoformat()
    snapshots = [r for r in snapshots if start <= _iso_date(r["captured_at"]) <= end]
    snapshots.sort(
        key=lambda row: (
            _timestamp_key(row.get("captured_at")),
            str(row.get("snapshot_id") or ""),
        )
    )

    seeds: dict[tuple[str, str], dict[str, Any]] = {}
    # The covering lookup index lets SQLite identify the newest row per
    # projection identity before fetching payload_json.  In particular, do not
    # iterate historical 100+ MB payloads just to discard all but one row.
    for row in connection.execute(
        """WITH latest AS (
               SELECT projection_kind,subject_id,
                      MAX(updated_at) AS updated_at
               FROM wechat_legacy_projections
               WHERE projection_kind IN ('bootstrap','full','keyword','account')
               GROUP BY projection_kind,subject_id
           )
           SELECT p.projection_kind,p.subject_id,p.payload_json
           FROM wechat_legacy_projections p
           JOIN latest l
             ON l.projection_kind=p.projection_kind
            AND l.subject_id=p.subject_id
            AND l.updated_at=p.updated_at
           WHERE p.projection_id=(
               SELECT p2.projection_id
               FROM wechat_legacy_projections p2
               WHERE p2.projection_kind=p.projection_kind
                 AND p2.subject_id=p.subject_id
                 AND p2.updated_at=p.updated_at
               ORDER BY p2.projection_id DESC
               LIMIT 1
           )"""
    ):
        key = (str(row["projection_kind"]), str(row["subject_id"]))
        seeds.setdefault(key, _old_payload(row["payload_json"]))
    full_seed = seeds.get(("full", ""), {})

    keyword_rows = [dict(r) for r in connection.execute(
        """SELECT k.*,s.pinned,s.note,s.group_id,s.refresh_strategy,
                  s.refresh_interval_minutes,s.commercial_value,
                  s.archived_at AS setting_archived_at,
                  s.payload_json AS setting_payload_json
           FROM keywords k LEFT JOIN search_keyword_settings s
             ON s.keyword_id=k.keyword_id AND s.system_key='wechat-search'
           WHERE k.platform=? AND k.status='active' AND s.archived_at IS NULL
           ORDER BY COALESCE(s.pinned,0) DESC,k.keyword,k.keyword_id""",
        (PLATFORM,),
    ).fetchall()]
    keywords: dict[str, dict[str, Any]] = {str(r["keyword_id"]): r for r in keyword_rows}

    creators = {
        str(r["creator_id"]): {
            **_json(r["payload_json"], {}),
            **dict(r),
            "headimg_url": (
                dict(r).get("headimg_url")
                or _json(r["payload_json"], {}).get("headimg_url")
            ),
        }
        for r in connection.execute("SELECT * FROM creators WHERE platform=?", (PLATFORM,))
    }
    hits_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_content_ids: set[str] = set()
    if snapshots:
        marks = ",".join("?" for _ in snapshots)
        rows = connection.execute(
            f"""SELECT h.*,s.keyword,s.keyword_id,s.captured_at,s.features_json,
                       c.title,c.canonical_url,c.creator_id,c.author_name,c.published_at,
                       c.payload_json AS content_payload
                FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                LEFT JOIN contents c ON c.content_id=h.content_id
                WHERE h.snapshot_id IN ({marks}) ORDER BY h.snapshot_id,h.rank,h.hit_id""",
            tuple(r["snapshot_id"] for r in snapshots),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            row["_hit_payload"] = _json(row.get("payload_json"), {})
            row["_content_payload"] = _json(row.get("content_payload"), {})
            all_content_ids.add(str(row.get("content_id") or ""))
            hits_by_snapshot[str(row["snapshot_id"])].append(row)
    metrics = _latest_metrics(connection, {x for x in all_content_ids if x})

    def article(row: dict[str, Any]) -> dict[str, Any]:
        hp, cp = row["_hit_payload"], row["_content_payload"]
        aid = str(row.get("creator_id") or "")
        creator = creators.get(aid, {})
        name = row.get("author_name") or row.get("creator_name_raw") or hp.get("creator_name_raw") or creator.get("canonical_name") or ""
        content_id = str(row.get("content_id") or "")
        mapped = dict(cp)
        mapped.update({k: v for k, v in metrics.get(content_id, {}).items() if v is not None})
        return {
            "rank": int(row.get("rank") or 0), "account": name, "account_id": aid,
            "account_headimg": creator.get("headimg_url"), "article_id": content_id,
            "title": row.get("title") or row.get("title_raw") or hp.get("title_raw") or "",
            "summary": hp.get("summary_raw") or mapped.get("summary_raw"),
            "published_at": row.get("published_at") or hp.get("published_at") or "",
            "url": row.get("canonical_url") or row.get("url_raw") or hp.get("url_raw") or "",
            "cover_url": mapped.get("cover_url"), "content_path": mapped.get("content_file_path"),
            "read_count": mapped.get("read_count"), "like_count": mapped.get("like_count"),
            "friends_follow_count": mapped.get("friends_follow_count"),
            "original_article_count": mapped.get("original_article_count"),
        }

    by_keyword: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snap in snapshots:
        run_articles = [article(x) for x in hits_by_snapshot.get(str(snap["snapshot_id"]), [])]
        features = _json(snap.get("features_json"), {})
        captured_source = _source_datetime(snap["captured_at"])
        run = {
            "id": str(snap["snapshot_id"]), "date": _iso_date(snap["captured_at"]),
            "time": (
                captured_source.strftime("%H:%M")
                if captured_source is not None
                else str(snap["captured_at"])[11:16]
            ),
            "run_at": (
                captured_source.strftime("%Y-%m-%d %H:%M")
                if captured_source is not None
                else str(snap["captured_at"]).replace("T", " ")[:16]
            ),
            "trigger_type": snap.get("trigger_type") or "manual", "is_primary": True,
            "result_count": int(snap.get("result_count") or len(run_articles)), "note": "",
            "articles": run_articles,
            "terms": {"suggestions": features.get("suggestions") or [], "related": features.get("related") or []},
        }
        by_keyword[str(snap["keyword_id"] or "")].append(run)

    keyword_payloads: dict[str, dict[str, Any]] = {}
    for kid, source in keywords.items():
        seed = dict(seeds.get(("keyword", kid), {}))
        keyword_text = _keyword_text(source, seed)
        runs = sorted(by_keyword.get(kid, []), key=lambda r: (r["run_at"], r["id"]), reverse=True)
        dates = [(end_day - timedelta(days=window_days - 1 - i)).isoformat() for i in range(window_days)]
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            by_day[run["date"]].extend(run["articles"])
        history_hits = [len(by_day.get(d, [])) for d in dates]
        history_best = [min((int(a["rank"]) for a in by_day.get(d, []) if a["rank"] > 0), default=0) for d in dates]
        unique_articles = {_safe_key(a) for xs in by_day.values() for a in xs} - {""}
        unique_accounts = {str(a.get("account_id") or a.get("account")) for xs in by_day.values() for a in xs if a.get("account_id") or a.get("account")}
        latest_run = runs[0] if runs else None
        item = {**seed, **source}
        item.update({
            "keyword": keyword_text,
            "keyword_id": kid, "topic": source.get("topic") or seed.get("topic") or keyword_text,
            "keyword_bucket": source.get("keyword_bucket") or seed.get("keyword_bucket"),
            "today_best": min((a["rank"] for a in (latest_run or {}).get("articles", []) if a["rank"] > 0), default=0),
            "today_count": len((latest_run or {}).get("articles", [])),
            "coverage_days": sum(x > 0 for x in history_hits), "tracked_accounts": len(unique_accounts),
            "article_count": len(unique_articles), "latest_run": latest_run,
            "runs": runs, "history_best": history_best, "history_hits": history_hits,
            "turnover_runs": [{"id": r["id"], "date": r["date"], "time": r["time"],
                               "articles": [{"article_id": a.get("article_id")} for a in r["articles"] if a.get("article_id")]}
                              for r in runs],
            "is_pinned": bool(source.get("pinned") or seed.get("is_pinned")),
            "pin_order": source.get("pin_order", seed.get("pin_order")),
        })
        lens: dict[str, dict[str, Any]] = {}
        for run in runs:
            for current in run["articles"]:
                aid = str(current.get("account_id") or "")
                if not aid:
                    continue
                entry = lens.setdefault(aid, {
                    "name": current.get("account") or creators.get(aid, {}).get("canonical_name") or "",
                    "account_id": aid, "headimg_url": current.get("account_headimg"),
                    "today_rank": None, "today_prev": None, "score": 0.0,
                    "hit_days": 0, "best_rank": None, "article_count": 0, "history": [],
                    "_days": set(), "_articles": set(),
                })
                entry["history"].append({"date": run["date"], "rank": current["rank"], "article_id": current.get("article_id")})
                entry["_days"].add(run["date"])
                entry["_articles"].add(_safe_key(current))
                entry["score"] += _rank_weight(int(current["rank"]))
                entry["best_rank"] = min(entry["best_rank"] or 9999, int(current["rank"]))
                if run is latest_run:
                    entry["today_rank"] = min(entry["today_rank"] or 9999, int(current["rank"]))
        if latest_run:
            previous_runs = [r for r in runs if r["date"] < latest_run["date"]]
            for entry in lens.values():
                prior = [a["rank"] for r in previous_runs for a in r["articles"]
                         if str(a.get("account_id") or "") == entry["account_id"]]
                entry["today_prev"] = min(prior) if prior else None
        for entry in lens.values():
            entry["score"] = round(entry["score"], 2)
            entry["hit_days"] = len(entry.pop("_days"))
            entry["article_count"] = len(entry.pop("_articles") - {""})
        item["accounts"] = sorted(lens.values(), key=lambda x: (x["today_rank"] is None, x["today_rank"] or 9999, -x["score"]))
        delta = _read_delta(connection, kid, start, end)
        if delta:
            metric_articles = {
                _safe_key(current)
                for articles in by_day.values()
                for current in articles
                if current.get("read_count") is not None
            } - {""}
            delta.setdefault("hit_articles", len(unique_articles))
            delta.setdefault("articles_with_metric", len(metric_articles))
            delta.setdefault(
                "coverage_ratio",
                len(metric_articles) / len(unique_articles)
                if unique_articles
                else None,
            )
        item["keyword_read_delta"] = delta or {
            "keyword_id": kid, "keyword": item["keyword"], "window_start": start,
            "window_end": end, "window_days": window_days, "status": "insufficient_data",
            "read_delta_estimated": None, "read_delta_raw": None,
            "steady_read_median": None, "confidence_score": None,
            "confidence_level": "insufficient",
            "trend_signal": None, "trend_label": "观察中",
            "recent_vs_baseline_ratio": None,
            "provisional_steady_read_median": None,
            "provisional_read_delta_estimated": None,
            "provisional_sample_count": None,
            "provisional_status": None,
            "insufficient_reason": "no_canonical_metric_observations",
        }
        for field in (
            "read_delta_estimated", "read_delta_raw", "steady_read_median",
            "confidence_score", "confidence_level", "trend_signal", "trend_label",
            "recent_vs_baseline_ratio", "observed_share", "estimated_share",
            "slot_coverage_ratio", "observed_days", "missing_days",
            "snapshot_count", "provisional_steady_read_median",
            "provisional_read_delta_estimated", "provisional_sample_count",
            "provisional_status", "status",
        ):
            item[field] = item["keyword_read_delta"].get(field)
        keyword_payloads[kid] = item

    # Account summaries are deliberately evidence-first.  Seed-only explanatory
    # fields remain intact, while all rank/count/score fields are recomputed.
    account_events: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for kid, item in keyword_payloads.items():
        for run in item["runs"]:
            for a in run["articles"]:
                aid = str(a.get("account_id") or "")
                if aid:
                    account_events[aid][run["date"]].append({
                        **a, "keyword_id": kid, "keyword": item["keyword"],
                        "topic": item.get("topic") or "", "bucket": item.get("keyword_bucket") or "",
                        "article_key": _safe_key(a), "published_at": a.get("published_at") or "",
                        "_day_idx": (date.fromisoformat(run["date"]) - start_day).days,
                    })
    account_payloads: dict[str, dict[str, Any]] = {}
    for aid in account_events:
        creator = creators.get(aid, {})
        seed = dict(seeds.get(("account", aid), {}))
        events = account_events[aid]
        history = [a for day in events.values() for a in day]
        today_events = events.get(end, [])
        yesterday_day = (end_day - timedelta(days=1)).isoformat()
        yesterday_events = events.get(yesterday_day, [])
        flat_events = history
        current_boards = _raw_boards(flat_events, window_days - 1, end_day)
        previous_boards = _raw_boards(flat_events, window_days - 2, end_day)
        current_normalized: dict[str, dict[str, Any]] = {}
        previous_normalized: dict[str, dict[str, Any]] = {}
        board_parts: dict[str, dict[str, Any]] = {}
        previous_parts: dict[str, dict[str, Any]] = {}
        # Benchmarks are attached after all accounts are collected below.
        item = {**seed, **creator}
        item.update({
            "name": creator.get("canonical_name") or seed.get("name") or "",
            "account_id": aid, "headimg_url": creator.get("headimg_url") or seed.get("headimg_url"),
            "_current_boards": current_boards, "_previous_boards": previous_boards,
            "history": sorted(history, key=lambda a: (str(a.get("keyword") or ""), int(a.get("rank") or 999))),
            "day_scores": [sum(_rank_weight(int(a["rank"])) for a in events.get(d, [])) for d in
                           [(end_day - timedelta(days=window_days - 1 - i)).isoformat() for i in range(window_days)]],
            "kw_count": len({a["keyword_id"] for a in history}), "article_count": len({_safe_key(a) for a in history}),
            "today_hit_count": len(today_events), "hit_days": len(events),
            "best_today": min((a["rank"] for a in today_events), default=None),
            "keywords": sorted({a["keyword"] for a in history}),
        })
        account_payloads[aid] = item

    board_benchmarks = {
        board: {
            key: _percentile([x["_current_boards"][board]["raw_axes"].get(key, 0)
                              for x in account_payloads.values()], SCORE_BENCHMARK_PERCENTILE)
            for key in config["weights"]
        }
        for board, config in BOARD_CONFIGS.items()
    }
    for item in account_payloads.values():
        current_hex: dict[str, Any] = {}
        previous_hex: dict[str, Any] = {}
        for board in BOARD_CONFIGS:
            current_normalized, current_parts = _score_board_snapshot(
                item["_current_boards"][board], board, board_benchmarks[board])
            previous_normalized, previous_parts = _score_board_snapshot(
                item["_previous_boards"][board], board, board_benchmarks[board])
            current_hex[board] = _hexagon(board, current_normalized, previous_normalized, board_benchmarks[board])
            item[{"account": "score", "timeliness": "timeliness_score", "today": "today_score"}[board]] = current_parts["score"]
            item[{"account": "score_raw", "timeliness": "timeliness_score_raw", "today": "today_score_raw"}[board]] = current_parts["score_raw"]
            item[{"account": "score_yesterday", "timeliness": "timeliness_score_yesterday", "today": "today_score_yesterday"}[board]] = previous_parts["score"]
            item[{"account": "score_delta", "timeliness": "timeliness_score_delta", "today": "today_score_delta"}[board]] = current_parts["score"] - previous_parts["score"]
            item[{"account": "score_level", "timeliness": "timeliness_score_level", "today": "today_score_level"}[board]] = _score_level(current_parts["score"])
            item[{"account": "account_score_parts", "timeliness": "timeliness_score_parts", "today": "today_score_parts"}[board]] = current_parts
            previous_hex[board] = previous_normalized
        item["account_score_hexagon"] = current_hex["account"]
        item["timeliness_score_hexagon"] = current_hex["timeliness"]
        item["today_score_hexagon"] = current_hex["today"]
        item["account_score_hexagon"]["population"] = {}
        item["timeliness_score_hexagon"]["population"] = {}
        item["today_score_hexagon"]["population"] = {}
        for board in BOARD_CONFIGS:
            item.pop("_current_boards", None)
            item.pop("_previous_boards", None)

    for field, rank_field in (("score", "rank"), ("timeliness_score", "timeliness_rank"), ("today_score", "today_rank")):
        ordered = sorted(account_payloads.values(), key=lambda x: (-int(x.get(field) or 0), str(x.get("name") or "")))
        for index, item in enumerate(ordered, 1):
            item[rank_field] = index
            item[f"{rank_field}_yesterday"] = None
        values = [float(x.get(field) or 0) for x in ordered]
        for item in ordered:
            hexagon_field = {
                "score": "account_score_hexagon",
                "timeliness_score": "timeliness_score_hexagon",
                "today_score": "today_score_hexagon",
            }[field]
            board = {
                "score": "account",
                "timeliness_score": "timeliness",
                "today_score": "today",
            }[field]
            axis_keys = [meta["key"] for meta in BOARD_CONFIGS[board]["axes_meta"]]
            current_axes = item[hexagon_field]["current"].get("axes", {})
            item[hexagon_field]["population"] = {
                "account_count": len(ordered),
                "score": _population_stat(values, float(item.get(field) or 0)),
                "axes": {
                    key: _population_stat(
                        [float(x[hexagon_field]["current"].get("axes", {}).get(key) or 0)
                         for x in ordered],
                        float(current_axes.get(key) or 0),
                    )
                    for key in axis_keys
                },
            }

    base = {
        "generated_at": latest, "window_days": window_days, "window_start": start,
        "window_end": end, "account_score_method": "three_board_breakthrough_v5_1",
        "keywords": sorted(keyword_payloads.values(), key=lambda x: (-int(x.get("today_count") or 0), str(x.get("keyword") or ""))),
        "accounts": sorted(account_payloads.values(), key=lambda x: (-int(x.get("score") or 0), str(x.get("name") or ""))),
    }
    base["keyword_source_total"] = len(base["keywords"])
    base["pinned_keyword_count"] = sum(bool(x.get("is_pinned")) for x in base["keywords"])
    bootstrap = {**base, "keywords": [
        {k: v for k, v in x.items() if k not in {"runs", "turnover_runs", "accounts"}}
        for x in base["keywords"]
    ]}
    return {
        "full": base,
        "bootstrap": bootstrap,
        **{f"keyword:{kid}": item for kid, item in keyword_payloads.items()},
        **{f"account:{aid}": item for aid, item in account_payloads.items()},
    }


def write(connection: Any, *, window_days: int = WINDOW_DAYS) -> dict[str, dict[str, Any]]:
    """Rebuild and persist projections using one transaction owned by caller."""
    payloads = rebuild(connection, window_days=window_days)
    if not payloads:
        return {}
    latest = next(iter(payloads.values())).get("generated_at")
    manifest_id = "wechat-live-projection-" + hashlib.sha256(str(latest).encode()).hexdigest()[:24]
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for key, payload in payloads.items():
        kind, subject = (key.split(":", 1) + [""])[:2] if ":" in key else (key, "")
        packed = _stored_projection_json(payload)
        digest = hashlib.sha256(packed.encode()).hexdigest()
        connection.execute(
            """INSERT INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,source_hash,
                source_manifest_id,source_ref,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(projection_kind,subject_id,source_hash) DO UPDATE SET
                 payload_json=excluded.payload_json,source_manifest_id=excluded.source_manifest_id,
                 source_ref=excluded.source_ref,updated_at=excluded.updated_at""",
            (f"live_{hashlib.sha256(f'{kind}:{subject}:{digest}'.encode()).hexdigest()[:28]}",
             kind, subject, packed, digest, manifest_id,
             "canonical://wechat-search/live-projection", now),
        )
    return payloads


__all__ = ["rebuild", "write", "WINDOW_DAYS"]
