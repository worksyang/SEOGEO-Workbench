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


def test_t173_wiki_tree_returns_list(client):
    r = _get(client, "/api/v1/wiki/tree")
    assert r.status_code == 200
    assert isinstance(r.json()["data"], list)


def test_t174_wiki_search_supports_empty_query(client):
    r = _get(client, "/api/v1/wiki/search?query=")
    assert r.status_code == 200


def test_t175_publishing_accounts_list_returns_one_demo(client):
    r = _get(client, "/api/v1/publishing/accounts")
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert len(items) >= 1
    for acct in items:
        for key in ("cookie_blob", "token_blob", "raw_cookie", "raw_token"):
            assert key not in acct


def test_t176_publishing_preview_with_default_body(client):
    r = _post(client, "/api/v1/publishing/preview", json={"content_id": "t176", "body": "# 预览\n\n内容"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert "<h1>" in body["html"]


def test_t177_publishing_publish_requires_confirm(client):
    r = _post(client, "/api/v1/publishing/publish", json={"account_id": "demo", "content_id": "t177", "body": "body"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["status"] == "needs_confirmation"


def test_t178_writing_jobs_endpoint_returns_payload(client):
    r = _get(client, "/api/v1/writing/jobs")
    assert r.status_code == 200
    assert "items" in r.json()["data"]


def test_t179_governance_states_returns_columns_with_status(client):
    r = _get(client, "/api/v1/governance/states")
    assert r.status_code == 200
    assert "columns" in r.json()["data"]


def test_t180_governance_reconcile_returns_total_count(client):
    r = _get(client, "/api/v1/governance/reconcile")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "total" in data
    assert "results" in data
    assert isinstance(data["results"], list)


def test_writing_router_runs_default_fake_provider_and_persists_artifact(client):
    created = _post(
        client,
        "/api/v1/writing/jobs",
        json={
            "mode": "mother_forge",
            "topic": "API Fake Provider 自检",
            "purpose": "验证任务提交、运行回执与产物登记",
        },
    )
    assert created.status_code == 200
    payload = created.json()["data"]
    assert payload["provider_kind"] == "fake"
    assert payload["demo"] is True

    ran = _post(client, f"/api/v1/writing/jobs/{payload['job_id']}/run")
    assert ran.status_code == 200
    result = ran.json()["data"]
    assert result["status"] == "demo_only"
    assert result["demo"] is True
    assert result["asset_ref"].startswith("generated/")

    detail = _get(client, f"/api/v1/writing/jobs/{payload['job_id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "blocked"

    listed = _get(client, "/api/v1/writing/jobs")
    assert listed.status_code == 200
    assert any(item["job_id"] == payload["job_id"] for item in listed.json()["data"]["items"])


def test_publishing_router_draft_dry_run_and_attempt_history_are_replayable(client):
    draft = _post(
        client,
        "/api/v1/publishing/draft",
        json={"account_id": "demo", "content_id": "api-draft", "body": "# 草稿\n\n正文"},
    )
    assert draft.status_code == 200
    assert draft.json()["data"]["status"] == "draft_only"

    dry_run = _post(
        client,
        "/api/v1/publishing/dry-run",
        json={"account_id": "demo", "content_id": "api-preview", "body": "# 预览\n\n正文"},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["data"]["status"] == "dry_run_only"

    attempts = _get(client, "/api/v1/publishing/attempts")
    assert attempts.status_code == 200
    modes = {item["mode"] for item in attempts.json()["data"]["items"]}
    assert {"draft", "dry_run"} <= modes


def test_writing_mutations_persist_legacy_island_state_and_events(client):
    created = _post(
        client,
        "/api/v1/writing/jobs",
        json={"mode": "mother_forge", "topic": "持久化岛屿", "purpose": "原始目的"},
    )
    job_id = created.json()["data"]["job_id"]

    purpose = _post(
        client,
        f"/api/v1/writing/jobs/{job_id}/mutate",
        json={"action": "purpose", "value": {"purpose": "修改后的目的"}},
    )
    assert purpose.status_code == 200

    url = _post(
        client,
        f"/api/v1/writing/jobs/{job_id}/mutate",
        json={
            "action": "add_url",
            "value": {"url": "https://mp.weixin.qq.com/s/example", "note": "客户资料"},
        },
    )
    assert url.status_code == 200
    detail = _get(client, f"/api/v1/writing/jobs/{job_id}").json()["data"]
    assert detail["payload"]["purpose"] == "修改后的目的"
    assert detail["payload"]["url_materials"][0]["parseStatus"] == "received"
    event_ids = [event["event_id"] for event in detail["events"]]
    assert len(event_ids) == len(set(event_ids))


def test_writing_batch_queue_mutations_are_replayable(client):
    created = _post(
        client,
        "/api/v1/writing/jobs",
        json={
            "mode": "batch_production",
            "topic": "持久批次",
            "source": "manual",
            "requirements": {"brief": "批次测试"},
            "keywords": ["关键词 A"],
            "target_article_count": 1,
        },
    )
    job_id = created.json()["data"]["job_id"]
    state = {
        "name": "持久批次",
        "source": "manual",
        "brief": "批次测试",
        "output_dir": "Hub/test/",
        "stage": "batch-config",
        "status": "pending",
        "keywords": [
            {
                "id": "kw-1",
                "keyword": "关键词 A",
                "purpose": "测试目的",
                "count": 2,
                "recommendedCount": 2,
                "readiness": "ready",
                "motherMatches": [{"motherId": "mother-1"}],
            }
        ],
        "queue": [],
    }
    updated = _post(
        client,
        f"/api/v1/writing/jobs/{job_id}/mutate",
        json={"action": "batch_state", "value": {"state": state}},
    )
    assert updated.status_code == 200
    queued = _post(
        client,
        f"/api/v1/writing/jobs/{job_id}/mutate",
        json={"action": "batch_confirm_queue", "value": {}},
    )
    assert queued.status_code == 200
    queued_payload = queued.json()["data"]["payload"]["batch_state"]
    assert len(queued_payload["queue"]) == 2

    ran = _post(client, f"/api/v1/writing/jobs/{job_id}/run")
    assert ran.status_code == 200
    detail = _get(client, f"/api/v1/writing/jobs/{job_id}").json()["data"]
    assert detail["payload"]["stage"] == "done"
    assert all(item["status"] == "done" for item in detail["payload"]["batch_state"]["queue"])


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
