from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.errors import NotFoundError
from content_hub.services.wechat_refresh import FakeWechatRefreshProvider, WechatRefreshService


def _keyword(settings, keyword_id: str = "kw_w2", platform: str = "wechat-search") -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO keywords(
                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (keyword_id, platform, f"固定关键词-{keyword_id}", "active", "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z", "{}"),
        )


def _hub_read_switch(settings, *contracts: str) -> None:
    with connect(settings) as con:
        for index, contract in enumerate(contracts):
            con.execute(
                """INSERT OR REPLACE INTO migration_switches(
                    switch_id,module_key,contract_key,data_mode,updated_at,updated_by
                ) VALUES(?,?,?,?,?,?)""",
                (f"refresh_{contract}_{index}", "wechat-search", contract, "hub", "2026-07-16T00:00:00Z", "test"),
            )


def _batch_default(settings, keyword_id: str, selected: int) -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO search_keyword_settings(
                setting_id,system_key,platform,keyword_id,batch_default_selected,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (f"setting_{keyword_id}", "wechat-search", "wechat-search", keyword_id, selected, "2026-07-16T00:00:00Z", "{}"),
        )


def test_disabled_provider_preserves_old_error_shape_and_persists(settings):
    _keyword(settings)
    _hub_read_switch(settings, "refresh-status")

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/keywords/kw_w2/refresh",
                    json={"keyword": "固定关键词", "idempotency_key": "disabled-1"},
                )
                assert response.status_code == 409
                body = response.json()
                assert body["status"] == "blocked"
                assert body["blocked"] is True
                assert body["upstream_called"] is False
                assert "ok" not in body
                status = await client.get(f"/api/refresh-status/{body['job_id']}")
                assert status.status_code == 200
                assert status.json()["status"] == "blocked"

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM command_runs WHERE idempotency_key='disabled-1'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM dual_write_receipts WHERE idempotency_key='disabled-1'").fetchone()[0] == 1
        assert con.execute("SELECT outcome FROM audit_log WHERE action='wechat.refresh'").fetchone()[0] == "blocked"
        assert con.execute("SELECT COUNT(*) FROM search_snapshots").fetchone()[0] == 0


def test_single_fake_refresh_is_idempotent_and_updates_runtime(settings):
    _keyword(settings)
    _hub_read_switch(settings, "refresh-status")

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider({
            "kw_w2": {
                "captured_at": "2026-07-16T01:00:00Z",
                "result_count": 2,
                "features": {"suggestions": ["固定建议"]},
                "hits": [{"rank": 1, "title_raw": "固定结果", "url_raw": "https://example.invalid/a"}],
                "metrics": [{"metric_key": "wechat.keyword.position", "subject_type": "keyword", "value": 1}],
                "source_ref": "provider:recorded",
            }
        })
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post("/api/keywords/kw_w2/refresh", json={"keyword": "固定关键词-kw_w2", "idempotency_key": "single-1"})
                again = await client.post("/api/keywords/kw_w2/refresh", json={"keyword": "固定关键词-kw_w2", "idempotency_key": "single-1"})
                conflict = await client.post("/api/keywords/kw_w2/refresh", json={"keyword": "固定关键词-kw_w2", "idempotency_key": "single-1", "confirm": False})
                assert first.status_code == 200
                assert again.status_code == 200
                assert first.json()["job_id"] == again.json()["job_id"]
                assert conflict.status_code == 409
                assert "idempotency" in conflict.json()["error"]
                job_id = first.json()["job_id"]
                assert (await client.get(f"/api/refresh-status/{job_id}")).json()["status"] == "succeeded"

        # 第二个 app 实例只靠数据库读取，不依赖线程内状态。
        app2 = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app2), base_url="http://test") as client:
            assert (await client.get(f"/api/refresh-status/{job_id}")).json()["status"] == "succeeded"

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM search_snapshots WHERE keyword_id='kw_w2'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM metric_observations").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM command_runs WHERE idempotency_key='single-1'").fetchone()[0] == 1


