from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from app.ingest.common import (
    PROJECT_ROOT,
    iter_article_content_paths,
    kw_id,
    md5_short,
    normalize_title_key,
    normalize_url,
    project_display_path,
    to_iso,
)
from app.ingest.content.article_content_indexer import (
    _is_likely_article_content_head,
    _read_head,
    extract_content_meta,
)
from app.ingest.content.metrics_parser import extract_metrics


PARSER_VERSION = "article_metric_observation_v1"


def _parse_compact_time(raw: str) -> tuple[str, str, str] | None:
    text = str(raw or "").strip()
    if not text.isdigit():
        return None
    if len(text) == 6:
        return text[:2], text[2:4], text[4:6]
    if len(text) == 4:
        return text[:2], text[2:4], "00"
    if len(text) == 3:
        return "0" + text[0], text[1:3], "00"
    if len(text) == 2:
        return text, "00", "00"
    return None


def _datetime_from_parts(date_raw: str, time_raw: str) -> datetime | None:
    parts = _parse_compact_time(time_raw)
    if not parts:
        return None
    hour, minute, second = parts
    try:
        return datetime.strptime(f"{date_raw}{hour}{minute}{second}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _strip_keyword_folder_prefix(folder: str) -> str:
    return re.sub(r"^\d{1,4}_", "", folder or "").strip()


def _batch_context(display_path: str) -> dict[str, str]:
    match = re.search(
        r"微信搜索结果/批量抓取/(?P<batch_id>(?:web|overnight)_\d{8}_\d{2,6})/(?P<keyword_folder>[^/]+)/",
        display_path,
    )
    if not match:
        return {}
    batch_id = match.group("batch_id")
    batch_match = re.match(r"(?:web|overnight)_(\d{8})_(\d{2,6})$", batch_id)
    batch_at = _datetime_from_parts(batch_match.group(1), batch_match.group(2)) if batch_match else None
    keyword = _strip_keyword_folder_prefix(match.group("keyword_folder"))
    return {
        "batch_id": batch_id,
        "batch_observed_at": to_iso(batch_at) if batch_at else "",
        "source_keyword": keyword,
        "source_keyword_id": kw_id(keyword) if keyword else "",
    }


def _filename_observed_at(path: Path) -> tuple[str, str]:
    name = path.name
    match = re.match(r"(?P<date>\d{6})(?:_(?P<time>\d{6}))?_", name)
    if not match:
        return "", ""
    date_raw = "20" + match.group("date")
    time_raw = match.group("time")
    if time_raw:
        observed = _datetime_from_parts(date_raw, time_raw)
        return (to_iso(observed), "second") if observed else ("", "")
    try:
        observed = datetime.strptime(date_raw, "%Y%m%d")
    except ValueError:
        return "", ""
    return to_iso(observed), "date"


def _snapshot_batch_id(source_file_path: str) -> str:
    match = re.search(r"微信搜索结果/批量抓取/((?:web|overnight)_\d{8}_\d{2,6})/", source_file_path or "")
    return match.group(1) if match else ""


def _build_snapshot_lookup(snapshots: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        batch_id = _snapshot_batch_id(snapshot.get("source_file_path", ""))
        keyword_id = snapshot.get("keyword_id", "")
        if not batch_id or not keyword_id:
            continue
        key = (keyword_id, batch_id)
        current = lookup.get(key)
        if current is None or str(snapshot.get("captured_at", "")) > str(current.get("captured_at", "")):
            lookup[key] = snapshot
    return lookup


def _resolve_observed_time(
    path: Path,
    display_path: str,
    batch: dict[str, str],
    snapshot_lookup: dict[tuple[str, str], dict[str, Any]],
) -> tuple[str, str, str, str]:
    """Return observed_at, precision, source, source_snapshot_id.

    We deliberately avoid filesystem mtime/ctime as the factual timestamp. They
    change when files are copied or touched, while batch directories and
    snapshot metadata are reproducible evidence from the crawler.
    """
    batch_id = batch.get("batch_id", "")
    source_keyword_id = batch.get("source_keyword_id", "")
    if batch_id and source_keyword_id:
        snapshot = snapshot_lookup.get((source_keyword_id, batch_id))
        if snapshot and snapshot.get("captured_at"):
            return snapshot["captured_at"], "second", "snapshot_source_file", snapshot.get("snapshot_id", "")
    if batch.get("batch_observed_at"):
        return batch["batch_observed_at"], "second", "batch_dir", ""
    filename_at, filename_precision = _filename_observed_at(path)
    if filename_at:
        return filename_at, filename_precision, "filename", ""
    return "", "", "", ""


def _article_indexes(articles: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        url = normalize_url(article.get("normalized_url") or article.get("raw_url") or "")
        if url and not url.startswith("placeholder://"):
            by_url[url].append(article)
        title_key = normalize_title_key(article.get("title") or "")
        if title_key:
            by_title[title_key].append(article)
    return by_url, by_title


def _resolve_articles_for_content(
    meta: Any,
    by_url: dict[str, list[dict[str, Any]]],
    by_title: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str, str]:
    if meta.normalized_url:
        matched = by_url.get(meta.normalized_url, [])
        if matched:
            return matched, "url", "high"
    if meta.title_key:
        matched = by_title.get(meta.title_key, [])
        if len(matched) == 1:
            return matched, "title", "medium"
        if len(matched) > 1:
            return [], "title_ambiguous", "low"
    return [], "not_matched", "low"


def build_article_metric_observations(
    articles: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_url, by_title = _article_indexes(articles)
    snapshot_lookup = _build_snapshot_lookup(snapshots)

    observations: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    stats: dict[str, Any] = {
        "content_paths_seen": 0,
        "content_like_files": 0,
        "files_with_metrics": 0,
        "files_with_observed_at": 0,
        "files_linked_to_article": 0,
        "observations": 0,
        "skipped_no_metric": 0,
        "skipped_no_time": 0,
        "skipped_no_article_match": 0,
        "skipped_ambiguous_title": 0,
        "observed_at_sources": defaultdict(int),
        "match_methods": defaultdict(int),
    }

    for path in iter_article_content_paths():
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        stats["content_paths_seen"] += 1
        if limit is not None and stats["content_paths_seen"] > limit:
            break

        head = _read_head(resolved, 40)
        if not _is_likely_article_content_head(resolved, head):
            continue
        stats["content_like_files"] += 1

        meta = extract_content_meta(resolved, head_lines=head)
        if not meta:
            continue
        metrics = extract_metrics(resolved)
        if (
            metrics.read_count is None
            and metrics.like_count is None
            and metrics.friends_follow_count is None
            and metrics.original_article_count is None
        ):
            stats["skipped_no_metric"] += 1
            continue
        stats["files_with_metrics"] += 1

        display_path = project_display_path(resolved)
        batch = _batch_context(display_path)
        observed_at, observed_precision, observed_source, source_snapshot_id = _resolve_observed_time(
            resolved,
            display_path,
            batch,
            snapshot_lookup,
        )
        if not observed_at:
            stats["skipped_no_time"] += 1
            continue
        stats["files_with_observed_at"] += 1

        matched_articles, match_method, confidence = _resolve_articles_for_content(meta, by_url, by_title)
        if not matched_articles:
            if match_method == "title_ambiguous":
                stats["skipped_ambiguous_title"] += 1
            else:
                stats["skipped_no_article_match"] += 1
            continue
        stats["files_linked_to_article"] += 1
        stats["observed_at_sources"][observed_source] += 1
        stats["match_methods"][match_method] += 1

        for article in matched_articles:
            article_id = article.get("article_id") or ""
            observation_key = f"{article_id}|{display_path}"
            observations.append({
                "observation_id": f"amo_{md5_short(observation_key, 16)}",
                "article_id": article_id,
                "account_id": article.get("account_id") or "",
                "title": article.get("title") or "",
                "normalized_url": normalize_url(article.get("normalized_url") or article.get("raw_url") or meta.normalized_url or ""),
                "observed_at": observed_at,
                "observed_at_precision": observed_precision,
                "observed_at_source": observed_source,
                "source_file_path": display_path,
                "source_batch_id": batch.get("batch_id", ""),
                "source_keyword_id": batch.get("source_keyword_id", ""),
                "source_keyword": batch.get("source_keyword", ""),
                "source_snapshot_id": source_snapshot_id,
                "read_count": metrics.read_count,
                "like_count": metrics.like_count,
                "friends_follow_count": metrics.friends_follow_count,
                "original_article_count": metrics.original_article_count,
                "match_method": match_method,
                "extract_confidence": confidence,
                "parser_version": PARSER_VERSION,
            })

    observations.sort(key=lambda item: (
        item.get("observed_at") or "",
        item.get("article_id") or "",
        item.get("source_file_path") or "",
    ))
    stats["observations"] = len(observations)
    stats["observed_at_sources"] = dict(stats["observed_at_sources"])
    stats["match_methods"] = dict(stats["match_methods"])
    return observations, stats


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _article_read_delta(
    points: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[int | None, int, str, str]:
    in_window = [
        (_parse_iso(point.get("observed_at", "")), point.get("read_count"))
        for point in points
        if point.get("read_count") is not None
    ]
    in_window = [
        (dt, int(read))
        for dt, read in in_window
        if dt and window_start <= dt <= window_end
    ]
    if len(in_window) < 2:
        return None, len(in_window), "", ""
    in_window.sort(key=lambda item: item[0])
    baseline_dt, baseline_read = in_window[0]
    latest_dt, latest_read = in_window[-1]
    return max(0, latest_read - baseline_read), len(in_window), to_iso(baseline_dt), to_iso(latest_dt)


def _empty_daily_read_delta_points(window_start: datetime, window_end: datetime) -> dict[str, dict[str, Any]]:
    points: dict[str, dict[str, Any]] = {}
    cursor = window_start.date()
    end_date = window_end.date()
    while cursor <= end_date:
        date_key = cursor.isoformat()
        points[date_key] = {
            "date": date_key,
            "read_delta": 0,
            "article_count": 0,
            "observation_count": 0,
        }
        cursor += timedelta(days=1)
    return points


def _add_article_daily_read_deltas(
    daily_points: dict[str, dict[str, Any]],
    points: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> None:
    in_window = [
        (_parse_iso(point.get("observed_at", "")), point.get("read_count"))
        for point in points
        if point.get("read_count") is not None
    ]
    in_window = [
        (dt, int(read))
        for dt, read in in_window
        if dt and window_start <= dt <= window_end
    ]
    if len(in_window) < 2:
        return
    in_window.sort(key=lambda item: item[0])
    remaining_delta = max(0, in_window[-1][1] - in_window[0][1])
    if remaining_delta <= 0:
        return

    touched_dates: set[str] = set()
    for idx in range(1, len(in_window)):
        if remaining_delta <= 0:
            break
        prev_dt, prev_read = in_window[idx - 1]
        curr_dt, curr_read = in_window[idx]
        delta = min(max(0, curr_read - prev_read), remaining_delta)
        if delta <= 0:
            continue
        remaining_delta -= delta
        date_key = curr_dt.date().isoformat()
        point = daily_points.get(date_key)
        if point is None:
            continue
        point["read_delta"] += delta
        point["observation_count"] += 1
        if delta > 0:
            touched_dates.add(date_key)

    for date_key in touched_dates:
        daily_points[date_key]["article_count"] += 1


def _steady_read_median(daily_points: dict[str, dict[str, Any]]) -> float | None:
    values = [
        max(0, int(point.get("read_delta") or 0))
        for point in daily_points.values()
    ]
    if not values:
        return None
    return float(median(values))


def build_keyword_read_deltas(
    observations: list[dict[str, Any]],
    keywords: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    ranking_hits: list[dict[str, Any]],
    *,
    window_days: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observation_dates = [_parse_iso(item.get("observed_at", "")) for item in observations]
    snapshot_dates = [_parse_iso(item.get("captured_at", "")) for item in snapshots]
    all_dates = [dt for dt in observation_dates + snapshot_dates if dt is not None]
    if not all_dates:
        return [], {"error": "no dated observations or snapshots"}

    window_end = max(all_dates)
    window_start = window_end - timedelta(days=window_days)
    snapshot_by_id = {item.get("snapshot_id"): item for item in snapshots}
    observations_by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        if item.get("article_id"):
            observations_by_article[item["article_id"]].append(item)

    hit_articles_by_keyword: dict[str, set[str]] = defaultdict(set)
    for hit in ranking_hits:
        snapshot = snapshot_by_id.get(hit.get("snapshot_id"))
        captured_at = _parse_iso(snapshot.get("captured_at", "")) if snapshot else None
        if not captured_at or captured_at < window_start or captured_at > window_end:
            continue
        keyword_id = snapshot.get("keyword_id", "")
        article_id = hit.get("article_id", "")
        if keyword_id and article_id:
            hit_articles_by_keyword[keyword_id].add(article_id)

    rows: list[dict[str, Any]] = []
    for keyword in keywords:
        keyword_id = keyword.get("keyword_id") or ""
        hit_articles = hit_articles_by_keyword.get(keyword_id, set())
        read_delta_raw = 0
        articles_with_metric = 0
        articles_with_enough_points = 0
        articles_with_delta = 0
        observed_point_count = 0
        evidence_start = ""
        evidence_end = ""
        daily_points = _empty_daily_read_delta_points(window_start, window_end)

        for article_id in sorted(hit_articles):
            points = observations_by_article.get(article_id, [])
            if points:
                articles_with_metric += 1
                observed_point_count += len(points)
            _add_article_daily_read_deltas(daily_points, points, window_start, window_end)
            delta, point_count, start_at, end_at = _article_read_delta(points, window_start, window_end)
            if delta is None:
                continue
            articles_with_enough_points += 1
            read_delta_raw += delta
            if delta > 0:
                articles_with_delta += 1
            if start_at and (not evidence_start or start_at < evidence_start):
                evidence_start = start_at
            if end_at and (not evidence_end or end_at > evidence_end):
                evidence_end = end_at

        status = "ok" if articles_with_enough_points > 0 else "insufficient_data"
        hit_article_count = len(hit_articles)
        steady_read_median = _steady_read_median(daily_points) if status == "ok" else None
        rows.append({
            "keyword_id": keyword_id,
            "keyword": keyword.get("keyword_text") or "",
            "window_start": to_iso(window_start),
            "window_end": to_iso(window_end),
            "window_days": window_days,
            "method": "article_membership_raw_v1",
            "status": status,
            "read_delta_raw": read_delta_raw if status == "ok" else None,
            "steady_read_median": steady_read_median,
            "hit_articles": hit_article_count,
            "articles_with_metric": articles_with_metric,
            "articles_with_enough_points": articles_with_enough_points,
            "articles_with_delta": articles_with_delta,
            "observed_point_count": observed_point_count,
            "coverage_ratio": round(articles_with_metric / hit_article_count, 4) if hit_article_count else None,
            "evidence_start_at": evidence_start,
            "evidence_end_at": evidence_end,
            "daily_read_delta_points": list(daily_points.values()),
        })

    rows.sort(key=lambda item: (
        item["status"] != "ok",
        -(item["read_delta_raw"] or 0),
        item["keyword"],
    ))
    stats = {
        "window_start": to_iso(window_start),
        "window_end": to_iso(window_end),
        "window_days": window_days,
        "keywords": len(rows),
        "keywords_ok": sum(1 for item in rows if item["status"] == "ok"),
        "keywords_insufficient": sum(1 for item in rows if item["status"] != "ok"),
    }
    return rows, stats


__all__ = [
    "PARSER_VERSION",
    "build_article_metric_observations",
    "build_keyword_read_deltas",
]
