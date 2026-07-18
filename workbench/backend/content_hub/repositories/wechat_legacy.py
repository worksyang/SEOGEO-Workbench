from __future__ import annotations

import base64
import hashlib
import json
import zlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from zoneinfo import ZoneInfo

from content_hub.db.connection import connect
from content_hub.errors import NotFoundError, ValidationAppError


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _row(value: Any) -> dict[str, Any]:
    result = dict(value)
    for key in ("payload_json", "features_json"):
        if key in result:
            try:
                result[key[:-5] if key.endswith("_json") else key] = json.loads(result[key] or "{}")
            except (TypeError, json.JSONDecodeError):
                result[key[:-5] if key.endswith("_json") else key] = {}
    return result


def _runtime_value(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    # subtype 只用于 Hub 内部区分单词 job / batch / scheduler，旧响应不得泄露。
    value.pop("runtime_subtype", None)
    return value


def _runtime_history_sort_key(value: dict[str, Any]) -> tuple[int, float, str]:
    """Sort history by business start time, newest first."""
    raw_started_at = value.get("started_at")
    if not isinstance(raw_started_at, str) or not raw_started_at.strip():
        return (1, float("inf"), str(value.get("batch_id") or ""))
    normalized = raw_started_at.strip().replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError, OverflowError):
        return (1, float("inf"), str(value.get("batch_id") or ""))
    return (0, -timestamp, str(value.get("batch_id") or ""))


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _projection_payload(raw: Any) -> dict[str, Any]:
    value = _json_object(raw)
    if value.get("__compressed_json__") != "zlib+base64":
        return value
    try:
        decoded = json.loads(
            zlib.decompress(base64.b64decode(value["data"])).decode("utf-8")
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        zlib.error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _legacy_parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _legacy_timestamp(value: Any) -> float:
    parsed = _legacy_parse_date(value)
    if parsed is None:
        return float("-inf")
    return parsed.replace(tzinfo=LOCAL_TIMEZONE).timestamp()


def _legacy_display_date(value: Any) -> datetime | None:
    """Parse stored timestamps in UTC for the public legacy date labels.

    Canonical snapshots are stored with a ``Z`` suffix.  Converting a late
    July 18 UTC snapshot to Asia/Shanghai before rendering made the UI show
    July 19 even though the source snapshot itself was still July 18.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _legacy_display_iso(value: Any) -> str:
    parsed = _legacy_display_date(value)
    if parsed is None:
        return str(value or "")
    return (
        parsed.isoformat(timespec="microseconds")
        .rstrip("0")
        .rstrip(".")
    )


def _compact_bootstrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the legacy list view fast without dropping detail endpoints.

    The live projection stores raw compatibility payloads and full account
    article histories for audit/replay.  Sending those fields in the initial
    bootstrap made the browser parse roughly 190 MB before it could render the
    first row.  The legacy page fetches keyword/account detail lazily, so the
    bootstrap only needs list metrics, short history bars and the latest-run
    summary.
    """
    result = {
        key: value
        for key, value in payload.items()
        if key not in {"keywords", "accounts"}
    }
    compact_keywords: list[dict[str, Any]] = []
    for source in payload.get("keywords") or []:
        if not isinstance(source, dict):
            continue
        item = {
            key: value
            for key, value in source.items()
            if key not in {
                "payload_json",
                "payload",
                "setting_payload_json",
                "setting_payload",
                "runs",
            }
        }
        latest_run = source.get("latest_run")
        if isinstance(latest_run, dict):
            item["latest_run"] = {
                key: latest_run.get(key)
                for key in (
                    "id",
                    "date",
                    "time",
                    "run_at",
                    "trigger_type",
                    "result_count",
                )
                if key in latest_run
            }
        else:
            item["latest_run"] = None
        compact_keywords.append(item)

    compact_accounts: list[dict[str, Any]] = []
    for source in payload.get("accounts") or []:
        if not isinstance(source, dict):
            continue
        item = {
            key: value
            for key, value in source.items()
            if key not in {
                "_today_article_ids",
                "_today_article_titles",
                "payload_json",
                "payload",
                "setting_payload_json",
                "setting_payload",
                "topics",
                "keywords",
                "history",
            }
        }

        # The account list uses a 15-cell rank heat bar.  The live projection
        # keeps raw article events for the detail endpoint; derive the compact
        # best-rank-per-day shape expected by the legacy renderer.
        day_scores = source.get("day_scores")
        window_size = len(day_scores) if isinstance(day_scores, list) else 0
        history = [0] * window_size
        for event in source.get("history") or []:
            if not isinstance(event, dict):
                continue
            try:
                day_index = int(event.get("_day_idx"))
                rank = int(event.get("rank"))
            except (TypeError, ValueError):
                continue
            if 0 <= day_index < window_size and rank > 0:
                history[day_index] = min(history[day_index] or rank, rank)
        item["history"] = history

        topics = source.get("topics")
        if isinstance(topics, dict):
            item["topic_names"] = [
                str(info.get("label") or topic)
                for topic, info in topics.items()
                if isinstance(info, dict) and str(info.get("label") or topic).strip()
            ][:12]
        else:
            item["topic_names"] = []

        keywords = source.get("keywords")
        if isinstance(keywords, dict):
            keyword_names = keywords.keys()
        elif isinstance(keywords, list):
            keyword_names = keywords
        else:
            keyword_names = ()
        item["keyword_names"] = [str(value) for value in keyword_names][:120]
        compact_accounts.append(item)

    result["keywords"] = compact_keywords
    result["accounts"] = compact_accounts
    return result


def _legacy_batch_id(source_path: Any) -> str:
    text = str(source_path or "").replace("\\", "/")
    marker = "/批量抓取/"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("/", 1)[0]


def _legacy_keyword_text(keyword_id: Any, *candidates: Any) -> str:
    """Return source keyword text without leaking an imported ``kw_*`` ID."""
    source_id = str(keyword_id or "").strip()
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text != source_id:
            return text
    return ""


def _legacy_keyword_order(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 999999


class WechatLegacyRepository:
    """从 Hub 规范化事实重建旧微信 GET 投影；不读取 182MB monitor-data。"""

    _article_cache_lock = Lock()
    _article_cache: dict[
        tuple[Any, ...],
        tuple[list[dict[str, Any]], list[dict[str, Any]]],
    ] = {}

    def __init__(self, settings: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.settings = settings
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock() if self.clock else datetime.now()
        return value.replace(tzinfo=None) if value.tzinfo else value

    @staticmethod
    def _wechat_article_predicate(alias: str = "c") -> str:
        return f"""
            (
                EXISTS (
                    SELECT 1 FROM content_identifiers wi
                    WHERE wi.namespace='wechat_article' AND wi.content_id={alias}.content_id
                )
                OR EXISTS (
                    SELECT 1 FROM content_discoveries wd
                    WHERE wd.discovery_system='wechat-search' AND wd.content_id={alias}.content_id
                )
                OR EXISTS (
                    SELECT 1
                    FROM search_hits wh
                    JOIN search_snapshots ws ON ws.snapshot_id=wh.snapshot_id
                    WHERE wh.content_id={alias}.content_id AND ws.platform='wechat-search'
                )
            )
        """

    def _keywords(self, con) -> list[dict[str, Any]]:
        rows = con.execute(
            """
            SELECT k.*, s.pinned, s.note, s.group_id, s.refresh_strategy,
                   s.refresh_interval_minutes, s.commercial_value,
                   s.archived_at AS setting_archived_at
            FROM keywords k
            LEFT JOIN search_keyword_settings s
              ON s.keyword_id=k.keyword_id AND s.system_key='wechat-search'
            WHERE k.platform='wechat-search'
            ORDER BY COALESCE(s.pinned,0) DESC, k.keyword, k.keyword_id
            """
        ).fetchall()
        return [_row(x) for x in rows]

    def _projection(self, kind: str, subject_id: str = "") -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT payload_json FROM wechat_legacy_projections
                WHERE projection_kind=? AND subject_id=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (kind, subject_id),
            ).fetchone()
        if row is None:
            return None
        value = _projection_payload(row["payload_json"])
        return value or None

    @staticmethod
    def _snapshot_run_at(captured_at: Any) -> tuple[str, str, str]:
        parsed = _legacy_display_date(captured_at)
        if parsed is None:
            text = str(captured_at or "")
            return text[:10], text[11:16], text.replace("T", " ")[:16]
        return (
            parsed.strftime("%Y-%m-%d"),
            parsed.strftime("%H:%M"),
            parsed.strftime("%Y-%m-%d %H:%M"),
        )

    def _dynamic_runs(
        self,
        con,
        *,
        since: str | None,
        keyword_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        where = ["s.platform='wechat-search'"]
        params: list[Any] = []
        if keyword_id:
            where.append("s.keyword_id=?")
            params.append(keyword_id)
        snapshots = [
            dict(row)
            for row in con.execute(
                f"""
                SELECT s.snapshot_id,s.keyword_id,s.keyword,s.captured_at,
                       s.trigger_type,s.result_count,s.features_json
                FROM search_snapshots s
                WHERE {' AND '.join(where)}
                ORDER BY s.captured_at DESC,s.snapshot_id DESC
                """,
                params,
            )
        ]
        if since:
            since_timestamp = _legacy_timestamp(since)
            snapshots = [
                snapshot
                for snapshot in snapshots
                if _legacy_timestamp(snapshot.get("captured_at")) > since_timestamp
            ]
        snapshots.sort(
            key=lambda snapshot: (
                _legacy_timestamp(snapshot.get("captured_at")),
                str(snapshot.get("snapshot_id") or ""),
            ),
            reverse=True,
        )
        if not snapshots:
            return {}

        snapshot_ids = [str(row["snapshot_id"]) for row in snapshots]
        placeholders = ",".join("?" for _ in snapshot_ids)
        hits = [
            dict(row)
            for row in con.execute(
                f"""
                SELECT h.snapshot_id,h.rank,h.content_id,h.title_raw,h.url_raw,
                       h.creator_name_raw,h.payload_json,
                       c.title,c.canonical_url,c.creator_id,c.author_name,
                       c.published_at,c.payload_json AS content_payload,
                       COALESCE(p.relative_path,p.asset_path) AS content_path
                FROM search_hits h
                LEFT JOIN contents c ON c.content_id=h.content_id
                LEFT JOIN wechat_article_paths p
                  ON p.rowid=(
                    SELECT p2.rowid FROM wechat_article_paths p2
                    WHERE p2.article_id=h.content_id
                    ORDER BY p2.created_at DESC,p2.rowid DESC LIMIT 1
                  )
                WHERE h.snapshot_id IN ({placeholders})
                ORDER BY h.snapshot_id,h.rank,h.hit_id
                """,
                snapshot_ids,
            )
        ]
        content_ids = {
            str(row.get("content_id") or "")
            for row in hits
            if row.get("content_id")
        }
        hit_days: dict[str, int] = {}
        if content_ids:
            content_placeholders = ",".join("?" for _ in content_ids)
            for row in con.execute(
                f"""
                SELECT h.content_id,COUNT(DISTINCT substr(s.captured_at,1,10)) AS days
                FROM search_hits h
                JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                WHERE s.platform='wechat-search'
                  AND h.content_id IN ({content_placeholders})
                GROUP BY h.content_id
                """,
                sorted(content_ids),
            ):
                hit_days[str(row["content_id"])] = int(row["days"] or 0)

        creators_by_id: dict[str, dict[str, Any]] = {}
        creators_by_name: dict[str, dict[str, Any]] = {}
        for row in con.execute(
            """SELECT creator_id,canonical_name,payload_json
               FROM creators WHERE platform='wechat-search'"""
        ):
            payload = _json_object(row["payload_json"])
            creator = {
                **payload,
                "creator_id": str(row["creator_id"]),
                "canonical_name": row["canonical_name"] or payload.get("canonical_name") or "",
            }
            creators_by_id[creator["creator_id"]] = creator
            if creator["canonical_name"]:
                creators_by_name[str(creator["canonical_name"])] = creator

        hits_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        for row in hits:
            hit_payload = _json_object(row.get("payload_json"))
            content_payload = _json_object(row.get("content_payload"))
            account_name = str(
                row.get("author_name")
                or row.get("creator_name_raw")
                or hit_payload.get("creator_name_raw")
                or ""
            )
            creator = creators_by_id.get(str(row.get("creator_id") or ""))
            if creator is None and account_name:
                creator = creators_by_name.get(account_name)
            article_id = str(row.get("content_id") or hit_payload.get("content_id") or "")
            hits_by_snapshot.setdefault(str(row["snapshot_id"]), []).append(
                {
                    "rank": int(row.get("rank") or hit_payload.get("rank") or 0),
                    "account": account_name,
                    "account_id": (creator or {}).get("creator_id") or "",
                    "account_headimg": (creator or {}).get("headimg_url") or "",
                    "article_id": article_id,
                    "title": row.get("title") or row.get("title_raw") or hit_payload.get("title_raw") or "",
                    "summary": hit_payload.get("summary_raw") or content_payload.get("summary_raw") or "",
                    "published_at": hit_payload.get("published_at") or row.get("published_at") or "",
                    "url": row.get("canonical_url") or row.get("url_raw") or hit_payload.get("url_raw") or "",
                    "cover_url": hit_payload.get("cover_url") or content_payload.get("cover_url"),
                    "content_path": row.get("content_path") or "",
                    "hit_days": hit_days.get(article_id, 0),
                    "read_count": content_payload.get("read_count"),
                    "like_count": content_payload.get("like_count"),
                    "friends_follow_count": content_payload.get("friends_follow_count"),
                    "original_article_count": content_payload.get("original_article_count"),
                }
            )

        result: dict[str, list[dict[str, Any]]] = {}
        for snapshot in snapshots:
            date, clock, run_at = self._snapshot_run_at(snapshot["captured_at"])
            try:
                features = json.loads(snapshot.get("features_json") or "{}")
            except json.JSONDecodeError:
                features = {}
            articles = hits_by_snapshot.get(str(snapshot["snapshot_id"]), [])
            run = {
                "id": str(snapshot["snapshot_id"]),
                "date": date,
                "time": clock,
                "run_at": run_at,
                "trigger_type": snapshot.get("trigger_type") or "manual",
                "is_primary": True,
                "result_count": int(snapshot.get("result_count") or len(articles)),
                "note": "",
                "articles": articles,
                "terms": {
                    "suggestions": features.get("suggestions") or [],
                    "related": features.get("related") or [],
                },
            }
            result.setdefault(str(snapshot["keyword_id"]), []).append(run)
        return result

    @staticmethod
    def _merge_keyword_runs(
        keyword: dict[str, Any],
        dynamic_runs: list[dict[str, Any]],
        *,
        include_runs: bool,
        old_window_start: str | None,
        old_window_days: int,
        new_window_end: str,
    ) -> None:
        if not dynamic_runs:
            return
        old_runs = keyword.get("runs") if isinstance(keyword.get("runs"), list) else []
        combined = {
            str(run.get("id") or f"{run.get('run_at')}:{index}"): run
            for index, run in enumerate(old_runs)
            if isinstance(run, dict)
        }
        for run in dynamic_runs:
            combined[str(run["id"])] = run
        merged_runs = sorted(
            combined.values(),
            key=lambda run: (str(run.get("run_at") or ""), str(run.get("id") or "")),
            reverse=True,
        )
        latest = merged_runs[0]
        keyword["latest_run"] = (
            latest
            if include_runs
            else {
                key: latest.get(key)
                for key in ("id", "date", "time", "run_at", "trigger_type", "result_count")
            }
        )
        latest_articles = [
            article for article in latest.get("articles", []) if isinstance(article, dict)
        ]
        keyword["today_count"] = int(latest.get("result_count") or len(latest_articles))
        ranks = [int(article.get("rank") or 0) for article in latest_articles if int(article.get("rank") or 0) > 0]
        keyword["today_best"] = min(ranks) if ranks else 0
        all_articles = {
            str(article.get("article_id") or article.get("url") or article.get("title") or "")
            for run in merged_runs
            for article in run.get("articles", [])
            if isinstance(article, dict)
        }
        all_accounts = {
            str(article.get("account_id") or article.get("account") or "")
            for run in merged_runs
            for article in run.get("articles", [])
            if isinstance(article, dict)
        }
        keyword["article_count"] = max(int(keyword.get("article_count") or 0), len(all_articles - {""}))
        keyword["tracked_accounts"] = max(int(keyword.get("tracked_accounts") or 0), len(all_accounts - {""}))

        old_end = _legacy_parse_date(old_window_start)
        old_hits = list(keyword.get("history_hits") or [])
        old_best = list(keyword.get("history_best") or [])
        hits_by_date: dict[str, int] = {}
        best_by_date: dict[str, int] = {}
        if old_end:
            for index in range(old_window_days):
                date = (old_end + timedelta(days=index)).date().isoformat()
                if index < len(old_hits):
                    hits_by_date[date] = int(old_hits[index] or 0)
                if index < len(old_best):
                    best_by_date[date] = int(old_best[index] or 0)
        latest_by_date: dict[str, dict[str, Any]] = {}
        for run in merged_runs:
            date = str(run.get("date") or "")
            if date and date not in latest_by_date:
                latest_by_date[date] = run
        for date, run in latest_by_date.items():
            articles = [x for x in run.get("articles", []) if isinstance(x, dict)]
            hits_by_date[date] = int(run.get("result_count") or len(articles))
            ranks = [int(x.get("rank") or 0) for x in articles if int(x.get("rank") or 0) > 0]
            best_by_date[date] = min(ranks) if ranks else 0
        parsed_end = _legacy_parse_date(new_window_end)
        if parsed_end:
            dates = [
                (parsed_end - timedelta(days=old_window_days - 1 - index)).date().isoformat()
                for index in range(old_window_days)
            ]
            keyword["history_hits"] = [hits_by_date.get(date, 0) for date in dates]
            keyword["history_best"] = [best_by_date.get(date, 0) for date in dates]
            keyword["coverage_days"] = sum(1 for date in dates if hits_by_date.get(date, 0) > 0)

        turnover = {
            str(run.get("id")): run
            for run in (keyword.get("turnover_runs") or [])
            if isinstance(run, dict) and run.get("id")
        }
        for run in dynamic_runs:
            turnover[str(run["id"])] = {
                "id": run["id"],
                "date": run["date"],
                "time": run["time"],
                "articles": [
                    {"article_id": article.get("article_id")}
                    for article in run.get("articles", [])
                    if isinstance(article, dict) and article.get("article_id")
                ],
            }
        keyword["turnover_runs"] = sorted(
            turnover.values(),
            key=lambda run: (str(run.get("date") or ""), str(run.get("time") or "")),
            reverse=True,
        )
        if include_runs:
            keyword["runs"] = merged_runs

    def _overlay_live_snapshots(
        self,
        projected: dict[str, Any],
        *,
        include_runs: bool,
        keyword_id: str | None = None,
    ) -> dict[str, Any]:
        result = deepcopy(projected)
        projection_time = str(result.get("generated_at") or "")
        with connect(self.settings, readonly=True) as con:
            snapshot_rows = con.execute(
                "SELECT snapshot_id,captured_at FROM search_snapshots WHERE platform='wechat-search'"
            ).fetchall()
            latest_row = max(
                snapshot_rows,
                key=lambda row: (
                    _legacy_timestamp(row["captured_at"]),
                    str(row["snapshot_id"] or ""),
                ),
                default=None,
            )
            latest = latest_row["captured_at"] if latest_row else None
            if not latest or (
                projection_time
                and _legacy_timestamp(latest) <= _legacy_timestamp(projection_time)
            ):
                return result
            runs_by_keyword = self._dynamic_runs(
                con,
                since=projection_time or None,
                keyword_id=keyword_id,
            )
        if not runs_by_keyword:
            return result
        window_days = max(1, int(result.get("window_days") or 15))
        latest_date, _, latest_run_at = self._snapshot_run_at(latest)
        parsed_latest = _legacy_parse_date(latest_date)
        window_start = (
            (parsed_latest - timedelta(days=window_days - 1)).date().isoformat()
            if parsed_latest
            else result.get("window_start")
        )
        old_window_start = result.get("window_start")
        keywords = result.get("keywords") if isinstance(result.get("keywords"), list) else []
        by_id = {
            str(item.get("keyword_id")): item
            for item in keywords
            if isinstance(item, dict) and item.get("keyword_id")
        }
        if keyword_id and result.get("keyword_id"):
            by_id.setdefault(str(result["keyword_id"]), result)
        for current_keyword_id, runs in runs_by_keyword.items():
            item = by_id.get(current_keyword_id)
            if item is None:
                continue
            self._merge_keyword_runs(
                item,
                runs,
                include_runs=include_runs,
                old_window_start=str(old_window_start or "") or None,
                old_window_days=window_days,
                new_window_end=latest_date,
            )
        result["generated_at"] = _legacy_display_iso(latest)
        result["window_end"] = latest_date
        result["window_start"] = window_start
        return result

    def full(self) -> dict[str, Any]:
        value = self._projection("full")
        if value is None:
            raise NotFoundError("微信兼容投影", "full")
        return self._overlay_live_snapshots(value, include_runs=True)

    def runtime(self, subject_id: str, *, subtype: str | None = None) -> dict[str, Any] | None:
        if subtype in {"single_job", "batch"}:
            with connect(self.settings, readonly=True) as con:
                job = con.execute(
                    """SELECT refresh_job_id
                       FROM search_refresh_jobs
                       WHERE refresh_job_id=?
                         AND system_key='wechat-search'
                         AND platform='wechat-search'""",
                    (subject_id,),
                ).fetchone()
                if job:
                    from content_hub.services.wechat_refresh import WechatRefreshService

                    return WechatRefreshService(self.settings)._job_payload(con, subject_id)
        subtype_clause = ""
        params: list[Any] = [subject_id]
        if subtype:
            subtype_clause = " AND json_extract(payload_json, '$.runtime_subtype')=?"
            params.append(subtype)
        query = (
            """SELECT payload_json FROM wechat_legacy_projections
               WHERE projection_kind='runtime' AND subject_id=?
                 AND json_extract(payload_json, '$.runtime_subtype') IN ('single_job','batch')"""
            + subtype_clause
            + " ORDER BY updated_at DESC LIMIT 1"
        )
        with connect(self.settings, readonly=True) as con:
            row = con.execute(query, params).fetchone()
        if not row:
            return None
        return _runtime_value(row["payload_json"])

    def runtime_history(self) -> list[dict[str, Any]]:
        with connect(self.settings, readonly=True) as con:
            rows = con.execute(
                """SELECT payload_json, updated_at FROM wechat_legacy_projections
                   WHERE projection_kind='runtime'
                     AND json_extract(payload_json, '$.runtime_subtype')='batch'"""
            ).fetchall()
        latest_by_batch: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in rows:
            value = _runtime_value(row["payload_json"])
            if value is None:
                continue
            if isinstance(value, dict):
                batch_id = str(value.get("batch_id") or "")
                previous = latest_by_batch.get(batch_id)
                updated_at = str(row["updated_at"] or "")
                if previous is None or updated_at >= previous[0]:
                    latest_by_batch[batch_id] = (updated_at, value)
        return sorted(
            (value for _, value in latest_by_batch.values()),
            key=_runtime_history_sort_key,
        )

    def active_batch_runtime(self) -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            # New batches persist their compatibility projection immediately,
            # but older/in-flight rows may predate that write. Read the runtime
            # tables directly as a compatibility fallback so status never
            # regresses to idle while a provider is still running.
            row = con.execute(
                """SELECT refresh_job_id
                   FROM search_refresh_jobs
                   WHERE system_key='wechat-search' AND platform='wechat-search'
                     AND trigger_type IN ('manual','scheduled')
                     AND status IN ('queued','running')
                     AND cancel_requested=0
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            if row:
                from content_hub.services.wechat_refresh import WechatRefreshService

                return WechatRefreshService(self.settings)._job_payload(con, row["refresh_job_id"])
            row = con.execute(
                """SELECT payload_json FROM wechat_legacy_projections
                   JOIN search_refresh_jobs j
                     ON j.refresh_job_id=wechat_legacy_projections.subject_id
                   WHERE projection_kind='runtime'
                     AND json_extract(payload_json, '$.runtime_subtype')='batch'
                     AND j.system_key='wechat-search'
                     AND j.platform='wechat-search'
                   AND j.status IN ('queued','running')
                   AND j.cancel_requested=0
                   AND json_extract(payload_json, '$.status') IN ('queued','running')
                   ORDER BY COALESCE(json_extract(payload_json, '$.started_at'),
                                    json_extract(payload_json, '$.created_at'),
                                    wechat_legacy_projections.updated_at) DESC LIMIT 1"""
            ).fetchone()
        if not row:
            return None
        return _runtime_value(row["payload_json"])

    def scheduler_runtime(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            projection = con.execute(
                """SELECT payload_json FROM wechat_legacy_projections
                   WHERE projection_kind='runtime' AND subject_id='scheduler'
                     AND json_extract(payload_json, '$.runtime_subtype')='scheduler'
                   ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
            state = con.execute(
                """SELECT enabled,next_run_at,last_run_at,active_refresh_job_id,payload_json
                   FROM search_scheduler_state
                   WHERE system_key='wechat-search' AND platform='wechat-search'"""
            ).fetchone()
        value = _runtime_value(projection["payload_json"]) if projection else None
        result = value if value is not None else {}
        if state:
            result.update(_json_object(state["payload_json"]))
            result.update(
                {
                    "enabled": bool(state["enabled"]),
                    "is_active": bool(state["active_refresh_job_id"]),
                    "next_run_at": state["next_run_at"],
                    "last_run_at": state["last_run_at"],
                }
            )
        return result or {"enabled": False, "is_active": False}

    def bootstrap(self) -> dict[str, Any]:
        projected = self._projection("bootstrap")
        if projected is not None:
            return _compact_bootstrap_payload(
                self._overlay_live_snapshots(projected, include_runs=False)
            )
        with connect(self.settings, readonly=True) as con:
            keywords = self._keywords(con)
            accounts = [
                _row(x) for x in con.execute(
                    "SELECT * FROM creators WHERE platform='wechat-search' ORDER BY canonical_name,creator_id"
                )
            ]
            snapshot_rows = con.execute(
                "SELECT snapshot_id,captured_at FROM search_snapshots WHERE platform='wechat-search'"
            ).fetchall()
            latest_row = max(
                snapshot_rows,
                key=lambda row: (
                    _legacy_timestamp(row["captured_at"]),
                    str(row["snapshot_id"] or ""),
                ),
                default=None,
            )
            latest = latest_row["captured_at"] if latest_row else None
            latest_date, _, latest_run_at = self._snapshot_run_at(latest)
        return _compact_bootstrap_payload({
            "generated_at": latest_run_at.replace(" ", "T") if latest else None,
            "window_days": None,
            "window_start": None,
            "window_end": latest_date if latest else None,
            "scope": {"total": len(keywords), "pinned": sum(bool(x.get("pinned")) for x in keywords)},
            "keywords": [self.keyword_summary(x) for x in keywords],
            "accounts": accounts,
            "bucket_options": sorted({x.get("keyword_bucket") for x in keywords if x.get("keyword_bucket")}),
        })

    @staticmethod
    def keyword_summary(item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        result["keyword_id"] = item.get("keyword_id")
        result["keyword"] = item.get("keyword")
        result["group"] = item.get("group_name") or item.get("keyword_bucket") or "未分组"
        result["status"] = item.get("status", "active")
        result["article_count"] = item.get("article_count", 0)
        return result

    def keyword(self, keyword_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            key = con.execute(
                """SELECT * FROM keywords
                   WHERE platform='wechat-search' AND keyword_id=?
                     AND status='active'""",
                (keyword_id,),
            ).fetchone()
        if key is None:
            raise NotFoundError("微信关键词", keyword_id)
        projected = self._projection("keyword", keyword_id)
        if projected is not None:
            return self._overlay_live_snapshots(
                projected,
                include_runs=True,
                keyword_id=keyword_id,
            )
        with connect(self.settings, readonly=True) as con:
            snapshots = [
                _row(x) for x in con.execute(
                    "SELECT * FROM search_snapshots WHERE platform='wechat-search' AND keyword_id=? ORDER BY captured_at",
                    (keyword_id,),
                )
            ]
            snapshot_ids = [x["snapshot_id"] for x in snapshots]
            hits: list[dict[str, Any]] = []
            if snapshot_ids:
                q = ",".join("?" for _ in snapshot_ids)
                hits = [_row(x) for x in con.execute(
                    f"SELECT h.*,s.keyword,s.captured_at FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE h.snapshot_id IN ({q}) ORDER BY s.captured_at,h.rank,h.hit_id",
                    snapshot_ids,
                )]
            articles = {}
            ids = [x["content_id"] for x in hits if x.get("content_id")]
            if ids:
                q = ",".join("?" for _ in ids)
                articles = {x["content_id"]: _row(x) for x in con.execute(f"SELECT * FROM contents WHERE content_id IN ({q})", ids)}
            terms = {}
            if snapshot_ids:
                q = ",".join("?" for _ in snapshot_ids)
                for x in con.execute(f"SELECT snapshot_id,features_json FROM search_snapshots WHERE snapshot_id IN ({q})", snapshot_ids):
                    features = json.loads(x["features_json"] or "{}")
                    terms[x["snapshot_id"]] = features
            observations = []
            if ids:
                q = ",".join("?" for _ in ids)
                observations = [_row(x) for x in con.execute(
                    f"SELECT * FROM metric_observations WHERE subject_type='content' AND subject_id IN ({q}) ORDER BY observed_at",
                    ids,
                )]
        views = []
        by_snapshot = {}
        for hit in hits:
            by_snapshot.setdefault(hit["snapshot_id"], []).append(hit)
        for snap in snapshots:
            snap_hits = by_snapshot.get(snap["snapshot_id"], [])
            feature = terms.get(snap["snapshot_id"], {})
            views.append({
                **snap,
                "hits": snap_hits,
                "articles": [articles[h["content_id"]] for h in snap_hits if h.get("content_id") in articles],
                "features": feature,
            })
        return _row(key)

    def account(self, account_id: str) -> dict[str, Any]:
        projected = self._projection("account", account_id)
        if projected is not None:
            return projected
        with connect(self.settings, readonly=True) as con:
            account = con.execute(
                "SELECT * FROM creators WHERE platform='wechat-search' AND creator_id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise NotFoundError("微信账号", account_id)
            articles = [_row(x) for x in con.execute(
                "SELECT * FROM contents WHERE creator_id=? ORDER BY published_at DESC,content_id",
                (account_id,),
            )]
            article_ids = [x["content_id"] for x in articles]
            hits = []
            if article_ids:
                q = ",".join("?" for _ in article_ids)
                hits = [_row(x) for x in con.execute(
                    f"SELECT h.*,s.keyword,s.captured_at FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE h.content_id IN ({q}) ORDER BY s.captured_at DESC,h.rank",
                    article_ids,
                )]
        return _row(account)

    def article_content(self, path: str) -> dict[str, Any]:
        if not path:
            raise ValidationAppError("path 是必填参数。")
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                f"""
                SELECT c.*,p.relative_path,p.asset_path
                FROM contents c
                JOIN wechat_article_paths p ON p.article_id=c.content_id
                WHERE ({self._wechat_article_predicate('c')})
                  AND (p.relative_path=? OR p.asset_path=?)
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                (path, path),
            ).fetchone()
        if row is None:
            raise NotFoundError("微信正文", path)
        record = dict(row)
        # 7 月 16–18 日新刷新链路已经把正文写入 asset_store，但历史记录的
        # relative_path 为空。前端仍以 asset_path 作为 content_file_path 请求，
        # 因此读取时把安全的资产相对路径补成兼容字段，避免真实存在的正文被
        # article-content 路由误判为 404。
        if not record.get("relative_path") and record.get("asset_path"):
            record["relative_path"] = record["asset_path"]
        return record

    def asset_content(self, record: dict[str, Any]) -> str:
        asset = record.get("asset_path")
        if not asset:
            raise NotFoundError("微信正文资产", str(record.get("content_id") or ""))
        root = self.settings.asset_store_path.resolve()
        candidate = self.settings.asset_store_path / str(asset)
        try:
            relative_parts = candidate.relative_to(self.settings.asset_store_path).parts
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if any((self.settings.asset_store_path.joinpath(*relative_parts[:index])).is_symlink()
               for index in range(1, len(relative_parts) + 1)):
            raise ValidationAppError("正文资产路径无效。")
        path = candidate.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if not path.is_file():
            raise NotFoundError("微信正文资产", str(asset))
        return path.read_text(encoding="utf-8")

    def hit_detail(self, article_id: str, url: str) -> dict[str, Any]:
        if article_id:
            projected = self._projection("article_detail", article_id)
            if projected is not None:
                return self._complete_hit_detail(projected, article_id)
        with connect(self.settings, readonly=True) as con:
            row = None
            if article_id:
                row = con.execute(
                    f"""
                    SELECT * FROM contents c
                    WHERE {self._wechat_article_predicate('c')}
                      AND (
                        c.content_id=?
                        OR c.content_id IN (
                            SELECT content_id FROM content_identifiers
                            WHERE namespace='wechat_article' AND external_id=?
                        )
                      )
                    LIMIT 1
                    """,
                    (article_id, article_id),
                ).fetchone()
            if row is None and url:
                row = con.execute(
                    f"SELECT * FROM contents c WHERE {self._wechat_article_predicate('c')} AND c.canonical_url=? LIMIT 1",
                    (url,),
                ).fetchone()
            if row is None:
                raise NotFoundError("微信文章", article_id or url)
            if not article_id:
                identifier = con.execute(
                    "SELECT external_id FROM content_identifiers WHERE namespace='wechat_article' AND content_id=? LIMIT 1",
                    (row["content_id"],),
                ).fetchone()
                if identifier:
                    projected = self._projection("article_detail", str(identifier["external_id"]))
                    if projected is not None:
                        return self._complete_hit_detail(
                            projected, str(identifier["external_id"])
                        )
            article = _row(row)
            hits = [_row(x) for x in con.execute("SELECT h.*,s.keyword,s.captured_at FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE h.content_id=? ORDER BY s.captured_at DESC,h.rank", (article["content_id"],))]
            paths = [
                dict(x)
                for x in con.execute(
                    "SELECT relative_path,asset_path FROM wechat_article_paths WHERE article_id=? ORDER BY created_at DESC",
                    (article["content_id"],),
                )
            ]
            primary_path = paths[0]["relative_path"] if paths else ""
        return {
            "article": {
                "article_id": article.get("content_id", ""),
                "title": article.get("title") or "",
                "url": article.get("canonical_url") or "",
                "normalized_url": article.get("canonical_url") or "",
                "published_at": article.get("published_at") or "",
                "summary": (article.get("source") or {}).get("summary", "") if isinstance(article.get("source"), dict) else "",
                "content_path": primary_path or "",
                "read_count": None, "like_count": None, "friends_follow_count": None,
                "original_article_count": None,
                "first_seen_at": article.get("first_seen_at") or "",
                "last_seen_at": article.get("updated_at") or "",
            },
            "account": {"account_id": article.get("creator_id") or "", "name": article.get("author_name") or "", "headimg_url": "", "wechat_biz": ""},
            "url_profile": {"url": article.get("canonical_url") or "", "article_ids": [article.get("content_id")], "article_record_count": 1, "title_variants": [article.get("title")] if article.get("title") else []},
            "keyword_groups": [],
            "keyword_cloud": [],
            "hit_count": len(hits),
            "keyword_count": len({x.get("keyword") for x in hits if x.get("keyword")}),
            "content_files": [{"article_id": article.get("content_id", ""), "title": article.get("title") or "", "path": x.get("relative_path"), "is_primary": index == 0} for index, x in enumerate(paths)],
            "metric_points": [],
            "timeline_events": [],
        }

    def _complete_hit_detail(
        self,
        projected: dict[str, Any],
        article_id: str,
    ) -> dict[str, Any]:
        """补回旧详情投影生成时遗漏的账号名和抓取批次。"""
        result = deepcopy(projected)
        snapshot_ids = {
            str(hit.get("snapshot_id") or "")
            for group in result.get("keyword_groups", [])
            if isinstance(group, dict)
            for hit in group.get("hits", [])
            if isinstance(hit, dict) and hit.get("snapshot_id")
        }
        with connect(self.settings, readonly=True) as con:
            identifier = con.execute(
                """
                SELECT payload_json
                FROM content_identifiers
                WHERE namespace='wechat_article' AND external_id=?
                """,
                (article_id,),
            ).fetchone()
            article = _json_object(identifier["payload_json"]) if identifier else {}
            account_id = str(
                article.get("account_id")
                or (result.get("account") or {}).get("account_id")
                or ""
            )
            creator = con.execute(
                """
                SELECT canonical_name,payload_json
                FROM creators
                WHERE platform='wechat-search' AND creator_id=?
                """,
                (account_id,),
            ).fetchone()
            monitor_account = con.execute(
                """
                SELECT payload_json
                FROM wechat_legacy_projections
                WHERE projection_kind='account' AND subject_id=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            content_row = con.execute(
                """
                SELECT c.content_id,c.title
                FROM contents c
                JOIN content_identifiers i
                  ON i.content_id=c.content_id AND i.namespace='wechat_article'
                WHERE i.external_id=?
                LIMIT 1
                """,
                (article_id,),
            ).fetchone()
            content_paths = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT relative_path,asset_path
                    FROM wechat_article_paths
                    WHERE article_id=?
                    ORDER BY created_at DESC
                    """,
                    (content_row["content_id"],),
                )
            ] if content_row else []
            snapshot_paths: dict[str, str] = {}
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                for row in con.execute(
                    f"""
                    SELECT snapshot_id,payload_json
                    FROM search_snapshots
                    WHERE snapshot_id IN ({placeholders})
                       OR json_extract(payload_json,'$.snapshot_id')
                          IN ({placeholders})
                    """,
                    (*sorted(snapshot_ids), *sorted(snapshot_ids)),
                ):
                    payload = _json_object(row["payload_json"])
                    source_id = str(payload.get("snapshot_id") or row["snapshot_id"])
                    source_path = str(payload.get("source_file_path") or "")
                    snapshot_paths[source_id] = source_path
                    snapshot_paths[str(row["snapshot_id"])] = source_path

        creator_payload = _json_object(creator["payload_json"]) if creator else {}
        monitor_payload = (
            _projection_payload(monitor_account["payload_json"])
            if monitor_account else {}
        )
        account = result.setdefault("account", {})
        account["account_id"] = account_id
        account["name"] = (
            creator_payload.get("canonical_name")
            or (creator["canonical_name"] if creator else None)
            or monitor_payload.get("name")
            or ""
        )
        account["headimg_url"] = (
            creator_payload.get("headimg_url")
            or monitor_payload.get("headimg_url")
            or ""
        )
        account["wechat_biz"] = creator_payload.get("wechat_biz") or ""
        if content_paths:
            article = result.setdefault("article", {})
            article["content_path"] = content_paths[0].get("relative_path") or ""
            result["content_files"] = [
                {
                    "article_id": article_id,
                    "title": (content_row["title"] if content_row else "") or "",
                    "path": row.get("relative_path"),
                    "is_primary": index == 0,
                }
                for index, row in enumerate(content_paths)
            ]
        else:
            result.setdefault("article", {})["content_path"] = ""
            result["content_files"] = []
        for group in result.get("keyword_groups", []):
            if not isinstance(group, dict):
                continue
            for hit in group.get("hits", []):
                if not isinstance(hit, dict):
                    continue
                hit["batch_id"] = _legacy_batch_id(
                    snapshot_paths.get(str(hit.get("snapshot_id") or ""), "")
                )
        return result

    def keyword_manage(self) -> dict[str, Any]:
        projected = self._projection("keyword_manage")
        if projected is not None:
            now = self._now()
            with connect(self.settings, readonly=True) as con:
                runtime_settings = {
                    str(row["keyword_id"]): _json_object(row["payload_json"])
                    for row in con.execute(
                        """
                        SELECT keyword_id,payload_json
                        FROM search_keyword_settings
                        WHERE system_key='wechat-search'
                        """
                    )
                }
            projected["updated_at"] = self._now().isoformat(timespec="seconds")
            for group in projected.get("groups", []):
                if not isinstance(group, dict):
                    continue
                keywords = group.get("keywords", [])
                if not isinstance(keywords, list):
                    continue
                indexed_keywords = list(enumerate(keywords))

                def legacy_order(entry: tuple[int, Any]) -> tuple[Any, ...]:
                    index, keyword = entry
                    if not isinstance(keyword, dict):
                        return (1, index, "")
                    runtime = runtime_settings.get(
                        str(keyword.get("keyword_id") or "")
                    )
                    if not runtime:
                        return (1, index, "")
                    return (
                        0,
                        _legacy_keyword_order(runtime.get("keyword_order")),
                        str(
                            runtime.get("keyword_text")
                            or keyword.get("keyword_text")
                            or ""
                        ),
                    )

                # 冻结旧 KeywordRegistryRepository.load_payload() 先按
                # keyword_order、keyword_text 排序，再按 group_id 分桶。
                # 导入 projection 的原始 SELECT * 顺序不具备这个语义。
                indexed_keywords.sort(key=legacy_order)
                keywords[:] = [keyword for _, keyword in indexed_keywords]

                for keyword in keywords:
                    if not isinstance(keyword, dict):
                        continue
                    runtime = runtime_settings.get(
                        str(keyword.get("keyword_id") or ""),
                        {},
                    )
                    frequency_days = int(
                        runtime.get("refresh_frequency_days")
                        or keyword.get("refresh_frequency_days")
                        or 1
                    )
                    refresh_source = (
                        runtime.get("refresh_frequency_source")
                        or keyword.get("refresh_frequency_source")
                        or "auto"
                    )
                    lifecycle_stage = (
                        runtime.get("lifecycle_stage")
                        or keyword.get("lifecycle_stage")
                        or "established"
                    )
                    effective_hours = (
                        3.0
                        if (
                            lifecycle_stage == "observing"
                            and refresh_source != "manual"
                        )
                        else float(max(1, frequency_days) * 24)
                    )
                    keyword["effective_refresh_interval_hours"] = effective_hours
                    last_refresh_at = (
                        runtime.get("last_refresh_at")
                        or runtime.get("last_seen_at")
                        or keyword.get("last_refresh_at")
                        or keyword.get("last_seen_at")
                    )
                    keyword["last_refresh_at"] = last_refresh_at
                    keyword["next_refresh_at"] = None
                    keyword["refresh_age_days"] = None
                    keyword["is_refresh_due"] = True
                    if not last_refresh_at:
                        continue
                    try:
                        refreshed_at = datetime.fromisoformat(
                            str(last_refresh_at)
                        ).replace(tzinfo=None)
                    except ValueError:
                        continue
                    next_refresh_at = refreshed_at + timedelta(hours=effective_hours)
                    keyword["next_refresh_at"] = next_refresh_at.isoformat(
                        timespec="seconds"
                    )
                    keyword["refresh_age_days"] = max(
                        0,
                        int((now - refreshed_at).total_seconds() // 86400),
                    )
                    keyword["is_refresh_due"] = now >= next_refresh_at
            return projected
        with connect(self.settings, readonly=True) as con:
            groups = [_row(x) for x in con.execute("SELECT * FROM search_keyword_groups WHERE system_key='wechat-search' ORDER BY sort_order,group_id")]
            keywords = self._keywords(con)
        grouped = {g["group_id"]: {**g, "keywords": []} for g in groups}
        for item in keywords:
            grouped.get(item.get("group_id"), {"keywords": []})["keywords"].append(item)
        return {
            "groups": list(grouped.values()),
            "keywords": keywords,
            "total": len(keywords),
            "updated_at": self._now().isoformat(timespec="seconds"),
        }

    def discovery(self, status: list[str] | None = None, limit: int = 100, candidate_status: list[str] | None = None) -> dict[str, Any]:
        limit = max(1, min(500, int(limit)))
        with connect(self.settings, readonly=True) as con:
            probe_statuses = [str(x).strip() for x in (status or []) if str(x).strip()]
            candidate_statuses = [str(x).strip() for x in (candidate_status or []) if str(x).strip()]
            params: list[Any] = []
            candidate_where = ""
            probe_where = ""
            if candidate_statuses:
                candidate_where = " WHERE status IN (" + ",".join("?" for _ in candidate_statuses) + ")"
                params.extend(candidate_statuses)
            probe_params: list[Any] = []
            if probe_statuses:
                probe_where = " WHERE status IN (" + ",".join("?" for _ in probe_statuses) + ")"
                probe_params.extend(probe_statuses)
            try:
                candidate_rows = con.execute(
                    f"""
                    SELECT payload_json
                    FROM wechat_discovery_candidates
                    {candidate_where}
                    ORDER BY
                        CAST(json_extract(payload_json,'$.validation_score') AS REAL) DESC,
                        CAST(json_extract(payload_json,'$.related_parent_probe_count') AS INTEGER) DESC,
                        CAST(json_extract(payload_json,'$.related_occurrence_count') AS INTEGER) DESC,
                        json_extract(payload_json,'$.first_seen_at')
                    LIMIT ?
                    """,
                    (*params, limit),
                ).fetchall()
                probe_rows = con.execute(
                    f"""
                    SELECT payload_json
                    FROM wechat_discovery_probes
                    {probe_where}
                    ORDER BY json_extract(payload_json,'$.proposed_at'), probe_id
                    LIMIT ?
                    """,
                    (*probe_params, limit),
                ).fetchall()
                probe_summary = con.execute(
                    """
                    SELECT status,COUNT(*) AS total
                    FROM wechat_discovery_probes
                    GROUP BY status
                    """
                ).fetchall()
                candidate_summary = con.execute(
                    """
                    SELECT status,COUNT(*) AS total
                    FROM wechat_discovery_candidates
                    GROUP BY status
                    """
                ).fetchall()
            except Exception:
                candidate_rows, probe_rows = [], []
                probe_summary, candidate_summary = [], []
        candidates = []
        for row in candidate_rows:
            item = _json_object(row["payload_json"])
            item["source_probe_ids"] = _json_list(
                item.get("source_probe_ids_json")
            )
            item["source_article_ids"] = _json_list(
                item.get("source_article_ids_json")
            )
            candidates.append(item)
        probes = []
        for row in probe_rows:
            item = _json_object(row["payload_json"])
            item["warming_facts"] = _json_list(item.get("warming_facts_json"))
            item["replacement_candidate_ids"] = _json_list(
                item.get("replacement_candidate_ids_json")
            )
            probes.append(item)
        return {
            "summary": {
                "probes": {
                    str(row["status"]): int(row["total"])
                    for row in probe_summary
                },
                "candidates": {
                    str(row["status"]): int(row["total"])
                    for row in candidate_summary
                },
            },
            "probes": probes,
            "candidates": candidates,
        }

    def _article_cache_token(self, con) -> tuple[Any, ...]:
        checkpoint = con.execute(
            """
            SELECT source_hash
            FROM ingestion_checkpoints
            WHERE adapter_key='wechat-search' AND checkpoint_key='normalized'
            """
        ).fetchone()
        identifier_stats = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest
            FROM content_identifiers
            WHERE namespace='wechat_article'
            """
        ).fetchone()
        snapshot_stats = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest
            FROM search_snapshots
            WHERE platform='wechat-search'
            """
        ).fetchone()
        hit_stats = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(h.rowid),0) AS latest
            FROM search_hits h
            JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
            WHERE s.platform='wechat-search'
            """
        ).fetchone()
        content_stats = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest,
                   COALESCE(MAX(updated_at),'') AS latest_update
            FROM contents
            """
        ).fetchone()
        metric_stats = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest,
                   COALESCE(MAX(observed_at),'') AS latest_observed
            FROM metric_observations
            WHERE subject_type='content'
              AND metric_key IN (
                'wechat.read_count','wechat.like_count',
                'wechat.article.read_count','wechat.article.like_count'
              )
            """
        ).fetchone()
        keyword_version = con.execute(
            """
            SELECT COALESCE(MAX(updated_at),'') AS latest
            FROM keywords
            WHERE platform='wechat-search'
            """
        ).fetchone()
        keyword_setting_version = con.execute(
            """
            SELECT COUNT(*) AS total,COALESCE(MAX(rowid),0) AS latest_row,
                   COALESCE(MAX(updated_at),'') AS latest_update
            FROM search_keyword_settings
            WHERE system_key='wechat-search'
            """
        ).fetchone()
        creator_version = con.execute(
            """
            SELECT COALESCE(MAX(updated_at),'') AS latest
            FROM creators
            WHERE platform='wechat-search'
            """
        ).fetchone()
        account_projection_version = con.execute(
            """
            SELECT COALESCE(MAX(updated_at),'') AS latest
            FROM wechat_legacy_projections
            WHERE projection_kind='account'
            """
        ).fetchone()
        return (
            str(Path(self.settings.database_path).resolve()),
            checkpoint["source_hash"] if checkpoint else "",
            identifier_stats["total"],
            identifier_stats["latest"],
            snapshot_stats["total"],
            snapshot_stats["latest"],
            hit_stats["total"],
            hit_stats["latest"],
            content_stats["total"],
            content_stats["latest"],
            content_stats["latest_update"],
            metric_stats["total"],
            metric_stats["latest"],
            metric_stats["latest_observed"],
            keyword_version["latest"],
            keyword_setting_version["total"],
            keyword_setting_version["latest_row"],
            keyword_setting_version["latest_update"],
            creator_version["latest"],
            account_projection_version["latest"],
        )

    def _legacy_article_rows(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with connect(self.settings, readonly=True) as con:
            token = self._article_cache_token(con)
            with self._article_cache_lock:
                cached = self._article_cache.get(token)
            if cached is not None:
                return cached

            creators: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                """
                SELECT creator_id,canonical_name,payload_json
                FROM creators
                WHERE platform='wechat-search'
                """
            ):
                payload = _json_object(row["payload_json"])
                creators[str(row["creator_id"])] = {
                    **payload,
                    "canonical_name": (
                        payload.get("canonical_name")
                        or row["canonical_name"]
                        or ""
                    ),
                }

            monitor_accounts: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                """
                SELECT subject_id,payload_json
                FROM wechat_legacy_projections
                WHERE projection_kind='account'
                ORDER BY rowid
                """
            ):
                monitor_accounts[str(row["subject_id"])] = _projection_payload(
                    row["payload_json"]
                )

            keyword_text: dict[str, str] = {}
            for row in con.execute(
                """
                SELECT keyword_id,payload_json
                FROM search_keyword_settings
                WHERE system_key='wechat-search'
                ORDER BY rowid
                """
            ):
                source_id = str(row["keyword_id"])
                payload = _json_object(row["payload_json"])
                text = _legacy_keyword_text(
                    source_id,
                    payload.get("keyword_text"),
                    payload.get("keyword"),
                )
                if text:
                    keyword_text[source_id] = text
            for row in con.execute(
                """
                SELECT keyword_id,keyword,payload_json
                FROM keywords
                WHERE platform='wechat-search'
                ORDER BY rowid
                """
            ):
                source_id = str(row["keyword_id"])
                if source_id in keyword_text:
                    continue
                payload = _json_object(row["payload_json"])
                text = _legacy_keyword_text(
                    source_id,
                    payload.get("keyword_text"),
                    payload.get("keyword"),
                    row["keyword"],
                )
                if text:
                    keyword_text[source_id] = text
            snapshots: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                """
                SELECT snapshot_id,keyword,keyword_id,captured_at,payload_json
                FROM search_snapshots
                WHERE platform='wechat-search'
                ORDER BY rowid
                """
            ):
                payload = _json_object(row["payload_json"])
                source_id = str(payload.get("snapshot_id") or row["snapshot_id"])
                snapshots[source_id] = {
                    **payload,
                    "keyword_id": payload.get("keyword_id") or row["keyword_id"],
                    "keyword": payload.get("keyword") or row["keyword"],
                    "captured_at": payload.get("captured_at") or row["captured_at"],
                }
                snapshots.setdefault(str(row["snapshot_id"]), snapshots[source_id])

            metric_fallbacks: dict[str, dict[str, Any]] = {}
            for row in con.execute(
                """
                SELECT subject_id,metric_key,numeric_value,observed_at,observation_id
                FROM metric_observations
                WHERE subject_type='content'
                  AND metric_key IN (
                    'wechat.read_count','wechat.like_count',
                    'wechat.article.read_count','wechat.article.like_count'
                  )
                ORDER BY observed_at DESC,observation_id DESC
                """
            ):
                content_id = str(row["subject_id"])
                field = str(row["metric_key"]).rsplit(".", 1)[-1]
                metric_fallbacks.setdefault(content_id, {}).setdefault(field, row["numeric_value"])

            article_keywords: dict[str, set[str]] = {}
            article_days: dict[str, set[str]] = {}
            for row in con.execute(
                """
                SELECT h.snapshot_id,h.content_id,h.payload_json
                FROM search_hits h
                JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                WHERE s.platform='wechat-search'
                ORDER BY h.rowid
                """
            ):
                payload = _json_object(row["payload_json"])
                content_id = str(row["content_id"] or "")
                if not content_id:
                    continue
                source_snapshot_id = str(
                    payload.get("snapshot_id") or row["snapshot_id"]
                )
                snapshot = snapshots.get(source_snapshot_id, {})
                keyword_id = str(snapshot.get("keyword_id") or "")
                text = keyword_text.get(keyword_id) or _legacy_keyword_text(
                    keyword_id,
                    snapshot.get("keyword"),
                )
                if text:
                    article_keywords.setdefault(content_id, set()).add(text)
                snapshot_date = str(
                    snapshot.get("snapshot_date")
                    or snapshot.get("captured_at")
                    or ""
                )[:10]
                if snapshot_date:
                    article_days.setdefault(content_id, set()).add(
                        snapshot_date
                    )

            external_ids: dict[str, str] = {}
            identifier_payloads: dict[str, dict[str, Any]] = {}
            for identifier in con.execute(
                """
                SELECT content_id,external_id,payload_json
                FROM content_identifiers
                WHERE namespace='wechat_article'
                ORDER BY rowid
                """
            ):
                content_id = str(identifier["content_id"])
                external_ids.setdefault(content_id, str(identifier["external_id"]))
                identifier_payloads.setdefault(
                    content_id,
                    _json_object(identifier["payload_json"]),
                )

            content_paths = {
                str(row["article_id"]): (
                    row["relative_path"] or row["asset_path"] or ""
                )
                for row in con.execute(
                    """
                    SELECT p.article_id,p.relative_path,p.asset_path
                    FROM wechat_article_paths p
                    WHERE p.rowid=(
                        SELECT p2.rowid FROM wechat_article_paths p2
                        WHERE p2.article_id=p.article_id
                        ORDER BY p2.created_at DESC,p2.rowid DESC LIMIT 1
                    )
                    """
                )
            }
            creators_by_name = {
                str(value.get("canonical_name") or ""): {
                    "creator_id": creator_id,
                    **value,
                }
                for creator_id, value in creators.items()
                if value.get("canonical_name")
            }
            rows: list[dict[str, Any]] = []
            for content in con.execute(
                f"""
                SELECT c.*
                FROM contents c
                WHERE {self._wechat_article_predicate('c')}
                ORDER BY c.rowid
                """
            ):
                content_id = str(content["content_id"])
                hit_keywords = article_keywords.get(content_id)
                if not hit_keywords:
                    continue
                content_payload = _json_object(content["payload_json"])
                raw = {
                    **identifier_payloads.get(content_id, {}),
                    **content_payload,
                }
                account_name = str(
                    content["author_name"]
                    or raw.get("canonical_name")
                    or raw.get("creator_name_raw")
                    or raw.get("account")
                    or ""
                )
                account_id = str(content["creator_id"] or raw.get("account_id") or "")
                if not account_id and account_name:
                    account_id = str(
                        creators_by_name.get(account_name, {}).get("creator_id")
                        or (
                            "acct_"
                            + hashlib.sha256(account_name.encode("utf-8")).hexdigest()[:16]
                        )
                    )
                account = creators.get(account_id, {})
                monitor_account = monitor_accounts.get(account_id, {})
                sorted_keywords = sorted(hit_keywords)
                metric_fallback = metric_fallbacks.get(content_id, {})
                read_count = raw.get("read_count")
                if read_count is None:
                    read_count = metric_fallback.get("read_count")
                like_count = raw.get("like_count")
                if like_count is None:
                    like_count = metric_fallback.get("like_count")
                rows.append({
                    "article_id": external_ids.get(content_id, content_id),
                    "title": content["title"] or raw.get("title") or raw.get("title_raw") or "",
                    "url": content["canonical_url"] or raw.get("normalized_url") or raw.get("raw_url") or raw.get("url_raw") or "",
                    "account_id": account_id,
                    "account_name": (
                        account.get("canonical_name")
                        or monitor_account.get("name")
                        or account_name
                    ),
                    "account_headimg": (
                        account.get("headimg_url")
                        or monitor_account.get("headimg_url", "")
                    ),
                    # Contents payload is the primary legacy value; canonical
                    # observations are a fallback for imported/older articles.
                    "read_count": read_count,
                    "like_count": like_count,
                    "hit_count": len(sorted_keywords),
                    "hit_keywords": sorted_keywords,
                    "on_rank_days": len(article_days.get(content_id, set())),
                    "account_score": monitor_account.get("score") or 0,
                    "published_at": content["published_at"] or raw.get("published_at"),
                    "content_file_path": (
                        content_paths.get(content_id)
                        or raw.get("content_file_path")
                    ),
                    "cover_url": raw.get("cover_url"),
                })

        account_map: dict[str, dict[str, Any]] = {}
        for article in rows:
            account_id = article["account_id"]
            if account_id not in account_map:
                account_map[account_id] = {
                    "account_id": account_id,
                    "name": article["account_name"],
                    "headimg_url": article["account_headimg"],
                    "article_count": 0,
                }
            account_map[account_id]["article_count"] += 1
        accounts = sorted(
            account_map.values(),
            key=lambda item: (-item["article_count"], item["name"]),
        )
        result = (rows, accounts)
        with self._article_cache_lock:
            self._article_cache[token] = result
            if len(self._article_cache) > 8:
                oldest = next(iter(self._article_cache))
                if oldest != token:
                    self._article_cache.pop(oldest, None)
        return result

    def articles(self, *, page: int = 1, page_size: int = 50, sort: str = "reads", time_range: int = 15, min_hits: int = 0, account: str = "", search: str = "", as_of: datetime | None = None) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = int(page_size)
        if page_size < 1 or page_size > 200:
            page_size = 50
        sort = sort if sort in {"reads", "hitCount", "publishTime", "likes", "accountScore", "todayReads", "onRankDays"} else "reads"
        now = (as_of or self._now()).replace(tzinfo=None)
        articles, _ = self._legacy_article_rows()
        cutoff = now - timedelta(days=int(time_range)) if time_range > 0 else None
        filtered = []
        for item in articles:
            published_at = _legacy_parse_date(item.get("published_at"))
            if cutoff is not None and (published_at is None or published_at < cutoff):
                continue
            if item["hit_count"] < int(min_hits):
                continue
            if account and item["account_id"] != account:
                continue
            if search and search.strip().lower() not in item["title"].lower():
                continue
            filtered.append(item)
        if sort == "reads":
            filtered.sort(
                key=lambda item: (
                    item["read_count"] is None,
                    -(item["read_count"] or 0),
                )
            )
        elif sort == "likes":
            filtered.sort(
                key=lambda item: (
                    item["like_count"] is None,
                    -(item["like_count"] or 0),
                )
            )
        elif sort == "hitCount":
            filtered.sort(key=lambda item: -item["hit_count"])
        elif sort == "onRankDays":
            filtered.sort(
                key=lambda item: (
                    -item["on_rank_days"],
                    item["read_count"] is None,
                    -(item["read_count"] or 0),
                )
            )
        elif sort == "publishTime":
            filtered.sort(
                key=lambda item: (
                    _legacy_parse_date(item.get("published_at")) or datetime.min
                ),
                reverse=True,
            )
        elif sort == "accountScore":
            by_account: dict[str, list[dict[str, Any]]] = {}
            for item in filtered:
                by_account.setdefault(item["account_id"], []).append(item)
            for values in by_account.values():
                values.sort(
                    key=lambda item: (
                        item["read_count"] is None,
                        -(item["read_count"] or 0),
                    )
                )
            filtered = [
                item
                for account_id in sorted(
                    by_account,
                    key=lambda key: -by_account[key][0]["account_score"],
                )
                for item in by_account[account_id][:3]
            ]
        elif sort == "todayReads":
            today = now.date()
            by_day: dict[str, list[dict[str, Any]]] = {}
            for item in filtered:
                if item.get("published_at"):
                    by_day.setdefault(
                        str(item["published_at"])[:10], []
                    ).append(item)
            result = []
            for days_back in range(30):
                day = (today - timedelta(days=days_back)).isoformat()
                values = by_day.get(day, [])
                values.sort(
                    key=lambda item: (
                        item["read_count"] is None,
                        -(item["read_count"] or 0),
                    )
                )
                result.extend(values)
                if len(result) >= 100:
                    result = result[:100]
                    break
            filtered = result
        start = (page - 1) * page_size
        return {
            "articles": filtered[start:start + page_size],
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
        }

    def article_accounts(self) -> dict[str, Any]:
        _, accounts = self._legacy_article_rows()
        return {"accounts": accounts}
