from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable

from app.ingest.common import to_iso


METHOD = "schedule_adjusted_read_rate_v3"
TOP_RANK = 10
MIN_MATURE_DAYS = 15
MIN_HISTORY_POINTS = 3
HISTORY_LOOKBACK_DAYS = 60
RANK_ATTENTION = {
    rank: 1.0 / math.log2(rank + 1)
    for rank in range(1, TOP_RANK + 1)
}
FULL_ATTENTION = sum(RANK_ATTENTION.values())


@dataclass(frozen=True)
class ObservationPoint:
    observed_at: datetime
    read_count: int
    batch_id: str


@dataclass(frozen=True)
class RateInterval:
    start_at: datetime
    end_at: datetime
    rate: float
    duration_days: float


@dataclass(frozen=True)
class TrainingRecord:
    age_band: str
    read_band: str
    target_rate: float


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _batch_id_from_path(path: str | None) -> str:
    match = re.search(
        r"批量抓取/((?:web|overnight)_\d{8}_\d{2,6})/",
        str(path or ""),
    )
    return match.group(1) if match else ""


def _safe_median(values: Iterable[float], default: float = 0.0) -> float:
    cleaned = [
        float(value)
        for value in values
        if math.isfinite(float(value))
    ]
    return float(median(cleaned)) if cleaned else default


def _mean(values: Iterable[float]) -> float:
    cleaned = [
        float(value)
        for value in values
        if math.isfinite(float(value))
    ]
    return sum(cleaned) / len(cleaned) if cleaned else 0.0


def _keyword_frequency(keyword: dict[str, Any]) -> int:
    try:
        return max(1, int(keyword.get("refresh_frequency_days") or 1))
    except (TypeError, ValueError):
        return 1


def _planned_observation_count(window_days: int, frequency_days: int) -> int:
    return max(1, round(window_days / max(1, frequency_days)))


def _minimum_window_observations(window_days: int, frequency_days: int) -> int:
    if frequency_days >= 7:
        return 1
    return min(3, _planned_observation_count(window_days, frequency_days))


