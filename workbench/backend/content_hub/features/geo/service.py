from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlparse

from content_hub.adapters.geo import GeoAdapter, GeoSourceError, RedfoxAdapter, scrub, stable_source_id
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        return None


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(str(value).encode()).hexdigest()[:24]}"


def _json(value: Any) -> str:
    return json.dumps(scrub(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_manifest(
    path: Any,
    *,
    root: Any | None = None,
    trusted: bool = False,
    file_hash: str | None = None,
) -> dict[str, Any]:
    path = os.fspath(path)
    try:
        if root is not None and not trusted:
            root_path = os.path.realpath(os.fspath(root))
            resolved = os.path.realpath(path)
            if os.path.commonpath((root_path, resolved)) != root_path:
                return {"path": path, "error": "outside_root"}
            current = path
            while current and current != os.path.dirname(current):
                if os.path.islink(current):
                    return {"path": path, "error": "symlink"}
                current = os.path.dirname(current)
        stat = os.stat(path)
        if file_hash is None:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            file_hash = digest.hexdigest()
        return {"path": path, "sha256": file_hash, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError as exc:
        return {"path": path, "error": type(exc).__name__}


def _validate_limit(limit: Any) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValidationAppError("limit 必须是 1–10000 的整数。")
    return limit


def _sorted_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class GeoService:
    def __init__(self, settings: Any):
        self.settings = settings
        self.adapter = GeoAdapter(settings)
        self.redfox = RedfoxAdapter(settings)

    def _redfox_summary(self) -> dict[str, Any]:
        rows = self.redfox.scan()
        bad = [row for row in rows if row["markdown"].get("error") == "bad_frontmatter"]
        manifest = [_file_manifest(row["path"]) for row in rows]
        return {
            "adapter": self.redfox.adapter_key,
            "lineage": "redfox",
            "historical_read_only_available": True,
            "snapshot_count": len(rows),
            "bad_frontmatter": len(bad),
            "manifest_id": _sorted_hash(manifest),
            "batch_import": False,
            "refresh": {"configured": False, "available": False, "paid": True, "batch": False},
        }

    def status(self) -> dict[str, Any]:
        try:
            snap = self.adapter.snapshot(limit=5)
            source = {"status": "healthy", "path": str(self.settings.geo_database_path), "read_only": True}
        except Exception as exc:
            snap = {"counts": {}, "batches": [], "answers": []}
            source = {"status": "offline", "path": str(self.settings.geo_database_path), "error": str(exc), "read_only": True}
        with connect(self.settings, readonly=True) as con:
            imported = con.execute("SELECT COUNT(*) FROM geo_answers WHERE source_ref LIKE 'geopromax:%'").fetchone()[0]
            redfox_imported = con.execute("SELECT COUNT(*) FROM geo_answers WHERE source_ref LIKE 'redfox:%'").fetchone()[0]
        return {
            "source_status": source,
            "source": snap,
            "redfox": self._redfox_summary(),
            "hub": {"sqlite_answers": imported, "redfox_answers": redfox_imported},
        }

    def bootstrap(self) -> dict[str, Any]:
        result = self.status()
        result["capabilities"] = {
            "read": True, "dry_run": True, "history_import": True,
            "manual_paid_refresh": {"configured": False, "available": False, "requires_confirm": True},
            "batch_refresh": False,
            "redfox": {"historical_read_only_available": True, "batch_import": False},
        }
        return scrub(result)

    def query(self, table: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = _validate_limit(limit)
        allowed = {"batches", "answers", "sources", "tools", "keywords", "metrics"}
        if table not in allowed:
            raise ValidationAppError("不支持的 GEO 查询资源。")
        source_table = {"keywords": "tool_search_keywords", "metrics": "source_metrics"}.get(table, table)
        with self.adapter._connect() as con:
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {source_table} LIMIT ? OFFSET ?", (limit, offset))]
            total = con.execute(f"SELECT COUNT(*) FROM {source_table}").fetchone()[0]
        if table == "sources":
            for row in rows:
                canonical, mapped = self.adapter.canonical_platform(row.get("platform"))
                row.update({
                    "raw_platform": row.get("platform"),
                    "canonical_platform": canonical,
                    "platform_mapped": mapped,
                    "identity_expected": stable_source_id(str(row.get("platform") or ""), row["url"]),
                    "identity_valid": stable_source_id(str(row.get("platform") or ""), row["url"]) == row["id"],
                    "domain": urlparse(row["url"]).hostname,
                })
        return scrub({"items": rows, "count": len(rows), "total": total, "limit": limit, "offset": offset, "source": "geopromax_sqlite"})

    def questions(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = _validate_limit(limit)
        with self.adapter._connect() as con:
            rows = con.execute(
                """
                SELECT a.id,a.question,a.status,a.mode,a.new_context,a.started_at,a.finished_at,
                       a.duration_seconds,a.share_link,a.markdown_path,a.error,
                       b.app,b.channel,b.mode AS batch_mode,b.status AS batch_status,
                       b.raw_file,b.input_file,b.output_file
                FROM answers a JOIN batches b ON b.id=a.batch_id
                WHERE trim(COALESCE(a.question,'')) <> ''
                ORDER BY COALESCE(a.question,''),COALESCE(a.finished_at,a.started_at),a.id
                """
            ).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                item = dict(row)
                item["captured_at_raw"] = item.get("finished_at") or item.get("started_at")
                item["captured_at"] = _utc(item["captured_at_raw"])
                grouped.setdefault(item["question"], []).append(item)
        items = [
            {"question": question, "answer_count": len(answers), "answers": sorted(answers, key=lambda item: (item.get("captured_at") or "", item["id"]))}
            for question, answers in grouped.items()
        ]
        items.sort(key=lambda item: item["question"])
        total = len(items)
        return scrub({"items": items[offset:offset + limit], "count": len(items[offset:offset + limit]), "total": total, "limit": limit, "offset": offset, "source": "geopromax_sqlite"})

    def detail(self, kind: str, item_id: int) -> dict[str, Any]:
        if kind not in {"batch", "answer"}:
            raise ValidationAppError("详情类型不支持。")
        with self.adapter._connect() as con:
            if kind == "batch":
                row = con.execute("SELECT * FROM batches WHERE id=?", (item_id,)).fetchone()
                if row is None:
                    raise NotFoundError(kind, str(item_id))
                result = dict(row)
                result["answers"] = [dict(item) for item in con.execute("SELECT * FROM answers WHERE batch_id=? ORDER BY id", (item_id,))]
                return scrub(result)
            row = con.execute("SELECT * FROM answers WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError(kind, str(item_id))
            result = dict(row)
            batch = con.execute("SELECT * FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
            result["batch"] = dict(batch) if batch else None
            result["app"] = batch["app"] if batch else None
            result["channel"] = batch["channel"] if batch else None
            result["raw_batch"] = {
                key: batch[key] if batch else None
                for key in ("app", "channel", "mode", "new_context", "status", "started_at",
                            "finished_at", "duration_seconds", "raw_file", "input_file", "output_file")
            }
            tools = [dict(item) for item in con.execute("SELECT * FROM tools WHERE answer_id=? ORDER BY position", (item_id,))]
            result["tools"] = tools
            tool_ids = [item["id"] for item in tools]
            placeholders = ",".join("?" for _ in tool_ids) or "NULL"
            result["keywords"] = [dict(item) for item in con.execute(f"SELECT * FROM tool_search_keywords WHERE tool_id IN ({placeholders}) ORDER BY tool_id,position", tuple(tool_ids))]
            relations = [dict(item) for item in con.execute("SELECT * FROM source_relations WHERE answer_id=? ORDER BY position,id", (item_id,))]
            result["relations"] = relations
            source_ids = [item["source_id"] for item in relations if item["source_id"]]
            placeholders = ",".join("?" for _ in source_ids) or "NULL"
            sources = [dict(item) for item in con.execute(f"SELECT * FROM sources WHERE id IN ({placeholders}) ORDER BY id", tuple(source_ids))]
            for source in sources:
                canonical, mapped = self.adapter.canonical_platform(source.get("platform"))
                source.update({"raw_platform": source.get("platform"), "canonical_platform": canonical, "platform_mapped": mapped, "identity_expected": stable_source_id(str(source.get("platform") or ""), source["url"]), "identity_valid": stable_source_id(str(source.get("platform") or ""), source["url"]) == source["id"], "domain": urlparse(source["url"]).hostname})
            result["sources"] = sources
            result["suggested_questions"] = [dict(item) for item in con.execute("SELECT * FROM suggested_questions WHERE answer_id=? ORDER BY position", (item_id,))]
            result["metrics"] = [dict(item) for item in con.execute("SELECT * FROM source_metrics WHERE answer_id=? ORDER BY source_id", (item_id,))]
            result["markdown"] = self.adapter.markdown(result.get("markdown_path"))
            return scrub(result)

    def _prepare(self, *, limit: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        limit = _validate_limit(limit)
        data = self.adapter.records(limit=limit)
        markdown_cache: dict[str, dict[str, Any]] = {}

        def read_markdown(relative: str | None) -> dict[str, Any]:
            key = "" if relative is None else str(relative)
            if key not in markdown_cache:
                markdown_cache[key] = self.adapter.markdown(relative)
            return markdown_cache[key]

        conflicts: list[dict[str, Any]] = []
        deduplications: list[dict[str, Any]] = []
        rejects: list[dict[str, Any]] = []
        source_by_id = {}
        for source in data["sources"]:
            raw = str(source.get("platform") or "")
            expected = stable_source_id(raw, source["url"])
            canonical, mapped = self.adapter.canonical_platform(raw)
            item = dict(source)
            item["raw_platform"] = raw
            item["canonical_platform"] = canonical
            item["platform_mapped"] = mapped
            item["identity_expected"] = expected
            item["identity_valid"] = expected == source["id"]
            source_by_id[source["id"]] = item
            source.update(item)
            if not item["identity_valid"]:
                rejects.append({"kind": "source", "id": source["id"], "reason": "source_id_mismatch", "expected": expected, "raw_platform": raw, "url": source["url"]})
        missing, bad, answer_rejects = [], [], []
        answer_reject_by_id: dict[int, dict[str, Any]] = {}
        for answer in data["answers"]:
            markdown = read_markdown(answer.get("markdown_path"))
            if not markdown["exists"]:
                missing.append({"kind": "answer", "id": answer["id"], "path": answer.get("markdown_path"), "error": markdown["error"]})
                answer_reject_by_id[answer["id"]] = {"kind": "answer", "id": answer["id"], "reason": "missing_answer_markdown", "error": markdown["error"]}
            elif markdown.get("error") == "bad_frontmatter":
                bad.append({"kind": "answer", "id": answer["id"], "path": answer.get("markdown_path"), "error": markdown["error"]})
            if not str(answer.get("question") or "").strip():
                answer_reject_by_id.setdefault(answer["id"], {"kind": "answer", "id": answer["id"], "reason": "empty_question"})
            raw_time = answer.get("finished_at") or answer.get("started_at")
            if raw_time and _utc(raw_time) is None:
                answer_reject_by_id.setdefault(answer["id"], {"kind": "answer", "id": answer["id"], "reason": "invalid_timestamp", "raw_value": raw_time})
        answer_rejects = list(answer_reject_by_id.values())
        for source in data["sources"]:
            if source.get("markdown_path"):
                read_markdown(source["markdown_path"])
        for source in data["sources"]:
            source["identity_expected"] = source_by_id[source["id"]]["identity_expected"]
        file_manifest = [
            _file_manifest(self.settings.geo_database_path, trusted=True),
            _file_manifest(self.settings.geo_platforms_path, trusted=True),
        ]
        for answer in data["answers"]:
            if answer.get("markdown_path"):
                path = self.settings.geo_source_root / answer["markdown_path"]
                markdown = markdown_cache.get(str(answer["markdown_path"]), {})
                file_manifest.append(_file_manifest(path, root=self.settings.geo_source_root, file_hash=markdown.get("file_hash")))
        for source in data["sources"]:
            if source.get("markdown_path"):
                markdown = markdown_cache.get(str(source["markdown_path"]), {})
                file_manifest.append(_file_manifest(self.settings.geo_source_root / source["markdown_path"], root=self.settings.geo_source_root, file_hash=markdown.get("file_hash")))
        manifest_id = _sorted_hash({
            "adapter": self.adapter.adapter_key,
            "limit": limit,
            "records": data,
            "files": file_manifest,
        })
        selected_counts = {key: len(data[key]) for key in ("batches", "answers", "tools", "keywords", "sources", "relations", "suggestions", "metrics")}
        selected_counts["source_identifier_count"] = len(data["sources"])
        selected_counts["source_content_count"] = len({item["url"] for item in data["sources"]})
        preview = {
            "dry_run": True,
            "manifest_id": manifest_id,
            "source": {"database": str(self.settings.geo_database_path), "root": str(self.settings.geo_source_root)},
            "counts": {"selected": selected_counts, "source_total": data["source_totals"]},
            "importable": {"answers": len(data["answers"]) - len(answer_rejects), "sources": len(data["sources"]) - len(rejects), "relations": len(data["relations"]), "metrics": len(data["metrics"])},
            "missing_markdown": missing,
            "bad_markdown": bad,
            "platforms": {
                "raw_counts": dict(Counter(str(item.get("platform") or "") for item in data["sources"])),
                "canonical_counts": dict(Counter(str(item.get("canonical_platform") or "") for item in source_by_id.values())),
                "mapped_sources": sum(1 for item in source_by_id.values() if item["platform_mapped"]),
            },
            "conflicts": conflicts,
            "deduplications": deduplications,
            "rejected": rejects + answer_rejects,
            "warnings": [],
            "writes": False,
            "redfox": self._redfox_summary(),
        }
        data["_markdown_cache"] = markdown_cache
        return data, preview

    def preview(self, *, limit: int | None = None) -> dict[str, Any]:
        _, preview = self._prepare(limit=limit)
        if any(item.get("kind") == "source" for item in preview["rejected"]):
            preview["warnings"].append("存在 source.id 与 raw platform/url 身份不一致的来源，已拒绝静默导入。")
        if any(item.get("kind") == "answer" for item in preview["rejected"]):
            preview["warnings"].append("存在回答 Markdown、问题或时间字段无效；这些 answer 将计入回答失败，不影响来源身份对账。")
        if preview["missing_markdown"]:
            preview["warnings"].append("存在缺失 Markdown；SQLite 结构事实仍可审计，但正文不可导入。")
        if preview["bad_markdown"]:
            preview["warnings"].append("存在坏 frontmatter Markdown。")
        return scrub(preview)

    @staticmethod
    def _creator_id(platform: str, author: str, profile: str | None) -> tuple[str, str, str]:
        if profile:
            external = profile
            derivation = "profile_url"
        else:
            external = f"geo_author:{platform}:{author}"
            derivation = "canonical_platform_author"
        return _id("creator", f"{platform}:{external}"), external, derivation

    def _upsert_source(
        self,
        con: sqlite3.Connection,
        source: dict[str, Any],
        now: str,
        content_ids: dict[str, str],
        conflicts: list[dict[str, Any]],
        deduplications: list[dict[str, Any]],
        markdown_cache: dict[str, dict[str, Any]],
    ) -> bool:
        if not source["identity_valid"]:
            return False
        raw, canonical = source["raw_platform"], source["canonical_platform"]
        requested_content_id = _id("content", f"geopromax-sqlite:{source['id']}")
        content_id = requested_content_id
        author = source.get("author")
        existing_identifier = con.execute(
            "SELECT i.content_id,c.canonical_url,c.payload_json FROM content_identifiers i LEFT JOIN contents c ON c.content_id=i.content_id WHERE i.namespace=? AND i.external_id=?",
            ("geopromax_sqlite_source", source["id"]),
        ).fetchone()
        if existing_identifier and existing_identifier["canonical_url"] != source.get("url"):
            conflicts.append({"kind": "identifier", "source_id": source["id"], "reason": "identifier_content_conflict", "existing_content_id": existing_identifier["content_id"], "requested_content_id": content_id})
            return False
        creator_id = None
        creator_external = None
        derivation = None
        if author:
            creator_id, creator_external, derivation = self._creator_id(canonical, author, source.get("author_profile_link"))
            con.execute(
                "INSERT INTO creators(creator_id,canonical_name,platform,external_id,profile_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json",
                (creator_id, author, canonical, creator_external, source.get("author_profile_link"), now, now, _json({"origin": "geopromax_sqlite", "external_id_derivation": derivation})),
            )
        md = markdown_cache.get(str(source.get("markdown_path") or ""), {})
        domain = urlparse(source["url"]).hostname or None
        file_hash = md.get("file_hash")
        content_hash = md.get("content_hash") or source.get("content_hash")
        raw_source = dict(source)
        raw_source.update({"markdown_path": source.get("markdown_path"), "file_hash": file_hash, "content_hash": content_hash})
        payload = {"origin": "geopromax_sqlite", "legacy_source_id": source["id"], "raw_platform": raw, "canonical_platform": canonical, "platform_mapped": source["platform_mapped"], "summary": source.get("summary"), "favicon": source.get("favicon"), "cover_image": source.get("cover_image"), "author_profile_link": source.get("author_profile_link"), "published_at_raw": source.get("published_at"), "markdown_path": source.get("markdown_path"), "file_hash": file_hash, "content_hash": content_hash, "raw_source": raw_source}
        if existing_identifier and existing_identifier["canonical_url"] == source.get("url"):
            content_id = existing_identifier["content_id"]
        existing = con.execute("SELECT content_id,canonical_url,payload_json FROM contents WHERE content_id=?", (content_id,)).fetchone()
        if existing is None:
            try:
                con.execute(
                    "INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,author_name,published_at,first_seen_at,updated_at,md_path,file_hash,content_hash,domain,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (content_id, "external_article", source.get("title"), source.get("url"), creator_id, author, _utc(source.get("published_at")), now, now, source.get("markdown_path"), file_hash, content_hash, domain, _json(payload)),
                )
            except sqlite3.IntegrityError:
                reused = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (source.get("url"),)).fetchone()
                if reused is None:
                    raise
                content_id = reused[0]
                existing = con.execute("SELECT content_id,canonical_url,payload_json FROM contents WHERE content_id=?", (content_id,)).fetchone()
        else:
            if existing["canonical_url"] != source.get("url"):
                conflicts.append({"kind": "content", "source_id": source["id"], "reason": "identity_content_url_conflict"})
                return False
        if existing is not None:
            try:
                existing_payload = json.loads(existing["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                existing_payload = {}
            if content_id == requested_content_id and existing_payload.get("origin") == "geopromax_sqlite":
                con.execute(
                    "UPDATE contents SET title=?,creator_id=?,author_name=?,published_at=?,updated_at=?,md_path=?,file_hash=?,content_hash=?,domain=?,payload_json=? WHERE content_id=?",
                    (source.get("title"), creator_id, author, _utc(source.get("published_at")), now, source.get("markdown_path"), file_hash, content_hash, domain, _json(payload), content_id),
                )
        if content_id != requested_content_id:
            deduplications.append({"kind": "canonical_url", "source_id": source["id"], "content_id": content_id})
        content_ids[source["id"]] = content_id
        geo_identifier_payload = {
            "origin": "geopromax_sqlite", "lineage": "sqlite", "legacy_source_id": source["id"],
            "raw_platform": raw, "canonical_platform": canonical, "raw_url": source["url"],
            "title": source.get("title"), "summary": source.get("summary"),
            "author": author, "author_profile_link": source.get("author_profile_link"),
            "favicon": source.get("favicon"), "cover_image": source.get("cover_image"),
            "published_at_raw": source.get("published_at"), "file_hash": file_hash, "content_hash": content_hash,
            "markdown_path": source.get("markdown_path"), "raw_source": raw_source,
            "reused_existing_content": content_id != requested_content_id,
        }
        if existing_identifier:
            con.execute("UPDATE content_identifiers SET payload_json=? WHERE namespace=? AND external_id=?", (_json(geo_identifier_payload), "geopromax_sqlite_source", source["id"]))
        else:
            con.execute(
                "INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?)",
                ("geopromax_sqlite_source", source["id"], content_id, now, _json(geo_identifier_payload)),
            )
        return True

    def import_history(self, *, confirm: bool, limit: int | None = None) -> dict[str, Any]:
        limit = _validate_limit(limit)
        if not confirm:
            raise ConflictError("正式导入必须显式 confirm=true；请先调用 dry-run。")
        data, preview = self._prepare(limit=limit)
        manifest_id = preview["manifest_id"]
        now = _now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                existing = con.execute("SELECT batch_id FROM ingestion_batches WHERE adapter_key=? AND source_scope=?", (self.adapter.adapter_key, manifest_id)).fetchone()
                if existing:
                    batch_row = con.execute(
                        "SELECT status,records_seen,records_written,records_failed,payload_json FROM ingestion_batches WHERE batch_id=?",
                        (existing[0],),
                    ).fetchone()
                    payload = json.loads(batch_row["payload_json"] or "{}") if batch_row else {}
                    counts = payload.get("counts", {}).get("selected", {}) if isinstance(payload, dict) else {}
                    return scrub({
                        "batch_id": existing[0], "manifest_id": manifest_id, "idempotent": True,
                        "status": batch_row["status"] if batch_row else "unknown",
                        "records_seen": batch_row["records_seen"] if batch_row else 0,
                        "records_imported": batch_row["records_written"] if batch_row else 0,
                        "records_written": batch_row["records_written"] if batch_row else 0,
                        "records_failed": batch_row["records_failed"] if batch_row else 0,
                        "answer_count": counts.get("answers", len(data["answers"])),
                        "source_count": counts.get("sources", len(data["sources"])),
                        "source_identifier_count": counts.get("source_identifier_count", len(data["sources"])),
                        "source_content_count": counts.get("source_content_count", len({item["url"] for item in data["sources"]})),
                        "preview": preview,
                    })
                batch_id = _id("batch", f"{self.adapter.adapter_key}:{manifest_id}")
                content_ids: dict[str, str] = {}
                conflicts = list(preview["conflicts"])
                deduplications = list(preview.get("deduplications", []))
                failed = 0
                with transaction(con):
                    con.execute(
                        "INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,records_seen,records_written,records_failed,source_ref,error_json,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, self.adapter.adapter_key, manifest_id, "running", now, len(data["answers"]), 0, 0, str(self.settings.geo_database_path), _json(preview["rejected"]), _json(preview)),
                    )
                try:
                    with transaction(con):
                        source_imported = 0
                        for source in data["sources"]:
                            if self._upsert_source(con, source, now, content_ids, conflicts, deduplications, data["_markdown_cache"]):
                                source_imported += 1
                            else:
                                failed += 1
                        written, answer_failed = 0, 0
                        batch_by_id = {row["id"]: row for row in data["batches"]}
                        source_by_id = {row["id"]: row for row in data["sources"]}
                        tools_by_answer: dict[int, list[dict[str, Any]]] = {}
                        for tool in data["tools"]:
                            tools_by_answer.setdefault(tool["answer_id"], []).append(tool)
                        keywords_by_tool: dict[int, list[dict[str, Any]]] = {}
                        for keyword in data["keywords"]:
                            keywords_by_tool.setdefault(keyword["tool_id"], []).append(keyword)
                        relations_by_answer: dict[int, list[dict[str, Any]]] = {}
                        for relation in data["relations"]:
                            relations_by_answer.setdefault(relation["answer_id"], []).append(relation)
                        suggestions_by_answer: dict[int, list[dict[str, Any]]] = {}
                        for suggestion in data["suggestions"]:
                            suggestions_by_answer.setdefault(suggestion["answer_id"], []).append(suggestion)
                        metrics_by_answer: dict[int, list[dict[str, Any]]] = {}
                        for metric in data["metrics"]:
                            metrics_by_answer.setdefault(metric["answer_id"], []).append(metric)
                        for answer in data["answers"]:
                            batch = batch_by_id.get(answer["batch_id"], {})
                            answer_id = _id("geo_answer", f"geopromax-sqlite:{answer['id']}")
                            content_id = _id("content", f"geopromax-answer:{answer['id']}")
                            md = data["_markdown_cache"].get(str(answer.get("markdown_path") or ""), {})
                            raw_time = answer.get("finished_at") or answer.get("started_at")
                            if not str(answer.get("question") or "").strip() or not md["exists"] or (raw_time and _utc(raw_time) is None):
                                answer_failed += 1
                                continue
                            captured_at = _utc(raw_time)
                            fingerprint = (batch.get("app") or "", answer.get("question") or "", captured_at, md.get("answer_hash"))
                            fingerprint_row = con.execute(
                                "SELECT answer_id,source_ref FROM geo_answers WHERE app=? AND question_raw=? AND captured_at=? AND answer_hash=? AND answer_id<>?",
                                (*fingerprint, answer_id),
                            ).fetchone()
                            if fingerprint_row:
                                answer_failed += 1
                                conflicts.append({
                                    "kind": "answer",
                                    "reason": "fingerprint_conflict",
                                    "legacy_answer_id": answer["id"],
                                    "existing_answer_id": fingerprint_row["answer_id"],
                                    "existing_source_ref": fingerprint_row["source_ref"],
                                })
                                continue
                            raw_batch = {key: batch.get(key) for key in ("app", "channel", "mode", "new_context", "status", "started_at", "finished_at", "duration_seconds", "raw_file", "input_file", "output_file")}
                            tools_json = []
                            for tool in tools_by_answer.get(answer["id"], []):
                                tools_json.append({**tool, "search_keywords": sorted(keywords_by_tool.get(tool["id"], []), key=lambda item: item["position"])})
                            recommended = sorted(suggestions_by_answer.get(answer["id"], []), key=lambda item: item["position"])
                            answer_payload = {"origin": "geopromax_sqlite", "lineage": "sqlite", "legacy_answer_id": answer["id"], "batch_id": answer["batch_id"], "batch": raw_batch, "share_link": answer.get("share_link"), "markdown_path": answer.get("markdown_path"), "error": answer.get("error"), "captured_at_raw": raw_time, "file_hash": md.get("file_hash"), "content_hash": md.get("content_hash"), "answer_hash": md.get("answer_hash"), "raw_answer": answer}
                            con.execute(
                                "INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,md_path,file_hash,content_hash,payload_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(content_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at,md_path=excluded.md_path,file_hash=excluded.file_hash,content_hash=excluded.content_hash,payload_json=excluded.payload_json",
                                (content_id, "ai_answer", answer.get("question"), now, now, answer.get("markdown_path"), md.get("file_hash"), md.get("content_hash"), _json(answer_payload)),
                            )
                            con.execute(
                                "INSERT INTO geo_answers(answer_id,content_id,app,mode,question_raw,captured_at,answer_hash,tools_json,recommended_json,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(answer_id) DO UPDATE SET content_id=excluded.content_id,app=excluded.app,mode=excluded.mode,question_raw=excluded.question_raw,captured_at=excluded.captured_at,answer_hash=excluded.answer_hash,tools_json=excluded.tools_json,recommended_json=excluded.recommended_json,payload_json=excluded.payload_json",
                                (answer_id, content_id, batch.get("app") or "", answer.get("mode") or batch.get("mode"), answer.get("question") or "", captured_at, md.get("answer_hash"), _json(tools_json), _json(recommended), f"geopromax:{answer['id']}", _json(answer_payload)),
                            )
                            for relation in relations_by_answer.get(answer["id"], []):
                                source = source_by_id.get(relation.get("source_id"), {})
                                source_md = data["_markdown_cache"].get(str(source.get("markdown_path") or ""), {}) if source else {}
                                relation_source = dict(source)
                                relation_source.update({"raw_url": source.get("url"), "raw_platform": source.get("platform"), "canonical_platform": source.get("canonical_platform"), "markdown_path": source.get("markdown_path"), "file_hash": source_md.get("file_hash"), "content_hash": source_md.get("content_hash")})
                                relation_id = _id("relation", f"geopromax:{relation['id']}")
                                relation_payload = {
                                    "origin": "geopromax_sqlite", "legacy_relation_id": relation["id"],
                                    "tool_id": relation.get("tool_id"), "anchor_index": relation.get("anchor_index"),
                                    "image_url": relation.get("image_url"), "error": relation.get("error"),
                                    "source_fact": relation_source,
                                }
                                relation_values = (
                                    relation_id, answer_id, content_ids.get(relation.get("source_id")),
                                    relation["type"], relation.get("position"), relation.get("anchor_text"),
                                    source.get("url"), _json(relation_payload),
                                )
                                relation_identity = (answer_id, relation["type"], relation.get("position"), source.get("url"), relation.get("anchor_text"))
                                relation_collision = con.execute(
                                    "SELECT relation_id FROM geo_source_relations WHERE answer_id=? AND relation_type=? AND COALESCE(position,-1)=COALESCE(?, -1) AND COALESCE(url_raw,'')=COALESCE(?, '') AND COALESCE(anchor_text,'')=COALESCE(?, '') AND relation_id<>?",
                                    (*relation_identity, relation_id),
                                ).fetchone()
                                if relation_collision:
                                    conflicts.append({"kind": "relation", "legacy_relation_id": relation["id"], "reason": "identity_conflict", "existing_relation_id": relation_collision["relation_id"]})
                                    continue
                                con.execute(
                                    "INSERT INTO geo_source_relations(relation_id,answer_id,source_content_id,relation_type,position,anchor_text,url_raw,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(relation_id) DO UPDATE SET answer_id=excluded.answer_id,source_content_id=excluded.source_content_id,relation_type=excluded.relation_type,position=excluded.position,anchor_text=excluded.anchor_text,url_raw=excluded.url_raw,payload_json=excluded.payload_json",
                                    relation_values,
                                )
                            for metric in metrics_by_answer.get(answer["id"], []):
                                for key in ("read_count", "like_count", "comment_count", "favorite_count", "share_count"):
                                    if metric.get(key) is None or metric.get("source_id") not in content_ids:
                                        continue
                                    metric_key = f"geo.{key}"
                                    con.execute(
                                        "INSERT INTO metric_definitions(metric_key,platform,subject_type,display_name) VALUES(?,?,?,?) ON CONFLICT(metric_key) DO NOTHING",
                                        (metric_key, "GEO", "content", metric_key),
                                    )
                                    observation_id = _id("observation", f"geopromax:{metric['source_id']}:{answer['id']}:{key}")
                                    observed_at = _utc(answer.get("finished_at") or answer.get("started_at")) or now
                                    observation_identity = ("content", content_ids[metric["source_id"]], metric_key, observed_at)
                                    observation_collision = con.execute(
                                        "SELECT observation_id FROM metric_observations WHERE subject_type=? AND subject_id=? AND metric_key=? AND observed_at=? AND COALESCE(snapshot_id,'no-snapshot')='no-snapshot' AND observation_id<>?",
                                        (*observation_identity, observation_id),
                                    ).fetchone()
                                    if observation_collision:
                                        conflicts.append({"kind": "metric", "observation_id": observation_id, "reason": "identity_conflict", "existing_observation_id": observation_collision["observation_id"]})
                                        continue
                                    con.execute(
                                        "INSERT INTO metric_observations(observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET subject_type=excluded.subject_type,subject_id=excluded.subject_id,metric_key=excluded.metric_key,observed_at=excluded.observed_at,numeric_value=excluded.numeric_value,payload_json=excluded.payload_json",
                                        (observation_id, "content", content_ids[metric["source_id"]], metric_key, observed_at, metric[key], f"geopromax:{answer['id']}", _json({"origin": "geopromax_sqlite", "legacy_source_id": metric["source_id"], "metric_key": key})),
                                    )
                            written += 1
                        imported = source_imported + written
                        records_seen = len(data["sources"]) + len(data["answers"])
                        records_failed = min(records_seen, failed + answer_failed)
                        status = "partial_failed" if records_failed or conflicts else "succeeded"
                        con.execute("UPDATE ingestion_batches SET status=?,finished_at=?,records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=?,updated_at=? WHERE batch_id=?", (status, now, records_seen, imported, records_failed, _json({"rejected": preview["rejected"], "conflicts": conflicts, "status": status}), _json({**preview, "conflicts": conflicts, "deduplications": deduplications, "counts": {**preview["counts"], "records_seen": records_seen, "records_imported": imported, "records_failed": records_failed}}), now, batch_id))
                        con.execute(
                            "INSERT INTO ingestion_checkpoints(adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET cursor_value=excluded.cursor_value,source_hash=excluded.source_hash,last_success_at=excluded.last_success_at,batch_id=excluded.batch_id,payload_json=excluded.payload_json",
                            (self.adapter.adapter_key, str(limit) if limit is not None else "full", str(max((row["id"] for row in data["answers"]), default="")), manifest_id, now, batch_id, _json({"manifest_id": manifest_id, "counts": preview["counts"]})),
                        )
                        con.execute(
                            "INSERT INTO system_connections(system_key,display_name,base_url,status,last_checked_at,capabilities_json,details_json) VALUES('geo','GEOProMax',NULL,?,?,?,?) ON CONFLICT(system_key) DO UPDATE SET status=excluded.status,last_checked_at=excluded.last_checked_at,capabilities_json=excluded.capabilities_json,details_json=excluded.details_json",
                            ("healthy" if status == "succeeded" else "degraded", now, _json(["read", "dry_run", "history_import"]), _json({"adapter": self.adapter.adapter_key, "manifest_id": manifest_id, "redfox": self._redfox_summary()})),
                        )
                        con.execute("INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)", (_id("audit", batch_id), now, "workbench", "geo_import", "ingestion_batch", batch_id, "failed" if status != "succeeded" else "succeeded", _json({"adapter": self.adapter.adapter_key, "manifest_id": manifest_id, "status": status, "conflicts": conflicts, "deduplications": deduplications})))
                except Exception as exc:
                    failed_at = _now()
                    error = {"type": type(exc).__name__, "message": str(exc)}
                    with transaction(con):
                        con.execute(
                            "UPDATE ingestion_batches SET status='failed',finished_at=?,records_seen=?,records_written=0,records_failed=?,error_json=?,payload_json=?,updated_at=? WHERE batch_id=?",
                            (failed_at, len(data["sources"]) + len(data["answers"]), len(data["sources"]) + len(data["answers"]), _json(error), _json({**preview, "status": "failed", "error": error, "conflicts": conflicts, "deduplications": deduplications}), failed_at, batch_id),
                        )
                        con.execute(
                            "INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)",
                            (_id("audit", f"{batch_id}:failed"), failed_at, "workbench", "geo_import", "ingestion_batch", batch_id, "failed", _json({"adapter": self.adapter.adapter_key, "manifest_id": manifest_id, "error": error})),
                        )
                    raise
        return scrub({"batch_id": batch_id, "manifest_id": manifest_id, "idempotent": False, "status": status, "records_seen": len(data["sources"]) + len(data["answers"]), "records_imported": source_imported + written, "records_written": source_imported + written, "records_failed": min(len(data["sources"]) + len(data["answers"]), failed + answer_failed), "answer_count": len(data["answers"]), "source_count": len(data["sources"]), "source_identifier_count": len(data["sources"]), "source_content_count": len({item["url"] for item in data["sources"]}), "conflicts": conflicts, "deduplications": deduplications, "preview": preview})

    def refresh_preview(self, answer_id: int) -> dict[str, Any]:
        detail = self.detail("answer", answer_id)
        return scrub({"dry_run": True, "refreshable": False, "configured": False, "available": False, "requires_confirm": True, "batch_refresh": False, "paid": True, "answer": {"id": answer_id, "question": detail.get("question")}, "message": "RedFox 正式刷新未接入；本入口不调用付费服务。"})

    def refresh_confirm(self, answer_id: int, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ConflictError("RedFox 刷新必须显式 confirm=true。")
        raise ConflictError("RedFox 正式刷新未接入；为避免付费调用，本入口仅保留保护闸门。")

    def redfox_read_only(self) -> dict[str, Any]:
        return scrub(self._redfox_summary())
