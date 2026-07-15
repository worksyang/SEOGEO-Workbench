from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from content_hub.adapters.mp import MpAdapter, MpSourceError, _scrub, _source_datetime, _trusted_url
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError


def _json(value: Any) -> str:
    return json.dumps(_scrub(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _compact(value: Any, *, depth: int = 0) -> Any:
    value = _scrub(value)
    if depth > 3:
        return "[TRUNCATED]"
    if isinstance(value, list):
        return [_compact(v, depth=depth + 1) for v in value[:100]]
    if isinstance(value, dict):
        return {k: _compact(v, depth=depth + 1) for k, v in list(value.items())[:100]}
    return value


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        value = payload.get("data")
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            return _rows(value, *keys)
    return []


def _payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("data")
    return value if isinstance(value, dict) else payload


def _auth_state(payload: Any) -> tuple[bool | None, bool]:
    data = _payload_data(payload)
    logged_in = data.get("logged_in")
    if logged_in is None:
        logged_in = data.get("authenticated")
    status = data.get("wechat_status") if isinstance(data.get("wechat_status"), dict) else {}
    if logged_in is None:
        logged_in = status.get("logged_in")
    inconsistent = status.get("inconsistent") is True or data.get("inconsistent") is True
    return (logged_in if isinstance(logged_in, bool) else None), inconsistent


class MpService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.adapter = MpAdapter(settings)

    def _audit(self, action: str, outcome: str, *, details: dict[str, Any] | None = None, subject_id: str | None = None) -> None:
        now = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    con.execute(
                        "INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)",
                        (_id("audit", f"{action}:{now}:{subject_id}"), now, "workbench", action, "mp", subject_id, outcome, _json(details or {})),
                    )

    def _connection(self, status: str, *, error: str | None = None) -> None:
        now = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT details_json FROM system_connections WHERE system_key='wechat-mp'").fetchone()
                    details = json.loads(row[0] or "{}") if row else {}
                    if error:
                        details.update({"last_error": error, "last_error_at": now})
                    else:
                        details.pop("last_error", None)
                    con.execute(
                        """INSERT INTO system_connections(system_key,display_name,base_url,status,last_checked_at,capabilities_json,details_json)
                        VALUES('wechat-mp','公众号监控',?,?,?,?,?)
                        ON CONFLICT(system_key) DO UPDATE SET base_url=excluded.base_url,status=excluded.status,last_checked_at=excluded.last_checked_at,details_json=excluded.details_json""",
                        (self.adapter.base_url, status, now, '["read","account_flags","jobs","auth_check","markdown_import"]', _json(details)),
                    )

    def _connection_snapshot(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                "SELECT status,last_checked_at FROM system_connections WHERE system_key='wechat-mp'"
            ).fetchone()
        if row is None:
            return {
                "status": "unknown",
                "source": "live_http",
                "checked_at": None,
                "verified": False,
            }
        return {
            "status": str(row["status"] or "unknown"),
            "source": "live_http",
            "checked_at": row["last_checked_at"],
            "verified": False,
        }

    def _connection_for_import_failure(self, status: str, *, error: str) -> None:
        snapshot = self._connection_snapshot()
        if snapshot["status"] in {"blocked", "degraded", "offline"}:
            return
        self._connection(status, error=error)

    @staticmethod
    def _remote_category_names(payload: Any) -> set[str]:
        rows = _rows(payload, "categories", "items")
        names = {str(row.get("name") or row.get("category_name") or row.get("title") or "").strip() for row in rows}
        if isinstance(payload, dict) and isinstance(payload.get("categories"), list):
            names |= {str(item).strip() for item in payload["categories"] if isinstance(item, str)}
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            names |= {str(item).strip() for item in payload["data"] if isinstance(item, str)}
        return {name for name in names if name}

    def _remote(self, fn):
        try:
            response = fn()
            return response
        except MpSourceError as exc:
            self._connection("degraded" if exc.status else "offline", error=str(exc))
            raise

    def bootstrap(self) -> dict[str, Any]:
        calls = {"health": self.adapter.health, "runtime": self.adapter.runtime_overview, "accounts": self.adapter.accounts, "categories": self.adapter.categories_remote, "jobs": self.adapter.jobs, "auth": self.adapter.auth_check}
        live: dict[str, Any] = {}
        errors: dict[str, dict[str, Any]] = {}
        for name, fn in calls.items():
            try:
                response = fn()
                live[name] = _compact(response.payload)
            except MpSourceError as exc:
                errors[name] = {"status": exc.status, "kind": exc.kind, "message": str(exc), "payload": _compact(exc.payload)}
        runtime = _payload_data(live.get("runtime"))
        wechat_status = runtime.get("wechat_status") if isinstance(runtime.get("wechat_status"), dict) else {}
        declared_inconsistent = wechat_status.get("inconsistent") is True
        checked_at = _now()
        if not errors and not declared_inconsistent:
            source_status = {"status": "healthy", "source": "live_http", "inconsistent": False}
            self._connection("healthy")
        elif live:
            source_status = {
                "status": "degraded", "source": "live_http", "inconsistent": True,
                "errors": errors,
                "logged_in": wechat_status.get("logged_in"),
                "display_status": wechat_status.get("display_status"),
                "message": wechat_status.get("message"),
            }
            error_text = "; ".join(f"{k}: {v['message']}" for k, v in errors.items())
            self._connection("degraded", error=error_text or str(wechat_status.get("message") or "上游声明微信状态不一致"))
        else:
            source_status = {"status": "offline", "source": "live_http", "inconsistent": True, "errors": errors}
            self._connection("offline", error="公众号监控上游不可用")
        source_status["evidence"] = {
            "base_url": self.adapter.base_url,
            "checked_at": checked_at,
            "runtime_endpoint": "/api/runtime/overview",
            "auth_endpoint": "/api/auth/wechat/check",
            "read_only_root": "configured/mp-source",
            "metadata_root": "configured/mp-metadata",
        }
        if source_status["status"] != "healthy":
            source_status["operation_hint"] = "请在旧公众号监控控制台完成 WeRSS 登录并重新检查；工作台不会自动扫码或伪造成功。"
        accounts = _rows(live.get("accounts"), "accounts", "items")
        categories = _rows(live.get("categories"), "categories", "items")
        jobs = _rows(live.get("jobs"), "jobs", "items")
        with connect(self.settings, readonly=True) as con:
            imported = [dict(row) for row in con.execute("SELECT DISTINCT c.content_id,c.title,c.author_name,c.published_at,c.creator_id,c.md_path,c.content_hash,c.payload_json FROM contents c JOIN content_discoveries d ON d.content_id=c.content_id AND d.discovery_system='wechat-mp' AND d.discovery_channel='account-feed' WHERE c.content_type='external_article' ORDER BY c.published_at DESC,c.content_id LIMIT 100").fetchall()]
            for row in imported:
                row["payload"] = json.loads(row.pop("payload_json") or "{}")
            imported_count = con.execute("SELECT COUNT(DISTINCT c.content_id) FROM contents c JOIN content_discoveries d ON d.content_id=c.content_id AND d.discovery_system='wechat-mp' AND d.discovery_channel='account-feed' WHERE c.content_type='external_article'").fetchone()[0]
        health = live.get("health")
        if isinstance(health, dict) and "database" in health:
            health = dict(health)
            health["database"] = "configured/mp-runtime"
        return {
            "source_status": source_status,
            "health": health, "runtime": live.get("runtime"), "auth": live.get("auth"),
            "accounts": accounts[:100], "categories": categories[:100], "jobs": jobs[:100],
            "summary": {"account_count": len(accounts), "category_count": len(categories), "job_count": len(jobs), "imported_article_count": imported_count, "configured_category_count": len(self.adapter.categories)},
            "hub_articles": imported,
        }

    def articles(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            rows = [dict(row) for row in con.execute("SELECT DISTINCT c.* FROM contents c JOIN content_discoveries d ON d.content_id=c.content_id AND d.discovery_system='wechat-mp' AND d.discovery_channel='account-feed' WHERE c.content_type='external_article' ORDER BY COALESCE(c.published_at,'' ) DESC,c.content_id").fetchall()]
            for row in rows:
                row["payload"] = json.loads(row.pop("payload_json") or "{}")
            return {"source_status": {"status": "healthy", "source": "hub_db"}, "articles": rows, "count": len(rows)}

    def article(self, content_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT DISTINCT c.* FROM contents c JOIN content_discoveries d ON d.content_id=c.content_id AND d.discovery_system='wechat-mp' AND d.discovery_channel='account-feed' WHERE c.content_id=? AND c.content_type='external_article'", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("公众号文章", content_id)
            article = dict(row)
            article["payload"] = json.loads(article.pop("payload_json") or "{}")
            discovery = [dict(item) for item in con.execute("SELECT * FROM content_discoveries WHERE content_id=? AND discovery_system='wechat-mp' ORDER BY discovered_at", (content_id,)).fetchall()]
            return {"source_status": {"status": "healthy", "source": "hub_db"}, "article": article, "discoveries": discovery}

    def import_history(self, *, dry_run: bool, limit: int | None) -> dict[str, Any]:
        try:
            records, manifest = self.adapter.scan(limit=limit)
        except MpSourceError as exc:
            if not dry_run:
                self._connection_for_import_failure("offline", error=str(exc))
            raise ConflictError(f"{exc.kind}: {exc}") from exc
        manifest_id = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        batch_id = _id("batch", f"wechat-mp:{manifest_id}:{limit if limit is not None else 'full'}")
        csv_rows = list(manifest.get("metadata_rows") or [])
        csv_records = [self._csv_record(row) for row in csv_rows]
        all_records = records + csv_records
        accounts_result: dict[str, Any] = {}
        try:
            accounts_result = self.adapter.accounts().payload
        except MpSourceError:
            pass
        live_accounts = _rows(accounts_result, "accounts", "items")
        rejected = list(manifest.get("rejected") or [])
        rejected.extend({"source": item.get("path"), "reason": item.get("reason")} for item in manifest.get("skipped", []))
        rejected_articles = manifest.get("rejected_articles") or {}
        if rejected_articles.get("rows"):
            rejected.append({
                "source": rejected_articles.get("path"),
                "reason": "rejected_articles",
                "rows": rejected_articles.get("rows"),
            })
        rejected_count = sum(int(item.get("rows") or 1) for item in rejected)
        report = {"manifest_id": manifest_id, "manifest": manifest, "rejected": rejected, "rejected_articles": manifest.get("rejected_articles", {}), "reconcile": manifest.get("reconcile", {})}
        counts = {"articles": len(all_records), "markdown_articles": len(records), "csv_only": len(csv_records), "creators": len(live_accounts), "accepted": 0, "rejected": rejected_count, "processed": len(all_records) + rejected_count}
        history_import = {
            "status": "dry_run" if dry_run else "pending",
            "source": "markdown",
            "manifest_id": manifest_id,
            "batch_id": batch_id,
        }
        source_status = self._connection_snapshot()
        if dry_run:
            report["accepted"] = 0
            report["processed"] = counts["processed"]
            history_import["status"] = "dry_run"
            return {
                "dry_run": True,
                "source": "markdown",
                "batch_id": batch_id,
                "counts": counts,
                "source_status": source_status,
                "history_import": history_import,
                "audit": report,
            }
        now = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    self._write(con, all_records, live_accounts, batch_id, report, now)
        counts["accepted"] = int(report.get("accepted", 0))
        history_import["status"] = "partial_failed" if report["rejected"] else "succeeded"
        history_import["accepted"] = counts["accepted"]
        history_import["rejected"] = counts["rejected"]
        self._audit("wechat-mp.import", "failed" if report["rejected"] else "succeeded", details={"batch_id": batch_id, "counts": counts, "audit": report})
        return {
            "dry_run": False,
            "source": "markdown",
            "batch_id": batch_id,
            "counts": counts,
            "source_status": source_status,
            "history_import": history_import,
            "audit": report,
        }

    @staticmethod
    def _csv_record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "relative_path": None,
            "absolute_path": None,
            "category": None,
            "title": row.get("title") or None,
            "author": row.get("mp_name") or None,
            "mp_name": row.get("mp_name") or None,
            "mp_id": row.get("mp_id") or None,
            "published_at": _source_datetime(row.get("publish_time") or row.get("publish_date", "")),
            "ingested_date_hint": None,
            "file_hash": None,
            "content_hash": None,
            "mtime_ns": None,
            "file_mtime_at": row.get("_source_mtime_at"),
            "source_updated_at": row.get("_source_mtime_at"),
            "size": None,
            "integrity_warnings": [],
            "canonical_url": _trusted_url(row.get("url", "")),
            "metadata_source": {"path": row.get("_source_path"), "row": row.get("_source_row")},
            "metadata_match_status": "csv_only",
            "metadata_match_method": None,
            "metadata_match_confidence": 1.0,
            "_metadata_identity": row.get("_identity"),
        }

    @staticmethod
    def _repoint_mp_identity(
        con: sqlite3.Connection,
        source_content_id: str,
        target_content_id: str,
        now: str,
        *,
        canonical_url: str,
    ) -> None:
        """把后补 URL 识别出的公众号占位身份安全归并到既有跨系统内容。"""
        if source_content_id == target_content_id:
            return
        con.execute(
            "UPDATE content_identifiers SET content_id=? WHERE content_id=? AND namespace LIKE 'mp_%'",
            (target_content_id, source_content_id),
        )
        con.execute(
            "DELETE FROM content_discoveries WHERE content_id=? AND discovery_system='wechat-mp' AND discovery_channel='account-feed'",
            (source_content_id,),
        )
        reference_checks = (
            ("content_identifiers", "content_id"),
            ("content_discoveries", "content_id"),
            ("search_hits", "content_id"),
            ("comments", "content_id"),
            ("geo_answers", "content_id"),
            ("geo_source_relations", "source_content_id"),
            ("production_jobs", "output_content_id"),
            ("identity_merge_candidates", "left_content_id"),
            ("identity_merge_candidates", "right_content_id"),
            ("identity_merge_map", "source_content_id"),
            ("identity_merge_map", "target_content_id"),
        )
        has_relational_reference = any(
            con.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (source_content_id,)).fetchone()
            for table, column in reference_checks
        )
        has_subject_reference = any(
            con.execute(
                f"SELECT 1 FROM {table} WHERE subject_type='content' AND subject_id=? LIMIT 1",
                (source_content_id,),
            ).fetchone()
            for table in ("metric_observations", "signals")
        )
        if not has_relational_reference and not has_subject_reference:
            con.execute("DELETE FROM contents WHERE content_id=?", (source_content_id,))
            return
        con.execute(
            """INSERT INTO identity_merge_map(
                source_content_id,target_content_id,merged_at,merged_by,reason_json
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(source_content_id) DO UPDATE SET
                target_content_id=excluded.target_content_id,
                merged_at=excluded.merged_at,
                merged_by=excluded.merged_by,
                reason_json=excluded.reason_json,
                reverted_at=NULL,
                reverted_by=NULL""",
            (
                source_content_id,
                target_content_id,
                now,
                "wechat-mp-adapter",
                _json({"reason": "canonical_url_convergence", "canonical_url": canonical_url}),
            ),
        )

    def _write(self, con: sqlite3.Connection, records: list[dict[str, Any]], live_accounts: list[dict[str, Any]], batch_id: str, report: dict[str, Any], now: str) -> None:
        con.execute("INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status='running',started_at=excluded.started_at,payload_json=excluded.payload_json", (batch_id, "wechat-mp", "markdown", "running", now, str(self.adapter.root), _json(report)))
        account_by_name: dict[str, dict[str, Any]] = {}
        account_by_id: dict[str, dict[str, Any]] = {}
        stable_source_at = min((str(row.get("source_updated_at") or "") for row in records if row.get("source_updated_at")), default=now)
        for item in live_accounts:
            name = str(item.get("mp_name") or item.get("name") or item.get("canonical_name") or "").strip()
            if name:
                account_by_name[name] = item
                mp_id = str(item.get("mp_id") or item.get("id") or item.get("account_id") or name)
                account_by_id[mp_id] = item
                creator_id = _id("creator", f"wechat-mp:{mp_id}")
                con.execute("INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (creator_id, name, "wechat-mp", mp_id, stable_source_at, stable_source_at, _json({"mp_id": mp_id, "mp_name": name})))
        accepted = 0
        for record in records:
            path = record["relative_path"]
            csv_identity = record.get("_metadata_identity")
            row = None
            if path:
                row = con.execute("SELECT content_id FROM content_identifiers WHERE namespace='mp_markdown_path' AND external_id=?", (path,)).fetchone()
            if row is None and csv_identity:
                row = con.execute("SELECT content_id FROM content_identifiers WHERE namespace='mp_csv_identity' AND external_id=?", (csv_identity,)).fetchone()
            cid = str(row[0]) if row else _id("content", f"{'mp_markdown_path:' + path + ':' + str(record.get('file_hash')) if path else 'mp_csv_identity:' + str(csv_identity)}")
            author = record.get("author") or record.get("mp_name")
            source_at = str(record.get("source_updated_at") or stable_source_at)
            record_mp_id = str(record.get("mp_id") or "")
            account = account_by_id.get(record_mp_id) or account_by_name.get(author or "")
            warning = list(record.get("integrity_warnings") or [])
            if account:
                mp_id = str(account.get("mp_id") or account.get("id") or account.get("account_id") or author)
                creator_id = _id("creator", f"wechat-mp:{mp_id}")
            elif record_mp_id:
                creator_id = _id("creator", f"wechat-mp:{record_mp_id}")
                con.execute("INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (creator_id, author, "wechat-mp", record_mp_id, source_at, source_at, _json({"mp_id": record_mp_id, "evidence": "metadata_csv"})))
            elif author:
                creator_id = _id("creator", f"wechat-mp:name:{author}")
                warning.append("unresolved_creator")
                con.execute("INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (creator_id, author, "wechat-mp", f"name:{author}", source_at, source_at, _json({"unresolved": True, "name": author})))
            else:
                creator_id = None
            trusted_url = record.get("canonical_url") if str(record.get("canonical_url") or "").startswith("https://mp.weixin.qq.com/") else None
            if trusted_url:
                existing = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (trusted_url,)).fetchone()
                if existing:
                    target_cid = str(existing[0])
                    if row is not None and cid != target_cid:
                        self._repoint_mp_identity(
                            con,
                            cid,
                            target_cid,
                            now,
                            canonical_url=trusted_url,
                        )
                    cid = target_cid
            payload = {"source": "wechat-mp", "relative_path": path, "absolute_path": record.get("absolute_path"), "mtime_ns": record.get("mtime_ns"), "category": record.get("category"), "mp_id": record.get("mp_id"), "metadata_source": record.get("metadata_source"), "metadata_match_status": record.get("metadata_match_status"), "metadata_match_method": record.get("metadata_match_method"), "metadata_match_confidence": record.get("metadata_match_confidence"), "ingested_date_hint": record.get("ingested_date_hint"), "integrity_warnings": warning}
            con.execute("INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,author_name,published_at,first_seen_at,updated_at,md_path,file_hash,content_hash,domain,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_id) DO UPDATE SET content_type='external_article',title=excluded.title,canonical_url=excluded.canonical_url,creator_id=excluded.creator_id,author_name=excluded.author_name,published_at=excluded.published_at,updated_at=excluded.updated_at,md_path=excluded.md_path,file_hash=excluded.file_hash,content_hash=excluded.content_hash,payload_json=excluded.payload_json", (cid, "external_article", record.get("title"), trusted_url, creator_id, author, record.get("published_at"), source_at, source_at, path, record["file_hash"], record["content_hash"], "mp.weixin.qq.com" if trusted_url else None, _json(payload)))
            if path:
                con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO UPDATE SET content_id=excluded.content_id,payload_json=excluded.payload_json", ("mp_markdown_path", path, cid, now, _json({"file_hash": record.get("file_hash")})))
            if csv_identity:
                con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO UPDATE SET content_id=excluded.content_id,payload_json=excluded.payload_json", ("mp_csv_identity", str(csv_identity), cid, now, _json({"metadata_source": record.get("metadata_source")})))
            if trusted_url:
                con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO UPDATE SET content_id=excluded.content_id,payload_json=excluded.payload_json", ("mp_csv_url", trusted_url, cid, now, _json({"metadata_source": record.get("metadata_source")})))
            discovery_id = _id("discovery", f"wechat-mp:account-feed:{cid}")
            con.execute("INSERT INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(discovery_id) DO UPDATE SET payload_json=excluded.payload_json", (discovery_id, cid, "wechat-mp", "account-feed", source_at, path or record.get("metadata_source", {}).get("path"), _json({"category": record.get("category"), "author": author, "metadata_match_status": record.get("metadata_match_status")})))
            accepted += 1
        report["accepted"] = accepted
        finished_at = _now()
        rejected_count = sum(int(item.get("rows") or 1) for item in report["rejected"])
        con.execute("UPDATE ingestion_batches SET status=?,finished_at=?,records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=? WHERE batch_id=?", ("succeeded" if not report["rejected"] else "partial_failed", finished_at, len(records) + rejected_count, accepted, rejected_count, _json(report["rejected"]), _json(report), batch_id))
        con.execute("INSERT INTO ingestion_checkpoints(adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET cursor_value=excluded.cursor_value,source_hash=excluded.source_hash,last_success_at=excluded.last_success_at,batch_id=excluded.batch_id,payload_json=excluded.payload_json", ("wechat-mp", "markdown", finished_at, report["manifest_id"], finished_at, batch_id, _json(report)))

    def accounts(self): return self._remote(self.adapter.accounts)
    def categories(self): return self._remote(self.adapter.categories_remote)
    def jobs(self): return self._remote(self.adapter.jobs)
    def job(self, job_id: str): return self._remote(lambda: self.adapter.job(job_id))
    def auth_check(self):
        response = self._remote(self.adapter.auth_check)
        logged_in, inconsistent = _auth_state(response.payload)
        if inconsistent or logged_in is False:
            status = "degraded" if inconsistent else "blocked"
            self._connection(status, error="WeRSS 登录状态不可用或与运行时不一致")
            payload = _scrub(response.payload)
            payload["source_status"] = {
                "status": status,
                "source": "live_http",
                "logged_in": logged_in,
                "inconsistent": inconsistent,
                "operation_hint": "请在旧公众号监控控制台重新完成 WeRSS 登录后再执行采集。",
                "evidence": {
                    "base_url": self.adapter.base_url,
                    "endpoint": "/api/auth/wechat/check",
                    "checked_at": _now(),
                },
            }
            return type(response)(payload, response.status)
        self._connection("healthy")
        return response
    def auth_qrcode(self):
        response = self._remote(self.adapter.auth_qrcode)
        payload = response.payload
        image_url = str(payload.get("image_url") or "")
        parsed = urllib.parse.urlparse(image_url)
        parts = [part for part in parsed.path.split("/") if part]
        qr_id = parts[-1] if parts and len(parts) >= 1 and parts[-2:] == ["image", parts[-1]] else None
        result = {
            "already_logged_in": payload.get("already_logged_in"),
            "auth_status": payload.get("auth_status"),
            "qr_id": qr_id,
            "image_url": f"/api/v1/mp/auth/qrcode/image/{urllib.parse.quote(qr_id, safe='')}" if qr_id else None,
        }
        return type(response)(result, response.status)
    def auth_qrcode_finish(self, payload: dict[str, Any]):
        return self._remote(self.adapter.auth_qrcode_finish)
    def auth_qrcode_image(self, qr_id: str):
        try:
            return self.adapter.auth_qrcode_image(qr_id)
        except MpSourceError as exc:
            self._connection("degraded" if exc.status else "offline", error=str(exc))
            raise

    def update_flags(self, mp_id: str, payload: dict[str, Any], confirm: bool):
        if confirm is not True:
            self._audit("wechat-mp.account_flags", "blocked", subject_id=mp_id, details={"reason": "confirm_required"})
            raise ValidationAppError("账号 flags 更新必须明确传入 confirm=true。")
        clean = dict(payload)
        clean.pop("confirm", None)
        allowed = {"monitor_enabled", "run_enabled", "category_name"}
        if not clean or set(clean) - allowed:
            raise ValidationAppError("flags 只允许 monitor_enabled、run_enabled、category_name，且不能是空更新。")
        for key in ("monitor_enabled", "run_enabled"):
            if key in clean and not isinstance(clean[key], bool):
                raise ValidationAppError(f"{key} 必须是布尔值。")
        if "category_name" in clean:
            try:
                remote_categories = self._remote(self.adapter.categories_remote)
            except MpSourceError as exc:
                raise ValidationAppError("无法读取上游账号分类，已安全拒绝 flags 更新。") from exc
            if clean["category_name"] not in self._remote_category_names(remote_categories.payload):
                raise ValidationAppError("category_name 不是上游当前返回的账号分类。")
        return self._remote(lambda: self.adapter.update_flags(mp_id, _scrub(clean)))

    def create_job(self, payload: dict[str, Any], confirm: bool):
        if confirm is not True:
            self._audit("wechat-mp.job_create", "blocked", details={"reason": "confirm_required"})
            raise ValidationAppError("创建公众号任务必须明确传入 confirm=true。")
        clean = dict(payload)
        clean.pop("confirm", None)
        try:
            auth = self.adapter.auth_check()
            runtime = self.adapter.runtime_overview()
        except MpSourceError as exc:
            self._audit("wechat-mp.job_create", "blocked", details={"reason": exc.kind, "status": exc.status})
            self._connection("degraded" if exc.status else "offline", error=str(exc))
            raise ConflictError("旧公众号监控上游当前不可用，任务未创建；请先恢复 WeRSS 登录并重新检查。") from exc
        logged_in, auth_inconsistent = _auth_state(auth.payload)
        runtime_data = _payload_data(runtime.payload)
        runtime_status = runtime_data.get("wechat_status") if isinstance(runtime_data.get("wechat_status"), dict) else {}
        if auth_inconsistent or runtime_status.get("inconsistent") is True or logged_in is False or runtime_status.get("logged_in") is False:
            self._audit("wechat-mp.job_create", "blocked", details={"reason": "login_inconsistent"})
            self._connection("degraded", error="WeRSS 登录状态不可用或与运行时不一致")
            raise ConflictError("WeRSS 登录状态不可用或与运行时不一致，任务未创建；请在旧控制台完成登录后重试。")
        return self._remote(lambda: self.adapter.create_job(_scrub(clean)))

    def cancel_job(self, job_id: str, confirm: bool):
        if confirm is not True:
            self._audit("wechat-mp.job_cancel", "blocked", subject_id=job_id, details={"reason": "confirm_required"})
            raise ValidationAppError("取消公众号任务必须明确传入 confirm=true。")
        return self._remote(lambda: self.adapter.cancel_job(job_id))
