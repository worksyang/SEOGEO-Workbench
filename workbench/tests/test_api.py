from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from content_hub.app import create_app
from content_hub.db.connection import connect


def with_client(settings, assertion: Callable[[httpx.AsyncClient], Awaitable[None]]) -> None:
    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                await assertion(client)

    asyncio.run(run())


def test_health_ready_and_overview(settings) -> None:
    async def assertion(client: httpx.AsyncClient) -> None:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.headers["x-content-type-options"] == "nosniff"

        ready = await client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["data"]["status"] == "ready"

        overview = await client.get("/api/v1/overview")
        assert overview.status_code == 200
        assert overview.json()["data"]["data_state"] == "empty"
        assert overview.json()["data"]["counts"]["contents"] == 0

    with_client(settings, assertion)


def test_system_status_reports_real_contract(settings) -> None:
    async def assertion(client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/system/status")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["database"]["status"] == "healthy"
        # The API field is retained for compatibility, but its value is the
        # SQLite migration level, not the v3.3 architecture revision.
        with connect(settings, readonly=True) as connection:
            applied_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        assert data["database"]["schema_version"] == applied_version
        assert len(data["connections"]) == 7
        assert {item["status"] for item in data["connections"]} == {"unknown"}
        assert all(isinstance(item["capabilities"], list) for item in data["connections"])
        assert all(isinstance(item["details"], dict) for item in data["connections"])
        assert data["service"]["frontend_built"] is False

    with_client(settings, assertion)


def test_missing_frontend_returns_clear_error(settings) -> None:
    async def assertion(client: httpx.AsyncClient) -> None:
        response = await client.get("/")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "FRONTEND_NOT_BUILT"

    with_client(settings, assertion)
