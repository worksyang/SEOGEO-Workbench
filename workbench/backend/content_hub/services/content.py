"""内容服务：内容列表 / 详情 / 发现链路 / 指标曲线 / Markdown 渲染。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..ingestion.markdown_store import MarkdownStore
from .helpers import parse_json_field, rows_to_dicts
from .safety import public_asset_ref, scrub_public_payload


class ContentService:
    def __init__(self, connection: sqlite3.Connection, markdown_store: MarkdownStore):
        self._conn = connection
        self._markdown = markdown_store

    def list(
        self,
        *,
        content_type: str | None = None,
        limit: int = 30,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        sql = [
            "SELECT content_id, content_type, title, canonical_url, author_name, published_at, updated_at",
            "FROM contents WHERE 1=1",
        ]
        params: list[Any] = []
        if content_type:
            sql.append("AND content_type = ?")
            params.append(content_type)
        if query:
            sql.append("AND (title LIKE ? OR canonical_url LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle])
        sql.append("ORDER BY updated_at DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        rows = rows_to_dicts(self._conn, " ".join(sql), params)
        count_params: list[Any] = []
        count_sql = "SELECT COUNT(*) AS n FROM contents WHERE 1=1"
        if content_type:
            count_sql += " AND content_type = ?"
            count_params.append(content_type)
        if query:
            count_sql += " AND (title LIKE ? OR canonical_url LIKE ?)"
            count_params.extend([needle, needle])
        row = self._conn.execute(count_sql, count_params).fetchone()
        total = row["n"] if row else 0
        return {"total": total, "items": rows}

    def detail(self, content_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM contents WHERE content_id=?",
            (content_id,),
        ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["entities"] = parse_json_field(record.pop("entities_json"), [])
        record["intents"] = parse_json_field(record.pop("intents_json"), [])
        record["payload"] = scrub_public_payload(
            parse_json_field(record.pop("payload_json"), {}),
            asset_root=self._markdown.root,
        )
        if record.get("md_path"):
            record["md_path"] = public_asset_ref(record["md_path"], self._markdown.root)
            if not record["md_path"]:
                record.pop("md_path", None)
        return record

    def context(self, content_id: str) -> dict[str, Any] | None:
        record = self.detail(content_id)
        if not record:
            return None
        identifiers = rows_to_dicts(
            self._conn,
            "SELECT namespace, external_id, first_seen_at FROM content_identifiers WHERE content_id=?",
            (content_id,),
        )
        discoveries = rows_to_dicts(
            self._conn,
            "SELECT discovery_system, discovery_channel, discovered_at, snapshot_id "
            "FROM content_discoveries WHERE content_id=? ORDER BY discovered_at DESC",
            (content_id,),
        )
        snapshots = rows_to_dicts(
            self._conn,
            "SELECT s.snapshot_id, s.platform, s.keyword, s.captured_at, s.trigger_type "
            "FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id "
            "WHERE h.content_id=? ORDER BY s.captured_at DESC LIMIT 50",
            (content_id,),
        )
        geo_relations = rows_to_dicts(
            self._conn,
            "SELECT a.answer_id, a.app, a.captured_at, r.relation_type, r.position "
            "FROM geo_source_relations r JOIN geo_answers a ON a.answer_id=r.answer_id "
            "WHERE r.source_content_id=? ORDER BY a.captured_at DESC LIMIT 50",
            (content_id,),
        )
        comments = rows_to_dicts(
            self._conn,
            "SELECT comment_id, platform, author_name, text_raw, current_visibility, last_seen_at "
            "FROM comments WHERE content_id=? ORDER BY last_seen_at DESC LIMIT 30",
            (content_id,),
        )
        return {
            "content": record,
            "identifiers": identifiers,
            "discoveries": discoveries,
            "snapshots": snapshots,
            "geo_relations": geo_relations,
            "comments": comments,
        }

    def metrics(self, content_id: str, limit_per_metric: int = 200) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self._conn,
            "SELECT metric_key, observed_at, numeric_value, text_value, snapshot_id, confidence "
            "FROM metric_observations WHERE subject_type='content' AND subject_id=? "
            "ORDER BY observed_at DESC LIMIT ?",
            (content_id, limit_per_metric * 6),
        )
        bucket: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            bucket.setdefault(row["metric_key"], []).append(row)
        out: list[dict[str, Any]] = []
        for key, items in bucket.items():
            items.sort(key=lambda r: r["observed_at"])
            out.append({"metric_key": key, "observations": items[-limit_per_metric:]})
        return out

    def markdown(self, content_id: str) -> dict[str, Any] | None:
        record = self.detail(content_id)
        if not record:
            return None
        md_path = record.get("md_path") or ""
        if not md_path:
            return {
                "content_id": content_id,
                "available": False,
                "reason": "此内容未写入 asset_store，仅展示元数据。",
            }
        if not self._markdown.exists(md_path):
            return {
                "content_id": content_id,
                "available": False,
                "reason": "Markdown 文件丢失，可能被外部清理，等待对账补回。",
                "asset_ref": md_path,
            }
        text = self._markdown.read(md_path)
        return {
            "content_id": content_id,
            "available": True,
            "asset_ref": md_path,
            "md_path": md_path,
            "body": text,
        }
