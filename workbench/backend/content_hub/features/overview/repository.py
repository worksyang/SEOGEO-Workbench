from __future__ import annotations

import sqlite3

COUNT_TABLES = {
    "contents": "contents",
    "creators": "creators",
    "snapshots": "search_snapshots",
    "observations": "metric_observations",
    "geo_answers": "geo_answers",
    "signals": "signals",
    "jobs": "production_jobs",
}


class OverviewRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, table in COUNT_TABLES.items():
            result[key] = int(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        return result

    def systems(self) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT system_key, display_name, status, last_checked_at
                FROM system_connections
                ORDER BY display_name
                """
            )
        ]
