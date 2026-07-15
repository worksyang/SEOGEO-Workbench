from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
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


def _relative_source_path(value: Any, root: Any) -> str | None:
    """将外部 Markdown 路径收敛为相对 source_ref；越界绝不写入本地绝对路径。"""
    if value is None or value == "":
        return None
    raw = os.fspath(value)
    if raw.startswith("~/") or raw.startswith("~\\"):
        return None
    root_path = Path(root).resolve()
    path = Path(raw)
    if path.is_absolute():
        try:
            return path.resolve(strict=False).relative_to(root_path).as_posix()
        except ValueError:
            return None
    return Path(raw).as_posix()


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

    def _refresh_state(self) -> dict[str, Any]:
        configured = bool(self.settings.geo_redfox_api_key_configured)
        blocked_reason = "not_integrated" if configured else "missing_api_key"
        message = "RedFox API Key 已配置，但付费刷新尚未接入。" if configured else "未配置 RedFox API Key，当前只能查看历史快照。"
        return {
            "configured": configured,
            "available": False,
            "blocked_reason": blocked_reason,
            "message": message,
            "paid": True,
            "requires_confirm": True,
        }

    def _platform_mapper(self):
        aliases = self.adapter.platform_rules()["aliases"]

        def mapper(raw: str | None) -> tuple[str, bool]:
            value = str(raw or "").strip()
            if value in aliases:
                return aliases[value], True
            return value or "其他网页", False

        return mapper

    def _markdown_available(self, relative: str | None) -> bool:
        if not relative:
            return False
        root = Path(self.settings.geo_source_root).resolve()
        path = Path(relative)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve(strict=True)
            if root != resolved and root not in resolved.parents:
                return False
            current = path
            while current != root and current != current.parent:
                if current.is_symlink():
                    return False
                current = current.parent
            return resolved.is_file()
        except OSError:
            return False

    @staticmethod
    def _captured_at(row: dict[str, Any] | sqlite3.Row) -> tuple[str | None, str | None]:
        raw = row["finished_at"] or row["started_at"]
        return raw, _utc(raw)

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
            "refresh": {**self._refresh_state(), "batch": False},
        }

    @staticmethod
    def _hub_import_status(con: sqlite3.Connection) -> dict[str, Any]:
        """只返回 Hub 导入事实，不把 SQLite 原始来源可读误报为导入健康。"""
        connection = con.execute(
            "SELECT status,last_checked_at FROM system_connections WHERE system_key='geo'"
        ).fetchone()
        batch = con.execute(
            """
            SELECT status,started_at,finished_at,records_seen,records_written,records_failed
            FROM ingestion_batches
            WHERE adapter_key='geopromax-sqlite'
            ORDER BY COALESCE(finished_at,started_at,created_at) DESC, created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if batch is None:
            return {
                "status": "not_checked",
                "last_checked_at": None,
                "records_seen": 0,
                "records_written": 0,
                "records_failed": 0,
                "message": "尚未检查 GEO 历史导入。",
            }

        batch_status = str(batch["status"] or "").lower()
        status = {
            "succeeded": "healthy",
            "partial_failed": "degraded",
            "queued": "degraded",
            "running": "degraded",
            "failed": "offline",
            "cancelled": "offline",
        }.get(batch_status, "degraded")
        checked_at = batch["finished_at"] or batch["started_at"] or (connection["last_checked_at"] if connection else None)
        message = {
            "healthy": "GEO 历史数据已导入 Hub。",
            "degraded": "GEO 历史数据已部分导入，存在失败记录。",
            "offline": "GEO 历史导入失败，Hub 数据不可视为完整。",
        }[status]
        return {
            "status": status,
            "last_checked_at": checked_at,
            "records_seen": int(batch["records_seen"] or 0),
            "records_written": int(batch["records_written"] or 0),
            "records_failed": int(batch["records_failed"] or 0),
            "message": message,
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
            hub_import_status = self._hub_import_status(con)
        return {
            "source_status": source,
            "source": snap,
            "redfox": self._redfox_summary(),
            "hub": {"sqlite_answers": imported, "redfox_answers": redfox_imported},
            "hub_import_status": hub_import_status,
        }

    def bootstrap(self) -> dict[str, Any]:
        result = self.status()
        result["refresh"] = self._refresh_state()
        result["capabilities"] = {
            "read": True, "dry_run": True, "history_import": True,
            "manual_paid_refresh": self._refresh_state(),
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

    def _question_summaries(self) -> tuple[list[dict[str, Any]], int]:
        mapper = self._platform_mapper()
        with self.adapter._connect() as con:
            answer_rows = con.execute(
                """
                SELECT a.id,a.question,a.status,a.mode,a.new_context,a.started_at,a.finished_at,
                       a.duration_seconds,a.share_link,a.markdown_path,a.error,
                       b.app,b.channel,b.mode AS batch_mode,b.status AS batch_status,
                       b.raw_file,b.input_file,b.output_file
                FROM answers a JOIN batches b ON b.id=a.batch_id
                WHERE trim(COALESCE(a.question,'')) <> ''
                ORDER BY a.question,COALESCE(a.finished_at,a.started_at),a.id
                """
            ).fetchall()
            excluded = con.execute(
                "SELECT COUNT(*) FROM answers WHERE trim(COALESCE(question,''))=''"
            ).fetchone()[0]
            relation_counts = con.execute(
                """
                SELECT answer_id,COUNT(*) AS relation_count,
                       COUNT(DISTINCT source_id) AS source_count
                FROM source_relations
                GROUP BY answer_id
                """
            ).fetchall()
            relation_type_rows = con.execute(
                "SELECT answer_id,type,COUNT(*) AS type_count FROM source_relations GROUP BY answer_id,type"
            ).fetchall()
            platform_rows = con.execute(
                """
                SELECT DISTINCT r.answer_id,s.platform
                FROM source_relations r JOIN sources s ON s.id=r.source_id
                """
            ).fetchall()
            creator_rows = con.execute(
                """
                SELECT DISTINCT r.answer_id,s.platform,s.author,s.author_profile_link
                FROM source_relations r JOIN sources s ON s.id=r.source_id
                WHERE trim(COALESCE(s.author,''))<>''
                """
            ).fetchall()

        stats: dict[int, dict[str, Any]] = defaultdict(lambda: {
            "relation_count": 0,
            "source_count": 0,
            "platforms": set(),
            "creators": set(),
            "relation_types": Counter(),
        })
        for row in relation_counts:
            item = stats[row["answer_id"]]
            item["relation_count"] = row["relation_count"]
            item["source_count"] = row["source_count"]
        for row in relation_type_rows:
            stats[row["answer_id"]]["relation_types"][row["type"]] = row["type_count"]
        for row in platform_rows:
            canonical, _ = mapper(row["platform"])
            stats[row["answer_id"]]["platforms"].add(canonical)
        for row in creator_rows:
            canonical, _ = mapper(row["platform"])
            author = str(row["author"] or "").strip()
            stats[row["answer_id"]]["creators"].add(
                self._creator_id(canonical, author, row["author_profile_link"])[0]
            )

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in answer_rows:
            snapshot = dict(row)
            captured_raw, captured_at = self._captured_at(row)
            answer_stats = stats[row["id"]]
            snapshot.update({
                "captured_at_raw": captured_raw,
                "captured_at": captured_at,
                "markdown_available": self._markdown_available(row["markdown_path"]),
                "relation_count": answer_stats["relation_count"],
                "source_count": answer_stats["source_count"],
                "platform_count": len(answer_stats["platforms"]),
                "creator_count": len(answer_stats["creators"]),
                "relation_type_counts": dict(sorted(answer_stats["relation_types"].items())),
            })
            grouped[row["question"]].append(snapshot)

        items = []
        for question, snapshots in grouped.items():
            snapshots.sort(key=lambda item: (item.get("captured_at") is None, item.get("captured_at") or "", item["id"]))
            status_counts = Counter(item["status"] for item in snapshots)
            captured_snapshots = [item for item in snapshots if item.get("captured_at")]
            first_snapshot = captured_snapshots[0] if captured_snapshots else snapshots[0]
            latest_snapshot = captured_snapshots[-1] if captured_snapshots else snapshots[-1]
            items.append({
                "question_id": _id("geo_question", question),
                "question": question,
                "answer_count": len(snapshots),
                "first_captured_at": first_snapshot.get("captured_at"),
                "latest_captured_at": latest_snapshot.get("captured_at"),
                "latest_answer_id": latest_snapshot["id"],
                "status_counts": dict(sorted(status_counts.items())),
                "answers": snapshots,
                "snapshots": snapshots,
            })
        items.sort(key=lambda item: item["question"])
        return items, excluded

    def questions(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = _validate_limit(limit)
        items, excluded = self._question_summaries()
        page = items[offset:offset + limit]
        return scrub({
            "items": page,
            "count": len(page),
            "total": len(items),
            "excluded_answer_count": excluded,
            "limit": limit,
            "offset": offset,
            "source": "geopromax_sqlite",
        })

    def question_detail(self, question_id: str) -> dict[str, Any]:
        summaries, excluded = self._question_summaries()
        summary = next((item for item in summaries if item["question_id"] == question_id), None)
        if summary is None:
            raise NotFoundError("geo_question", question_id)
        question = summary["question"]
        mapper = self._platform_mapper()
        with self.adapter._connect() as con:
            relation_rows = con.execute(
                """
                SELECT r.id AS relation_id,r.answer_id,r.source_id,r.type,r.position,
                       s.title,s.url,s.platform,s.author,s.author_profile_link,s.published_at
                FROM source_relations r
                JOIN answers a ON a.id=r.answer_id
                LEFT JOIN sources s ON s.id=r.source_id
                WHERE a.question=?
                ORDER BY r.answer_id,r.position,r.id
                """,
                (question,),
            ).fetchall()

        snapshots = summary["snapshots"]
        columns = [
            {"answer_id": item["id"], "captured_at": item["captured_at"], "status": item["status"]}
            for item in snapshots
        ]
        column_index = {item["answer_id"]: index for index, item in enumerate(columns)}
        captured_by_answer = {item["id"]: item["captured_at"] for item in snapshots}
        sources: dict[str, dict[str, Any]] = {}
        relation_count = 0
        for row in relation_rows:
            relation_count += 1
            source_id = row["source_id"]
            if not source_id:
                continue
            canonical, mapped = mapper(row["platform"])
            source = sources.setdefault(source_id, {
                "source_id": source_id,
                "title": row["title"],
                "url": row["url"],
                "raw_platform": row["platform"],
                "canonical_platform": canonical,
                "platform_mapped": mapped,
                "author": row["author"],
                "author_profile_link": row["author_profile_link"],
                "published_at": row["published_at"],
                "ranks": [None] * len(columns),
                "relation_types": set(),
                "_hit_answers": set(),
                "_cited_at": [],
            })
            index = column_index[row["answer_id"]]
            rank = row["position"]
            current = source["ranks"][index]
            if rank is not None and (current is None or rank < current):
                source["ranks"][index] = rank
            source["relation_types"].add(row["type"])
            source["_hit_answers"].add(row["answer_id"])
            cited_at = captured_by_answer.get(row["answer_id"])
            if cited_at:
                source["_cited_at"].append(cited_at)

        matrix_rows = []
        for source in sources.values():
            ranks = [rank for rank in source["ranks"] if rank is not None]
            cited_at = sorted(source.pop("_cited_at"))
            hit_answers = source.pop("_hit_answers")
            source["relation_types"] = sorted(source["relation_types"])
            source.update({
                "hit_snapshots": len(hit_answers),
                "best_rank": min(ranks) if ranks else None,
                "first_cited_at": cited_at[0] if cited_at else None,
                "last_cited_at": cited_at[-1] if cited_at else None,
            })
            matrix_rows.append(source)
        matrix_rows.sort(key=lambda item: (item["best_rank"] is None, item["best_rank"] or 0, item["title"] or "", item["source_id"]))
        creator_ids = {
            self._creator_id(item["canonical_platform"], item["author"], item["author_profile_link"])[0]
            for item in matrix_rows if str(item.get("author") or "").strip()
        }
        result = {
            **summary,
            "excluded_answer_count": excluded,
            "citation_matrix": {"columns": columns, "rows": matrix_rows},
            "totals": {
                "snapshot_count": len(snapshots),
                "source_count": len(matrix_rows),
                "platform_count": len({item["canonical_platform"] for item in matrix_rows}),
                "creator_count": len(creator_ids),
                "relation_count": relation_count,
            },
        }
        return scrub(result)

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
            captured_raw, captured_at = self._captured_at(row)
            result["captured_at_raw"] = captured_raw
            result["captured_at"] = captured_at
            tools = [dict(item) for item in con.execute("SELECT * FROM tools WHERE answer_id=? ORDER BY position", (item_id,))]
            result["tools"] = tools
            tool_ids = [item["id"] for item in tools]
            placeholders = ",".join("?" for _ in tool_ids) or "NULL"
            keywords = [dict(item) for item in con.execute(f"SELECT * FROM tool_search_keywords WHERE tool_id IN ({placeholders}) ORDER BY tool_id,position", tuple(tool_ids))]
            result["keywords"] = keywords
            keywords_by_tool: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for keyword in keywords:
                keywords_by_tool[keyword["tool_id"]].append(keyword)
            result["tools_nested"] = [
                {**tool, "search_keywords": keywords_by_tool.get(tool["id"], [])}
                for tool in tools
            ]
            relations = [dict(item) for item in con.execute("SELECT * FROM source_relations WHERE answer_id=? ORDER BY position,id", (item_id,))]
            result["relations"] = relations
            source_ids = sorted({item["source_id"] for item in relations if item["source_id"]})
            placeholders = ",".join("?" for _ in source_ids) or "NULL"
            sources = [dict(item) for item in con.execute(f"SELECT * FROM sources WHERE id IN ({placeholders}) ORDER BY id", tuple(source_ids))]
            mapper = self._platform_mapper()
            markdown_cache: dict[str, dict[str, Any]] = {}
            for source in sources:
                canonical, mapped = mapper(source.get("platform"))
                markdown_path = str(source.get("markdown_path") or "")
                if markdown_path not in markdown_cache:
                    markdown_cache[markdown_path] = self.adapter.markdown(source.get("markdown_path"))
                markdown = markdown_cache[markdown_path]
                source.update({
                    "raw_platform": source.get("platform"),
                    "canonical_platform": canonical,
                    "platform_mapped": mapped,
                    "identity_expected": stable_source_id(str(source.get("platform") or ""), source["url"]),
                    "identity_valid": stable_source_id(str(source.get("platform") or ""), source["url"]) == source["id"],
                    "domain": urlparse(source["url"]).hostname,
                    "markdown_available": markdown.get("exists", False),
                    "file_hash": markdown.get("file_hash"),
                    "content_hash": markdown.get("content_hash") or source.get("content_hash"),
                    "markdown": markdown,
                })
            result["sources"] = sources
            result["suggested_questions"] = [dict(item) for item in con.execute("SELECT * FROM suggested_questions WHERE answer_id=? ORDER BY position", (item_id,))]
            metrics = [dict(item) for item in con.execute("SELECT * FROM source_metrics WHERE answer_id=? ORDER BY source_id", (item_id,))]
            result["metrics"] = metrics
            source_by_id = {source["id"]: source for source in sources}
            metrics_by_source = {metric["source_id"]: metric for metric in metrics}
            metric_keys = ("read_count", "like_count", "comment_count", "favorite_count", "share_count")
            citations = []
            platform_stats: dict[str, dict[str, Any]] = {}
            creator_stats: dict[str, dict[str, Any]] = {}
            for relation in relations:
                source = source_by_id.get(relation.get("source_id"))
                source_metrics = metrics_by_source.get(relation.get("source_id"), {})
                citation = {
                    **relation,
                    "source": source,
                    "metrics": {key: source_metrics.get(key) for key in metric_keys},
                    "metrics_observed_at": captured_at,
                }
                citations.append(citation)
                if not source:
                    continue
                canonical = source["canonical_platform"]
                platform = platform_stats.setdefault(canonical, {
                    "canonical_platform": canonical,
                    "raw_platforms": set(),
                    "relation_count": 0,
                    "sources": set(),
                })
                platform["raw_platforms"].add(source["raw_platform"])
                platform["relation_count"] += 1
                platform["sources"].add(source["id"])
                author = str(source.get("author") or "").strip()
                if author:
                    creator_id = self._creator_id(canonical, author, source.get("author_profile_link"))[0]
                    creator = creator_stats.setdefault(creator_id, {
                        "creator_id": creator_id,
                        "name": author,
                        "profile_url": source.get("author_profile_link"),
                        "canonical_platform": canonical,
                        "raw_platforms": set(),
                        "relation_count": 0,
                        "sources": set(),
                    })
                    creator["raw_platforms"].add(source["raw_platform"])
                    creator["relation_count"] += 1
                    creator["sources"].add(source["id"])
            relation_count = len(relations)
            platform_items = []
            for platform in platform_stats.values():
                platform_items.append({
                    "canonical_platform": platform["canonical_platform"],
                    "raw_platforms": sorted(platform["raw_platforms"]),
                    "relation_count": platform["relation_count"],
                    "source_count": len(platform["sources"]),
                    "share_of_relations": platform["relation_count"] / relation_count if relation_count else 0,
                })
            platform_items.sort(key=lambda item: (-item["relation_count"], item["canonical_platform"]))
            creator_items = []
            for creator in creator_stats.values():
                creator_items.append({
                    "creator_id": creator["creator_id"],
                    "name": creator["name"],
                    "profile_url": creator["profile_url"],
                    "canonical_platform": creator["canonical_platform"],
                    "raw_platforms": sorted(creator["raw_platforms"]),
                    "relation_count": creator["relation_count"],
                    "source_count": len(creator["sources"]),
                })
            creator_items.sort(key=lambda item: (-item["relation_count"], item["name"], item["creator_id"]))
            result["citations"] = citations
            result["citation_type_counts"] = dict(sorted(Counter(item["type"] for item in relations).items()))
            result["platform_summary"] = {
                "denominator": "relation_count",
                "relation_count": relation_count,
                "items": platform_items,
            }
            result["creator_summary"] = {
                "creator_count": len(creator_items),
                "items": creator_items,
            }
            result["metrics_observed_at"] = captured_at
            result["markdown"] = self.adapter.markdown(result.get("markdown_path"))
            return scrub(result)

    def source_overview(
        self,
        *,
        platform: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationAppError("limit 必须是 1–1000 的整数。")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationAppError("offset 必须是非负整数。")
        mapper = self._platform_mapper()
        platform_canonical = mapper(platform)[0] if str(platform or "").strip() else None
        with self.adapter._connect() as con:
            source_rows = [dict(row) for row in con.execute(
                "SELECT id,url,title,platform,author,author_profile_link,published_at FROM sources ORDER BY id"
            )]
            relation_rows = con.execute(
                """
                SELECT r.source_id,r.position,a.question,a.started_at,a.finished_at
                FROM source_relations r
                JOIN answers a ON a.id=r.answer_id
                ORDER BY r.id
                """
            ).fetchall()
            question_count = con.execute(
                "SELECT COUNT(DISTINCT question) FROM answers WHERE trim(COALESCE(question,''))<>''"
            ).fetchone()[0]

        platform_stats: dict[str, dict[str, Any]] = {}
        creator_stats: dict[str, dict[str, Any]] = {}
        source_meta: dict[str, str] = {}
        source_to_creator: dict[str, str] = {}
        for source in source_rows:
            canonical, _ = mapper(source["platform"])
            source_meta[source["id"]] = canonical
            platform_item = platform_stats.setdefault(canonical, {
                "canonical_platform": canonical,
                "raw_platforms": set(),
                "sources": set(),
                "citation_count": 0,
                "questions": set(),
                "creators": set(),
            })
            platform_item["raw_platforms"].add(source["platform"])
            platform_item["sources"].add(source["id"])
            author = str(source.get("author") or "").strip()
            if not author:
                continue
            creator_id = self._creator_id(canonical, author, source.get("author_profile_link"))[0]
            source_to_creator[source["id"]] = creator_id
            platform_item["creators"].add(creator_id)
            creator = creator_stats.setdefault(creator_id, {
                "creator_id": creator_id,
                "canonical_platform": canonical,
                "name": author,
                "profile_url": source.get("author_profile_link"),
                "raw_platforms": set(),
                "sources": set(),
                "citation_count": 0,
                "questions": set(),
                "ranks": [],
                "cited_at": [],
            })
            creator["raw_platforms"].add(source["platform"])
            creator["sources"].add(source["id"])

        for relation in relation_rows:
            source_id = relation["source_id"]
            canonical = source_meta.get(source_id)
            if not canonical:
                continue
            platform_item = platform_stats[canonical]
            platform_item["citation_count"] += 1
            if str(relation["question"] or "").strip():
                platform_item["questions"].add(relation["question"])
            creator_id = source_to_creator.get(source_id)
            if creator_id:
                creator = creator_stats[creator_id]
                creator["citation_count"] += 1
                if str(relation["question"] or "").strip():
                    creator["questions"].add(relation["question"])
                if relation["position"] is not None:
                    creator["ranks"].append(relation["position"])
                captured_at = _utc(relation["finished_at"] or relation["started_at"])
                if captured_at:
                    creator["cited_at"].append(captured_at)

        citation_total = len(relation_rows)
        platforms = [{
            "canonical_platform": item["canonical_platform"],
            "raw_platforms": sorted(item["raw_platforms"]),
            "source_count": len(item["sources"]),
            "citation_count": item["citation_count"],
            "question_count": len(item["questions"]),
            "creator_count": len(item["creators"]),
            "share_of_citations": item["citation_count"] / citation_total if citation_total else 0,
        } for item in platform_stats.values()]
        platforms.sort(key=lambda item: (-item["citation_count"], item["canonical_platform"]))
        if platform_canonical:
            platforms = [item for item in platforms if item["canonical_platform"] == platform_canonical]

        creators = []
        for item in creator_stats.values():
            cited_at = sorted(item["cited_at"])
            creators.append({
                "creator_id": item["creator_id"],
                "canonical_platform": item["canonical_platform"],
                "name": item["name"],
                "profile_url": item["profile_url"],
                "raw_platforms": sorted(item["raw_platforms"]),
                "source_count": len(item["sources"]),
                "citation_count": item["citation_count"],
                "question_count": len(item["questions"]),
                "best_rank": min(item["ranks"]) if item["ranks"] else None,
                "first_cited_at": cited_at[0] if cited_at else None,
                "last_cited_at": cited_at[-1] if cited_at else None,
            })
        if platform_canonical:
            creators = [item for item in creators if item["canonical_platform"] == platform_canonical]
        query = str(q or "").strip().casefold()
        if query:
            directly_matched_platforms = {
                item["canonical_platform"]
                for item in platforms
                if query in item["canonical_platform"].casefold()
                or any(query in raw.casefold() for raw in item["raw_platforms"])
            }
            directly_matched_creators = {
                item["creator_id"]
                for item in creators
                if query in item["canonical_platform"].casefold()
                or any(query in raw.casefold() for raw in item["raw_platforms"])
                or query in item["name"].casefold()
                or query in str(item["profile_url"] or "").casefold()
            }
            creator_platforms = {
                item["canonical_platform"]
                for item in creators if item["creator_id"] in directly_matched_creators
            }
            visible_platforms = directly_matched_platforms | creator_platforms
            platforms = [
                item for item in platforms
                if item["canonical_platform"] in visible_platforms
            ]
            creators = [
                item for item in creators
                if item["creator_id"] in directly_matched_creators
                or item["canonical_platform"] in directly_matched_platforms
            ]
        creators.sort(key=lambda item: (-item["citation_count"], item["name"], item["creator_id"]))
        total = len(creators)
        page = creators[offset:offset + limit]
        return scrub({
            "totals": {
                "identifier_count": len(source_rows),
                "source_count": len(source_rows),
                "source_row_count": len(source_rows),
                "url_count": len({item["url"] for item in source_rows}),
                "distinct_url_count": len({item["url"] for item in source_rows}),
                "citation_count": citation_total,
                "question_count": question_count,
                "creator_count": len(creator_stats),
                "author_count": len(creator_stats),
            },
            "platforms": platforms,
            "creators": page,
            "total": total,
            "count": len(page),
            "limit": limit,
            "offset": offset,
            "filters": {"platform": platform, "platform_canonical": platform_canonical, "q": q},
            "search_fields": [
                "canonical_platform", "raw_platforms", "creator_name", "creator_profile_url",
            ],
            "source": "geopromax_sqlite",
        })

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
            answer["markdown_path"] = _relative_source_path(answer.get("markdown_path"), self.settings.geo_source_root)
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
            source["markdown_path"] = _relative_source_path(source.get("markdown_path"), self.settings.geo_source_root)
            if source.get("markdown_path"):
                read_markdown(source["markdown_path"])
        for source in data["sources"]:
            source["identity_expected"] = source_by_id[source["id"]]["identity_expected"]
        for batch in data["batches"]:
            for key in ("raw_file", "input_file", "output_file"):
                batch[key] = _relative_source_path(batch.get(key), self.settings.geo_source_root)
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
            "refresh": self._refresh_state(),
            "redfox": self._redfox_summary(),
        }
        data["_markdown_cache"] = markdown_cache
        data["_file_manifest"] = file_manifest
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
                    # 每一次真实导入都冻结来源清单；对外只保存相对标签和哈希，
                    # 不把本机绝对路径泄露到 Hub/API/审计。
                    manifest_entries = list(data.get("_file_manifest") or [])
                    con.execute(
                        """
                        INSERT INTO source_manifests(
                            manifest_id,system_key,source_kind,root_fingerprint,manifest_hash,
                            entry_count,captured_at,immutable,payload_json
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(manifest_id) DO NOTHING
                        """,
                        (
                            manifest_id,
                            "geo",
                            "geopromax_sqlite",
                            _sorted_hash({"adapter": self.adapter.adapter_key, "root_name": Path(self.settings.geo_source_root).name}),
                            manifest_id,
                            len(manifest_entries),
                            now,
                            1,
                            _json({"adapter": self.adapter.adapter_key, "lineage": "sqlite"}),
                        ),
                    )
                    for index, item in enumerate(manifest_entries):
                        raw_path = str(item.get("path") or "")
                        label = "database" if raw_path == str(self.settings.geo_database_path) else (
                            "platform_rules" if raw_path == str(self.settings.geo_platforms_path) else
                            _relative_source_path(raw_path, self.settings.geo_source_root) or f"unmapped/{index}"
                        )
                        con.execute(
                            """
                            INSERT INTO source_manifest_entries(
                                manifest_id,relative_path,content_hash,size_bytes,observed_at,payload_json
                            ) VALUES(?,?,?,?,?,?)
                            ON CONFLICT(manifest_id,relative_path) DO NOTHING
                            """,
                            (
                                manifest_id,
                                label,
                                item.get("sha256"),
                                item.get("size"),
                                now,
                                _json({"error": item.get("error")} if item.get("error") else {}),
                            ),
                        )
                    con.execute(
                        "INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,records_seen,records_written,records_failed,source_ref,error_json,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, self.adapter.adapter_key, manifest_id, "running", now, len(data["answers"]), 0, 0, f"geopromax:{manifest_id}", _json(preview["rejected"]), _json(preview)),
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
                        con.execute("INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)", (_id("audit", batch_id), now, "workbench", "geo_import", "ingestion_batch", batch_id, "failed" if status != "succeeded" else "succeeded", _json({"adapter": self.adapter.adapter_key, "manifest_id": manifest_id, "status": status, "rejected": preview["rejected"], "conflicts": conflicts, "deduplications": deduplications})))
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
        return scrub({
            "dry_run": True,
            "refreshable": False,
            **self._refresh_state(),
            "batch_refresh": False,
            "answer": {"id": answer_id, "question": detail.get("question")},
        })

    def refresh_confirm(self, answer_id: int, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ConflictError("RedFox 刷新必须显式 confirm=true。")
        raise ConflictError("RedFox 正式刷新未接入；为避免付费调用，本入口仅保留保护闸门。")

    def redfox_read_only(self) -> dict[str, Any]:
        return scrub(self._redfox_summary())
