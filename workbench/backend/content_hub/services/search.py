"""搜索监控服务：关键词列表 / 关键词详情 / 快照时间线 / 排名命中 / 触发刷新。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .helpers import as_int, parse_json_field, rows_to_dicts


class SearchService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def keywords(self, *, platform: str | None = None, limit: int = 200) -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if platform:
            where = "WHERE k.platform = ?"
            params.append(platform)
        sql = f"""
            SELECT k.keyword_id, k.keyword_text, k.platform, k.lifecycle_stage,
                   k.topic, k.refresh_frequency_days, k.last_seen_at,
                   COUNT(DISTINCT s.snapshot_id) AS snapshot_count,
                   COUNT(h.hit_id) AS hit_count
            FROM keywords k
            LEFT JOIN search_snapshots s ON s.keyword_id = k.keyword_id
            LEFT JOIN search_hits h ON h.snapshot_id = s.snapshot_id
            {where}
            GROUP BY k.keyword_id
            ORDER BY snapshot_count DESC, k.keyword_text
            LIMIT ?
        """
        params.append(limit)
        rows = rows_to_dicts(self._conn, sql, params)
        return {"items": rows, "total": len(rows)}

    def keyword_detail(self, keyword_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM keywords WHERE keyword_id=?",
            (keyword_id,),
        ).fetchone()
        if not row:
            return None
        info = dict(row)
        info["payload"] = parse_json_field(info.pop("payload_json"), {})
        snapshots = rows_to_dicts(
            self._conn,
            "SELECT snapshot_id, platform, keyword, captured_at, trigger_type, result_count "
            "FROM search_snapshots WHERE keyword_id=? ORDER BY captured_at DESC LIMIT 80",
            (keyword_id,),
        )
        for snap in snapshots:
            snap["features"] = parse_json_field(snap.pop("features_json"), {})
            snap["payload"] = parse_json_field(snap.pop("payload_json"), {})
        terms = rows_to_dicts(
            self._conn,
            """
            SELECT t.term_id, t.term_type, t.position, t.term_text, t.snapshot_id
            FROM search_terms t JOIN search_snapshots s ON s.snapshot_id = t.snapshot_id
            WHERE s.keyword_id=?
            ORDER BY s.captured_at DESC, t.position
            LIMIT 200
            """,
            (keyword_id,),
        )
        return {"keyword": info, "snapshots": snapshots, "terms": terms}

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        snap = self._conn.execute(
            "SELECT * FROM search_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if not snap:
            return None
        result = dict(snap)
        result["features"] = parse_json_field(result.pop("features_json"), {})
        result["payload"] = parse_json_field(result.pop("payload_json"), {})
        hits = rows_to_dicts(
            self._conn,
            "SELECT * FROM search_hits WHERE snapshot_id=? ORDER BY rank",
            (snapshot_id,),
        )
        for hit in hits:
            hit["payload"] = parse_json_field(hit.pop("payload_json"), {})
        terms = rows_to_dicts(
            self._conn,
            "SELECT term_id, term_type, position, term_text FROM search_terms WHERE snapshot_id=? ORDER BY position",
            (snapshot_id,),
        )
        return {"snapshot": result, "hits": hits, "terms": terms}

    def latest_snapshot_for_keyword(self, keyword_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT snapshot_id FROM search_snapshots WHERE keyword_id=? "
            "ORDER BY captured_at DESC LIMIT 1",
            (keyword_id,),
        ).fetchone()
        if not row:
            return None
        return self.snapshot(row["snapshot_id"])

    def status(self) -> dict[str, Any]:
        counts = self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM keywords) AS keywords,
                (SELECT COUNT(*) FROM search_snapshots) AS snapshots,
                (SELECT COUNT(*) FROM search_hits) AS hits,
                (SELECT COUNT(DISTINCT platform) FROM search_snapshots) AS platforms,
                (SELECT MAX(captured_at) FROM search_snapshots) AS latest_captured_at
            """
        ).fetchone()
        return dict(counts)
