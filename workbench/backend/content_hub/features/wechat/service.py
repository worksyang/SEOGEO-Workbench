from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.validation.urls import canonicalize_url

SOURCE_TZ = ZoneInfo("Asia/Shanghai")
def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:20]}"


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_time(value: Any) -> str | None:
    if value is None or str(value).strip() == "": return None
    raw = str(value).strip()
    candidates = [raw, raw.replace("/", "-")]
    parsed = None
    for candidate in candidates:
        try:
            if re.fullmatch(r"\d{2}-\d{2}-\d{2}", candidate): parsed = datetime.strptime(candidate, "%y-%m-%d").replace(tzinfo=SOURCE_TZ); break
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00")); break
        except ValueError: continue
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try: parsed = datetime.strptime(raw, fmt).replace(tzinfo=SOURCE_TZ); break
            except ValueError: pass
    if parsed is None: return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=SOURCE_TZ)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool): return None
    try:
        number = float(str(value).replace(",", "").strip())
        if not math.isfinite(number): return None
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError): return None


def _safe_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or _is_placeholder_url(raw): return None
    try: return canonicalize_url(raw)
    except ValidationAppError: return None


def _is_placeholder_url(value: Any) -> bool:
    return str(value or "").strip().lower().startswith("placeholder://")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WechatService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.adapter = WechatAdapter(settings)

    def _connection_status(self, status: str, *, error: str | None = None, success_at: str | None = None) -> None:
        checked_at = _utc_now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT details_json FROM system_connections WHERE system_key='wechat-search'").fetchone()
                    details = json.loads(row[0] or "{}") if row else {}
                    if error:
                        details["last_error"] = error
                        details["last_error_at"] = checked_at
                    else:
                        details.pop("last_error", None)
                    if success_at: details["last_success_at"] = success_at
                    con.execute(
                        """
                        INSERT INTO system_connections(system_key,display_name,base_url,status,last_checked_at,capabilities_json,details_json)
                        VALUES('wechat-search','微信搜一搜',?,?,?,?,?)
                        ON CONFLICT(system_key) DO UPDATE SET
                            status=excluded.status,last_checked_at=excluded.last_checked_at,details_json=excluded.details_json
                        """,
                        (self.adapter.base_url, status, checked_at, '["read","keyword_refresh","history_import"]', _json(details)),
                    )

    def _audit(self, action: str, outcome: str, *, details: dict[str, Any], subject_id: str | None = None) -> None:
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    occurred = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    con.execute("INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)", (_id("audit", f"{action}:{subject_id}:{occurred}"), occurred, "system", action, "wechat", subject_id, outcome, _json(details)))

    def bootstrap(self) -> dict[str, Any]:
        try: result = self.adapter.bootstrap()
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc
        self._connection_status(result.status, error=result.error, success_at=_utc_now())
        payload = result.payload
        keywords = payload.get("keywords") or []
        return {"source_status": {"status": result.status, "source": result.source, "error": result.error}, "summary": {"keyword_count": len(keywords), "account_count": len(payload.get("accounts") or []), "generated_at": payload.get("generated_at"), "window_days": payload.get("window_days")}, "keywords": [self._keyword_summary(x) for x in keywords if isinstance(x, dict)], "updated_at": payload.get("generated_at")}

    def _safe_status(self, status: str, error: str | None = None) -> None:
        try: self._connection_status(status, error=error, success_at=None)
        except Exception: pass

    @staticmethod
    def _keyword_summary(item: dict[str, Any]) -> dict[str, Any]:
        bucket = item.get("keyword_bucket") or item.get("bucket") or "未分组"
        return {
            "keyword_id": item.get("keyword_id"),
            "keyword": item.get("keyword") or item.get("keyword_text"),
            "group": item.get("group") or bucket,
            "status": item.get("status") or "active",
            "topic": item.get("topic") or item.get("keyword"),
            "bucket": bucket,
            "keyword_bucket": bucket,
            "today_best": item.get("today_best"),
            "today_count": item.get("today_count", 0),
            "article_count": item.get("article_count", 0),
            "latest_run": item.get("latest_run"),
        }

    @classmethod
    def _keyword_detail(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            **cls._keyword_summary(item),
            "history_best": item.get("history_best"),
            "history_hits": item.get("history_hits"),
            "turnover_runs": item.get("turnover_runs"),
            "kw_score": item.get("kw_score"),
        }

    def keyword(self, keyword_id: str) -> dict[str, Any]:
        try:
            remote = self.adapter.remote_keyword(keyword_id)
            self._connection_status("healthy", success_at=_utc_now())
            hub_records = self._hub_keyword_records(keyword_id)
            if hub_records["snapshots"]:
                return self._keyword_response(
                    remote,
                    records=hub_records,
                    source_status={"status": "healthy", "source": "legacy_http", "data_source": "hub_db"},
                )
            return self._keyword_response(remote, source_status={"status": "healthy", "source": "legacy_http"})
        except WechatSourceError as remote_error:
            hub_records = self._hub_keyword_records(keyword_id)
            if hub_records["snapshots"]:
                self._connection_status("degraded", error=str(remote_error), success_at=_utc_now())
                keyword = hub_records["keyword"]
                return self._keyword_response(
                    keyword,
                    records=hub_records,
                    source_status={"status": "degraded", "source": "hub_db", "error": str(remote_error)},
                )
            try: records = self.adapter.all_records()
            except WechatSourceError as exc:
                self._safe_status("offline", str(exc))
                raise ConflictError(str(exc)) from exc
            item = next((x for x in records["keywords"] if x.get("keyword_id") == keyword_id), None)
            if item is None: raise NotFoundError("微信关键词", keyword_id)
            self._connection_status("degraded", error="旧服务不可用，使用 normalized 降级", success_at=_utc_now())
            return self._keyword_response(item, records=records, source_status={"status": "degraded", "source": "legacy_normalized"})

    def _hub_keyword_records(self, keyword_id: str) -> dict[str, Any]:
        """读取已导入的关键词闭包，避免详情页重新解析旧源大 JSON。"""
        with connect(self.settings, readonly=True) as con:
            keyword_row = con.execute("SELECT * FROM keywords WHERE keyword_id=?", (keyword_id,)).fetchone()
            if keyword_row is None:
                return {"keyword": {}, "snapshots": [], "hits": [], "articles": [], "terms": [], "observations": []}
            snapshots = []
            for row in con.execute(
                "SELECT * FROM search_snapshots WHERE keyword_id=? ORDER BY captured_at",
                (keyword_id,),
            ).fetchall():
                snapshot = dict(row)
                try:
                    snapshot["features"] = json.loads(snapshot.get("features_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    snapshot["features"] = {}
                snapshots.append(snapshot)
            if not snapshots:
                return {"keyword": dict(keyword_row), "snapshots": [], "hits": [], "articles": [], "terms": [], "observations": []}
            hits = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT h.*, s.keyword, s.captured_at
                    FROM search_hits h
                    JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                    WHERE s.keyword_id=?
                    ORDER BY s.captured_at, h.rank, h.hit_id
                    """,
                    (keyword_id,),
                ).fetchall()
            ]
            for hit in hits:
                if hit.get("content_id") and not hit.get("article_id"):
                    hit["article_id"] = hit["content_id"]
            content_ids = sorted({str(row["content_id"]) for row in hits if row.get("content_id")})
            articles = []
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                for row in con.execute(
                    f"SELECT * FROM contents WHERE content_id IN ({placeholders})",
                    content_ids,
                ).fetchall():
                    article = dict(row)
                    article["article_id"] = article["content_id"]
                    articles.append(article)
            observations = []
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                observations = [
                    dict(row)
                    for row in con.execute(
                        f"""
                        SELECT *
                        FROM metric_observations
                        WHERE subject_type='content' AND subject_id IN ({placeholders})
                        ORDER BY observed_at
                        """,
                        content_ids,
                    ).fetchall()
                ]
                for row in observations:
                    row["article_id"] = row["subject_id"]
                    row["source_snapshot_id"] = row.get("snapshot_id")
            return {
                "keyword": dict(keyword_row),
                "snapshots": snapshots,
                "hits": hits,
                "articles": articles,
                "terms": [],
                "observations": observations,
            }

    def _keyword_response(self, item: dict[str, Any], *, records: dict[str, Any] | None = None, source_status: dict[str, Any]) -> dict[str, Any]:
        kid = item.get("keyword_id")
        if records is None:
            try: records = self.adapter.detail_records()
            except WechatSourceError: records = {"snapshots": [], "hits": [], "articles": [], "observations": []}
        snapshots = [x for x in records["snapshots"] if x.get("keyword_id") == kid]
        snapshot_views = self._snapshot_views(snapshots, records)
        return {
            "source_status": source_status,
            "keyword": self._keyword_detail(item),
            "snapshots": snapshot_views,
            "hits": [hit for view in snapshot_views for hit in view["hits"]],
            "articles": [article for view in snapshot_views for article in view["articles"]],
            "features": {"today_best": item.get("today_best"), "today_count": item.get("today_count"), "coverage_days": item.get("coverage_days"), "heat_summary": item.get("heat_summary") or {}},
            "observations": [obs for view in snapshot_views for obs in view["observations"]],
        }

    @staticmethod
    def _snapshot_views(snapshots: list[dict[str, Any]], records: dict[str, Any]) -> list[dict[str, Any]]:
        articles_by_id = {str(row.get("article_id")): row for row in records.get("articles", [])}
        hits_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        for row in records.get("hits", []):
            hits_by_snapshot.setdefault(str(row.get("snapshot_id")), []).append(row)
        for rows in hits_by_snapshot.values():
            rows.sort(key=lambda row: (int(row.get("rank")) if str(row.get("rank", "")).isdigit() else 10**9, str(row.get("hit_id") or "")))
        observations_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        observations_by_article_time: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in records.get("observations", []):
            if row.get("source_snapshot_id"):
                observations_by_snapshot.setdefault(str(row["source_snapshot_id"]), []).append(row)
            else:
                key = (str(row.get("article_id")), str(row.get("observed_at")))
                observations_by_article_time.setdefault(key, []).append(row)
        terms_by_snapshot: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for snapshot in snapshots:
            snapshot_features = snapshot.get("features")
            if isinstance(snapshot_features, dict):
                bucket = terms_by_snapshot.setdefault(str(snapshot.get("snapshot_id")), {"suggestions": [], "related": []})
                for feature_key in ("suggestions", "related"):
                    for term in snapshot_features.get(feature_key) or []:
                        if isinstance(term, dict):
                            bucket[feature_key].append({"term": term.get("term"), "position": term.get("position")})
        for term in records.get("terms", []):
            bucket = terms_by_snapshot.setdefault(str(term.get("snapshot_id")), {"suggestions": [], "related": []})
            term_type = str(term.get("term_type") or "related").lower()
            target = "suggestions" if term_type in {"suggestion", "suggestions"} else "related"
            bucket[target].append({"term": term.get("term_text"), "position": term.get("position")})
        views = []
        for snapshot in snapshots:
            sid = str(snapshot.get("snapshot_id"))
            hits = hits_by_snapshot.get(sid, [])
            article_ids = {str(row.get("article_id")) for row in hits if row.get("article_id")}
            observations = observations_by_snapshot.get(sid, [])
            if not observations:
                observations = [
                    row
                    for article_id in article_ids
                    for row in observations_by_article_time.get((article_id, str(snapshot.get("captured_at"))), [])
                ]
            terms = terms_by_snapshot.get(sid, {"suggestions": [], "related": []})
            views.append({
                "snapshot_id": snapshot.get("snapshot_id"),
                "captured_at": snapshot.get("captured_at"),
                "trigger_type": snapshot.get("trigger_type"),
                "result_count": snapshot.get("result_count"),
                "hits": hits,
                "articles": [articles_by_id[str(hit.get("article_id"))] for hit in hits if hit.get("article_id") and str(hit.get("article_id")) in articles_by_id],
                "features": {"suggestions": sorted(terms["suggestions"], key=lambda x: (x["position"] is None, x["position"])), "related": sorted(terms["related"], key=lambda x: (x["position"] is None, x["position"]))},
                "observations": observations,
            })
        return views

    def article(self, article_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            article = con.execute("SELECT * FROM contents WHERE content_id=?", (article_id,)).fetchone()
            if article:
                hits = [dict(x) for x in con.execute("SELECT h.*,s.keyword,s.captured_at,s.features_json FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE h.content_id=? ORDER BY s.captured_at DESC,h.rank", (article_id,)).fetchall()]
                obs = [dict(x) for x in con.execute("SELECT * FROM metric_observations WHERE subject_type='content' AND subject_id=? ORDER BY observed_at DESC", (article_id,)).fetchall()]
                payload = json.loads(article["payload_json"] or "{}")
                snapshot_rows: dict[str, dict[str, Any]] = {}
                for hit in hits:
                    sid = str(hit["snapshot_id"])
                    snapshot_rows.setdefault(sid, {"snapshot_id": sid, "captured_at": hit["captured_at"], "keyword": hit["keyword"], "features": json.loads(hit.get("features_json") or "{}"), "hits": []})["hits"].append(hit)
                return {"source_status": {"status": "healthy", "source": "hub_db"}, "article": {**dict(article), "source": payload}, "snapshots": list(snapshot_rows.values()), "hits": hits, "articles": [{**dict(article), "source": payload}], "features": {"canonical_url": article["canonical_url"], "published_at": article["published_at"]}, "observations": obs}
        try: records = self.adapter.detail_records()
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc
        article = next((x for x in records["articles"] if x.get("article_id") == article_id), None)
        if article is None: raise NotFoundError("微信文章", article_id)
        self._connection_status("degraded", error="旧服务不可用，使用 normalized 降级", success_at=_utc_now())
        hits = [x for x in records["hits"] if x.get("article_id") == article_id]
        obs = [x for x in records["observations"] if x.get("article_id") == article_id]
        content = None
        try:
            if article.get("content_file_path"): content = self.adapter.remote_article_content(article["content_file_path"])
        except WechatSourceError: pass
        snapshot_rows = [x for x in records["snapshots"] if x.get("snapshot_id") in {h.get("snapshot_id") for h in hits}]
        views = self._snapshot_views(snapshot_rows, records)
        return {"source_status": {"status": "degraded", "source": "legacy_normalized"}, "article": article, "snapshots": views, "hits": hits, "articles": [article], "features": {"content": content, "canonical_url": _safe_url(article.get("normalized_url") or article.get("raw_url"))}, "observations": obs}

    def refresh(self, keyword_id: str, confirm: bool) -> dict[str, Any]:
        if confirm is not True: raise ValidationAppError("刷新必须明确传入 confirm=true。")
        try:
            item = self.adapter.remote_keyword(keyword_id)
            self._connection_status("healthy", success_at=_utc_now())
        except WechatSourceError:
            try:
                monitor = self.adapter.local_json("normalized/monitor-data.json")
                item = next((x for x in monitor.get("keywords", []) if x.get("keyword_id") == keyword_id), None)
            except WechatSourceError as exc:
                self._safe_status("offline", str(exc))
                self._audit("wechat.refresh", "blocked", details={"error": str(exc)}, subject_id=keyword_id)
                raise ConflictError(str(exc)) from exc
            if item is None:
                raise NotFoundError("微信关键词", keyword_id)
            self._connection_status("degraded", error="旧服务不可用，刷新仍拒绝伪成功", success_at=_utc_now())
        except Exception as exc:
            self._safe_status("offline", str(exc)); self._audit("wechat.refresh", "blocked", details={"error": str(exc)}, subject_id=keyword_id); raise
        keyword = str(item.get("keyword") or item.get("keyword_text") or "").strip()
        try:
            response = self.adapter.remote_refresh(keyword_id, keyword)
        except WechatSourceError as exc:
            outcome = "rejected" if exc.status == 409 else "failed"
            self._safe_status("degraded" if exc.status else "offline", str(exc)); self._audit("wechat.refresh", "blocked" if outcome == "rejected" else "failed", details={"semantic_status": outcome, "status": exc.status, "kind": exc.kind, "payload": exc.payload}, subject_id=keyword_id)
            if exc.status == 409: return {"http_status": 409, "source_status": {"status": "degraded", "source": "legacy_http"}, "result": exc.payload}
            raise ConflictError(str(exc)) from exc
        outcome = "queued" if response.status == 202 or response.payload.get("status") == "queued" else "running" if response.payload.get("status") == "running" else "succeeded"
        self._connection_status("healthy", success_at=_utc_now()); self._audit("wechat.refresh", "succeeded", details={"semantic_status": outcome, "status": response.status, "result": response.payload}, subject_id=keyword_id)
        return {"http_status": response.status, "source_status": {"status": "healthy", "source": "legacy_http"}, "result": response.payload}

    def refresh_status(self, job_id: str) -> dict[str, Any]:
        try:
            result = self.adapter.remote_refresh_status(job_id)
            self._connection_status("healthy", success_at=_utc_now())
            return {"source_status": {"status": "healthy", "source": "legacy_http"}, "result": result}
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc

    def import_history(self, *, dry_run: bool, limit: int | None) -> dict[str, Any]:
        try:
            return self._import_history(dry_run=dry_run, limit=limit)
        finally:
            self.adapter.clear_cache_for_root(self.adapter.root)

    def _import_history(self, *, dry_run: bool, limit: int | None) -> dict[str, Any]:
        try: records, manifest, reconcile = self.adapter.import_records(limit=limit)
        except WechatSourceError as exc:
            message = f"{exc.kind}: {exc}"
            self._safe_status("offline", message); self._audit("wechat.import", "failed", details={"kind": exc.kind, "error": str(exc)}); raise ConflictError(message) from exc
        counts = self._counts(records)
        scope = "full" if limit is None else _id("selection", json.dumps(reconcile.get("selection_snapshot_ids", []), ensure_ascii=False))
        batch_id = _id("batch", f"wechat:{self.adapter.manifest_id(manifest)}:{scope}")
        report = {
            "manifest_id": self.adapter.manifest_id(manifest),
            "manifest": manifest,
            "reconcile": reconcile,
            "rejected": [],
            "placeholder_count": 0,
            "placeholder_samples": [],
            "metric_fact_count": 0,
            "metric_unique_count": 0,
            "metric_collision_extra_count": 0,
            "metric_collision_group_count": 0,
            "metric_collision_same_value_count": 0,
            "metric_collision_value_diff_count": 0,
            "metric_collisions": [],
        }
        if dry_run:
            with connect(self.settings, readonly=True) as con:
                ids, snapshot_map = self._metric_context(con, records)
                self._prepare_metric_facts(con, records, ids, snapshot_map, report, planned_snapshot_ids=set(snapshot_map.values()))
            self._connection_status("healthy", success_at=_utc_now())
            self._audit("wechat.import", "succeeded", details={"dry_run": True, "batch_id": batch_id, "counts": counts, "audit": report})
            return {"dry_run": True, "source": "legacy_normalized", "counts": counts, "batch_id": batch_id, "audit": report}
        now = _utc_now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con): self._write(con, records, batch_id, report, now)
        if report["rejected"]:
            self._connection_status("degraded", error=f"{len(report['rejected'])} rows rejected", success_at=now)
        else:
            self._connection_status("healthy", success_at=now)
        self._audit("wechat.import", "failed" if report["rejected"] else "succeeded", details={"batch_id": batch_id, "counts": counts, "audit": report})
        return {"dry_run": False, "source": "legacy_normalized", "counts": counts, "batch_id": batch_id, "audit": report}

    @staticmethod
    def _counts(records: dict[str, Any]) -> dict[str, int]:
        return {"keywords": len(records["keywords"]), "creators": len(records["accounts"]), "contents": len(records["articles"]), "search_snapshots": len(records["snapshots"]), "search_hits": len(records["hits"]), "snapshot_terms": len(records["terms"]), "metric_observations": len(records["observations"])}

    def _write(self, con: sqlite3.Connection, records: dict[str, Any], batch_id: str, report: dict[str, Any], now: str) -> None:
        records_seen = sum(len(v) for v in records.values() if isinstance(v, list))
        con.execute("INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status='running',started_at=excluded.started_at,payload_json=excluded.payload_json", (batch_id, "wechat-search", "history", "running", now, str(self.adapter.root), _json(report)))
        ids: dict[str, str] = {}
        accepted_rows: dict[str, set[str]] = {}

        def accept(kind: str, key: Any) -> None:
            accepted_rows.setdefault(kind, set()).add(str(key))
        for row in records["keywords"]:
            kid, keyword = str(row.get("keyword_id") or _id("kw", row.get("keyword"))), str(row.get("keyword") or "").strip()
            if not keyword: report["rejected"].append({"kind": "keyword", "row": row, "reason": "missing keyword"}); continue
            con.execute("INSERT INTO keywords(keyword_id,platform,keyword,status,topic,keyword_bucket,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id) DO UPDATE SET keyword=excluded.keyword,status=excluded.status,topic=excluded.topic,keyword_bucket=excluded.keyword_bucket,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (kid,"wechat-search",keyword,row.get("status") or "active",row.get("topic") or keyword,row.get("keyword_bucket") or row.get("bucket"),_source_time(row.get("first_seen_at")) or now,_source_time(row.get("updated_at")) or now,_json(row)))
            accept("keywords", kid)
        for row in records["accounts"]:
            aid = str(row.get("account_id") or _id("creator", row.get("canonical_name")))
            con.execute("INSERT INTO creators(creator_id,canonical_name,platform,external_id,profile_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (aid,row.get("canonical_name"),"wechat-search",aid,None,_source_time(row.get("first_seen_at")) or now,_source_time(row.get("last_seen_at")) or now,_json(row)))
            accept("accounts", aid)
        for row in records["articles"]:
            source_id = str(row.get("article_id") or _id("content", row.get("title"))); cid = source_id; raw_url = row.get("normalized_url") or row.get("raw_url"); url = _safe_url(raw_url)
            if raw_url and _is_placeholder_url(raw_url):
                report["placeholder_count"] += 1
                if len(report["placeholder_samples"]) < 10:
                    report["placeholder_samples"].append({"source_id": source_id, "value": raw_url})
            elif raw_url and url is None:
                report["rejected"].append({"kind": "url", "source_id": source_id, "value": raw_url, "reason": "invalid_url"})
            if url:
                existing = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (url,)).fetchone(); cid = str(existing[0]) if existing else cid
            ids[source_id] = cid
            creator = row.get("account_id")
            if creator: con.execute("INSERT OR IGNORE INTO creators(creator_id,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)", (creator,"wechat-search",creator,now,now,_json({"source": "article_reference"})))
            con.execute("INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,author_name,published_at,first_seen_at,updated_at,md_path,domain,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_id) DO UPDATE SET title=excluded.title,canonical_url=excluded.canonical_url,creator_id=excluded.creator_id,published_at=excluded.published_at,updated_at=excluded.updated_at,md_path=excluded.md_path,payload_json=excluded.payload_json", (cid,"external_article",row.get("title"),url,creator,row.get("author_name"),_source_time(row.get("published_at")),_source_time(row.get("first_seen_at")) or now,_source_time(row.get("last_seen_at")) or now,row.get("content_file_path"),"mp.weixin.qq.com" if url and "mp.weixin.qq.com" in url else None,_json({**row,"source_timezone":"Asia/Shanghai"})))
            accept("articles", source_id)
            for namespace, external in (("wechat_article", row.get("article_id")), ("wechat_url", url)):
                if external: con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO UPDATE SET content_id=excluded.content_id,payload_json=excluded.payload_json", (namespace,str(external),cid,now,_json(row)))
        snapshot_features: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for term in records["terms"]:
            bucket = snapshot_features.setdefault(str(term.get("snapshot_id")), {"suggestions": [], "related": []})
            target = "suggestions" if str(term.get("term_type") or "").lower() in {"suggestion", "suggestions"} else "related"
            bucket[target].append({"term": term.get("term_text"), "position": term.get("position")})
        snapshot_map: dict[str, str] = {}
        valid_snapshots: set[str] = set()
        for row in records["snapshots"]:
            sid = str(row.get("snapshot_id")); kid = row.get("keyword_id"); keyword = next((x.get("keyword") for x in records["keywords"] if x.get("keyword_id") == kid), str(kid or "")); captured = _source_time(row.get("captured_at"))
            result_count = _number(row.get("result_count"))
            if not sid or not captured: report["rejected"].append({"kind": "snapshot", "row": row, "reason": "invalid snapshot_id/captured_at"}); continue
            if kid: con.execute("INSERT OR IGNORE INTO keywords(keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)", (kid,"wechat-search",keyword or str(kid),"active",captured,captured,_json({"source":"snapshot_reference"})))
            if result_count is None and row.get("result_count") is not None:
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "invalid_result_count"})
            elif isinstance(result_count, float) and not result_count.is_integer():
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "fractional_result_count"})
                result_count = None
            elif isinstance(result_count, (int, float)) and result_count < 0:
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "negative_result_count"})
                result_count = None
            existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("wechat-search", keyword, captured)).fetchone()
            actual_sid = str(existing[0]) if existing else sid
            snapshot_map[sid] = actual_sid
            valid_snapshots.add(actual_sid)
            features = snapshot_features.get(sid, {"suggestions": [], "related": []})
            con.execute("INSERT INTO search_snapshots(snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET keyword=excluded.keyword,keyword_id=excluded.keyword_id,captured_at=excluded.captured_at,result_count=excluded.result_count,features_json=excluded.features_json,payload_json=excluded.payload_json", (actual_sid,"wechat-search",keyword,kid,captured,row.get("trigger_type"),result_count,_json(features),row.get("source_file_path"),_json({**row,"source_timezone":row.get("timezone") or "Asia/Shanghai"})))
            accept("snapshots", sid)
        for row in records["terms"]:
            if row.get("snapshot_id") in snapshot_map:
                accept("terms", row.get("term_id") or f"{row.get('snapshot_id')}:{row.get('position')}")
        for row in records["hits"]:
            source_sid, rank, hid = row.get("snapshot_id"), _number(row.get("rank")), str(row.get("hit_id") or _id("hit", f"{row.get('snapshot_id')}:{row.get('rank')}"))
            sid = snapshot_map.get(str(source_sid))
            if sid not in valid_snapshots or not isinstance(rank, (int, float)) or int(rank) <= 0 or int(rank) != rank:
                report["rejected"].append({"kind":"hit","row":row,"reason":"invalid snapshot/rank"}); continue
            mapped = ids.get(str(row.get("article_id")))
            con.execute("DELETE FROM search_hits WHERE snapshot_id=? AND rank=? AND hit_id<>?", (sid,int(rank),hid))
            con.execute("INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(hit_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,rank=excluded.rank,content_id=excluded.content_id,title_raw=excluded.title_raw,url_raw=excluded.url_raw,creator_name_raw=excluded.creator_name_raw,payload_json=excluded.payload_json", (hid,sid,int(rank),mapped,row.get("title_raw"),row.get("url_raw"),row.get("account_name_raw"),_json(row)))
            accept("hits", hid)
            if mapped:
                snapshot_time = con.execute("SELECT captured_at FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?)", (_id("discovery", f"{mapped}:{sid}"),mapped,"wechat-search","keyword-rank",snapshot_time,sid,row.get("url_raw"),_json(row)))
        facts = self._prepare_metric_facts(con, records, ids, snapshot_map, report)
        for fact in facts:
            self._write_metric_fact(con, fact)
            accept("observations", fact["source_observation_id"])
        records_failed = len(report["rejected"])
        records_written = sum(len(values) for values in accepted_rows.values())
        batch_status = "partial_failed" if records_failed else "succeeded"
        con.execute("UPDATE ingestion_batches SET status=?,finished_at=?,records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=? WHERE batch_id=?", (batch_status, now, records_seen, records_written, records_failed, _json(report["rejected"]), _json({**report, "accepted_rows": {key: len(value) for key, value in accepted_rows.items()}, "count_semantics": "records_seen=source rows; records_failed=rejected facts; records_written=accepted source rows by entity"}), batch_id))
        con.execute("INSERT INTO ingestion_checkpoints(adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET cursor_value=excluded.cursor_value,source_hash=excluded.source_hash,last_success_at=excluded.last_success_at,batch_id=excluded.batch_id,payload_json=excluded.payload_json", ("wechat-search","normalized",now,report["manifest_id"],now,batch_id,_json(report)))

    @staticmethod
    def _time_precision(value: Any) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if re.fullmatch(r"\d{2}[-/]\d{2}[-/]\d{2}", raw):
            return "date_2digit_year"
        if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", raw):
            return "date"
        if "T" in raw or re.search(r"\d{2}:\d{2}", raw):
            return "datetime"
        return None

    @staticmethod
    def _candidate_sort_key(fact: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            fact["observation_id"],
            str(fact.get("source_file_path") or ""),
            str(fact["numeric_value"]),
            fact["canonical_row_json"],
        )

    def _metric_context(self, con: sqlite3.Connection, records: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        ids: dict[str, str] = {}
        source_url_ids: dict[str, str] = {}
        for row in records["articles"]:
            source_id = str(row.get("article_id") or _id("content", row.get("title")))
            url = _safe_url(row.get("normalized_url") or row.get("raw_url"))
            existing = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (url,)).fetchone() if url else None
            ids[source_id] = str(existing[0]) if existing else source_url_ids.get(url, source_id)
            if url:
                source_url_ids.setdefault(url, ids[source_id])
        snapshot_map: dict[str, str] = {}
        source_snapshot_keys: dict[tuple[str, str, str], str] = {}
        for row in records["snapshots"]:
            sid = str(row.get("snapshot_id") or "")
            captured = _source_time(row.get("captured_at"))
            kid = row.get("keyword_id")
            keyword = next((x.get("keyword") for x in records["keywords"] if x.get("keyword_id") == kid), str(kid or ""))
            existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("wechat-search", keyword, captured)).fetchone() if captured else None
            mapped = con.execute("SELECT snapshot_id FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
            if existing:
                snapshot_map[sid] = str(existing[0])
            elif mapped:
                snapshot_map[sid] = str(mapped[0])
            elif sid and captured:
                snapshot_map[sid] = source_snapshot_keys.setdefault(("wechat-search", keyword, captured), sid)
            if sid and captured and (("wechat-search", keyword, captured) not in source_snapshot_keys):
                source_snapshot_keys[("wechat-search", keyword, captured)] = snapshot_map[sid]
        return ids, snapshot_map

    def _prepare_metric_facts(self, con: sqlite3.Connection, records: dict[str, Any], ids: dict[str, str], snapshot_map: dict[str, str], report: dict[str, Any], *, planned_snapshot_ids: set[str] | None = None) -> list[dict[str, Any]]:
        planned_snapshot_ids = planned_snapshot_ids or set()
        groups: dict[tuple[str, str, str, str, str | None], list[dict[str, Any]]] = {}
        labels = (("read_count", "微信阅读数"), ("like_count", "微信点赞数"), ("friends_follow_count", "微信在看数"), ("original_article_count", "微信原创数"))
        for row in records["observations"]:
            cid = ids.get(str(row.get("article_id")))
            observed = _source_time(row.get("observed_at"))
            source_oid = str(row.get("observation_id") or "")
            snapshot_id = snapshot_map.get(str(row.get("source_snapshot_id"))) if row.get("source_snapshot_id") else None
            if snapshot_id and snapshot_id not in planned_snapshot_ids and con.execute("SELECT 1 FROM search_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone() is None:
                snapshot_id = None
            for key, label in labels:
                if not cid or row.get(key) is None:
                    continue
                if observed is None:
                    report["rejected"].append({"kind": "metric", "source_observation_id": source_oid, "metric": key, "value": row.get(key), "reason": "invalid_observed_at"})
                    continue
                value = _number(row.get(key))
                if value is None:
                    report["rejected"].append({"kind": "metric", "source_observation_id": source_oid, "metric": key, "value": row.get(key), "reason": "invalid_numeric"})
                    continue
                metric_key = f"wechat.{key}"
                raw_observed_at = row.get("raw_observed_at") or row.get("observed_at")
                source_precision = row.get("observed_at_precision") or self._time_precision(raw_observed_at)
                source_origin = row.get("observed_at_source")
                source_file_path = row.get("source_file_path") or row.get("source_ref")
                canonical_row_json = _json(row)
                oid = f"{source_oid}:{metric_key}" if source_oid else _id("observation", f"{cid}:{metric_key}:{observed}:{snapshot_id}:{canonical_row_json}")
                fact = {
                    "observation_id": oid, "source_observation_id": source_oid, "subject_id": cid,
                    "metric_key": metric_key, "metric_label": label, "observed_at": observed,
                    "numeric_value": value, "snapshot_id": snapshot_id, "source_ref": source_file_path,
                    "source_file_path": source_file_path, "raw_observed_at": raw_observed_at,
                    "observed_at_precision": source_precision, "observed_at_source": source_origin,
                    "canonical_row_json": canonical_row_json,
                    "row": {**row, "source_snapshot_id": snapshot_id},
                }
                groups.setdefault(("content", cid, metric_key, observed, snapshot_id), []).append(fact)
        # A reused source observation_id must not make two different natural keys
        # fight over one PRIMARY KEY. Keep the base id where possible, and add a
        # deterministic variant only for cross-natural-key reuse.
        by_base_id: dict[str, list[tuple[tuple[str, str, str, str, str | None], dict[str, Any]]]] = {}
        for natural_key, candidates in groups.items():
            for candidate in candidates:
                by_base_id.setdefault(candidate["observation_id"], []).append((natural_key, candidate))
        for base_id, entries in by_base_id.items():
            natural_keys = {entry[0] for entry in entries}
            if len(natural_keys) > 1:
                for natural_key, candidate in entries:
                    candidate["observation_id"] = f"{base_id}:{_id('variant', _json({'natural_key': natural_key, 'candidate': candidate['canonical_row_json']}))[-20:]}"
        winners: list[dict[str, Any]] = []
        for natural_key in sorted(groups, key=lambda x: tuple("" if v is None else str(v) for v in x)):
            candidates = sorted(groups[natural_key], key=self._candidate_sort_key)
            winner = candidates[0]
            winners.append(winner)
            if len(candidates) > 1:
                same_value = len({str(x["numeric_value"]) for x in candidates}) == 1
                report["metric_collisions"].append({
                    "natural_key": {"subject_type": natural_key[0], "subject_id": natural_key[1], "metric_key": natural_key[2], "observed_at": natural_key[3], "snapshot_id": natural_key[4]},
                    "same_value": same_value,
                    "candidates": [{
                        "observation_id": x["observation_id"],
                        "numeric_value": x["numeric_value"],
                        "source_file_path": x["source_file_path"],
                        "source_ref": x["source_ref"],
                        "raw_observed_at": x["raw_observed_at"],
                        "observed_at_precision": x["observed_at_precision"],
                        "observed_at_source": x["observed_at_source"],
                        "winner": x is winner,
                    } for x in candidates],
                })
        report["metric_fact_count"] = sum(len(v) for v in groups.values())
        report["metric_unique_count"] = len(groups)
        report["metric_collision_group_count"] = len(report["metric_collisions"])
        report["metric_collision_extra_count"] = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
        report["metric_collision_same_value_count"] = sum(1 for x in report["metric_collisions"] if x["same_value"])
        report["metric_collision_value_diff_count"] = sum(1 for x in report["metric_collisions"] if not x["same_value"])
        return winners

    @staticmethod
    def _write_metric_fact(con: sqlite3.Connection, fact: dict[str, Any]) -> None:
        con.execute("INSERT OR IGNORE INTO metric_definitions(metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description) VALUES(?,?,?,?,?,?,?,?)", (fact["metric_key"], "wechat-search", "content", fact["metric_label"], "number", "count", "gauge", "旧微信 normalized 事实"))
        key = (fact["subject_id"], fact["metric_key"], fact["observed_at"], fact["snapshot_id"])
        con.execute("DELETE FROM metric_observations WHERE subject_type='content' AND subject_id=? AND metric_key=? AND observed_at=? AND COALESCE(snapshot_id,'no-snapshot')=COALESCE(?,'no-snapshot') AND observation_id<>?", (*key, fact["observation_id"]))
        existing = con.execute("SELECT subject_type,subject_id,metric_key,observed_at,snapshot_id FROM metric_observations WHERE observation_id=?", (fact["observation_id"],)).fetchone()
        if existing and tuple(existing) != ("content", *key):
            con.execute("DELETE FROM metric_observations WHERE observation_id=?", (fact["observation_id"],))
        con.execute("INSERT INTO metric_observations(observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET subject_type=excluded.subject_type,subject_id=excluded.subject_id,metric_key=excluded.metric_key,observed_at=excluded.observed_at,numeric_value=excluded.numeric_value,snapshot_id=excluded.snapshot_id,source_ref=excluded.source_ref,payload_json=excluded.payload_json", (fact["observation_id"], "content", fact["subject_id"], fact["metric_key"], fact["observed_at"], fact["numeric_value"], fact["snapshot_id"], fact["source_ref"], _json({**fact["row"], "source_observation_id": fact["source_observation_id"]})))
