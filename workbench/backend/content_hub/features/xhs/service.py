from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from content_hub.adapters.xhs import XhsAdapter, XhsSourceError, scrub
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.services.dual_write import DualWriteReceiptService
from content_hub.services.migration import MigrationResolver
from content_hub.services.search_runtime import SearchRefreshRuntime
from content_hub.ingestion.source_manifests import manifest_id_for, manifest_ref, write_manifest


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(str(value).encode()).hexdigest()[:24]}"


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _active(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "active"}


def _trusted_url(value: Any) -> str | None:
    raw = _clean_url(value)
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in {"www.xiaohongshu.com", "xhslink.com"}:
        return None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    secret_query = {"xsec_token", "xsec_source", "access_token", "token", "api_key", "apikey", "secret"}
    query = urlencode(
        sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in secret_query),
        doseq=True,
    )
    return urlunsplit((parsed.scheme.lower(), (parsed.hostname or "").lower(), path, query, ""))


def _clean_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        secret_query = {"xsec_token", "xsec_source", "access_token", "token", "api_key", "apikey", "secret"}
        query = urlencode(
            sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in secret_query),
            doseq=True,
        )
        return urlunsplit((parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path or "/", query, ""))
    except ValueError:
        return raw


def _scrub_payload(value: Any) -> Any:
    value = scrub(value)
    if isinstance(value, dict):
        return {str(key): _scrub_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_payload(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return _clean_url(value)
    return value


def _scoped(prefix: str, raw: Any) -> str:
    return _id(prefix, str(raw))


class XhsService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.adapter = XhsAdapter(settings)

    def _connection(self, status: str, *, error: str | None = None) -> None:
        checked = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT details_json FROM system_connections WHERE system_key='xhs-search'").fetchone()
                    details = json.loads(row[0] or "{}") if row else {}
                    if error:
                        details["last_error"] = error
                    else:
                        details.pop("last_error", None)
                        details["last_success_at"] = checked
                    con.execute(
                        """INSERT INTO system_connections(system_key,display_name,base_url,status,last_checked_at,capabilities_json,details_json)
                        VALUES('xhs-search','小红书',?,?,?,?,?)
                        ON CONFLICT(system_key) DO UPDATE SET base_url=excluded.base_url,status=excluded.status,
                        last_checked_at=excluded.last_checked_at,
                        capabilities_json=excluded.capabilities_json,
                        details_json=excluded.details_json""",
                        (
                            self.adapter.base_url,
                            status,
                            checked,
                            '["read","history_import","keyword_refresh_dry_run"]',
                            _json(details),
                        ),
                    )

    def _audit(self, action: str, outcome: str, details: dict[str, Any], subject_id: str | None = None) -> None:
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    occurred = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    con.execute(
                        "INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)",
                        (_id("audit", f"{action}:{subject_id}:{occurred}"), occurred, "system", action, "xiaohongshu", subject_id, outcome, _json(details)),
                    )

    def _records(self, *, persist_error: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self.adapter.read_records()
        except XhsSourceError as exc:
            if persist_error:
                self._connection("offline", error=f"{exc.kind}: {exc}")
                self._audit("xhs.import", "failed", {"kind": exc.kind, "error": str(exc)})
            raise ConflictError(f"{exc.kind}: {exc}") from exc

    @staticmethod
    def _counts(records: dict[str, Any]) -> dict[str, int]:
        return {key: len(value) for key, value in records.items() if isinstance(value, list) and not key.startswith("_")}

    def import_history(self, *, dry_run: bool) -> dict[str, Any]:
        migration = MigrationResolver(self.settings, module_key="xhs-search", contract_key="history-import").describe()
        records, manifest = self._records(persist_error=not dry_run)
        manifest_id = self.adapter.manifest_id(manifest)
        batch_id = _id("batch", f"xhs:{manifest_id}:full")
        report: dict[str, Any] = {"manifest": manifest, "manifest_id": manifest_id, "rejected": [], "warnings": [], "metric_collisions": []}
        if dry_run:
            return {"dry_run": True, "source": "legacy_normalized", "counts": self._counts(records), "batch_id": batch_id, "audit": report, "migration": migration}
        now = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    self._write(con, records, manifest, batch_id, report, now)
        self._connection("degraded" if report["rejected"] else "healthy", error=f"{len(report['rejected'])} rows rejected" if report["rejected"] else None)
        self._audit("xhs.import", "failed" if report["rejected"] else "succeeded", {"batch_id": batch_id, "counts": self._counts(records), "audit": report})
        return {"dry_run": False, "source": "legacy_normalized", "counts": self._counts(records), "batch_id": batch_id, "audit": report, "migration": migration}

    def _write(self, con: sqlite3.Connection, records: dict[str, Any], manifest: dict[str, Any], batch_id: str, report: dict[str, Any], now: str) -> None:
        entries = [
            {"relative_path": item.get("path"), "content_hash": item.get("sha256"), "size_bytes": item.get("size")}
            for item in manifest.values() if isinstance(item, dict) and item.get("path")
        ]
        manifest_id = manifest_id_for("xiaohongshu", {"source_kind": "normalized"}, entries)
        write_manifest(
            con,
            manifest_id=manifest_id,
            system_key="xiaohongshu",
            source_kind="normalized",
            root_fingerprint=hashlib.sha256(f"xiaohongshu:{self.adapter.root.name}".encode()).hexdigest(),
            entries=entries,
            captured_at=now,
            payload={"source_root": "configured/xiaohongshu-normalized", "batch_id": batch_id},
        )
        report["manifest_id"] = manifest_id
        con.execute(
            "INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status='running',started_at=excluded.started_at,source_ref=excluded.source_ref",
            (batch_id, "xiaohongshu", "history", "running", now, manifest_ref("xiaohongshu", manifest_id), _json(report)),
        )
        settings_records = records.get("_settings", [])
        settings_rows = {str(row.get("keyword_id")): row for row in settings_records}
        if not settings_rows and records.get("keywords"):
            report["warnings"].append({
                "kind": "settings_db_empty",
                "message": "小红书 settings DB 为空，关键词 active 状态遵循 normalized.is_active",
            })
        keyword_map: dict[str, str] = {}
        accepted_keywords: dict[str, dict[str, Any]] = {}
        account_map: dict[str, str] = {}
        accepted_accounts: dict[str, dict[str, Any]] = {}
        snapshots = {str(r["snapshot_id"]): r for r in records["snapshots"]}
        snapshot_map: dict[str, str] = {}
        content_map: dict[str, str] = {}
        for row in records["keywords"]:
            kid, keyword = str(row.get("keyword_id")), str(row.get("keyword_text") or "").strip()
            if not keyword:
                report["rejected"].append({"kind": "keyword", "id": kid, "reason": "missing_keyword"})
                continue
            scoped_id = _scoped("xhs_keyword", kid)
            occupied = con.execute("SELECT platform,payload_json FROM keywords WHERE keyword_id=?", (scoped_id,)).fetchone()
            if occupied and (
                occupied["platform"] != "xiaohongshu"
                or json.loads(occupied["payload_json"] or "{}").get("source_keyword_id") not in {None, kid}
            ):
                report["rejected"].append({"kind": "keyword", "id": kid, "reason": "scoped_id_conflict", "occupied_platform": occupied["platform"]})
                continue
            setting = settings_rows.get(kid) or settings_rows.get(str(row.get("keyword_id")))
            topic = setting.get("topic") if setting else None
            bucket = setting.get("keyword_bucket") if setting else None
            is_active = _active(setting["is_active"]) if setting and "is_active" in setting else _active(row.get("is_active"), default=False)
            payload = _scrub_payload({**row, "source_keyword_id": kid, "settings": setting or {}})
            con.execute(
                """INSERT INTO keywords(keyword_id,platform,keyword,status,topic,keyword_bucket,first_seen_at,updated_at,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id) DO UPDATE SET keyword=excluded.keyword,status=excluded.status,
                topic=excluded.topic,keyword_bucket=excluded.keyword_bucket,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                (scoped_id, "xiaohongshu", keyword, "active" if is_active else "paused", topic, bucket, _time(row.get("first_seen_at")) or now, _time(row.get("last_seen_at")) or now, _json(payload)),
            )
            keyword_map[kid] = scoped_id
            accepted_keywords[kid] = row
        for row in records["accounts"]:
            aid = str(row.get("account_id"))
            if not aid.strip():
                report["rejected"].append({"kind": "account", "id": aid, "reason": "missing_account_id"})
                continue
            payload = _scrub_payload({**row, "profile": row.get("platform_payload") or {}, "red_id": (row.get("platform_payload") or {}).get("red_id")})
            existing_creator = con.execute(
                "SELECT creator_id FROM creators WHERE platform=? AND external_id=?",
                ("xiaohongshu", aid),
            ).fetchone()
            creator_id = str(existing_creator[0]) if existing_creator else _scoped("xhs_creator", aid)
            occupied_creator = con.execute("SELECT platform,external_id FROM creators WHERE creator_id=?", (creator_id,)).fetchone()
            if occupied_creator and (occupied_creator["platform"], occupied_creator["external_id"]) != ("xiaohongshu", aid):
                report["rejected"].append({"kind": "account", "id": aid, "reason": "scoped_id_conflict"})
                continue
            existing_creator_row = con.execute("SELECT first_seen_at,updated_at FROM creators WHERE creator_id=?", (creator_id,)).fetchone()
            con.execute(
                """INSERT INTO creators(creator_id,canonical_name,platform,external_id,profile_url,first_seen_at,updated_at,payload_json)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,
                updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                (creator_id, row.get("canonical_name"), "xiaohongshu", aid, None,
                 _time(row.get("first_seen_at")) or (existing_creator_row["first_seen_at"] if existing_creator_row else None) or now,
                 _time(row.get("last_seen_at")) or (existing_creator_row["updated_at"] if existing_creator_row else None) or now, _json(payload)),
            )
            account_map[aid] = creator_id
            accepted_accounts[aid] = row
            for field in ("fans", "total_works", "likes", "collects", "follows"):
                value = _number(row.get(field))
                observed = _time(row.get("last_seen_at"))
                if value is not None and observed:
                    self._metric(con, "creator", creator_id, f"xhs.creator.{field}", f"小红书{field}", observed, value, None, row, report, manifest_ref("xiaohongshu", report["manifest_id"], "accounts.json"))
        for row in records["articles"]:
            aid = str(row.get("article_id"))
            if not aid.startswith("xhs_tk_"):
                report["rejected"].append({"kind": "article", "id": aid, "reason": "invalid_article_id"})
                continue
            identifier_values = (("xiaohongshu_article", aid), ("xiaohongshu_note", aid.removeprefix("xhs_tk_")))
            owners = set()
            for namespace, external in identifier_values:
                owner = con.execute("SELECT content_id FROM content_identifiers WHERE namespace=? AND external_id=?", (namespace, external)).fetchone()
                if owner:
                    owners.add(str(owner["content_id"]))
            url = _trusted_url(row.get("normalized_url") or row.get("raw_url"))
            if url:
                owner = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (url,)).fetchone()
                if owner:
                    owners.add(str(owner["content_id"]))
            if len(owners) > 1:
                report["rejected"].append({"kind": "article", "id": aid, "reason": "multiple_identity_owners", "owners": sorted(owners)})
                continue
            cid = next(iter(owners), aid)
            existing_cid_row = con.execute("SELECT content_id,content_type FROM contents WHERE content_id=?", (cid,)).fetchone()
            if existing_cid_row and not owners and existing_cid_row["content_type"] != "social_note":
                report["rejected"].append({"kind": "article", "id": aid, "reason": "content_identity_conflict", "owner": cid})
                continue
            if existing_cid_row is None and cid != aid:
                report["rejected"].append({"kind": "article", "id": aid, "reason": "invalid_identity_owner", "owner": cid})
                continue
            payload = _scrub_payload({**row, "source_article_id": aid, "is_relevant": None, "relevance_score": None})
            if row.get("raw_url") and not url:
                payload["raw_url"] = _clean_url(row.get("raw_url"))
                payload["untrusted_url"] = True
            creator_id = None
            creator_row = None
            if str(row.get("account_id")) in accepted_accounts:
                creator_id = account_map.get(str(row.get("account_id")))
                creator_row = con.execute("SELECT creator_id,canonical_name FROM creators WHERE creator_id=?", (creator_id,)).fetchone()
            author_name = creator_row["canonical_name"] if creator_id and creator_row else None
            existing_content = con.execute("SELECT first_seen_at,updated_at FROM contents WHERE content_id=?", (cid,)).fetchone()
            con.execute(
                """INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,author_name,published_at,first_seen_at,updated_at,md_path,domain,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_id) DO UPDATE SET title=excluded.title,
                canonical_url=excluded.canonical_url,creator_id=excluded.creator_id,published_at=excluded.published_at,
                author_name=excluded.author_name,md_path=excluded.md_path,domain=excluded.domain,
                updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                (cid, "social_note", row.get("title"), url, creator_id, author_name,
                 _time(row.get("published_at")),
                 _time(row.get("first_seen_at")) or (existing_content["first_seen_at"] if existing_content else None) or now,
                 _time(row.get("last_seen_at")) or (existing_content["updated_at"] if existing_content else None) or now,
                 row.get("content_file_path"), (urlsplit(url).hostname if url else None), _json(payload)),
            )
            content_map[aid] = cid
            for namespace, external in (("xiaohongshu_article", aid), ("xiaohongshu_note", aid.removeprefix("xhs_tk_"))):
                existing = con.execute("SELECT content_id FROM content_identifiers WHERE namespace=? AND external_id=?", (namespace, external)).fetchone()
                if existing and str(existing[0]) != cid:
                    report["rejected"].append({"kind": "identifier", "namespace": namespace, "external_id": external, "reason": "identity_conflict"})
                    continue
                con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO UPDATE SET payload_json=excluded.payload_json", (namespace, external, cid, now, _json({"article_id": aid})))
        terms_pending: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in records.get("snapshot_terms", []):
            source_sid = str(row.get("snapshot_id"))
            if source_sid not in snapshots:
                report["rejected"].append({"kind": "snapshot_term", "id": row.get("term_id"), "reason": "dangling_snapshot"})
                continue
            term_type = str(row.get("term_type") or "").lower()
            if term_type not in {"suggestion", "related"}:
                report["rejected"].append({"kind": "snapshot_term", "id": row.get("term_id"), "reason": "invalid_term_type"})
                continue
            bucket = terms_pending.setdefault(source_sid, {"suggestions": [], "related": []})
            bucket["suggestions" if term_type == "suggestion" else "related"].append(scrub(dict(row)))

        for row in records["snapshots"]:
            sid, captured = str(row.get("snapshot_id")), _time(row.get("captured_at"))
            keyword_row = accepted_keywords.get(str(row.get("keyword_id")))
            keyword = keyword_row.get("keyword_text") if keyword_row else None
            internal_keyword_id = keyword_map.get(str(row.get("keyword_id")))
            if not captured or not keyword_row or not internal_keyword_id:
                report["rejected"].append({"kind": "snapshot", "id": sid, "reason": "dangling_keyword" if not keyword_row or not internal_keyword_id else "invalid_captured_at"})
                continue
            existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("xiaohongshu", keyword, captured)).fetchone()
            actual = str(existing[0]) if existing else sid
            occupied_snapshot = con.execute("SELECT platform FROM search_snapshots WHERE snapshot_id=?", (actual,)).fetchone()
            if occupied_snapshot and occupied_snapshot["platform"] != "xiaohongshu":
                report["rejected"].append({"kind": "snapshot", "id": sid, "reason": "snapshot_id_conflict"})
                continue
            snapshot_map[sid] = actual
            con.execute(
                """INSERT INTO search_snapshots(snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET keyword=excluded.keyword,keyword_id=excluded.keyword_id,
                captured_at=excluded.captured_at,result_count=excluded.result_count,payload_json=excluded.payload_json""",
                (actual, "xiaohongshu", keyword, internal_keyword_id, captured, row.get("trigger_type"), _number(row.get("result_count")), _json({"suggestions": [], "related": []}), row.get("raw_file_path"), _json(_scrub_payload(row))),
            )
        terms_by_snapshot: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for source_sid, terms in terms_pending.items():
            if source_sid not in snapshot_map:
                report["rejected"].extend(
                    {"kind": "snapshot_term", "id": term.get("term_id"), "reason": "dangling_snapshot"}
                    for values in terms.values() for term in values
                )
                continue
            terms_by_snapshot[source_sid] = terms
            con.execute(
                "UPDATE search_snapshots SET features_json=? WHERE snapshot_id=?",
                (_json(_scrub_payload(terms)), snapshot_map[source_sid]),
            )
        for row in records["ranking_hits"]:
            sid, rank = snapshot_map.get(str(row.get("snapshot_id"))), _number(row.get("rank"))
            cid = content_map.get(str(row.get("article_id")))
            if not sid or not cid or not isinstance(rank, (int, float)) or int(rank) <= 0 or int(rank) != rank:
                report["rejected"].append({"kind": "hit", "id": row.get("hit_id"), "reason": "dangling_snapshot_or_article"})
                continue
            hit_id = str(row.get("hit_id") or _id("xhs_hit", f"{sid}:{int(rank)}"))
            existing_rank = con.execute("SELECT * FROM search_hits WHERE snapshot_id=? AND rank=?", (sid, int(rank))).fetchone()
            existing_id = con.execute("SELECT * FROM search_hits WHERE hit_id=?", (hit_id,)).fetchone()
            clean_hit_url = _clean_url(row.get("url_raw"))
            incoming_values = (cid, row.get("title_raw"), clean_hit_url, row.get("account_name_raw"))
            if existing_id and (
                str(existing_id["snapshot_id"]) != sid
                or int(existing_id["rank"]) != int(rank)
                or tuple(existing_id[key] for key in ("content_id", "title_raw", "url_raw", "creator_name_raw")) != incoming_values
            ):
                report["rejected"].append({"kind": "hit", "id": hit_id, "reason": "hit_identity_conflict"})
                continue
            if existing_rank and (
                str(existing_rank["hit_id"]) != hit_id
                or tuple(existing_rank[key] for key in ("content_id", "title_raw", "url_raw", "creator_name_raw")) != incoming_values
            ):
                report["rejected"].append({"kind": "hit", "id": hit_id, "reason": "snapshot_rank_conflict", "existing_hit_id": existing_rank["hit_id"]})
                report.setdefault("hit_conflicts", []).append({"snapshot_id": sid, "rank": int(rank), "existing_hit_id": existing_rank["hit_id"], "incoming_hit_id": hit_id})
                continue
            if existing_rank:
                continue
            con.execute(
                """INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (hit_id, sid, int(rank), cid, row.get("title_raw"), clean_hit_url, row.get("account_name_raw"), _json(_scrub_payload({**row, "url_raw": clean_hit_url}))),
            )
            if cid:
                captured = con.execute("SELECT captured_at FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0]
                con.execute(
                    "INSERT OR IGNORE INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    (_id("xhs_discovery", f"{cid}:{sid}"), cid, "xhs-search", "keyword-rank", captured, sid, clean_hit_url, _json(_scrub_payload({**row, "url_raw": clean_hit_url}))),
                )
        for row in records["note_metric_observations"]:
            cid, sid, observed = content_map.get(str(row.get("article_id"))), snapshot_map.get(str(row.get("snapshot_id"))), _time(row.get("captured_at"))
            if not cid or not sid:
                report["rejected"].append({"kind": "metric", "source_observation_id": row.get("observation_id"), "reason": "dangling_article_or_snapshot"})
                continue
            for field, label in (("liked_count", "小红书点赞"), ("collected_count", "小红书收藏"), ("comment_count", "小红书评论"), ("shared_count", "小红书分享")):
                value = _number(row.get(field))
                if not observed:
                    report["rejected"].append({"kind": "metric", "source_observation_id": row.get("observation_id"), "metric": field, "reason": "invalid_captured_at"})
                elif value is not None:
                    self._metric(con, "content", cid, f"xhs.note.{field.removesuffix('_count')}", label, observed, value, sid, row, report, manifest_ref("xiaohongshu", report["manifest_id"], "note_metric_observations.json"))
        records_seen = sum(len(v) for key, v in records.items() if isinstance(v, list) and not key.startswith("_"))
        rejected = len(report["rejected"])
        con.execute("UPDATE ingestion_batches SET status=?,finished_at=?,records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=? WHERE batch_id=?", ("partial_failed" if rejected else "succeeded", now, records_seen, max(0, records_seen - rejected), rejected, _json(report["rejected"]), _json({**report, "count_semantics": "records_seen=source rows; records_written=accepted source facts; records_failed=rejected facts"}), batch_id))
        con.execute("INSERT INTO ingestion_checkpoints(adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET cursor_value=excluded.cursor_value,source_hash=excluded.source_hash,last_success_at=excluded.last_success_at,batch_id=excluded.batch_id,payload_json=excluded.payload_json", ("xiaohongshu", "normalized", now, self.adapter.manifest_id(manifest), now, batch_id, _json(report)))

    @staticmethod
    def _metric(con: sqlite3.Connection, subject_type: str, subject_id: str, metric_key: str, label: str, observed: str, value: int | float, snapshot_id: str | None, source: dict[str, Any], report: dict[str, Any], evidence_ref: str) -> None:
        con.execute("INSERT OR IGNORE INTO metric_definitions(metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description) VALUES(?,?,?,?,?,?,?,?)", (metric_key, "xiaohongshu", subject_type, label, "number", "count", "gauge", "小红书 normalized 事实"))
        identity = (subject_type, subject_id, metric_key, observed, snapshot_id)
        source_observation_id = str(source.get("observation_id") or _id("xhs_obs", f"{identity}:{_json(source)}"))
        by_id = con.execute("SELECT subject_type,subject_id,metric_key,observed_at,snapshot_id,numeric_value FROM metric_observations WHERE observation_id=?", (source_observation_id + ":" + metric_key,)).fetchone()
        if by_id:
            existing_identity = tuple(by_id[key] for key in ("subject_type", "subject_id", "metric_key", "observed_at", "snapshot_id"))
            reason = "value_conflict" if existing_identity == identity and float(by_id["numeric_value"]) != float(value) else "observation_identity_conflict"
            if existing_identity != identity or float(by_id["numeric_value"]) != float(value):
                report["rejected"].append({"kind": "metric", "source_observation_id": source_observation_id, "metric": metric_key, "reason": reason})
                return
        existing = con.execute("SELECT observation_id,numeric_value FROM metric_observations WHERE subject_type=? AND subject_id=? AND metric_key=? AND observed_at=? AND COALESCE(snapshot_id,'no-snapshot')=COALESCE(?,'no-snapshot')", identity).fetchone()
        oid = source_observation_id + ":" + metric_key
        if existing and float(existing["numeric_value"]) != float(value):
            report["metric_collisions"].append({"identity": list(identity), "existing": existing["numeric_value"], "incoming": value, "source_observation_id": source.get("observation_id")})
            report["rejected"].append({"kind": "metric", "source_observation_id": source.get("observation_id"), "metric": metric_key, "reason": "value_conflict"})
            return
        if existing:
            return
        con.execute("INSERT INTO metric_observations(observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?)", (oid, subject_type, subject_id, metric_key, observed, value, snapshot_id, evidence_ref, _json(_scrub_payload({**source, "source_observation_id": source.get("observation_id")}))))

    def bootstrap(self, *, summary: bool = False) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="bootstrap-summary" if summary else "bootstrap")
        result, migration = resolver.read(
            request_fingerprint=f"xhs:bootstrap:{summary}",
            legacy=lambda: self._bootstrap_legacy(summary=summary),
            hub=lambda: self._bootstrap_hub(summary=summary),
        )
        result["migration"] = migration
        return result

    def _bootstrap_hub(self, *, summary: bool = False) -> dict[str, Any]:
        payload = self._hub_bootstrap()
        if payload is None:
            raise NotImplementedError("Hub 尚无小红书事实")
        if summary:
            return {
                "source_status": {"status": "healthy", "source": "hub_db"},
                "counts": payload["counts"],
                "available_fact_arrays": sorted(k for k, v in payload.items() if isinstance(v, list)),
            }
        return {"source_status": {"status": "healthy", "source": "hub_db"}, **payload}

    def _bootstrap_legacy(self, *, summary: bool = False) -> dict[str, Any]:
        try:
            response = self.adapter.bootstrap()
            if not 200 <= response.status < 300:
                raise XhsSourceError(f"小红书 bootstrap HTTP {response.status}", kind="remote_http", status=response.status, payload=response.payload)
            self._connection("healthy")
            payload = response.payload
            fact_keys = ("keywords", "accounts", "snapshots", "ranking_hits", "articles")
            if summary:
                available_counts = {
                    key: len(value)
                    for key, value in payload.items()
                    if key in fact_keys and isinstance(value, list)
                }
                source_counts = payload.get("counts")
                counts = (
                    _scrub_payload(source_counts)
                    if isinstance(source_counts, dict)
                    else available_counts
                )
                return {
                    "source_status": {"status": "healthy", "source": "legacy_http"},
                    "counts": counts,
                    "available_fact_arrays": sorted(available_counts),
                }
            if any(not isinstance(payload.get(key), list) for key in fact_keys):
                raise XhsSourceError("小红书 live bootstrap 缺少事实层数组", kind="invalid_source_payload", status=response.status, payload=payload)
            allowed = _scrub_payload({key: payload.get(key) for key in (*fact_keys, "snapshot_terms") if key in payload})
            allowed["snapshot_terms"] = allowed.get("snapshot_terms") if isinstance(allowed.get("snapshot_terms"), list) else []
            counts = payload.get("counts")
            allowed["counts"] = counts if isinstance(counts, dict) else self._hub_counts()
            return {"source_status": {"status": "healthy", "source": "legacy_http"}, **allowed}
        except XhsSourceError as exc:
            if summary:
                counts = self._hub_counts()
                if any(counts.values()):
                    self._connection("degraded", error=str(exc))
                    return {
                        "source_status": {"status": "degraded", "source": "hub_db", "error": str(exc)},
                        "counts": counts,
                        "available_fact_arrays": [],
                    }
                self._connection("offline", error=str(exc))
                raise ConflictError(f"{exc.kind}: {exc}") from exc
            fallback = self._hub_bootstrap()
            if fallback is not None:
                self._connection("degraded", error=str(exc))
                return {"source_status": {"status": "degraded", "source": "hub_db", "error": str(exc)}, **fallback}
            self._connection("offline", error=str(exc))
            raise ConflictError(f"{exc.kind}: {exc}") from exc

    def _hub_bootstrap(self) -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            keywords = []
            for row in con.execute("SELECT * FROM keywords WHERE platform='xiaohongshu' ORDER BY keyword_id"):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                keywords.append(item)
            accounts = []
            for row in con.execute("SELECT * FROM creators WHERE platform='xiaohongshu' ORDER BY creator_id"):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                accounts.append(item)
            snapshots = []
            for row in con.execute("SELECT * FROM search_snapshots WHERE platform='xiaohongshu' ORDER BY captured_at"):
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json") or "{}")
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                snapshots.append(item)
            articles = []
            for row in con.execute(
                """SELECT c.* FROM contents c
                   WHERE c.content_type='social_note'
                     AND EXISTS (SELECT 1 FROM content_identifiers i WHERE i.content_id=c.content_id AND i.namespace='xiaohongshu_article')
                   ORDER BY c.published_at"""
            ):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                articles.append(item)
            hits = []
            for row in con.execute(
                """SELECT h.* FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                   WHERE s.platform='xiaohongshu' ORDER BY s.captured_at,h.rank"""
            ):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                hits.append(item)
        if not any((keywords, accounts, snapshots, articles, hits)):
            return None
        return {
            "keywords": _scrub_payload(keywords),
            "accounts": _scrub_payload(accounts),
            "snapshots": _scrub_payload(snapshots),
            "ranking_hits": _scrub_payload(hits),
            "articles": _scrub_payload(articles),
            "snapshot_terms": [],
            "counts": {
                "keywords": len(keywords), "accounts": len(accounts), "snapshots": len(snapshots),
                "ranking_hits": len(hits), "articles": len(articles), "snapshot_terms": 0,
            },
        }

    def _hub_counts(self) -> dict[str, int]:
        """只做轻量 COUNT，不为 live bootstrap 加载历史明细。"""
        with connect(self.settings, readonly=True) as con:
            counts = {
                "keywords": con.execute("SELECT count(*) FROM keywords WHERE platform='xiaohongshu'").fetchone()[0],
                "accounts": con.execute("SELECT count(*) FROM creators WHERE platform='xiaohongshu'").fetchone()[0],
                "snapshots": con.execute("SELECT count(*) FROM search_snapshots WHERE platform='xiaohongshu'").fetchone()[0],
                "ranking_hits": con.execute(
                    "SELECT count(*) FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE s.platform='xiaohongshu'"
                ).fetchone()[0],
                "articles": con.execute(
                    """SELECT count(*) FROM contents c
                       WHERE c.content_type='social_note'
                         AND EXISTS (SELECT 1 FROM content_identifiers i
                                     WHERE i.content_id=c.content_id AND i.namespace='xiaohongshu_article')"""
                ).fetchone()[0],
                "snapshot_terms": 0,
            }
        return {key: int(value) for key, value in counts.items()}

    def keyword(self, keyword_id: str) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="keyword-detail")
        result, migration = resolver.read(
            request_fingerprint=f"xhs:keyword:{keyword_id}",
            legacy=lambda: self._keyword_legacy(keyword_id),
            hub=lambda: self._keyword_hub(keyword_id),
        )
        result["migration"] = migration
        return result

    def _keyword_hub(self, keyword_id: str) -> dict[str, Any]:
        payload = self._hub_bootstrap()
        if payload is None:
            raise NotImplementedError("Hub 尚无小红书关键词事实")
        for row in payload["keywords"]:
            if row.get("keyword_id") == keyword_id or row.get("payload", {}).get("source_keyword_id") == keyword_id:
                return self._keyword_from_hub_payload(row, payload)
        raise NotFoundError("小红书关键词", keyword_id)

    def _keyword_from_hub_payload(self, keyword: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        kid = keyword["keyword_id"]
        snapshots = [row for row in payload["snapshots"] if row.get("keyword_id") == kid]
        hits = [row for row in payload["ranking_hits"] if row.get("snapshot_id") in {x.get("snapshot_id") for x in snapshots}]
        articles = [row for row in payload["articles"] if row.get("content_id") in {x.get("content_id") for x in hits}]
        return {
            "source_status": {"status": "healthy", "source": "hub_db"},
            "keyword": keyword,
            "snapshots": snapshots,
            "hits": hits,
            "articles": articles,
            "features": (snapshots[-1].get("features") if snapshots else {}) or {"suggestions": [], "related": []},
        }

    def _keyword_legacy(self, keyword_id: str) -> dict[str, Any]:
        remote_id = keyword_id
        with connect(self.settings, readonly=True) as con:
            hub_row = con.execute("SELECT * FROM keywords WHERE keyword_id=? AND platform='xiaohongshu'", (keyword_id,)).fetchone()
            if hub_row:
                payload = json.loads(hub_row["payload_json"] or "{}")
                remote_id = str(payload.get("source_keyword_id") or keyword_id)
        try:
            response = self.adapter.keyword(remote_id)
            if not 200 <= response.status < 300:
                raise XhsSourceError(f"小红书 keyword HTTP {response.status}", kind="remote_http", status=response.status, payload=response.payload)
            self._connection("healthy")
            return {"source_status": {"status": "healthy", "source": "legacy_http"}, **_scrub_payload(response.payload)}
        except XhsSourceError as exc:
            with connect(self.settings, readonly=True) as con:
                row = con.execute("SELECT * FROM keywords WHERE keyword_id=? AND platform='xiaohongshu'", (keyword_id,)).fetchone()
                if row is None:
                    rows = con.execute("SELECT * FROM keywords WHERE platform='xiaohongshu'").fetchall()
                    row = next((item for item in rows if json.loads(item["payload_json"] or "{}").get("source_keyword_id") == keyword_id), None)
            if not row:
                raise NotFoundError("小红书关键词", keyword_id)
            keyword = dict(row)
            keyword["payload"] = json.loads(keyword.pop("payload_json") or "{}")
            internal_id = keyword["keyword_id"]
            with connect(self.settings, readonly=True) as con:
                snapshot_rows = [dict(item) for item in con.execute(
                    "SELECT * FROM search_snapshots WHERE platform='xiaohongshu' AND keyword_id=? ORDER BY captured_at",
                    (internal_id,),
                ).fetchall()]
                for item in snapshot_rows:
                    item["features"] = json.loads(item.pop("features_json") or "{}")
                    item["payload"] = json.loads(item.pop("payload_json") or "{}")
                hit_rows = [dict(item) for item in con.execute(
                    """SELECT h.* FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                       WHERE s.platform='xiaohongshu' AND s.keyword_id=? ORDER BY s.captured_at,h.rank""",
                    (internal_id,),
                ).fetchall()]
                for item in hit_rows:
                    item["payload"] = json.loads(item.pop("payload_json") or "{}")
                content_ids = {item["content_id"] for item in hit_rows if item.get("content_id")}
                article_rows = []
                if content_ids:
                    placeholders = ",".join("?" for _ in content_ids)
                    article_rows = [dict(item) for item in con.execute(
                        f"""SELECT c.* FROM contents c
                            WHERE c.content_type='social_note'
                              AND c.content_id IN ({placeholders})
                              AND EXISTS (SELECT 1 FROM content_identifiers i WHERE i.content_id=c.content_id AND i.namespace='xiaohongshu_article')""",
                        tuple(content_ids),
                    ).fetchall()]
                    for item in article_rows:
                        item["payload"] = json.loads(item.pop("payload_json") or "{}")
            latest = snapshot_rows[-1] if snapshot_rows else {}
            return {
                "source_status": {"status": "degraded", "source": "hub_db", "error": str(exc)},
                "keyword": keyword,
                "snapshots": snapshot_rows,
                "hits": hit_rows,
                "articles": article_rows,
                "features": latest.get("features") or {"suggestions": [], "related": []},
            }

    def account(self, account_id: str) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="account-detail")
        result, migration = resolver.read(
            request_fingerprint=f"xhs:account:{account_id}",
            legacy=lambda: self._account_legacy(account_id),
            hub=lambda: self._account_hub(account_id),
        )
        result["migration"] = migration
        return result

    def _account_hub(self, account_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT * FROM creators WHERE platform='xiaohongshu' AND external_id=?", (account_id,)).fetchone()
        if not row:
            raise NotFoundError("小红书账号", account_id)
        account = dict(row)
        account["payload"] = json.loads(account.pop("payload_json") or "{}")
        return {"source_status": {"status": "healthy", "source": "hub_db"}, "account": account}

    def _account_legacy(self, account_id: str) -> dict[str, Any]:
        remote_error = None
        try:
            response = self.adapter.account(account_id)
            if not 200 <= response.status < 300:
                raise XhsSourceError(f"小红书 account HTTP {response.status}", kind="remote_http", status=response.status, payload=response.payload)
            self._connection("healthy")
            return {"source_status": {"status": "healthy", "source": "legacy_http"}, "account": self._normalize_live_account(response.payload)}
        except XhsSourceError as exc:
            remote_error = str(exc)
            self._connection("degraded", error=remote_error)
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT * FROM creators WHERE platform='xiaohongshu' AND external_id=?", (account_id,)).fetchone()
            if not row:
                raise NotFoundError("小红书账号", account_id)
            account = dict(row)
            account["payload"] = json.loads(account.pop("payload_json") or "{}")
            return {"source_status": {"status": "degraded", "source": "hub_db", "error": remote_error}, "account": account}

    @staticmethod
    def _normalize_live_account(payload: dict[str, Any]) -> dict[str, Any]:
        raw = _scrub_payload(payload)
        account = dict(raw)
        name = next((str(raw[key]).strip() for key in ("name", "canonical_name", "accountNickname", "nickname") if raw.get(key) not in (None, "")), None)
        account["name"] = name
        account["canonical_name"] = name
        score = _number(next((raw[key] for key in ("score", "account_score", "accountScore") if raw.get(key) is not None), None))
        account["score"] = score
        account["raw_evidence"] = raw
        return account

    def articles(self, limit: int = 100) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="article-list")
        result, migration = resolver.read(
            request_fingerprint=f"xhs:articles:{limit}",
            legacy=lambda: self._articles_legacy(limit),
            hub=lambda: self._articles_hub(limit),
        )
        result["migration"] = migration
        return result

    def _articles_hub(self, limit: int = 100) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            rows = [dict(row) for row in con.execute(
                """SELECT c.* FROM contents c
                   WHERE c.content_type='social_note'
                     AND EXISTS (SELECT 1 FROM content_identifiers i WHERE i.content_id=c.content_id AND i.namespace='xiaohongshu_article')
                   ORDER BY c.published_at DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return {"source_status": {"status": "healthy", "source": "hub_db"}, "articles": rows}

    def _articles_legacy(self, limit: int = 100) -> dict[str, Any]:
        return self._articles_hub(limit)

    def article(self, article_id: str) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="article-detail")
        result, migration = resolver.read(
            request_fingerprint=f"xhs:article:{article_id}",
            legacy=lambda: self._article_legacy(article_id),
            hub=lambda: self._article_hub(article_id),
        )
        result["migration"] = migration
        return result

    def _article_hub(self, article_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """SELECT c.* FROM contents c
                   WHERE c.content_id=? AND c.content_type='social_note'
                     AND EXISTS (SELECT 1 FROM content_identifiers i WHERE i.content_id=c.content_id AND i.namespace='xiaohongshu_article')""",
                (article_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("小红书笔记", article_id)
            hits = [dict(x) for x in con.execute(
                """SELECT h.* FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                   WHERE h.content_id=? AND s.platform='xiaohongshu' ORDER BY s.captured_at,h.rank""",
                (article_id,),
            ).fetchall()]
            observations = [dict(x) for x in con.execute("SELECT * FROM metric_observations WHERE subject_type='content' AND subject_id=? AND metric_key LIKE 'xhs.%' ORDER BY observed_at DESC", (article_id,)).fetchall()]
        article = dict(row)
        article["payload"] = json.loads(article.pop("payload_json") or "{}")
        for item in hits + observations:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        return {"source_status": {"status": "healthy", "source": "hub_db"}, "article": article, "hits": hits, "observations": observations}

    def _article_legacy(self, article_id: str) -> dict[str, Any]:
        return self._article_hub(article_id)

    def refresh(self, keyword_id: str, confirm: bool, *, idempotency_key: str = "") -> dict[str, Any]:
        key = idempotency_key or f"xhs-refresh:{keyword_id}:{_now()}"
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="refresh")
        receipts = DualWriteReceiptService(self.settings, module_key="xhs-search")
        if confirm is not True:
            receipts.record(
                command_id=None, idempotency_key=key, legacy_status="not_attempted",
                hub_status="not_attempted", reconcile_status="blocked",
                details={"keyword_id": keyword_id, "reason_code": "confirm_required"},
            )
            raise ValidationAppError("刷新必须明确传入 confirm=true。")
        if resolver.mode() == "hub":
            details = {
                "keyword_id": keyword_id,
                "reason_code": "xhs.hub_refresh_not_implemented",
                "message": "小红书刷新尚未接入 Hub Provider，未向旧系统或 Hub 伪造成功。",
            }
            receipt = receipts.record(
                command_id=None, idempotency_key=key, legacy_status="not_attempted",
                hub_status="not_implemented", reconcile_status="blocked", details=details,
            )
            return {"http_status": 409, "source_status": {"status": "blocked", "source": "hub_policy", "mode": "hub"}, "blocked": True, **details, **receipt}
        if resolver.mode() == "compare":
            result, migration = resolver.compare(
                request_fingerprint=f"xhs:refresh:{keyword_id}:{key}",
                legacy=lambda: self._refresh_legacy(keyword_id, True, idempotency_key=key),
                hub=lambda: (_ for _ in ()).throw(NotImplementedError("小红书刷新 Hub Provider 尚未实现")),
            )
            result["migration"] = migration
            result["dual_write_receipt"] = receipts.record(
                command_id=result.get("command_id"), idempotency_key=key,
                legacy_status="blocked", hub_status="not_implemented",
                reconcile_status="blocked", details={"keyword_id": keyword_id, "migration": migration},
            )
            return result
        result = self._refresh_legacy(keyword_id, True, idempotency_key=key)
        result["dual_write_receipt"] = receipts.record(
            command_id=result.get("command_id"), idempotency_key=key,
            legacy_status="blocked", hub_status="not_attempted",
            reconcile_status="blocked", details={"keyword_id": keyword_id},
        )
        return result

    def _refresh_legacy(self, keyword_id: str, confirm: bool, *, idempotency_key: str = "") -> dict[str, Any]:
        if confirm is not True:
            raise ValidationAppError("刷新必须明确传入 confirm=true。")
        runtime = SearchRefreshRuntime(
            self.settings, system_key="xhs-search", platform="xiaohongshu"
        )
        runtime_key = idempotency_key or f"legacy-xhs-readonly:{keyword_id}:{_now()}"
        command = runtime.begin(
            keyword_id=keyword_id,
            actor_id="user",
            idempotency_key=runtime_key,
        )
        if not command["created"]:
            return {
                "http_status": 200,
                "source_status": {"status": "degraded", "source": "hub_runtime"},
                "refresh_job_id": command["refresh_job_id"],
                "command_id": command["command_id"],
                "status": command["status"],
                "replayed": True,
            }
        keyword_text = None
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT keyword,payload_json FROM keywords WHERE keyword_id=? AND platform='xiaohongshu'", (keyword_id,)).fetchone()
            if row:
                payload = json.loads(row["payload_json"] or "{}")
                keyword_text = str(row["keyword"] or payload.get("keyword_text") or "").strip()
        if not keyword_text:
            raise NotFoundError("小红书关键词", keyword_id)
        # 旧小红书服务的 refresh 是有副作用的写接口。统一工作台当前只把旧
        # normalized/HTTP 服务当作读取事实源，不能因为原版按钮仍在就把写入
        # 偷渡回旧系统；等 Hub 自己的采集 Provider 接入后，再在这里落任务。
        details = {
            "keyword_id": keyword_id,
            "keyword": keyword_text,
            "upstream_called": False,
            "reason_code": "xhs.legacy_refresh_blocked",
            "message": "小红书旧系统刷新属于外部写操作，当前版本只读旧事实源；未向旧系统发起请求。",
        }
        runtime.finish(
            command["refresh_job_id"],
            status="blocked",
            external_result=details,
            error={"reason_code": details["reason_code"], "message": details["message"]},
        )
        self._connection("degraded", error=details["reason_code"])
        self._audit("xhs.refresh", "blocked", details, subject_id=keyword_id)
        return {
            "http_status": 409,
            "source_status": {
                "status": "degraded",
                "source": "hub_policy",
                "mode": "blocked",
            },
            "blocked": True,
            "upstream_called": False,
            "refresh_job_id": command["refresh_job_id"],
            "command_id": command["command_id"],
            "keyword_id": keyword_id,
            "keyword": keyword_text,
            "reason_code": details["reason_code"],
            "message": details["message"],
        }

    def refresh_status(self, job_id: str) -> dict[str, Any]:
        resolver = MigrationResolver(self.settings, module_key="xhs-search", contract_key="refresh-status")
        if resolver.mode() == "hub":
            runtime = SearchRefreshRuntime(
                self.settings, system_key="xhs-search", platform="xiaohongshu"
            ).status(job_id)
            if not runtime:
                raise ConflictError("小红书刷新状态的 Hub 运行记录不存在，未回退到旧系统。")
            return {"http_status": 200, "source_status": {"status": "healthy", "source": "hub_runtime"}, "result": runtime, "migration": {"mode": "hub"}}
        if resolver.mode() == "compare":
            result, migration = resolver.compare(
                request_fingerprint=f"xhs:refresh-status:{job_id}",
                legacy=lambda: self._refresh_status_legacy(job_id),
                hub=lambda: SearchRefreshRuntime(self.settings, system_key="xhs-search", platform="xiaohongshu").status(job_id)
                    or (_ for _ in ()).throw(NotImplementedError("Hub 刷新状态不存在")),
            )
            result["migration"] = migration
            return result
        result = self._refresh_status_legacy(job_id)
        result["migration"] = {"mode": "legacy"}
        return result

    def _refresh_status_legacy(self, job_id: str) -> dict[str, Any]:
        runtime = SearchRefreshRuntime(
            self.settings, system_key="xhs-search", platform="xiaohongshu"
        ).status(job_id)
        if runtime:
            return {
                "http_status": 200,
                "source_status": {"status": "degraded", "source": "hub_runtime"},
                "result": runtime,
            }
        try:
            response = self.adapter.refresh_status(job_id)
            self._connection("healthy")
            return {"http_status": response.status, "source_status": {"status": "healthy", "source": "legacy_http"}, "result": _scrub_payload(response.payload)}
        except XhsSourceError as exc:
            self._connection("degraded" if exc.status else "offline", error=str(exc))
            raise ConflictError(f"{exc.kind}: {exc}") from exc
