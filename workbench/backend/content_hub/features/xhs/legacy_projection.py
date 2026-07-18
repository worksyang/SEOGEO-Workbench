from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any


_PLATFORM = "xiaohongshu"
_EMPTY_AXES: list[dict[str, str]] = []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _time(value: Any) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str | None:
    parsed = _time(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _date(value: Any) -> str:
    parsed = _time(value)
    return parsed.date().isoformat() if parsed else ""


def _time_short(value: Any) -> str:
    parsed = _time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else ""


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    return _dict(row.get("payload"))


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(_payload(row))
    result.update({key: value for key, value in row.items() if key != "payload"})
    return result


def _source_keyword_id(row: dict[str, Any]) -> str:
    payload = _payload(row)
    return _text(
        row.get("source_keyword_id")
        or payload.get("source_keyword_id")
        or row.get("keyword_id")
    )


def _source_article_id(row: dict[str, Any]) -> str:
    payload = _payload(row)
    return _text(
        row.get("source_article_id")
        or payload.get("source_article_id")
        or row.get("article_id")
        or row.get("content_id")
    )


def _source_account_id(row: dict[str, Any]) -> str:
    payload = _payload(row)
    return _text(
        row.get("account_id")
        or row.get("external_id")
        or payload.get("account_id")
        or payload.get("external_id")
        or row.get("creator_id")
    )


def _is_xhs(row: dict[str, Any]) -> bool:
    platform = row.get("platform")
    return platform in (None, "", _PLATFORM, "小红书")


def _warning(warnings: dict[str, int], key: str) -> None:
    warnings[key] = warnings.get(key, 0) + 1


def _rank_history(events: list[dict[str, Any]], start_date: datetime.date, window_days: int = 15) -> list[int]:
    history = [0] * window_days
    for event in events:
        event_date = event.get("date")
        rank = _int(event.get("rank"))
        if not event_date or rank <= 0:
            continue
        index = (event_date - start_date).days
        if 0 <= index < window_days and (history[index] == 0 or rank < history[index]):
            history[index] = rank
    return history


def _streaks(history: list[int]) -> tuple[int, int]:
    current = 0
    longest = 0
    running = 0
    for value in history:
        if value > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0
    for value in reversed(history):
        if value > 0:
            current += 1
        else:
            break
    return current, longest


def _day_scores(history: list[int]) -> list[float]:
    return [
        round((11 - min(rank, 10)) / 10.0 * 20, 4) if rank > 0 else 0.0
        for rank in history
    ]


def _article(
    row: dict[str, Any],
    *,
    account_by_creator: dict[str, dict[str, Any]],
    account_by_external: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    flat = _flat(row)
    public_flat = {
        key: value
        for key, value in flat.items()
        if key not in {"raw", "payload", "platform_payload"}
    }
    creator = account_by_creator.get(_text(row.get("creator_id")))
    account_id = _source_account_id(row)
    account = creator or account_by_external.get(account_id) or {}
    account_id = _text(account.get("account_id") or account_id)
    article_id = _source_article_id(row)
    title = _text(row.get("title") or flat.get("title"))
    url = _text(row.get("canonical_url") or flat.get("normalized_url") or flat.get("url"))
    author_name = _text(row.get("author_name") or flat.get("account_name_raw") or account.get("name"))
    compact_payload = {
        key: value
        for key, value in public_flat.items()
        if key in {
            "source_article_id",
            "account_id",
            "account_name_raw",
            "cover_url",
            "work_type",
            "published_at",
            "liked_count",
            "collected_count",
            "comment_count",
            "shared_count",
            "read_count",
            "normalized_url",
        }
    }
    result = {
        "article_id": article_id,
        "title": title,
        "url": url,
        "content_path": _text(row.get("md_path") or flat.get("content_file_path") or article_id),
        "account": author_name,
        "account_id": account_id,
        "account_headimg": _text(account.get("headimg_url") or account.get("avatar_url")),
        "cover_url": _text(flat.get("cover_url") or flat.get("cover")),
        "work_type": _text(flat.get("work_type") or "normal"),
        "published_at": _iso(row.get("published_at") or flat.get("published_at")),
        "liked_count": flat.get("liked_count"),
        "collected_count": flat.get("collected_count"),
        "comment_count": flat.get("comment_count"),
        "shared_count": flat.get("shared_count"),
        "read_count": flat.get("read_count"),
        "like_count": flat.get("like_count", flat.get("liked_count")),
        "is_relevant": flat.get("is_relevant"),
        "relevance_score": flat.get("relevance_score"),
        "payload": compact_payload,
    }
    return {**public_flat, **result}


def _hit(
    row: dict[str, Any],
    *,
    article_by_content: dict[str, dict[str, Any]],
    article_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    flat = _flat(row)
    content_id = _text(row.get("content_id"))
    article = article_by_content.get(content_id) or article_by_id.get(
        _text(row.get("article_id") or flat.get("article_id"))
    )
    article_id = _text(
        (article or {}).get("article_id")
        or row.get("article_id")
        or flat.get("article_id")
        or content_id
    )
    account_id = _text(
        row.get("account_id")
        or flat.get("account_id")
        or (article or {}).get("account_id")
    )
    return {
        **flat,
        "hit_id": _text(row.get("hit_id")),
        "rank": _int(row.get("rank") or flat.get("rank"), 0),
        "article_id": article_id,
        "account_id": account_id,
        "title_raw": _text(row.get("title_raw") or flat.get("title_raw") or (article or {}).get("title")),
        "url_raw": _text(row.get("url_raw") or flat.get("url_raw") or (article or {}).get("url")),
        "account_name_raw": _text(
            row.get("creator_name_raw")
            or row.get("account_name_raw")
            or flat.get("account_name_raw")
            or (article or {}).get("account")
        ),
        "content_id": content_id or None,
    }


def project_hub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the old XHS monitor-data shape from Hub facts without I/O.

    The projection deliberately preserves fields already carried in imported
    payloads. It only derives grouping, run and relationship fields needed by
    the frozen monitor page; missing score/detail fields remain empty.
    """
    source = _dict(payload)
    warnings: dict[str, int] = {}
    raw_keywords = [row for row in _list(source.get("keywords")) if isinstance(row, dict) and _is_xhs(row)]
    raw_accounts = [row for row in _list(source.get("accounts")) if isinstance(row, dict) and _is_xhs(row)]
    raw_snapshots = [row for row in _list(source.get("snapshots")) if isinstance(row, dict) and _is_xhs(row)]
    raw_hits = [row for row in _list(source.get("ranking_hits")) if isinstance(row, dict) and _is_xhs(row)]
    raw_articles = [row for row in _list(source.get("articles")) if isinstance(row, dict) and _is_xhs(row)]

    account_by_creator: dict[str, dict[str, Any]] = {}
    account_by_external: dict[str, dict[str, Any]] = {}
    accounts: list[dict[str, Any]] = []
    for row in raw_accounts:
        flat = _flat(row)
        external_id = _source_account_id(row)
        creator_id = _text(row.get("creator_id"))
        name = _text(row.get("canonical_name") or flat.get("canonical_name") or flat.get("name"))
        account = {
            **flat,
            "account_id": external_id,
            "name": name,
            "headimg_url": _text(flat.get("headimg_url") or flat.get("avatar_url")),
            "platform": "小红书",
            "description": flat.get("description"),
            "fans": flat.get("fans"),
            "total_works": flat.get("total_works"),
            "likes_total": flat.get("likes_total", flat.get("likes")),
            "collects_total": flat.get("collects_total", flat.get("collects")),
            "follows_total": flat.get("follows_total", flat.get("follows")),
            "ip_location": flat.get("ip_location"),
            "verify_info": flat.get("verify_info"),
            "red_id": flat.get("red_id") or _dict(flat.get("profile")).get("red_id"),
            "first_seen_at": _iso(row.get("first_seen_at") or flat.get("first_seen_at")),
            "last_seen_at": _iso(row.get("updated_at") or flat.get("last_seen_at")),
            "is_focus": bool(flat.get("is_focus", False)),
            "note": flat.get("note"),
            "score": flat.get("score"),
            "timeliness_score": flat.get("timeliness_score"),
            "today_score": flat.get("today_score"),
            "keywords": {},
            "articles": [],
        }
        accounts.append(account)
        if creator_id:
            account_by_creator[creator_id] = account
        if external_id:
            account_by_external[external_id] = account

    article_by_content: dict[str, dict[str, Any]] = {}
    article_by_id: dict[str, dict[str, Any]] = {}
    articles: list[dict[str, Any]] = []
    for row in raw_articles:
        article = _article(row, account_by_creator=account_by_creator, account_by_external=account_by_external)
        article_id = _source_article_id(row)
        content_id = _text(row.get("content_id"))
        existing = article_by_content.get(content_id) if content_id else None
        existing = existing or article_by_id.get(article_id)
        if existing:
            continue
        if content_id:
            article_by_content[content_id] = article
        if article_id:
            article_by_id[article_id] = article
        articles.append(article)
    articles.sort(key=lambda item: (_time(item.get("published_at")) or datetime.min.replace(tzinfo=UTC), item.get("article_id", "")), reverse=True)

    hits_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_hits:
        hit = _hit(row, article_by_content=article_by_content, article_by_id=article_by_id)
        snapshot_id = _text(row.get("snapshot_id"))
        if snapshot_id:
            hits_by_snapshot[snapshot_id].append(hit)
    for values in hits_by_snapshot.values():
        values.sort(key=lambda item: (item.get("rank", 0), item.get("article_id", "")))

    snapshots = []
    snapshot_by_keyword: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_snapshots:
        snapshot = {**_flat(row)}
        snapshot["snapshot_id"] = _text(row.get("snapshot_id"))
        snapshot["keyword_id"] = _text(row.get("keyword_id"))
        snapshot["keyword"] = _text(row.get("keyword") or _payload(row).get("keyword"))
        snapshot["captured_at"] = _iso(row.get("captured_at"))
        snapshot["trigger_type"] = _text(row.get("trigger_type") or "import")
        snapshot["result_count"] = _int(row.get("result_count"), len(hits_by_snapshot.get(snapshot["snapshot_id"], [])))
        snapshot["hits"] = hits_by_snapshot.get(snapshot["snapshot_id"], [])
        snapshots.append(snapshot)
        snapshot_by_keyword[snapshot["keyword_id"]].append(snapshot)
    snapshots.sort(key=lambda item: (_time(item.get("captured_at")) or datetime.min.replace(tzinfo=UTC), item.get("snapshot_id", "")))
    for values in snapshot_by_keyword.values():
        values.sort(key=lambda item: (_time(item.get("captured_at")) or datetime.min.replace(tzinfo=UTC), item.get("snapshot_id", "")))

    snapshot_dates = [_date(item.get("captured_at")) for item in snapshots if _date(item.get("captured_at"))]
    window_end_date = max(
        (datetime.fromisoformat(value).date() for value in snapshot_dates),
        default=datetime.now(UTC).date(),
    )
    window_start_date = window_end_date - timedelta(days=14)
    keywords: list[dict[str, Any]] = []
    keyword_by_internal_id: dict[str, dict[str, Any]] = {}
    for row in raw_keywords:
        flat = _flat(row)
        settings = _dict(row.get("settings"))
        source_id = _source_keyword_id(row)
        internal_id = _text(row.get("keyword_id"))
        keyword_text = _text(row.get("keyword") or flat.get("keyword_text") or flat.get("keyword"))
        topic = _text(row.get("topic") or flat.get("topic") or settings.get("topic") or keyword_text)
        keyword_bucket = _text(
            row.get("keyword_bucket")
            or flat.get("keyword_bucket")
            or settings.get("keyword_bucket")
            or "未分类"
        )
        related_snapshots = snapshot_by_keyword.get(internal_id, [])
        if not related_snapshots:
            related_snapshots = [item for item in snapshots if _text(item.get("keyword")) == keyword_text]
        if not related_snapshots:
            _warning(warnings, "keyword_without_snapshot")
        runs: list[dict[str, Any]] = []
        for index, snapshot in enumerate(related_snapshots):
            run_hits = snapshot.get("hits") or []
            run_articles = []
            seen_articles: set[str] = set()
            for hit in run_hits:
                article = article_by_content.get(_text(hit.get("content_id"))) or article_by_id.get(_text(hit.get("article_id")))
                if not article:
                    continue
                article_key = _text(article.get("article_id") or article.get("content_id"))
                if article_key in seen_articles:
                    continue
                seen_articles.add(article_key)
                run_articles.append({**article, "rank": hit.get("rank"), "hit_days": 1})
            ranks = [_int(hit.get("rank")) for hit in run_hits if _int(hit.get("rank")) > 0]
            captured = snapshot.get("captured_at")
            runs.append({
                "id": snapshot.get("snapshot_id"),
                "date": _date(captured),
                "time": _time_short(captured),
                "captured_at": captured,
                "is_primary": index == len(related_snapshots) - 1,
                "trigger_type": snapshot.get("trigger_type") or "import",
                "status": "success",
                "result_count": len(run_hits),
                "best_rank": min(ranks) if ranks else None,
                "top3_count": sum(rank <= 3 for rank in ranks),
                "top10_count": sum(rank <= 10 for rank in ranks),
                "articles": run_articles,
            })
        latest = runs[-1] if runs else {}
        previous = runs[-2] if len(runs) > 1 else {}
        all_articles: dict[str, dict[str, Any]] = {}
        account_ids: set[str] = set()
        for run in runs:
            for article in run.get("articles", []):
                all_articles[_text(article.get("article_id"))] = article
                if article.get("account_id"):
                    account_ids.add(_text(article.get("account_id")))
        keyword = {
            **flat,
            "keyword_id": source_id,
            "keyword": keyword_text,
            "topic": topic,
            "keyword_bucket": keyword_bucket,
            "enabled": row.get("status") == "active" if row.get("status") is not None else bool(flat.get("is_active", True)),
            "is_pinned": bool(settings.get("pinned", flat.get("is_pinned", False))),
            "pin_order": settings.get("pin_order", flat.get("pin_order")),
            "note": settings.get("note", flat.get("note", "")) or "",
            "group_id": settings.get("group_id", flat.get("group_id")),
            "group_name": settings.get("group_name", flat.get("group_name")),
            "today_best": latest.get("best_rank"),
            "today_count": latest.get("result_count", 0),
            "yesterday_hits": previous.get("result_count", 0),
            "coverage_days": len({_date(item.get("captured_at")) for item in related_snapshots if _date(item.get("captured_at"))}),
            "tracked_accounts": len(account_ids),
            "article_count": len(all_articles),
            "latest_run": latest or None,
            "runs": runs,
            "accounts": sorted(account_ids),
            "history_best": [
                min(
                    (
                        _int(run.get("best_rank"))
                        for run in runs
                        if run.get("date") == (window_start_date + timedelta(days=index)).isoformat()
                        and _int(run.get("best_rank")) > 0
                    ),
                    default=0,
                )
                for index in range(15)
            ],
            "history_hits": [
                sum(
                    _int(run.get("result_count"))
                    for run in runs
                    if run.get("date") == (window_start_date + timedelta(days=index)).isoformat()
                )
                for index in range(15)
            ],
            "today_relevant_count": 0,
            "is_relevant_count": 0,
            "relevance_stats": {},
            "move_summary": {},
            "kw_score": {},
            "keyword_heat_metric": {},
            "heat_summary": {},
            "keyword_read_delta": {},
        }
        keywords.append(keyword)
        keyword_by_internal_id[internal_id] = keyword
        for account_id in account_ids:
            account = account_by_external.get(account_id)
            if account is None:
                continue
            account["keywords"].setdefault(keyword_text, {"keyword": keyword_text, "articles": []})
            account["keywords"][keyword_text]["articles"].extend(
                article for article in all_articles.values() if _text(article.get("account_id")) == account_id
            )
            account["articles"].extend(
                article for article in all_articles.values() if _text(article.get("account_id")) == account_id
            )
    keywords.sort(key=lambda item: (item.get("keyword", ""), item.get("keyword_id", "")))
    for account in accounts:
        unique_articles: dict[str, dict[str, Any]] = {}
        for article in account.get("articles", []):
            unique_articles[_text(article.get("article_id"))] = article
        account["articles"] = list(unique_articles.values())
        for detail in account.get("keywords", {}).values():
            unique: dict[str, dict[str, Any]] = {}
            for article in detail.get("articles", []):
                unique[_text(article.get("article_id"))] = article
            detail["articles"] = list(unique.values())
    events_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_keyword_account: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for snapshot in snapshots:
        captured_at = snapshot.get("captured_at")
        captured = _time(captured_at)
        event_date = captured.date() if captured else None
        if event_date is None:
            continue
        internal_keyword_id = _text(snapshot.get("keyword_id"))
        keyword = keyword_by_internal_id.get(internal_keyword_id) or {}
        for hit in snapshot.get("hits") or []:
            article = article_by_content.get(_text(hit.get("content_id"))) or article_by_id.get(
                _text(hit.get("article_id"))
            )
            account_id = _text(hit.get("account_id") or (article or {}).get("account_id"))
            article_id = _text(hit.get("article_id") or (article or {}).get("article_id"))
            if not account_id:
                continue
            event = {
                "date": event_date,
                "rank": _int(hit.get("rank")),
                "article_id": article_id,
                "keyword_id": internal_keyword_id,
                "keyword": _text(keyword.get("keyword") or snapshot.get("keyword")),
                "topic": _text(keyword.get("topic") or keyword.get("keyword") or snapshot.get("keyword")),
                "keyword_bucket": _text(keyword.get("keyword_bucket") or "未分类"),
                "captured_at": captured_at,
            }
            events_by_account[account_id].append(event)
            events_by_keyword_account[internal_keyword_id][account_id].append(event)

    account_by_id = {
        _text(account.get("account_id")): account
        for account in accounts
        if _text(account.get("account_id"))
    }
    account_summaries: dict[str, dict[str, Any]] = {}
    for account_id, account in account_by_id.items():
        events = events_by_account.get(account_id, [])
        history = _rank_history(events, window_start_date)
        current_streak, longest_streak = _streaks([1 if value > 0 else 0 for value in history])
        recent_history = history[-7:]
        article_event_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        keyword_event_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("article_id"):
                article_event_map[event["article_id"]].append(event)
            keyword_event_map[event["keyword_id"]].append(event)

        article_summaries: dict[str, dict[str, Any]] = {}
        for article_id, article_events in article_event_map.items():
            base = dict(article_by_id.get(article_id) or {"article_id": article_id})
            ranks = [_int(event.get("rank")) for event in article_events if _int(event.get("rank")) > 0]
            event_dates = sorted({event["date"] for event in article_events})
            today_events = [event for event in article_events if event["date"] == window_end_date]
            previous_dates = [value for value in event_dates if value < window_end_date]
            previous_date = max(previous_dates) if previous_dates else None
            previous_events = [
                event for event in article_events if previous_date and event["date"] == previous_date
            ]
            article_summaries[article_id] = {
                **base,
                "rank": min(ranks) if ranks else None,
                "today_rank": min(
                    (_int(event.get("rank")) for event in today_events if _int(event.get("rank")) > 0),
                    default=None,
                ),
                "today_prev": min(
                    (_int(event.get("rank")) for event in previous_events if _int(event.get("rank")) > 0),
                    default=None,
                ),
                "is_today": bool(today_events),
                "hit_days": len(event_dates),
                "matched_keyword_count": len({event["keyword_id"] for event in article_events}),
                "latest_seen_at": base.get("last_seen_at") or base.get("updated_at"),
                "first_seen_at": base.get("first_seen_at"),
                "signal": base.get("signal") or 0,
            }

        keyword_details: dict[str, dict[str, Any]] = {}
        for internal_keyword_id, keyword_events in keyword_event_map.items():
            keyword = keyword_by_internal_id.get(internal_keyword_id) or {}
            keyword_text = _text(keyword.get("keyword") or internal_keyword_id)
            keyword_article_ids = {
                event["article_id"] for event in keyword_events if event.get("article_id")
            }
            keyword_history = _rank_history(keyword_events, window_start_date)
            keyword_details[keyword_text] = {
                "keyword_id": _text(keyword.get("keyword_id") or internal_keyword_id),
                "keyword_text": keyword_text,
                "history": keyword_history,
                "day_scores": _day_scores(keyword_history),
                "hit_days": sum(1 for value in keyword_history if value > 0),
                "best_rank": min((value for value in keyword_history if value > 0), default=None),
                "today_rank": keyword_history[-1] if keyword_history else None,
                "today_prev": next(
                    (value for value in reversed(keyword_history[:-1]) if value > 0),
                    None,
                ),
                "articles": sorted(
                    [
                        dict(article_summaries[article_id])
                        for article_id in keyword_article_ids
                        if article_id in article_summaries
                    ],
                    key=lambda item: (_int(item.get("rank"), 999), item.get("article_id", "")),
                ),
            }

        topic_event_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            topic_event_map[event["topic"]].append(event)
        topic_details: dict[str, dict[str, Any]] = {}
        for topic, topic_events in topic_event_map.items():
            topic_history = _rank_history(topic_events, window_start_date)
            topic_article_ids = {
                event["article_id"] for event in topic_events if event.get("article_id")
            }
            topic_keyword_ids = {event["keyword_id"] for event in topic_events}
            topic_details[topic] = {
                "label": topic,
                "theme_type": "topic",
                "history": topic_history,
                "day_scores": _day_scores(topic_history),
                "hit_days": sum(1 for value in topic_history if value > 0),
                "best_rank": min((value for value in topic_history if value > 0), default=None),
                "article_count": len(topic_article_ids),
                "keyword_count": len(topic_keyword_ids),
                "bucket_count": len({event["keyword_bucket"] for event in topic_events}),
                "buckets": sorted({event["keyword_bucket"] for event in topic_events}),
                "keywords": sorted(
                    {
                        _text((keyword_by_internal_id.get(keyword_id) or {}).get("keyword") or keyword_id)
                        for keyword_id in topic_keyword_ids
                    }
                ),
                "articles": sorted(
                    [
                        dict(article_summaries[article_id])
                        for article_id in topic_article_ids
                        if article_id in article_summaries
                    ],
                    key=lambda item: (_int(item.get("rank"), 999), item.get("article_id", "")),
                ),
            }

        account.update(
            {
                "history": [1 if value > 0 else 0 for value in history],
                "day_scores": _day_scores(history),
                "history_active_days": sum(1 for value in history if value > 0),
                "recent_active_days": sum(1 for value in recent_history if value > 0),
                "recent_hit_days": sum(1 for value in recent_history if value > 0),
                "current_streak": current_streak,
                "longest_streak": longest_streak,
                "history_keyword_count": len(keyword_details),
                "history_article_count": len(article_summaries),
                "article_count": len(article_summaries),
                "kw_count": len(keyword_details),
                "topic_count": len(topic_details),
                "bucket_count": len({event["keyword_bucket"] for event in events}),
                "today_hit_count": sum(1 for event in events if event["date"] == window_end_date),
                "matched_keywords": [
                    {
                        "keyword_id": detail["keyword_id"],
                        "keyword_text": detail["keyword_text"],
                    }
                    for detail in keyword_details.values()
                ],
                "covered_topics": sorted(topic_details),
                "keywords": keyword_details,
                "topics": topic_details,
                "articles": sorted(
                    article_summaries.values(),
                    key=lambda item: (_int(item.get("rank"), 999), item.get("article_id", "")),
                ),
                "best_articles": sorted(
                    article_summaries.values(),
                    key=lambda item: (_int(item.get("rank"), 999), item.get("article_id", "")),
                )[:6],
                "classic_articles": [],
                "classic_article_count": 0,
                "classic_pair_count": 0,
                "durable_article_count": 0,
                "durable_pair_count": 0,
                "durable_rank_days": 0,
                "durable_notes_status": "waiting",
                "durable_notes_message": "Hub 当前仅提供冻结搜索快照，稳定笔记观察期尚未重新建立。",
                "breakthrough": False,
                "move_summary": account.get("move_summary") or {},
                "relevance_stats": account.get("relevance_stats") or {},
                "is_relevant_count": account.get("is_relevant_count") or 0,
                "score_delta": account.get("score_delta") or 0,
                "score_yesterday": account.get("score_yesterday") or 0,
                "timeliness_score_delta": account.get("timeliness_score_delta") or 0,
                "timeliness_score_yesterday": account.get("timeliness_score_yesterday") or 0,
                "today_score_delta": account.get("today_score_delta") or 0,
                "today_score_yesterday": account.get("today_score_yesterday") or 0,
                "account_score_method": account.get("account_score_method") or "hub_fact_projection",
            }
        )
        for score_key in (
            "score",
            "score_raw",
            "timeliness_score",
            "timeliness_score_raw",
            "today_score",
            "today_score_raw",
        ):
            if account.get(score_key) is None:
                account[score_key] = 0
        account_summaries[account_id] = account

    for keyword in keywords:
        internal_keyword_id = next(
            (
                internal_id
                for internal_id, item in keyword_by_internal_id.items()
                if item is keyword
            ),
            "",
        )
        keyword_accounts = []
        for account_id, events in events_by_keyword_account.get(internal_keyword_id, {}).items():
            account = account_summaries.get(account_id)
            if account is None:
                continue
            history = _rank_history(events, window_start_date)
            keyword_accounts.append(
                {
                    "account_id": account_id,
                    "name": account.get("name") or account_id,
                    "headimg_url": account.get("headimg_url") or "",
                    "history": history,
                    "hit_days": sum(1 for value in history if value > 0),
                    "best_rank": min((value for value in history if value > 0), default=None),
                    "today_rank": history[-1] if history else None,
                    "today_prev": next(
                        (value for value in reversed(history[:-1]) if value > 0),
                        None,
                    ),
                    "score": account.get("score", 0),
                    "timeliness_score": account.get("timeliness_score", 0),
                    "today_score": account.get("today_score", 0),
                }
            )
        keyword["accounts"] = sorted(
            keyword_accounts,
            key=lambda item: (
                item.get("today_rank") is None,
                item.get("today_rank") or 999,
                item.get("name", ""),
            ),
        )
        keyword["tracked_accounts"] = len(keyword_accounts)
    accounts.sort(key=lambda item: (item.get("name", ""), item.get("account_id", "")))

    generated_at = max(
        (_time(item.get("captured_at")) for item in snapshots if _time(item.get("captured_at"))),
        default=None,
    )
    dates = sorted({_date(item.get("captured_at")) for item in snapshots if _date(item.get("captured_at"))})
    return {
        "generated_at": generated_at.isoformat().replace("+00:00", "Z") if generated_at else None,
        "window_days": 15,
        "window_start": window_start_date.isoformat() if snapshots else None,
        "window_end": dates[-1] if dates else None,
        "platform": "小红书",
        "account_score_method": "hub_fact_projection",
        "hexagon_axes": list(source.get("hexagon_axes") or _EMPTY_AXES),
        "timeliness_axes": list(source.get("timeliness_axes") or _EMPTY_AXES),
        "today_axes": list(source.get("today_axes") or _EMPTY_AXES),
        "keywords": keywords,
        "accounts": accounts,
        "snapshots": snapshots,
        "ranking_hits": [hit for values in hits_by_snapshot.values() for hit in values],
        "articles": articles,
        "snapshot_terms": _list(source.get("snapshot_terms")),
        "counts": {
            "keywords": len(keywords),
            "accounts": len(accounts),
            "snapshots": len(snapshots),
            "ranking_hits": sum(len(values) for values in hits_by_snapshot.values()),
            "articles": len(articles),
            "snapshot_terms": len(_list(source.get("snapshot_terms"))),
        },
        "source_status": {"status": "healthy", "source": "hub_db_projection"},
        "projection_warnings": [{"code": key, "count": count} for key, count in sorted(warnings.items())],
    }
