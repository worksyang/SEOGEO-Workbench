"""GEO 观察服务：问题列表 / 回答快照 / 来源透视 / 引用位次 / 单一刷新入口。"""
from __future__ import annotations

import sqlite3
from typing import Any

from .helpers import parse_json_field, rows_to_dicts


class GeoService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def questions(self, *, limit: int = 100, query: str | None = None) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if query:
            where = "WHERE a.question_raw LIKE ?"
            params.append(f"%{query}%")
        sql = f"""
            SELECT a.question_raw AS question,
                   MIN(a.captured_at) AS first_captured_at,
                   MAX(a.captured_at) AS latest_captured_at,
                   COUNT(a.answer_id) AS answer_count,
                   COUNT(DISTINCT r.source_content_id) AS source_count
            FROM geo_answers a
            LEFT JOIN geo_source_relations r ON r.answer_id = a.answer_id
            {where}
            GROUP BY a.question_raw
            ORDER BY latest_captured_at DESC
            LIMIT ?
        """
        params.append(limit)
        items = rows_to_dicts(self._conn, sql, params)
        return {"items": items, "total": len(items)}

    def answers(self, *, question: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self._conn,
            "SELECT answer_id, app, mode, question_raw, captured_at, answer_hash, source_ref, "
            "tools_json, recommended_json, payload_json FROM geo_answers "
            "WHERE question_raw=? ORDER BY captured_at DESC LIMIT ?",
            (question, limit),
        )
        for row in rows:
            row["tools"] = parse_json_field(row.pop("tools_json"), [])
            row["recommended"] = parse_json_field(row.pop("recommended_json"), [])
            row["payload"] = parse_json_field(row.pop("payload_json"), {})
        return rows

    def answer(self, answer_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM geo_answers WHERE answer_id=?",
            (answer_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["tools"] = parse_json_field(result.pop("tools_json"), [])
        result["recommended"] = parse_json_field(result.pop("recommended_json"), [])
        result["payload"] = parse_json_field(result.pop("payload_json"), {})
        relations = rows_to_dicts(
            self._conn,
            "SELECT * FROM geo_source_relations WHERE answer_id=? ORDER BY relation_type, position",
            (answer_id,),
        )
        for rel in relations:
            rel["payload"] = parse_json_field(rel.pop("payload_json"), {})
        result["relations"] = relations
        return result

    def source_overview(self, *, query: str | None = None, limit: int = 60) -> dict[str, Any]:
        platforms = rows_to_dicts(
            self._conn,
            """
            SELECT r.canonical_platform AS platform,
                   COUNT(DISTINCT r.relation_id) AS relation_count,
                   COUNT(DISTINCT r.answer_id) AS answer_count
            FROM (
                SELECT relation_id, answer_id,
                       json_extract(payload_json, '$.platform_canonical') AS canonical_platform
                FROM geo_source_relations
            ) r
            GROUP BY r.canonical_platform
            ORDER BY relation_count DESC
            LIMIT ?
            """,
            (limit,),
        )
        creators = rows_to_dicts(
            self._conn,
            """
            SELECT json_extract(payload_json, '$.author_name') AS author,
                   COUNT(DISTINCT relation_id) AS relation_count
            FROM geo_source_relations
            WHERE json_extract(payload_json, '$.author_name') IS NOT NULL
            GROUP BY author
            ORDER BY relation_count DESC
            LIMIT ?
            """,
            (limit,),
        )
        if query:
            needle = query.lower()
            platforms = [item for item in platforms if needle in str(item).lower()]
            creators = [item for item in creators if needle in str(item).lower()]
        return {
            "platforms": platforms,
            "creators": creators,
            "totals": {
                "platforms": len(platforms),
                "creators": len(creators),
            },
        }
