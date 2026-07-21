from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
from fastapi import FastAPI

from content_hub.db.connection import connect
from content_hub.features.wechat.legacy_aux_router import router
from content_hub.services.wechat_agent_projection import build_projection, publish_projection
from content_hub.services.wechat_aux import WechatAuxService

from test_wechat_agent_projection import _seed


def _request(app: FastAPI, path: str) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(run())


def _hub_app(settings, service: WechatAuxService) -> FastAPI:
    with connect(settings) as con:
        con.execute(
            """UPDATE migration_switches SET data_mode='hub'
               WHERE module_key='wechat-search' AND contract_key IN (
                 'agent-manifest','agent-daily-brief','agent-metric-dictionary','agent-evidence'
               )"""
        )
        con.commit()
    app = FastAPI()
    app.state.settings = service.settings
    app.state.wechat_aux_service = service
    app.include_router(router)
    return app


def test_agent_contract_reads_hub_artifacts_without_legacy_files(settings, tmp_path):
    staging = build_projection(settings, monitor_payload=_seed(settings))
    publish_projection(settings, staging)
    missing_legacy = tmp_path / "legacy-does-not-exist"
    service = WechatAuxService(replace(settings, wechat_source_root=missing_legacy))
    app = _hub_app(settings, service)

    manifest = _request(app, "/api/agent/manifest")
    brief = _request(app, "/api/agent/daily-brief")
    dictionary = _request(app, "/api/agent/metric-dictionary")
    evidence_id = next(iter(staging["evidence"]))
    evidence = _request(app, f"/api/agent/evidence/{evidence_id}")

    for response in (manifest, brief, dictionary, evidence):
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
    assert manifest.json()["brief"]["brief_id"] == brief.json()["brief_id"]
    assert evidence.json()["evidence_id"] == evidence_id
    assert manifest.headers["cache-control"].startswith("no-store")


def test_all_candidate_evidence_is_reachable_through_contract(settings):
    staging = build_projection(settings, monitor_payload=_seed(settings))
    publish_projection(settings, staging)
    service = WechatAuxService(settings)
    app = _hub_app(settings, service)

    for candidate in staging["brief"]["event_candidates"]:
        for evidence_id in candidate["evidence_ids"]:
            response = _request(app, f"/api/agent/evidence/{evidence_id}")
            assert response.status_code == 200
            assert response.json()["evidence_id"] == evidence_id


def test_publish_is_single_run_for_same_source_fingerprint(settings):
    staging = build_projection(settings, monitor_payload=_seed(settings))
    first = publish_projection(settings, staging)
    second = publish_projection(settings, staging)
    assert first["run_id"] == second["run_id"]
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM wechat_agent_projection_runs").fetchone()[0] == 1