def test_w17_requires_keyword_and_returns_start_receipt(settings):
    _keyword(settings)
    _hub_read_switch(settings, "refresh-status")

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                missing = await client.post("/api/keywords/kw_w2/refresh", json={"idempotency_key": "missing-keyword"})
                assert missing.status_code == 400
                assert missing.json() == {"error": "keyword is required"}
                started = await client.post(
                    "/api/keywords/kw_w2/refresh",
                    json={"keyword": "固定关键词-kw_w2", "idempotency_key": "w17-explicit"},
                )
                assert started.status_code == 200
                assert started.json()["status"] == "running"
                assert started.json()["start_receipt"] is True
                assert (await client.get(f"/api/refresh-status/{started.json()['job_id']}")).json()["status"] == "succeeded"

    asyncio.run(run())


def test_refresh_write_operations_require_explicit_keys(settings):
    _keyword(settings)

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                single = await client.post(
                    "/api/keywords/kw_w2/refresh",
                    json={"keyword": "固定关键词-kw_w2"},
                )
                assert single.status_code == 400
                batch = await client.post(
                    "/api/refresh-all",
                    json={"keyword_ids": ["kw_w2"]},
                )
                assert batch.status_code == 400
                config = await client.post(
                    "/api/scheduler/config",
                    json={"enabled": False},
                )
                assert config.status_code == 400
                trigger = await client.post(
                    "/api/scheduler/trigger",
                    json={"idempotency_key": "explicit-trigger"},
                )
                assert trigger.status_code in {200, 409}

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            "SELECT idempotency_key FROM command_runs "
            "WHERE module_key='wechat-search' ORDER BY created_at"
        ).fetchall()
        assert rows
        assert all(not row["idempotency_key"].startswith("implicit:") for row in rows)


def test_w19_empty_ids_selects_defaults_and_incremental_rejects(settings):
    for keyword_id in ("kw_default_a", "kw_default_b", "kw_not_default"):
        _keyword(settings, keyword_id)
    _batch_default(settings, "kw_not_default", 0)

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                all_default = await client.post("/api/refresh-all", json={"idempotency_key": "all-defaults"})
                assert all_default.status_code == 202
                assert all_default.json()["total"] == 2
                assert set(all_default.json()["completed_keywords"]) == {"固定关键词-kw_default_a", "固定关键词-kw_default_b"}
                incremental = await client.post("/api/refresh-all", json={"incremental": 1, "idempotency_key": "empty-incremental"})
                assert incremental.status_code == 400
                assert incremental.json() == {"error": "incremental refresh requires keyword_ids"}
                invalid = await client.post("/api/refresh-all", json={"keyword_ids": ["kw_default_a", "bad"], "idempotency_key": "invalid-items"})
                assert invalid.status_code == 400
                assert invalid.json() == {"error": "keyword_ids contains invalid items", "invalid_keyword_ids": ["bad"]}
                bad_round = await client.post("/api/refresh-all", json={"keyword_ids": ["kw_default_a"], "refresh_round": "x", "idempotency_key": "bad-round"})
                assert bad_round.status_code == 400

    asyncio.run(run())


