from __future__ import annotations

import base64
import json
import zlib
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from content_hub.db.connection import connect
from content_hub.errors import NotFoundError, ValidationAppError


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
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


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

    def full(self) -> dict[str, Any]:
        value = self._projection("full")
        if value is None:
            raise NotFoundError("微信兼容投影", "full")
        return value

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
            return projected
        with connect(self.settings, readonly=True) as con:
            keywords = self._keywords(con)
            accounts = [
                _row(x) for x in con.execute(
                    "SELECT * FROM creators WHERE platform='wechat-search' ORDER BY canonical_name,creator_id"
                )
            ]
            latest = con.execute(
                "SELECT MAX(captured_at) AS latest FROM search_snapshots WHERE platform='wechat-search'"
            ).fetchone()["latest"]
        return {
            "generated_at": latest,
            "window_days": None,
            "window_start": None,
            "window_end": latest,
            "scope": {"total": len(keywords), "pinned": sum(bool(x.get("pinned")) for x in keywords)},
            "keywords": [self.keyword_summary(x) for x in keywords],
            "accounts": accounts,
            "bucket_options": sorted({x.get("keyword_bucket") for x in keywords if x.get("keyword_bucket")}),
        }

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
            return projected
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
                  AND p.relative_path=?
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                (path,),
            ).fetchone()
        if row is None:
            raise NotFoundError("微信正文", path)
        return dict(row)

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
                SELECT snapshot_id,keyword,keyword_id,payload_json
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
                }
                snapshots.setdefault(str(row["snapshot_id"]), snapshots[source_id])

            article_keywords: dict[str, set[str]] = {}
            article_days: dict[str, set[str]] = {}
            for row in con.execute(
                """
                SELECT h.snapshot_id,h.payload_json
                FROM search_hits h
                JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                WHERE s.platform='wechat-search'
                ORDER BY h.rowid
                """
            ):
                payload = _json_object(row["payload_json"])
                source_article_id = str(payload.get("article_id") or "")
                if not source_article_id:
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
                    article_keywords.setdefault(source_article_id, set()).add(text)
                snapshot_date = str(snapshot.get("snapshot_date") or "")
                if snapshot_date:
                    article_days.setdefault(source_article_id, set()).add(
                        snapshot_date
                    )

            rows: list[dict[str, Any]] = []
            for identifier in con.execute(
                """
                SELECT external_id,payload_json
                FROM content_identifiers
                WHERE namespace='wechat_article'
                ORDER BY rowid
                """
            ):
                article_id = str(identifier["external_id"])
                hit_keywords = article_keywords.get(article_id)
                if not hit_keywords:
                    continue
                raw = _json_object(identifier["payload_json"])
                account_id = str(raw.get("account_id") or "")
                account = creators.get(account_id, {})
                monitor_account = monitor_accounts.get(account_id, {})
                sorted_keywords = sorted(hit_keywords)
                rows.append({
                    "article_id": article_id,
                    "title": raw.get("title", ""),
                    "url": raw.get("normalized_url") or raw.get("raw_url", ""),
                    "account_id": account_id,
                    "account_name": (
                        account.get("canonical_name")
                        or monitor_account.get("name", "")
                    ),
                    "account_headimg": (
                        account.get("headimg_url")
                        or monitor_account.get("headimg_url", "")
                    ),
                    "read_count": raw.get("read_count"),
                    "like_count": raw.get("like_count"),
                    "hit_count": len(sorted_keywords),
                    "hit_keywords": sorted_keywords,
                    "on_rank_days": len(article_days.get(article_id, set())),
                    "account_score": monitor_account.get("score") or 0,
                    "published_at": raw.get("published_at"),
                    "content_file_path": raw.get("content_file_path"),
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