def _percentile(values: Iterable[float], ratio: float) -> float:
    ordered = sorted(
        float(value)
        for value in values
        if math.isfinite(float(value))
    )
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def _age_band(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 2:
        return "0-1d"
    if days < 8:
        return "2-7d"
    if days < 31:
        return "8-30d"
    if days < 91:
        return "31-90d"
    if days < 366:
        return "91-365d"
    return "366d+"


def _read_band(read_count: int | None) -> str:
    if read_count is None:
        return "unknown"
    if read_count < 100:
        return "0-99"
    if read_count < 500:
        return "100-499"
    if read_count < 2000:
        return "500-1999"
    if read_count < 10000:
        return "2000-9999"
    return "10000+"


class ExpectedRateModel:
    def __init__(self, records: list[TrainingRecord]):
        self.rate_cap = _percentile(
            (record.target_rate for record in records),
            0.995,
        )
        capped_rates = [
            min(self.rate_cap, max(0.0, record.target_rate))
            for record in records
        ]
        self.global_rate = _mean(capped_rates)
        age_groups: dict[str, list[float]] = defaultdict(list)
        age_read_groups: dict[tuple[str, str], list[float]] = defaultdict(list)
        for record, capped_rate in zip(records, capped_rates):
            age_groups[record.age_band].append(capped_rate)
            age_read_groups[(record.age_band, record.read_band)].append(capped_rate)
        self.age_groups = {
            key: (sum(values), len(values))
            for key, values in age_groups.items()
        }
        self.age_read_groups = {
            key: (sum(values), len(values))
            for key, values in age_read_groups.items()
        }

    @staticmethod
    def _shrunk_mean(
        grouped: dict[Any, tuple[float, int]],
        key: Any,
        parent: float,
        strength: float,
    ) -> tuple[float, int]:
        value = grouped.get(key)
        if not value:
            return parent, 0
        value_sum, count = value
        return (value_sum + parent * strength) / (count + strength), count

    def predict(self, age: str, reads: str) -> tuple[float, str]:
        age_rate, age_count = self._shrunk_mean(
            self.age_groups,
            age,
            self.global_rate,
            18,
        )
        age_read_rate, age_read_count = self._shrunk_mean(
            self.age_read_groups,
            (age, reads),
            age_rate,
            12,
        )
        if age_read_count:
            return age_read_rate, "age_read"
        if age_count:
            return age_rate, "age"
        return self.global_rate, "global"


def _build_observation_points(
    observations: list[dict[str, Any]],
) -> dict[str, list[ObservationPoint]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        article_id = str(row.get("article_id") or "")
        observed_at = _parse_iso(row.get("observed_at"))
        read_count = row.get("read_count")
        if not article_id or observed_at is None or read_count is None:
            continue
        batch_id = str(row.get("source_batch_id") or "")
        grouped[(article_id, batch_id or observed_at.isoformat())].append(row)

    by_article: dict[str, list[ObservationPoint]] = defaultdict(list)
    for (article_id, grouping_key), rows in grouped.items():
        timestamps = sorted(
            parsed
            for parsed in (_parse_iso(row.get("observed_at")) for row in rows)
            if parsed is not None
        )
        read_counts = sorted(
            int(row["read_count"])
            for row in rows
            if row.get("read_count") is not None
        )
        if not timestamps or not read_counts:
            continue
        by_article[article_id].append(ObservationPoint(
            observed_at=timestamps[len(timestamps) // 2],
            read_count=int(round(_safe_median(read_counts))),
            batch_id=(
                grouping_key
                if grouping_key.startswith(("web_", "overnight_"))
                else ""
            ),
        ))

    for article_id, points in by_article.items():
        points.sort(key=lambda point: point.observed_at)
        monotonic: list[ObservationPoint] = []
        running_max = 0
        for point in points:
            running_max = max(running_max, point.read_count)
            monotonic.append(ObservationPoint(
                observed_at=point.observed_at,
                read_count=running_max,
                batch_id=point.batch_id,
            ))
        by_article[article_id] = monotonic
    return by_article


def _build_rate_intervals(
    points_by_article: dict[str, list[ObservationPoint]],
) -> dict[str, list[RateInterval]]:
    result: dict[str, list[RateInterval]] = {}
    for article_id, points in points_by_article.items():
        intervals: list[RateInterval] = []
        for previous, current, duration_days in _stable_point_pairs(
            points,
            maximum_days=30,
        ):
            intervals.append(RateInterval(
                start_at=previous.observed_at,
                end_at=current.observed_at,
                rate=max(0, current.read_count - previous.read_count) / duration_days,
                duration_days=duration_days,
            ))
        result[article_id] = intervals
    return result


def _stable_point_pairs(
    points: list[ObservationPoint],
    *,
    minimum_days: float = 0.2,
    maximum_days: float = 15,
) -> list[tuple[ObservationPoint, ObservationPoint, float]]:
    """生成非重叠、最小间隔明确的观测对，使模型不受额外高频切片影响。"""
    pairs: list[tuple[ObservationPoint, ObservationPoint, float]] = []
    if len(points) < 2:
        return pairs
    anchor_index = 0
    while anchor_index < len(points) - 1:
        previous = points[anchor_index]
        selected_index: int | None = None
        selected_duration = 0.0
        for index in range(anchor_index + 1, len(points)):
            duration_days = (
                points[index].observed_at - previous.observed_at
            ).total_seconds() / 86400
            if duration_days < minimum_days:
                continue
            selected_index = index
            selected_duration = duration_days
            break
        if selected_index is None:
            break
        if selected_duration <= maximum_days:
            pairs.append((
                previous,
                points[selected_index],
                selected_duration,
            ))
        anchor_index = selected_index
    return pairs


def _build_training_records(
    articles_by_id: dict[str, dict[str, Any]],
    points_by_article: dict[str, list[ObservationPoint]],
) -> list[TrainingRecord]:
    records: list[TrainingRecord] = []
    for article_id, points in points_by_article.items():
        published_at = _parse_iso(
            articles_by_id.get(article_id, {}).get("published_at")
        )
        for previous, current, duration_days in _stable_point_pairs(
            points,
            maximum_days=15,
        ):
            age_days = (
                max(
                    0.25,
                    (previous.observed_at - published_at).total_seconds() / 86400,
                )
                if published_at
                else None
            )
            records.append(TrainingRecord(
                age_band=_age_band(age_days),
                read_band=_read_band(previous.read_count),
                target_rate=max(
                    0,
                    current.read_count - previous.read_count,
                ) / duration_days,
            ))
    return records


def _nearest_read_count(
    points: list[ObservationPoint],
    target: datetime,
) -> int | None:
    if not points:
        return None
    prior_points = [
        point
        for point in points
        if point.observed_at <= target + timedelta(hours=12)
    ]
    if prior_points:
        return prior_points[-1].read_count
    nearest = min(
        points,
        key=lambda point: abs((point.observed_at - target).total_seconds()),
    )
    if abs((nearest.observed_at - target).total_seconds()) <= 86400:
        return nearest.read_count
    return None


def _observed_rate_near(
    intervals: list[RateInterval],
    target: datetime,
    rate_cap: float,
) -> tuple[float | None, float]:
    best: tuple[float, RateInterval] | None = None
    for interval in intervals:
        if interval.start_at <= target <= interval.end_at:
            distance_days = 0.0
        else:
            distance_days = min(
                abs((target - interval.start_at).total_seconds()),
                abs((target - interval.end_at).total_seconds()),
            ) / 86400
        if distance_days > 3:
            continue
        score = distance_days + max(0, interval.duration_days - 7) * 0.15
        if best is None or score < best[0]:
            best = (score, interval)
    if best is None:
        return None, 0.0
    score, interval = best
    duration_confidence = min(1.0, interval.duration_days / 2)
    resolution_confidence = min(1.0, 7 / max(7, interval.duration_days))
    temporal_confidence = math.exp(-min(score, 3) / 2)
    reliability = min(
        0.9,
        0.9
        * duration_confidence
        * resolution_confidence
        * temporal_confidence,
    )
    return min(rate_cap, interval.rate), reliability


def _estimate_article_rate(
    article_id: str,
    captured_at: datetime,
    articles_by_id: dict[str, dict[str, Any]],
    points_by_article: dict[str, list[ObservationPoint]],
    intervals_by_article: dict[str, list[RateInterval]],
    model: ExpectedRateModel,
) -> tuple[float, float, str]:
    article = articles_by_id.get(article_id, {})
    published_at = _parse_iso(article.get("published_at"))
    age_days = (
        max(0.25, (captured_at - published_at).total_seconds() / 86400)
        if published_at
        else None
    )
    read_count = _nearest_read_count(
        points_by_article.get(article_id, []),
        captured_at,
    )
    prior, prior_source = model.predict(
        _age_band(age_days),
        _read_band(read_count),
    )
    observed_rate, reliability = _observed_rate_near(
        intervals_by_article.get(article_id, []),
        captured_at,
        model.rate_cap,
    )
    if observed_rate is None:
        return prior, 0.0, prior_source
    estimate = observed_rate * reliability + prior * (1 - reliability)
    return estimate, reliability, "observed_shrunk"


def _legacy_metrics(
    keywords: list[dict[str, Any]],
    snapshots_by_id: dict[str, dict[str, Any]],
    ranking_hits: list[dict[str, Any]],
    points_by_article: dict[str, list[ObservationPoint]],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, dict[str, Any]]:
    articles_by_keyword: dict[str, set[str]] = defaultdict(set)
    for hit in ranking_hits:
        snapshot = snapshots_by_id.get(str(hit.get("snapshot_id") or ""))
        article_id = str(hit.get("article_id") or "")
        if not snapshot or not article_id:
            continue
        captured_at = _parse_iso(snapshot.get("captured_at"))
        if not captured_at or not window_start <= captured_at <= window_end:
            continue
        keyword_id = str(snapshot.get("keyword_id") or "")
        if keyword_id:
            articles_by_keyword[keyword_id].add(article_id)

    result: dict[str, dict[str, Any]] = {}
    for keyword in keywords:
        keyword_id = str(keyword.get("keyword_id") or "")
        article_ids = articles_by_keyword.get(keyword_id, set())
        daily: dict[str, float] = defaultdict(float)
        articles_with_metric = 0
        articles_with_enough_points = 0
        articles_with_delta = 0
        observed_point_count = 0
        total_delta = 0.0
        for article_id in article_ids:
            points = [
                point
                for point in points_by_article.get(article_id, [])
                if window_start <= point.observed_at <= window_end
            ]
            if points:
                articles_with_metric += 1
                observed_point_count += len(points)
            if len(points) < 2:
                continue
            articles_with_enough_points += 1
            article_delta = max(0, points[-1].read_count - points[0].read_count)
            total_delta += article_delta
            if article_delta > 0:
                articles_with_delta += 1
            for previous, current in zip(points, points[1:]):
                daily[current.observed_at.date().isoformat()] += max(
                    0,
                    current.read_count - previous.read_count,
                )
        daily_values = [
            daily.get(
                (window_start.date() + timedelta(days=offset)).isoformat(),
                0.0,
            )
            for offset in range((window_end.date() - window_start.date()).days + 1)
        ]
        result[keyword_id] = {
            "hit_articles": len(article_ids),
            "articles_with_metric": articles_with_metric,
            "articles_with_enough_points": articles_with_enough_points,
            "articles_with_delta": articles_with_delta,
            "observed_point_count": observed_point_count,
            "read_delta_raw": round(total_delta),
            "steady_read_median": _safe_median(daily_values),
        }
    return result


def _term_momentum(
    primary_snapshots: dict[str, dict[str, Any]],
    snapshot_terms: list[dict[str, Any]],
    date_keys: list[str],
) -> dict[str, dict[str, Any]]:
    terms_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for term in snapshot_terms:
        snapshot_id = str(term.get("snapshot_id") or "")
        if snapshot_id in primary_snapshots:
            terms_by_snapshot[snapshot_id].append(term)

    daily: dict[
        str,
        dict[str, dict[str, list[float]]],
    ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for snapshot_id, snapshot in primary_snapshots.items():
        captured_at = _parse_iso(snapshot.get("captured_at"))
        keyword_id = str(snapshot.get("keyword_id") or "")
        if not captured_at or not keyword_id:
            continue
        date_key = captured_at.date().isoformat()
        for term in terms_by_snapshot.get(snapshot_id, []):
            term_text = str(term.get("term_text") or "").strip()
            term_type = str(term.get("term_type") or "")
            position = int(term.get("position") or 999)
            if term_text and position > 0:
                daily[keyword_id][date_key][f"{term_type}|{term_text}"].append(
                    1.0 / math.log2(position + 1)
                )

    recent_dates = set(date_keys[-3:])
    baseline_dates = set(date_keys[-10:-3])
    result: dict[str, dict[str, Any]] = {}
    for keyword_id, dates in daily.items():
        recent_scores: dict[str, list[float]] = defaultdict(list)
        baseline_scores: dict[str, list[float]] = defaultdict(list)
        for date_key, terms in dates.items():
            target = (
                recent_scores
                if date_key in recent_dates
                else baseline_scores
                if date_key in baseline_dates
                else None
            )
            if target is None:
                continue
            for term_text, values in terms.items():
                target[term_text].append(_safe_median(values))
        changes = []
        new_terms = 0
        rising_terms = 0
        for term_text in set(recent_scores) | set(baseline_scores):
            recent = _safe_median(recent_scores.get(term_text, []))
            baseline = _safe_median(baseline_scores.get(term_text, []))
            change = recent - baseline
            changes.append(change)
            if (
                recent > 0
                and baseline == 0
                and len(recent_scores.get(term_text, [])) >= 2
            ):
                new_terms += 1
            if change >= 0.15:
                rising_terms += 1
        result[keyword_id] = {
            "momentum": max(-1.0, min(1.0, _mean(changes) * 2.5)),
            "new_terms": new_terms,
            "rising_terms": rising_terms,
        }
    return result


def _confidence_level(score: float, status: str) -> str:
    if status != "ok":
        return "insufficient"
    if score >= 0.72:
        return "high"
    if score >= 0.50:
        return "medium"
    return "low"


def _trend_label(signal: float, status: str) -> str:
    if status != "ok":
        return "观察中"
    if signal >= 0.20:
        return "上升"
    if signal <= -0.20:
        return "下降"
    return "平稳"


def _aggregate_daily_points(
    daily: dict[str, list[dict[str, float]]],
    date_keys: Iterable[str],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for date_key in date_keys:
        snapshots_for_day = daily.get(date_key, [])
        if not snapshots_for_day:
            continue
        day_value = _safe_median(
            item["value"]
            for item in snapshots_for_day
        )
        direct_value = _safe_median(
            item["direct_value"]
            for item in snapshots_for_day
        )
        points.append({
            "date": date_key,
            "read_delta": day_value,
            "observed_component": min(day_value, direct_value),
            "estimated_component": max(0.0, day_value - direct_value),
            "snapshot_count": len(snapshots_for_day),
            "slot_coverage_ratio": _safe_median(
                item["slot_ratio"]
                for item in snapshots_for_day
            ),
            "article_count": round(_safe_median(
                item["article_count"]
                for item in snapshots_for_day
            )),
        })
    return points


def _estimate_calendar_value(
    date_key: str,
    observed_points: list[dict[str, Any]],
    *,
    long_term_steady: float,
    frequency_days: int,
) -> float:
    """用相邻观测插值，并随距离向长期常态收缩。

    低频抓取时，一个观测点不能代表整个窗口。相邻点之间使用线性插值；
    窗口边缘使用最近点，但距离越远越依赖长期常态，避免单次异常被平铺15天。
    """
    target = datetime.fromisoformat(date_key).date()
    previous: dict[str, Any] | None = None
    following: dict[str, Any] | None = None
    for point in observed_points:
        point_date = datetime.fromisoformat(str(point["date"])).date()
        if point_date <= target:
            previous = point
        if point_date >= target:
            following = point
            break

    if previous and following:
        previous_date = datetime.fromisoformat(str(previous["date"])).date()
        following_date = datetime.fromisoformat(str(following["date"])).date()
        gap = max(0, (following_date - previous_date).days)
        if gap == 0:
            return float(previous["read_delta"])
        offset = max(0, (target - previous_date).days)
        ratio = min(1.0, offset / gap)
        local_value = (
            float(previous["read_delta"]) * (1 - ratio)
            + float(following["read_delta"]) * ratio
        )
        nearest_distance = min(
            abs((target - previous_date).days),
            abs((following_date - target).days),
        )
    elif previous:
        previous_date = datetime.fromisoformat(str(previous["date"])).date()
        local_value = float(previous["read_delta"])
        nearest_distance = abs((target - previous_date).days)
    elif following:
        following_date = datetime.fromisoformat(str(following["date"])).date()
        local_value = float(following["read_delta"])
        nearest_distance = abs((following_date - target).days)
    else:
        return max(0.0, long_term_steady)

    distance_scale = max(2.0, float(frequency_days) * 0.9)
    if frequency_days >= 15:
        minimum_local_weight, maximum_local_weight = 0.10, 0.40
    elif frequency_days >= 7:
        minimum_local_weight, maximum_local_weight = 0.15, 0.70
    else:
        minimum_local_weight, maximum_local_weight = 0.20, 0.92
    local_weight = max(
        minimum_local_weight,
        min(
            maximum_local_weight,
            math.exp(-nearest_distance / distance_scale),
        ),
    )
    return max(
        0.0,
        local_value * local_weight + long_term_steady * (1 - local_weight),
    )


def _reconstruct_calendar_points(
    date_keys: list[str],
    window_observed_points: list[dict[str, Any]],
    history_observed_points: list[dict[str, Any]],
    *,
    long_term_steady: float,
    frequency_days: int,
    status: str,
) -> list[dict[str, Any]]:
    observed_by_date = {
        str(point["date"]): point
        for point in window_observed_points
    }
    if status != "ok":
        return [
            {
                **point,
                "read_delta": round(float(point["read_delta"])),
                "observed_component": round(float(point["observed_component"])),
                "estimated_component": round(float(point["estimated_component"])),
                "is_imputed_day": False,
            }
            for point in window_observed_points
        ]

    result: list[dict[str, Any]] = []
    for date_key in date_keys:
        observed = observed_by_date.get(date_key)
        if observed:
            result.append({
                **observed,
                "read_delta": round(float(observed["read_delta"])),
                "observed_component": round(float(observed["observed_component"])),
                "estimated_component": round(float(observed["estimated_component"])),
                "is_imputed_day": False,
            })
            continue
        estimate = _estimate_calendar_value(
            date_key,
            history_observed_points,
            long_term_steady=long_term_steady,
            frequency_days=frequency_days,
        )
        result.append({
            "date": date_key,
            "read_delta": round(estimate),
            "observed_component": 0,
            "estimated_component": round(estimate),
            "snapshot_count": 0,
            "slot_coverage_ratio": 0.0,
            "article_count": 0,
            "is_imputed_day": True,
        })
    return result


def build_keyword_read_deltas(
    observations: list[dict[str, Any]],
    keywords: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    ranking_hits: list[dict[str, Any]],
    *,
    articles: list[dict[str, Any]] | None = None,
    snapshot_terms: list[dict[str, Any]] | None = None,
    window_days: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot_dates = [
        parsed
        for parsed in (_parse_iso(row.get("captured_at")) for row in snapshots)
        if parsed is not None
    ]
    if not snapshot_dates:
        return [], {"error": "no dated snapshots"}

    window_end = max(snapshot_dates)
    window_start = datetime.combine(
        window_end.date() - timedelta(days=window_days - 1),
        datetime.min.time(),
    )
    history_start = datetime.combine(
        window_end.date() - timedelta(days=HISTORY_LOOKBACK_DAYS - 1),
        datetime.min.time(),
    )
    date_keys = [
        (window_start.date() + timedelta(days=offset)).isoformat()
        for offset in range(window_days)
    ]
    history_date_keys = [
        (history_start.date() + timedelta(days=offset)).isoformat()
        for offset in range(
            (window_end.date() - history_start.date()).days + 1
        )
    ]
    articles_by_id = {
        str(row.get("article_id") or ""): row
        for row in articles or []
        if row.get("article_id")
    }
    points_by_article = _build_observation_points(observations)
    intervals_by_article = _build_rate_intervals(points_by_article)
    training_records = _build_training_records(
        articles_by_id,
        points_by_article,
    )
    if not training_records:
        return [], {"error": "no reusable read-rate intervals"}
    model = ExpectedRateModel(training_records)

    all_snapshots_by_id = {
        str(row.get("snapshot_id") or ""): row
        for row in snapshots
        if row.get("snapshot_id")
    }
    primary_snapshots = {
        snapshot_id: row
        for snapshot_id, row in all_snapshots_by_id.items()
        if row.get("status") == "success"
        and row.get("is_primary") is True
        and (captured_at := _parse_iso(row.get("captured_at"))) is not None
        and history_start <= captured_at <= window_end
    }
    first_success_at: dict[str, datetime] = {}
    for row in snapshots:
        if row.get("status") != "success" or row.get("is_primary") is not True:
            continue
        captured_at = _parse_iso(row.get("captured_at"))
        keyword_id = str(row.get("keyword_id") or "")
        if not captured_at or not keyword_id:
            continue
        previous = first_success_at.get(keyword_id)
        if previous is None or captured_at < previous:
            first_success_at[keyword_id] = captured_at
    hits_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in ranking_hits:
        snapshot_id = str(hit.get("snapshot_id") or "")
        rank = int(hit.get("rank") or 999)
        if snapshot_id in primary_snapshots and rank <= TOP_RANK:
            hits_by_snapshot[snapshot_id].append(hit)

    exposures_by_batch_article: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for snapshot_id, snapshot in primary_snapshots.items():
        captured_at = _parse_iso(snapshot.get("captured_at"))
        keyword_id = str(snapshot.get("keyword_id") or "")
        batch_id = _batch_id_from_path(snapshot.get("source_file_path")) or snapshot_id
        if not captured_at or not keyword_id:
            continue
        for hit in hits_by_snapshot.get(snapshot_id, []):
            article_id = str(hit.get("article_id") or "")
            rank = int(hit.get("rank") or 999)
            if article_id:
                exposures_by_batch_article[(batch_id, article_id)].append({
                    "snapshot_id": snapshot_id,
                    "keyword_id": keyword_id,
                    "article_id": article_id,
                    "rank": rank,
                    "captured_at": captured_at,
                })

    snapshot_values: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "value": 0.0,
            "direct_value": 0.0,
            "slot_count": 0.0,
            "attention": 0.0,
            "article_count": 0.0,
        }
    )
    source_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for (_, article_id), exposures in exposures_by_batch_article.items():
        estimates = [
            _estimate_article_rate(
                article_id,
                exposure["captured_at"],
                articles_by_id,
                points_by_article,
                intervals_by_article,
                model,
            )
            for exposure in exposures
        ]
        batch_rate = _safe_median(estimate[0] for estimate in estimates)
        batch_reliability = _safe_median(estimate[1] for estimate in estimates)
        source = max(
            (estimate[2] for estimate in estimates),
            key=lambda name: sum(
                1
                for estimate in estimates
                if estimate[2] == name
            ),
        )
        for exposure in exposures:
            attention = RANK_ATTENTION[exposure["rank"]]
            # 关键词级指标必须独立于“同批次还搜索了哪些词”。跨词去重适合
            # 词库总盘点，不适合单个关键词横向比较，否则调度批次变化会让
            # 同一关键词在文章和排名不变时产生机械涨跌。
            allocated = batch_rate * attention
            values = snapshot_values[exposure["snapshot_id"]]
            values["value"] += allocated
            values["direct_value"] += allocated * batch_reliability
            values["slot_count"] += 1
            values["attention"] += attention
            values["article_count"] += 1
            source_counts[exposure["keyword_id"]][source] += 1

    daily_values: dict[
        str,
        dict[str, list[dict[str, float]]],
    ] = defaultdict(lambda: defaultdict(list))
    for snapshot_id, values in snapshot_values.items():
        snapshot = primary_snapshots[snapshot_id]
        captured_at = _parse_iso(snapshot.get("captured_at"))
        keyword_id = str(snapshot.get("keyword_id") or "")
        if (
            not captured_at
            or not keyword_id
            or values["slot_count"] < 5
        ):
            continue
        completeness_multiplier = min(
            1.4,
            FULL_ATTENTION / max(
                values["attention"],
                FULL_ATTENTION / 2,
            ),
        )
        standardized = values["value"] * completeness_multiplier
        daily_values[keyword_id][captured_at.date().isoformat()].append({
            "value": standardized,
            "direct_value": values["direct_value"] * completeness_multiplier,
            "slot_ratio": min(1.0, values["slot_count"] / TOP_RANK),
            "article_count": values["article_count"],
        })

    legacy = _legacy_metrics(
        keywords,
        all_snapshots_by_id,
        ranking_hits,
        points_by_article,
        window_start,
        window_end,
    )
    terms = _term_momentum(
        primary_snapshots,
        snapshot_terms or [],
        date_keys,
    )

    rows: list[dict[str, Any]] = []
    for keyword in keywords:
        keyword_id = str(keyword.get("keyword_id") or "")
        daily = daily_values.get(keyword_id, {})
        frequency_days = _keyword_frequency(keyword)
        history_multiplier = 4 if frequency_days >= 7 else 3
        history_lookback_days = min(
            HISTORY_LOOKBACK_DAYS,
            max(window_days, frequency_days * history_multiplier),
        )
        keyword_history_start = (
            window_end.date() - timedelta(days=history_lookback_days - 1)
        ).isoformat()
        history_points = _aggregate_daily_points(
            daily,
            (
                date_key
                for date_key in history_date_keys
                if date_key >= keyword_history_start
            ),
        )
        observed_points = [
            point
            for point in history_points
            if point["date"] in set(date_keys)
        ]
        observed_days = len(observed_points)
        snapshot_count = sum(
            point["snapshot_count"]
            for point in observed_points
        )
        first_observed = first_success_at.get(keyword_id)
        calendar_span_days = (
            max(0, (window_end.date() - first_observed.date()).days + 1)
            if first_observed
            else 0
        )
        planned_observation_count = _planned_observation_count(
            window_days,
            frequency_days,
        )
        minimum_window_observations = _minimum_window_observations(
            window_days,
            frequency_days,
        )
        long_term_steady = (
            _safe_median(point["read_delta"] for point in history_points)
            if history_points
            else 0.0
        )
        status = (
            "ok"
            if calendar_span_days >= MIN_MATURE_DAYS
            and len(history_points) >= MIN_HISTORY_POINTS
            and observed_days >= minimum_window_observations
            and snapshot_count >= minimum_window_observations
            else "insufficient_data"
        )
        missing_days = window_days - observed_days
        chart_points = _reconstruct_calendar_points(
            date_keys,
            observed_points,
            history_points,
            long_term_steady=long_term_steady,
            frequency_days=frequency_days,
            status=status,
        )
        steady = (
            _safe_median(point["read_delta"] for point in chart_points)
            if status == "ok"
            else None
        )
        estimated_delta = (
            sum(float(point["read_delta"]) for point in chart_points)
            if status == "ok"
            else None
        )
        provisional_sample_count = (
            snapshot_count
            if status == "insufficient_data"
            else None
        )
        provisional_steady = (
            _safe_median(
                point["read_delta"]
                for point in chart_points
            )
            if status == "insufficient_data" and snapshot_count >= 2
            else None
        )
        provisional_delta = (
            provisional_steady * window_days
            if provisional_steady is not None
            else None
        )
        provisional_status = (
            "provisional"
            if provisional_steady is not None
            else "insufficient_sample"
            if status == "insufficient_data"
            else None
        )

        total_value = sum(
            float(point["read_delta"])
            for point in chart_points
        )
        direct_value = sum(
            float(point["observed_component"])
            for point in chart_points
        )
        observed_share = (
            min(1.0, direct_value / total_value)
            if total_value > 0
            else 0.0
        )
        slot_coverage = _mean(
            point["slot_coverage_ratio"]
            for point in observed_points
        )
        planned_coverage = min(
            1.0,
            observed_days / max(1, planned_observation_count),
        )
        history_expected_count = max(
            MIN_HISTORY_POINTS,
            _planned_observation_count(
                history_lookback_days,
                frequency_days,
            ),
        )
        history_coverage = min(
            1.0,
            len(history_points) / history_expected_count,
        )
        last_observed_date = (
            datetime.fromisoformat(str(history_points[-1]["date"])).date()
            if history_points
            else None
        )
        freshness_days = (
            max(0, (window_end.date() - last_observed_date).days)
            if last_observed_date
            else window_days
        )
        freshness_score = max(
            0.0,
            1 - freshness_days / max(2.0, frequency_days * 1.5),
        )
        confidence_score = (
            planned_coverage * 0.25
            + slot_coverage * 0.15
            + observed_share * 0.25
            + history_coverage * 0.20
            + freshness_score * 0.15
        )
        term_signal = terms.get(keyword_id, {
            "momentum": 0.0,
            "new_terms": 0,
            "rising_terms": 0,
        })
        recent_dates = set(date_keys[-3:])
        baseline_dates = set(date_keys[-10:-3])
        recent_value = _safe_median(
            point["read_delta"]
            for point in chart_points
            if point["date"] in recent_dates
        )
        baseline_value = _safe_median(
            point["read_delta"]
            for point in chart_points
            if point["date"] in baseline_dates
        )
        read_trend = (
            (recent_value - baseline_value)
            / max(baseline_value, model.global_rate, 1.0)
            if recent_value or baseline_value
            else 0.0
        )
        trend_signal = max(
            -1.0,
            min(
                1.0,
                read_trend * 0.8
                + float(term_signal["momentum"]) * 0.2,
            ),
        )
        confidence_level = _confidence_level(
            confidence_score,
            status,
        )
        imputed_value = sum(
            float(point["read_delta"])
            for point in chart_points
            if point.get("is_imputed_day")
        )
        imputed_day_share = (
            min(1.0, imputed_value / total_value)
            if total_value > 0
            else 0.0
        )
        legacy_row = legacy.get(keyword_id, {})
        hit_articles = int(legacy_row.get("hit_articles") or 0)
        rows.append({
            "keyword_id": keyword_id,
            "keyword": keyword.get("keyword_text") or "",
            "window_start": to_iso(window_start),
            "window_end": to_iso(window_end),
            "window_days": window_days,
            "method": METHOD,
            "status": status,
            "refresh_frequency_days": frequency_days,
            "history_lookback_days": history_lookback_days,
            "first_observed_at": (
                to_iso(first_observed)
                if first_observed
                else None
            ),
            "last_observed_at": (
                datetime.combine(
                    last_observed_date,
                    datetime.min.time(),
                ).isoformat()
                if last_observed_date
                else None
            ),
            "calendar_span_days": calendar_span_days,
            "steady_read_median": (
                round(float(steady))
                if steady is not None
                else None
            ),
            "read_delta_estimated": (
                round(float(estimated_delta))
                if estimated_delta is not None
                else None
            ),
            "provisional_steady_read_median": (
                round(float(provisional_steady))
                if provisional_steady is not None
                else None
            ),
            "provisional_read_delta_estimated": (
                round(float(provisional_delta))
                if provisional_delta is not None
                else None
            ),
            "provisional_sample_count": provisional_sample_count,
            "provisional_status": provisional_status,
            "read_delta_raw": (
                int(legacy_row.get("read_delta_raw") or 0)
                if legacy_row
                else None
            ),
            "legacy_steady_read_median": (
                round(float(legacy_row.get("steady_read_median") or 0))
                if legacy_row
                else None
            ),
            "hit_articles": hit_articles,
            "articles_with_metric": int(
                legacy_row.get("articles_with_metric") or 0
            ),
            "articles_with_enough_points": int(
                legacy_row.get("articles_with_enough_points") or 0
            ),
            "articles_with_delta": int(
                legacy_row.get("articles_with_delta") or 0
            ),
            "observed_point_count": int(
                legacy_row.get("observed_point_count") or 0
            ),
            "coverage_ratio": (
                round(
                    int(legacy_row.get("articles_with_metric") or 0)
                    / hit_articles,
                    4,
                )
                if hit_articles
                else None
            ),
            "observed_days": observed_days,
            "missing_days": missing_days,
            "history_observed_days": len(history_points),
            "planned_observation_count": planned_observation_count,
            "planned_coverage_ratio": round(planned_coverage, 4),
            "freshness_days": freshness_days,
            "snapshot_count": snapshot_count,
            "slot_coverage_ratio": round(slot_coverage, 4),
            "observed_share": round(observed_share, 4),
            "estimated_share": round(1 - observed_share, 4),
            "imputed_day_share": round(imputed_day_share, 4),
            "confidence_score": round(confidence_score, 4),
            "confidence_level": confidence_level,
            "trend_signal": round(trend_signal, 4),
            "trend_label": _trend_label(trend_signal, status),
            "recent_vs_baseline_ratio": round(read_trend, 4),
            "term_momentum": round(float(term_signal["momentum"]), 4),
            "new_term_count": int(term_signal["new_terms"]),
            "rising_term_count": int(term_signal["rising_terms"]),
            "imputation_sources": dict(source_counts.get(keyword_id, {})),
            "daily_read_delta_points": chart_points,
            "model": {
                "top_rank": TOP_RANK,
                "prior": "article_age_x_current_read_band_expected",
                "rate_cap": "training_interval_p99_5",
                "rank_weight": "1/log2(rank+1)",
                "overlap_adjustment": "none_for_keyword_metric",
                "snapshot_aggregation": "daily_median",
                "missing_day_fill": "neighbor_interpolation_shrunk_to_long_term_median",
                "maturity_gate_days": MIN_MATURE_DAYS,
                "history_points_required": MIN_HISTORY_POINTS,
                "trend_weights": {
                    "fair_read_change": 0.8,
                    "suggestion_related_momentum": 0.2,
                },
            },
        })

    rows.sort(key=lambda item: (
        item["status"] != "ok",
        -(item["read_delta_estimated"] or 0),
        item["keyword"],
    ))
    stats = {
        "window_start": to_iso(window_start),
        "window_end": to_iso(window_end),
        "window_days": window_days,
        "method": METHOD,
        "keywords": len(rows),
        "keywords_ok": sum(
            1
            for item in rows
            if item["status"] == "ok"
        ),
        "keywords_insufficient": sum(
            1
            for item in rows
            if item["status"] != "ok"
        ),
        "training_intervals": len(training_records),
        "rate_cap": round(model.rate_cap, 2),
    }
    return rows, stats


__all__ = [
    "METHOD",
    "build_keyword_read_deltas",
]
