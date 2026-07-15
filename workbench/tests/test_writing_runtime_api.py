from __future__ import annotations

import asyncio
from pathlib import Path
from dataclasses import replace

import httpx

from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.migrations import migrate


def test_writing_runtime_crud_queue_and_idempotency(tmp_path: Path) -> None:
    settings = replace(
        Settings.load(),
        database_path=tmp_path / "hub.sqlite",
        asset_store_path=tmp_path / "asset_store",
        frontend_dist=tmp_path / "frontend",
    )
    migrate(settings)
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Idempotency-Key": "writing-project-1"}
                first = await client.post(
                    "/api/v1/writing/projects",
                    headers=headers,
                    json={"title": "真实项目", "purpose": "持久化"},
                )
                replay = await client.post(
                    "/api/v1/writing/projects",
                    headers=headers,
                    json={"title": "真实项目", "purpose": "持久化"},
                )
                assert first.status_code == replay.status_code == 200
                assert first.json()["data"]["job_id"] == replay.json()["data"]["job_id"]
                job_id = first.json()["data"]["job_id"]
                project = (await client.get("/api/v1/writing/projects")).json()["data"]["items"][0]
                project_id = project["wm_project_id"]
                updated = await client.patch(
                    f"/api/v1/writing/projects/{project_id}",
                    headers={"Idempotency-Key": "writing-project-update-1"},
                    json={"purpose": "已更新"},
                )
                assert updated.status_code == 200
                assert updated.json()["data"]["purpose"] == "已更新"

                created_batch = await client.post(
                    "/api/v1/writing/batches",
                    headers={"Idempotency-Key": "writing-batch-1"},
                    json={"topic": "真实批次", "keywords": ["初始词"], "target_article_count": 1},
                )
                assert created_batch.status_code == 200
                batch_job_id = created_batch.json()["data"]["job_id"]
                batch_detail = (await client.get(f"/api/v1/writing/jobs/{batch_job_id}")).json()["data"]
                batch_id = batch_detail["wm_batch_id"]
                keyword = await client.post(
                    f"/api/v1/writing/batches/{batch_id}/keywords",
                    headers={"Idempotency-Key": "writing-keyword-1"},
                    json={"keyword": "新关键词", "target_article_count": 2},
                )
                assert keyword.status_code == 200
                keyword_id = keyword.json()["data"]["wm_batch_keyword_id"]
                keyword_replay = await client.post(
                    f"/api/v1/writing/batches/{batch_id}/keywords",
                    headers={"Idempotency-Key": "writing-keyword-1"},
                    json={"keyword": "新关键词", "target_article_count": 2},
                )
                assert keyword_replay.json()["data"]["wm_batch_keyword_id"] == keyword_id

                draft = await client.post(
                    "/api/v1/writing/drafts",
                    headers={"Idempotency-Key": "writing-draft-1"},
                    json={"wm_batch_id": batch_id, "wm_batch_keyword_id": keyword_id, "title": "等待生成"},
                )
                assert draft.status_code == 200
                draft_id = draft.json()["data"]["wm_draft_id"]
                assert (await client.patch(
                    f"/api/v1/writing/drafts/{draft_id}",
                    headers={"Idempotency-Key": "writing-draft-update-1"},
                    json={"status": "review"},
                )).json()["data"]["status"] == "review"

                run = await client.post(
                    f"/api/v1/writing/jobs/{job_id}/run",
                    headers={"Idempotency-Key": "writing-run-1"},
                )
                assert run.status_code == 200
                assert run.json()["data"]["status"] == "demo_only"
                assert run.json()["data"]["blocked"] is False

    asyncio.run(scenario())

    import sqlite3

    connection = sqlite3.connect(settings.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM command_runs WHERE module_key='writing'").fetchone()[0] >= 5
        assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'writing.%'").fetchone()[0] >= 5
    finally:
        connection.close()
