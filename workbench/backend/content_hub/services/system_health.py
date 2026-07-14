"""系统状态服务：连接探测 / Schema 版本 / Hub 数据规模。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..domain.ids import generate_ulid_like
from ..validation.timestamps import utc_now_iso


class SystemHealthService:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def status(self) -> dict[str, Any]:
        integrity = self._conn.execute("PRAGMA integrity_check").fetchone()
        integrity_text = integrity[0] if integrity else "unknown"
        version_row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        schema_version = int(version_row["v"] if version_row else 0)
        pragmas = self._conn.execute(
            "SELECT journal_mode, foreign_keys, busy_timeout FROM pragma_journal_mode, pragma_foreign_keys, pragma_busy_timeout"
        ).fetchone()
        pragmas_dict = dict(pragmas) if pragmas else {}
        connections = [
            dict(row)
            for row in self._conn.execute(
                "SELECT system_key, display_name, base_url, status, capabilities_json, details_json, last_checked_at "
                "FROM system_connections ORDER BY system_key"
            ).fetchall()
        ]
        for conn in connections:
            try:
                conn["capabilities"] = __import__("json").loads(conn.pop("capabilities_json") or "[]")
            except Exception:
                conn["capabilities"] = []
            try:
                conn["details"] = __import__("json").loads(conn.pop("details_json") or "{}")
            except Exception:
                conn["details"] = {}
        return {
            "database": {
                "status": "healthy" if integrity_text == "ok" else "degraded",
                "integrity": integrity_text,
                "schema_version": schema_version,
                "pragmas": pragmas_dict,
            },
            "connections": connections,
        }

    def counts(self) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM contents) AS contents,
                (SELECT COUNT(*) FROM creators) AS creators,
                (SELECT COUNT(*) FROM content_discoveries) AS discoveries,
                (SELECT COUNT(*) FROM search_snapshots) AS snapshots,
                (SELECT COUNT(*) FROM search_hits) AS hits,
                (SELECT COUNT(*) FROM geo_answers) AS geo_answers,
                (SELECT COUNT(*) FROM metric_observations) AS observations,
                (SELECT COUNT(*) FROM comments) AS comments,
                (SELECT COUNT(*) FROM production_jobs) AS jobs,
                (SELECT COUNT(*) FROM signals) AS signals
            """
        ).fetchone()
        return dict(row)

    def record_connection_status(self, system_key: str, *, status: str, latency_ms: int = 0, error: str = "") -> None:
        self._conn.execute(
            """
            UPDATE system_connections SET status=?, latency_ms=?, last_error=?, last_checked_at=?
            WHERE system_key=?
            """,
            (status, latency_ms, error, utc_now_iso(), system_key),
        )
