from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from content_hub.db.connection import connect

router = APIRouter(tags=["system"])

CORE_TABLES = (
    "contents",
    "creators",
    "content_identifiers",
    "content_discoveries",
    "search_snapshots",
    "search_hits",
    "metric_definitions",
    "metric_observations",
    "comments",
    "comment_events",
    "geo_answers",
    "geo_source_relations",
    "signals",
    "production_jobs",
)


def _database_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    try:
        with connect(settings, readonly=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            found = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        missing = sorted(set(CORE_TABLES) - found)
        return {
            "status": "healthy" if integrity == "ok" and not missing else "degraded",
            "integrity": integrity,
            "schema_version": version,
            "missing_core_tables": missing,
        }
    except (OSError, sqlite3.Error) as exc:
        return {"status": "offline", "error": str(exc)}


@router.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "data": {"status": "ok", "service": "全域内容工作台"}}


@router.get("/ready")
def ready(request: Request) -> dict[str, object]:
    database = _database_status(request)
    ok = database["status"] == "healthy"
    return {
        "ok": ok,
        "data": {
            "status": "ready" if ok else "degraded",
            "checks": {"database": database},
        },
    }


@router.get("/api/v1/system/status")
def system_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    database = _database_status(request)
    with connect(settings, readonly=True) as connection:
        connections = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    system_key,
                    display_name,
                    base_url,
                    status,
                    last_checked_at,
                    capabilities_json,
                    details_json
                FROM system_connections
                ORDER BY system_key
                """
            )
        ]
    for connection in connections:
        try:
            connection["capabilities"] = json.loads(connection.pop("capabilities_json") or "[]")
        except (TypeError, ValueError):
            connection["capabilities"] = []
        try:
            connection["details"] = json.loads(connection.pop("details_json") or "{}")
        except (TypeError, ValueError):
            connection["details"] = {}
    return {
        "ok": database["status"] != "offline",
        "data": {
            "service": {
                "name": "全域内容工作台",
                "version": request.app.version,
                "bind": f"{settings.host}:{settings.port}",
                "frontend_built": (settings.frontend_dist / "index.html").is_file(),
            },
            "database": database,
            "connections": connections,
            "readonly_contract": {
                "source": Path(settings.project_root / "source").exists(),
                "demo": Path(
                    settings.project_root / "unified-content-platform-demo.html"
                ).exists(),
            },
        },
    }