def test_w19_rejects_inactive_or_non_wechat_ids(settings):
    _keyword(settings, "kw_active")
    _keyword(settings, "kw_xhs", platform="xhs-search")
    _keyword(settings, "kw_archived")
    with connect(settings) as con:
        con.execute("UPDATE keywords SET status='archived' WHERE keyword_id='kw_archived'")
        con.execute(
            """INSERT INTO search_keyword_settings(
                setting_id,system_key,platform,keyword_id,batch_default_selected,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            ("setting_kw_active", "wechat-search", "wechat-search", "kw_active", 0, "2026-07-16T00:00:00Z", "{}"),
        )

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for keyword_id in ("kw_xhs", "kw_archived", "kw_active"):
                    response = await client.post(
                        "/api/refresh-all",
                        json={"keyword_ids": [keyword_id], "idempotency_key": f"invalid-{keyword_id}"},
                    )
                    assert response.status_code == 400
                    assert response.json() == {
                        "error": "keyword_ids contains invalid items",
                        "invalid_keyword_ids": [keyword_id],
                    }

    asyncio.run(run())


def test_failed_refresh_is_readable_after_restart(settings):
    _keyword(settings)
    _hub_read_switch(settings, "refresh-status")

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider(fail_ids={"kw_w2"})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/keywords/kw_w2/refresh",
                    json={"keyword": "固定关键词-kw_w2", "idempotency_key": "failed-restart"},
                )
                assert response.status_code == 409
                job_id = response.json()["job_id"]

        app2 = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://test") as client:
            status = await client.get(f"/api/refresh-status/{job_id}")
            assert status.status_code == 200
            assert status.json()["status"] == "failed"

    asyncio.run(run())


def test_w19_running_conflict_includes_batch_state(settings):
    _keyword(settings)
    with connect(settings) as con:
        now = "2026-07-16T00:00:00Z"
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)""",
            ("cmd_preseed", "wechat-search", "wechat.refresh_all", "preseed-key", "test", "{}", "{}", "{}", now, now),
        )
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,requested_count,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,'running',1,?,?,?)""",
            ("srj_preseed", "wechat-search", "wechat-search", "cmd_preseed", "manual", now, now, "web_refresh_all"),
        )

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/refresh-all",
                    json={"keyword_ids": ["kw_w2"], "idempotency_key": "conflict-batch"},
                )
                assert response.status_code == 409
                assert response.json()["error"] == "batch already running"
                assert response.json()["batch"]["batch_id"] == "srj_preseed"

    asyncio.run(run())


def test_disabled_batch_and_trigger_are_blocked_without_snapshots(settings):
    _keyword(settings, "kw_disabled")
    _hub_read_switch(settings, "scheduler-status")

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                batch = await client.post("/api/refresh-all", json={"keyword_ids": ["kw_disabled"], "idempotency_key": "disabled-batch"})
                assert batch.status_code == 409
                assert batch.json()["status"] == "blocked"
                trigger = await client.post("/api/scheduler/trigger", json={"idempotency_key": "disabled-trigger"})
                assert trigger.status_code == 409
                assert trigger.json()["blocked"] is True
                assert "enabled" in trigger.json()

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM search_snapshots").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat.scheduler.trigger' AND outcome='blocked'").fetchone()[0] == 1


def test_scheduler_config_preserves_legacy_shape_and_next_run(settings):
    with connect(settings) as con:
        con.execute(
            """INSERT INTO search_scheduler_state(
                system_key,platform,enabled,next_run_at,last_run_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "wechat-search", "wechat-search", 0, None, "2026-07-15T01:00:00Z", "2026-07-15T01:00:00Z",
                json.dumps({
                    "base_url": "http://127.0.0.1:8765",
                    "last_triggered_at": "2026-07-15T02:00:00Z",
                    "last_result": "done:completed",
                    "budget": {"reserved_count": 3},
                    "budget_breakdown": {"scheduled": {"used": 3}},
                    "last_plan": {"selected_count": 2},
                    "last_discovery": {"status": "idle"},
                }),
            ),
        )

    async def run():
        app = create_app(settings)
        _hub_read_switch(settings, "scheduler-status")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/scheduler/config",
                    json={"enabled": 1, "interval_hours": 1, "daily_keyword_budget": 10, "max_keywords_per_batch": 2, "idempotency_key": "scheduler-shape"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["base_url"] == "http://127.0.0.1:8765"
                assert body["last_result"] == "done:completed"
                assert body["budget"]["reserved_count"] == 3
                assert datetime.fromisoformat(body["next_run_at"].replace("Z", "+00:00")) > datetime.now(UTC)

    asyncio.run(run())


def test_batch_validation_partial_failure_and_cancel(settings):
    for keyword_id in ("kw_a", "kw_b", "kw_c"):
        _keyword(settings, keyword_id)
    _hub_read_switch(settings, "refresh-all-status", "refresh-all-history")

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider(fail_ids={"kw_b"})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                foreign = await client.post("/api/refresh-all", json={"keyword_ids": ["missing"], "idempotency_key": "bad"})
                assert foreign.status_code == 400
                batch = await client.post(
                    "/api/refresh-all",
                    json={"keyword_ids": ["kw_a", "kw_a", "kw_b", "kw_c"], "incremental": True, "idempotency_key": "batch-1"},
                )
                assert batch.status_code == 202
                body = batch.json()
                assert body["total"] == 3
                assert body["success_count"] == 2
                assert body["failed_count"] == 1
                assert body["status"] == "partial_failed"
                history = await client.get("/api/refresh-all/history")
                assert history.status_code == 200
                assert history.json()[0]["batch_id"] == body["batch_id"]
                ended_cancel = await client.post("/api/refresh-all/cancel", json={"batch_id": body["batch_id"], "idempotency_key": "cancel-1"})
                assert ended_cancel.status_code == 200
                assert ended_cancel.json()["message"] == "批次已结束"

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM search_refresh_events").fetchone()[0] >= 4
        assert con.execute("SELECT COUNT(*) FROM dual_write_receipts WHERE idempotency_key IN ('batch-1','cancel-1')").fetchone()[0] == 2
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_scheduler_config_trigger_and_runtime_projection(settings):
    _keyword(settings, "kw_scheduler")
    _hub_read_switch(settings, "scheduler-status")

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = FakeWechatRefreshProvider()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                config = await client.post(
                    "/api/scheduler/config",
                    json={"enabled": True, "interval_hours": 1, "daily_keyword_budget": 10, "max_keywords_per_batch": 2, "idempotency_key": "scheduler-config-1"},
                )
                assert config.status_code == 200
                assert config.json()["enabled"] is True
                trigger = await client.post("/api/scheduler/trigger", json={"idempotency_key": "scheduler-trigger-1"})
                assert trigger.status_code == 200
                assert trigger.json()["source"] == "scheduler"
                status = await client.get("/api/scheduler/status")
                assert status.status_code == 200
                assert status.json()["max_keywords_per_batch"] == 2

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        projection = con.execute(
            "SELECT payload_json FROM wechat_legacy_projections WHERE projection_kind='runtime' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        assert projection is not None
        assert json.loads(projection["payload_json"])["runtime_subtype"] == "scheduler"


def test_runtime_scopes_jobs_to_wechat_for_single_and_batch(settings):
    """同一张共享任务表里的其他模块任务不能被微信 runtime 读取。"""
    _keyword(settings, "kw_xhs_runtime", platform="xhs-search")
    _keyword(settings, "kw_wechat_runtime", platform="wechat-search")
    now = "2026-07-16T00:00:00Z"
    with connect(settings) as con:
        for job_id, system_key, platform, keyword_id in (
            ("srj_xhs_runtime_01", "xhs-search", "xhs-search", "kw_xhs_runtime"),
            ("srj_wechat_runtime_01", "wechat-search", "wechat-search", "kw_wechat_runtime"),
        ):
            con.execute(
                """INSERT INTO search_refresh_jobs(
                    refresh_job_id,system_key,platform,trigger_type,status,requested_count,
                    created_at,updated_at,trigger_source
                ) VALUES(?,?,?,'manual','succeeded',1,?,?,?)""",
                (job_id, system_key, platform, now, now, "test"),
            )
            con.execute(
                """INSERT INTO search_refresh_items(
                    refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase
                ) VALUES(?,?,?,0,'succeeded',1,'completed')""",
                (f"sri_{job_id}", job_id, keyword_id),
            )

    service = WechatRefreshService(settings)
    for batch in (False, True):
        with pytest.raises(NotFoundError):
            service.runtime("srj_xhs_runtime_01", batch=batch)

        result = service.runtime("srj_wechat_runtime_01", batch=batch)
        assert result["job_id"] == "srj_wechat_runtime_01"
        assert result["status"] == "succeeded"
