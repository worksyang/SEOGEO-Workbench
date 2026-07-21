"""API 端到端：使用 httpx + ASGITransport 直接驱动 ASGI。
对应矩阵 T166-T180 浏览器 / 系统层。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.migrations import migrate


@pytest.fixture
def client(tmp_path: Path):
    db = tmp_path / "h.sqlite"
    settings = Settings.load()
    from dataclasses import replace
    settings = replace(
        settings,
        database_path=db,
        asset_store_path=tmp_path / "asset_store",
        host="127.0.0.1",
        port=18799,
    )
    migrate(settings)
    app = create_app(settings)

    async def factory() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield factory


def _get(factory, path):
    async def _run():
        async with await factory() as c:
            return await c.get(path)
    return asyncio.run(_run())


def _post(factory, path, json=None):
    async def _run():
        async with await factory() as c:
            return await c.post(path, json=json)
    return asyncio.run(_run())


def _put(factory, path, json=None):
    async def _run():
        async with await factory() as c:
            return await c.put(path, json=json)
    return asyncio.run(_run())


def test_t166_health_endpoint_ok(client):
    r = _get(client, "/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_t167_ready_endpoint_keeps_database_state(client):
    r = _get(client, "/ready")
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True


def test_t168_overview_endpoint_returns_counts(client):
    r = _get(client, "/api/v1/overview")
    assert r.status_code == 200
    data = r.json().get("data", {})
    assert isinstance(data, dict)


def test_t169_system_status_endpoint_health(client):
    r = _get(client, "/api/v1/system/status")
    body = r.json()
    assert body["data"]["database"]["status"] == "healthy"


def test_t170_signals_endpoint_no_auth_required(client):
    r = _get(client, "/api/v1/signals?limit=10")
    assert r.status_code == 200
    assert "items" in r.json()["data"]


def test_t171_contents_list_paginates(client):
    r = _get(client, "/api/v1/contents?limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "items" in data
    assert isinstance(data["items"], list)


def test_t172_contents_detail_404_for_unknown(client):
    r = _get(client, "/api/v1/contents/cnt_definitely_does_not_exist")
    assert r.status_code == 404


def test_migration_switches_require_confirmation_and_leave_audit(client):
    blocked = _put(
        client,
        "/api/v1/governance/switches/wechat-search/keyword-detail",
        json={"data_mode": "hub"},
    )
    assert blocked.status_code == 409
    switched = _put(
        client,
        "/api/v1/governance/switches/wechat-search/keyword-detail",
        json={
            "data_mode": "compare",
            "rollback_mode": "legacy",
            "operator": "test",
            "reason": "contract verification",
        },
    )
    assert switched.status_code == 200
    listed = _get(client, "/api/v1/governance/switches")
    assert listed.status_code == 200
    item = next(
        item
        for item in listed.json()["data"]["items"]
        if item["module_key"] == "wechat-search"
        and item["contract_key"] == "keyword-detail"
    )
    assert item["data_mode"] == "compare"
    compared = _post(
        client,
        "/api/v1/governance/comparisons",
        json={
            "module_key": "wechat-search",
            "contract_key": "keyword-detail",
            "request_fingerprint": "req-1",
            "legacy_hash": "same",
            "hub_hash": "same",
        },
    )
    assert compared.status_code == 200
    assert compared.json()["data"]["status"] == "matched"
    listed = _get(
        client,
        "/api/v1/governance/comparisons?module_key=wechat-search&status=matched"
        "&since=2026-01-01T00:00:00Z&until=2027-01-01T00:00:00Z&limit=1&offset=0",
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] >= 1
    comparison_id = listed.json()["data"]["items"][0]["comparison_id"]
    detail = _get(client, f"/api/v1/governance/comparisons/{comparison_id}")
    assert detail.status_code == 200
    assert isinstance(detail.json()["data"]["comparison"]["summary"], dict)
    assert "diffs" in detail.json()["data"]


def test_wechat_atomic_cutover_preflight_and_switch(client):
    preflight = _post(
        client,
        "/api/v1/governance/switches/wechat-search/cutover",
        json={
            "data_mode": "compare",
            "expected_mode": "legacy",
            "dry_run": True,
            "actor": "governance-smoke",
            "reason": "verify all read contracts before compare",
        },
    )
    assert preflight.status_code == 200
    assert preflight.json()["data"]["changed_count"] == 0
    assert preflight.json()["data"]["would_change_count"] == 22

    switched = _post(
        client,
        "/api/v1/governance/switches/wechat-search/cutover",
        json={
            "data_mode": "compare",
            "expected_mode": "legacy",
            "actor": "governance-smoke",
            "reason": "enter compare as one transaction",
        },
    )
    assert switched.status_code == 200
    assert switched.json()["data"]["changed_count"] == 22
    assert switched.json()["data"]["previous_mode"] == "legacy"
















def test_t180_governance_reconcile_returns_total_count(client):
    r = _get(client, "/api/v1/governance/reconcile")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "total" in data
    assert "results" in data
    assert isinstance(data["results"], list)










def test_governance_backup_endpoints_show_verified_state_and_isolated_drill(client):
    created = _post(client, "/api/v1/governance/backups", json={"label": "api"})
    assert created.status_code == 200
    backup = created.json()["data"]["backup"]
    assert backup["verifiable"] is True

    listed = _get(client, "/api/v1/governance/backups")
    assert listed.status_code == 200
    assert listed.json()["data"]["verifiable"] >= 1

    drilled = _post(
        client,
        f"/api/v1/governance/backups/{backup['name']}/restore-drill",
        json={"operator": "test"},
    )
    assert drilled.status_code == 200
    data = drilled.json()["data"]
    assert data["runtime_database_unchanged"] is True
    assert data["integrity"] == "ok"


def test_governance_backup_endpoint_rejects_path_traversal(client):
    response = _post(client, "/api/v1/governance/backups/..%2Fhub.sqlite/restore-drill")
    assert response.status_code in {404, 409}
