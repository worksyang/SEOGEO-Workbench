from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import NotFoundError
from content_hub.repositories.wechat_legacy import WechatLegacyRepository
from content_hub.services import signals as signals_module
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
                assert body["status"] == "failed"
                assert body["hub_status"] == "blocked"
                assert body["blocked"] is True
                assert body["upstream_called"] is False
                assert "ok" not in body
                status = await client.get(f"/api/refresh-status/{body['job_id']}")
                assert status.status_code == 200
                assert status.json()["status"] == "failed"
                assert status.json()["hub_status"] == "blocked"

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
                assert (await client.get(f"/api/refresh-status/{job_id}")).json()["status"] == "completed"

        # 第二个 app 实例只靠数据库读取，不依赖线程内状态。
        app2 = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app2), base_url="http://test") as client:
            assert (await client.get(f"/api/refresh-status/{job_id}")).json()["status"] == "completed"

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
                assert (await client.get(f"/api/refresh-status/{started.json()['job_id']}")).json()["status"] == "completed"

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


def test_batch_progress_is_persisted_while_provider_sleeps_and_cancel_does_not_hold_lock(settings):
    keyword_ids = ["kw_progress_a", "kw_progress_b", "kw_progress_c"]
    for keyword_id in keyword_ids:
        _keyword(settings, keyword_id)

    class SleepingProvider(FakeWechatRefreshProvider):
        def __init__(self):
            super().__init__()
            self.second_started = Event()
            self.release_second = Event()
            self.calls: list[str] = []

        def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round=None):
            self.calls.append(keyword_id)
            if keyword_id == "kw_progress_b":
                self.second_started.set()
                assert self.release_second.wait(5), "test provider was not released"
            return super().fetch(
                keyword_id=keyword_id,
                keyword=keyword,
                incremental=incremental,
                refresh_round=refresh_round,
            )

    provider = SleepingProvider()
    service = WechatRefreshService(settings, provider=provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.refresh_batch,
            keyword_ids=keyword_ids,
            key="progress-regression",
        )
        assert provider.second_started.wait(5), "second keyword did not reach provider"
        with connect(settings, readonly=True) as con:
            job_id = con.execute(
                "SELECT refresh_job_id FROM search_refresh_jobs WHERE command_id IN "
                "(SELECT command_id FROM command_runs WHERE idempotency_key='progress-regression')"
            ).fetchone()["refresh_job_id"]

        # The initial transaction and the first keyword checkpoint are visible
        # while the second provider call is still sleeping.
        during_fetch = service.runtime(job_id, batch=True)
        assert during_fetch["status"] == "running"
        assert during_fetch["hub_status"] == "running"
        assert during_fetch["success_count"] == 1
        assert during_fetch["completed_keywords"] == ["固定关键词-kw_progress_a"]
        assert during_fetch["current_keyword"] == "固定关键词-kw_progress_b"

        cancelled = service.cancel_batch(batch_id=job_id, key="progress-cancel")
        assert cancelled["hub_status"] == "cancelling"
        provider.release_second.set()
        result = future.result(timeout=10)

    assert result["hub_status"] == "cancelled"
    assert result["success_count"] == 2
    assert result["cancelled_count"] == 1
    assert service.runtime(job_id, batch=True)["snapshot_count"] == 2

    # A cancelled/finished provider run must not leave the process writer lock held.
    with writer_lock(settings.lock_path, timeout_seconds=0.2):
        pass


def test_refresh_all_route_status_is_running_during_slow_provider_and_history_matches_cancel(settings):
    keyword_ids = ["kw_route_slow_a", "kw_route_slow_b", "kw_route_slow_c"]
    for keyword_id in keyword_ids:
        _keyword(settings, keyword_id)
    _hub_read_switch(settings, "refresh-all-status", "refresh-all-history")

    class RouteSleepingProvider(FakeWechatRefreshProvider):
        def __init__(self):
            super().__init__()
            self.second_started = Event()
            self.release_second = Event()

        def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round=None):
            if keyword_id == "kw_route_slow_b":
                self.second_started.set()
                assert self.release_second.wait(5), "route test provider was not released"
            return super().fetch(
                keyword_id=keyword_id,
                keyword=keyword,
                incremental=incremental,
                refresh_round=refresh_round,
            )

    provider = RouteSleepingProvider()

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = provider
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                post_task = asyncio.create_task(
                    client.post(
                        "/api/refresh-all",
                        json={
                            "keyword_ids": keyword_ids,
                            "idempotency_key": "route-slow-batch",
                        },
                    )
                )
                assert await asyncio.to_thread(provider.second_started.wait, 5)

                status = await client.get("/api/refresh-all/status")
                assert status.status_code == 200
                status_body = status.json()
                assert status_body["status"] == "running"
                assert status_body["processed_count"] == 1
                assert status_body["success_count"] == 1
                assert status_body["failed_count"] == 0
                assert status_body["current_keyword"] == "固定关键词-kw_route_slow_b"
                with connect(settings, readonly=True) as con:
                    projection = con.execute(
                        """SELECT payload_json FROM wechat_legacy_projections
                           WHERE projection_kind='runtime' AND subject_id=?
                           ORDER BY updated_at DESC LIMIT 1""",
                        (status_body["batch_id"],),
                    ).fetchone()
                assert projection is not None
                projection_body = json.loads(projection["payload_json"])
                assert projection_body["status"] == "running"
                assert projection_body["processed_count"] == 1
                assert projection_body["current_keyword"] == status_body["current_keyword"]

                cancelled = await client.post(
                    "/api/refresh-all/cancel",
                    json={
                        "batch_id": status_body["batch_id"],
                        "idempotency_key": "route-slow-cancel",
                    },
                )
                assert cancelled.status_code == 200
                cancel_body = cancelled.json()
                assert cancel_body["status"] == "running"
                assert cancel_body["hub_status"] == "cancelling"
                assert cancel_body["batch"]["batch_id"] == status_body["batch_id"]
                assert cancel_body["batch"]["cancel_requested"] is True
                assert cancel_body["batch"]["cancel_reason"] == "user_requested"

                provider.release_second.set()
                completed = await post_task
                assert completed.status_code == 202
                completed_body = completed.json()
                assert completed_body["status"] == "cancelled"
                assert completed_body["cancelled_count"] == 1
                assert completed_body["success_count"] == 2

                history = await client.get("/api/refresh-all/history")
                assert history.status_code == 200
                history_body = history.json()
                history_row = next(
                    item for item in history_body
                    if item["batch_id"] == completed_body["batch_id"]
                )
                assert history_row["status"] == completed_body["status"]
                assert history_row["processed_count"] == completed_body["processed_count"]
                assert history_row["success_count"] == completed_body["success_count"]
                assert history_row["cancelled_count"] == completed_body["cancelled_count"]
                assert history_row["cancel_reason"] == "user_requested"

    asyncio.run(run())


def test_batch_failure_keeps_success_checkpoint_and_releases_writer_lock(settings):
    _keyword(settings, "kw_failure_a")
    _keyword(settings, "kw_failure_b")
    service = WechatRefreshService(
        settings,
        provider=FakeWechatRefreshProvider(fail_ids={"kw_failure_b"}),
    )

    result = service.refresh_batch(
        keyword_ids=["kw_failure_a", "kw_failure_b"],
        key="failure-lock-regression",
    )

    assert result["hub_status"] == "partial_failed"
    assert result["success_count"] == 1
    assert result["failed_count"] == 1
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT status FROM search_refresh_items WHERE keyword_id='kw_failure_a'"
        ).fetchone()["status"] == "succeeded"
        assert con.execute(
            "SELECT status FROM search_refresh_items WHERE keyword_id='kw_failure_b'"
        ).fetchone()["status"] == "failed"
    with writer_lock(settings.lock_path, timeout_seconds=0.2):
        pass


def test_batch_retries_transient_failure_then_continues(settings):
    for keyword_id in ("kw_retry_a", "kw_retry_b"):
        _keyword(settings, keyword_id)

    class TransientError(RuntimeError):
        reason_code = "remote_unavailable"

    class RetryProvider(FakeWechatRefreshProvider):
        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round=None):
            self.calls.append(keyword_id)
            if keyword_id == "kw_retry_a" and self.calls.count(keyword_id) < 3:
                raise TransientError("temporary network failure")
            return super().fetch(
                keyword_id=keyword_id,
                keyword=keyword,
                incremental=incremental,
                refresh_round=refresh_round,
            )

    provider = RetryProvider()
    service = WechatRefreshService(
        settings,
        provider=provider,
        max_attempts=3,
        retry_delays_seconds=(0, 0),
    )
    result = service.refresh_batch(
        keyword_ids=["kw_retry_a", "kw_retry_b"],
        key="transient-retry",
    )

    assert result["hub_status"] == "succeeded"
    assert provider.calls == ["kw_retry_a", "kw_retry_a", "kw_retry_a", "kw_retry_b"]
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            """SELECT keyword_id,status,attempt_count
               FROM search_refresh_items ORDER BY ordinal"""
        ).fetchall()
        assert [(row["keyword_id"], row["status"], row["attempt_count"]) for row in rows] == [
            ("kw_retry_a", "succeeded", 3),
            ("kw_retry_b", "succeeded", 1),
        ]
        assert con.execute(
            "SELECT COUNT(*) FROM search_refresh_events WHERE event_type='retry_scheduled'"
        ).fetchone()[0] == 2


def test_retry_exhaustion_logs_failure_and_runs_next_keyword(settings):
    for keyword_id in ("kw_exhaust_a", "kw_exhaust_b"):
        _keyword(settings, keyword_id)

    class TransientError(RuntimeError):
        reason_code = "remote_timeout"

    class ExhaustingProvider(FakeWechatRefreshProvider):
        def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round=None):
            if keyword_id == "kw_exhaust_a":
                raise TransientError("remote request timed out")
            return super().fetch(
                keyword_id=keyword_id,
                keyword=keyword,
                incremental=incremental,
                refresh_round=refresh_round,
            )

    service = WechatRefreshService(
        settings,
        provider=ExhaustingProvider(),
        max_attempts=3,
        retry_delays_seconds=(0, 0),
    )
    result = service.refresh_batch(
        keyword_ids=["kw_exhaust_a", "kw_exhaust_b"],
        key="retry-exhaustion",
    )

    assert result["hub_status"] == "partial_failed"
    assert result["success_count"] == 1
    assert result["failed_count"] == 1
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            """SELECT keyword_id,status,attempt_count
               FROM search_refresh_items ORDER BY ordinal"""
        ).fetchall()
        assert [(row["keyword_id"], row["status"], row["attempt_count"]) for row in rows] == [
            ("kw_exhaust_a", "failed", 3),
            ("kw_exhaust_b", "succeeded", 1),
        ]
    failure_log = settings.database_path.parent / "刷新失败点.md"
    text = failure_log.read_text(encoding="utf-8")
    assert "kw_exhaust_a" in text
    assert "remote_timeout" in text
    assert "remote request timed out" in text
    assert not failure_log.with_suffix(".md.lock").exists()


def test_process_restart_requeues_running_item_and_resumes_checkpoint(settings):
    for keyword_id in ("kw_recover_a", "kw_recover_b"):
        _keyword(settings, keyword_id)
    now = "2026-07-18T04:24:24.313Z"
    with connect(settings) as con:
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)""",
            (
                "cmd_recovery",
                "wechat-search",
                "wechat.refresh_all",
                "recovery-key",
                "scheduler",
                json.dumps({
                    "keyword_ids": ["kw_recover_a", "kw_recover_b"],
                    "incremental": False,
                    "refresh_round": None,
                    "source": "scheduler",
                }),
                "{}",
                "{}",
                now,
                now,
            ),
        )
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,
                requested_count,started_at,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,'running',2,?,?,?,?)""",
            (
                "srj_recovery",
                "wechat-search",
                "wechat-search",
                "cmd_recovery",
                "scheduled",
                now,
                now,
                now,
                "scheduler",
            ),
        )
        con.execute(
            """INSERT INTO search_refresh_items(
                refresh_item_id,refresh_job_id,keyword_id,ordinal,status,
                attempt_count,current_phase,started_at
            ) VALUES(?,?,?,0,'running',1,'provider',?)""",
            ("sri_recovery_a", "srj_recovery", "kw_recover_a", now),
        )
        con.execute(
            """INSERT INTO search_refresh_items(
                refresh_item_id,refresh_job_id,keyword_id,ordinal,status,
                attempt_count,current_phase
            ) VALUES(?,?,?,1,'queued',0,'queued')""",
            ("sri_recovery_b", "srj_recovery", "kw_recover_b"),
        )

    service = WechatRefreshService(
        settings,
        provider=FakeWechatRefreshProvider(),
        max_attempts=3,
        retry_delays_seconds=(0, 0),
    )
    assert service.recover_active_batches() == ["srj_recovery"]
    result = service.run_batch("srj_recovery")

    assert result["hub_status"] == "succeeded"
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            """SELECT keyword_id,status,attempt_count
               FROM search_refresh_items ORDER BY ordinal"""
        ).fetchall()
        assert [(row["keyword_id"], row["status"], row["attempt_count"]) for row in rows] == [
            ("kw_recover_a", "succeeded", 2),
            ("kw_recover_b", "succeeded", 1),
        ]
        assert con.execute(
            "SELECT COUNT(*) FROM search_refresh_events WHERE event_type='process_restarted'"
        ).fetchone()[0] == 1
    text = (settings.database_path.parent / "刷新失败点.md").read_text(encoding="utf-8")
    assert "process_restarted" in text
    assert "kw_recover_a" in text


def test_zombie_batch_is_force_completed_and_clears_active_slot(settings):
    """超过阈值仍在 running 的僵尸批次应被强制终止，并释放调度器占用槽。"""
    for keyword_id in ("kw_zombie_a", "kw_zombie_b"):
        _keyword(settings, keyword_id)
    stale = (datetime.now(UTC) - timedelta(hours=8)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with connect(settings) as con:
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)""",
            ("cmd_zombie", "wechat-search", "wechat.refresh_all", "zombie-key", "scheduler",
             json.dumps({"keyword_ids": ["kw_zombie_a", "kw_zombie_b"], "source": "scheduler"}),
             "{}", "{}", stale, stale),
        )
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,
                requested_count,started_at,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,'running',2,?,?,?,?)""",
            ("srj_zombie", "wechat-search", "wechat-search", "cmd_zombie", "scheduled",
             stale, stale, stale, "scheduler"),
        )
        con.execute(
            """INSERT INTO search_refresh_items(
                refresh_item_id,refresh_job_id,keyword_id,ordinal,status,
                attempt_count,current_phase,started_at
            ) VALUES(?,?,?,0,'running',1,'provider',?)""",
            ("sri_zombie_a", "srj_zombie", "kw_zombie_a", stale),
        )
        con.execute(
            """INSERT INTO search_refresh_items(
                refresh_item_id,refresh_job_id,keyword_id,ordinal,status,
                attempt_count,current_phase
            ) VALUES(?,?,?,1,'queued',0,'queued')""",
            ("sri_zombie_b", "srj_zombie", "kw_zombie_b"),
        )
        con.execute(
            """INSERT INTO search_scheduler_state(
                system_key,platform,enabled,active_refresh_job_id,updated_at,payload_json
            ) VALUES(?,?,1,?,?,?)""",
            ("wechat-search", "wechat-search", "srj_zombie", stale, "{}"),
        )

    service = WechatRefreshService(settings, provider=FakeWechatRefreshProvider())
    assert service.recover_zombie_batches() == ["srj_zombie"]

    with connect(settings, readonly=True) as con:
        job = con.execute(
            "SELECT status,finished_at FROM search_refresh_jobs WHERE refresh_job_id='srj_zombie'"
        ).fetchone()
        assert job["status"] in {"failed", "partial_failed"}
        assert job["finished_at"] is not None
        items = con.execute(
            "SELECT status FROM search_refresh_items WHERE refresh_job_id='srj_zombie'"
        ).fetchall()
        assert all(row["status"] in {"failed", "succeeded", "cancelled"} for row in items)
        active = con.execute(
            "SELECT active_refresh_job_id FROM search_scheduler_state WHERE system_key='wechat-search'"
        ).fetchone()["active_refresh_job_id"]
        assert active is None


def test_recent_batch_is_not_treated_as_zombie(settings):
    """刚启动不久的批次不应被僵尸清理误杀。"""
    _keyword(settings, "kw_fresh")
    now = _now_iso()
    with connect(settings) as con:
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running','{}','{}','{}',?,?)""",
            ("cmd_fresh", "wechat-search", "wechat.refresh_all", "fresh-key", "scheduler", now, now),
        )
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,
                requested_count,started_at,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,'running',1,?,?,?,?)""",
            ("srj_fresh", "wechat-search", "wechat-search", "cmd_fresh", "scheduled", now, now, now, "scheduler"),
        )
        con.execute(
            """INSERT INTO search_refresh_items(
                refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase,started_at
            ) VALUES(?,?,?,0,'running',1,'provider',?)""",
            ("sri_fresh", "srj_fresh", "kw_fresh", now),
        )

    service = WechatRefreshService(settings, provider=FakeWechatRefreshProvider())
    assert service.recover_zombie_batches() == []
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT status FROM search_refresh_jobs WHERE refresh_job_id='srj_fresh'"
        ).fetchone()["status"] == "running"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_disabled_batch_and_trigger_are_blocked_without_snapshots(settings):
    _keyword(settings, "kw_disabled")
    _hub_read_switch(settings, "scheduler-status")

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                batch = await client.post("/api/refresh-all", json={"keyword_ids": ["kw_disabled"], "idempotency_key": "disabled-batch"})
                assert batch.status_code == 409
                assert batch.json()["status"] == "failed"
                assert batch.json()["hub_status"] == "blocked"
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
                assert body["base_url"] == settings.wechat_search_api_url
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
                assert batch.status_code == 409
                body = batch.json()
                assert body["total"] == 3
                assert body["success_count"] == 2
                assert body["failed_count"] == 1
                assert body["status"] == "completed_with_failures"
                assert body["hub_status"] == "partial_failed"
                assert body["failed_keywords"] == [{"keyword": "固定关键词-kw_b", "reason": "provider_failed"}]
                assert body["failure_reasons"] == ["provider_failed"]
                assert body["snapshot_count"] == 2
                history = await client.get("/api/refresh-all/history")
                assert history.status_code == 200
                assert history.json()[0]["batch_id"] == body["batch_id"]
                assert history.json()[0]["status"] == "completed_with_failures"
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


def test_scheduler_only_selects_due_keywords_and_updates_refresh_state(settings):
    for keyword_id in ("kw_due", "kw_future"):
        _keyword(settings, keyword_id)
        _batch_default(settings, keyword_id, 1)
    now = datetime.now(UTC)
    with connect(settings) as con:
        for keyword_id, last_refresh in (
            ("kw_due", now - timedelta(days=2)),
            ("kw_future", now),
        ):
            row = con.execute(
                "SELECT payload_json FROM search_keyword_settings WHERE keyword_id=?",
                (keyword_id,),
            ).fetchone()
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(
                {
                    "last_refresh_at": last_refresh.isoformat().replace("+00:00", "Z"),
                    "refresh_frequency_days": 1,
                    "refresh_frequency_source": "manual",
                    "lifecycle_stage": "established",
                }
            )
            con.execute(
                """UPDATE search_keyword_settings
                   SET refresh_strategy='scheduled',refresh_interval_minutes=1,payload_json=?
                   WHERE keyword_id=?""",
                (json.dumps(payload), keyword_id),
            )

    service = WechatRefreshService(settings, provider=FakeWechatRefreshProvider())
    service.scheduler_config(
        payload={
            "enabled": True,
            "interval_hours": 1,
            "daily_keyword_budget": 10,
            "max_keywords_per_batch": 10,
        },
        key="scheduler-due-config",
    )
    result = service.scheduler_trigger(key="scheduler-due-trigger")
    assert result["batch"]["completed_keywords"] == ["固定关键词-kw_due"]
    assert result["last_plan"]["due_count"] == 1
    assert result["last_plan"]["selected_count"] == 1

    with connect(settings, readonly=True) as con:
        payload = json.loads(
            con.execute(
                "SELECT payload_json FROM search_keyword_settings WHERE keyword_id='kw_due'"
            ).fetchone()["payload_json"]
        )
        assert payload["last_refresh_status"] == "success"
        assert payload["last_refresh_at"]
        assert payload["snapshot_count"] == 1


def test_scheduler_prioritizes_pinned_and_cools_recent_failures(settings):
    for keyword_id in ("kw_policy_pinned", "kw_policy_normal", "kw_policy_failed"):
        _keyword(settings, keyword_id)
        _batch_default(settings, keyword_id, 1)
    now = datetime.now(UTC)
    with connect(settings) as con:
        for keyword_id in ("kw_policy_pinned", "kw_policy_normal", "kw_policy_failed"):
            row = con.execute(
                "SELECT payload_json FROM search_keyword_settings WHERE keyword_id=?",
                (keyword_id,),
            ).fetchone()
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(
                {
                    "last_refresh_at": (now - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                    "refresh_frequency_days": 1,
                    "refresh_frequency_source": "auto",
                    "snapshot_count": 3,
                    "lifecycle_stage": "established",
                }
            )
            if keyword_id == "kw_policy_failed":
                payload.update(
                    {
                        "last_refresh_status": "failed",
                        "last_refresh_attempt_at": now.isoformat().replace("+00:00", "Z"),
                    }
                )
            con.execute(
                """UPDATE search_keyword_settings
                   SET refresh_strategy='scheduled',
                       refresh_interval_minutes=1440,
                       pinned=?,
                       payload_json=?
                   WHERE keyword_id=?""",
                (
                    1 if keyword_id == "kw_policy_pinned" else 0,
                    json.dumps(payload),
                    keyword_id,
                ),
            )

    service = WechatRefreshService(settings, provider=FakeWechatRefreshProvider())
    with connect(settings, readonly=True) as con:
        plan = service._scheduler_plan(
            con,
            config={"daily_keyword_budget": 10, "max_keywords_per_batch": 10},
        )

    assert plan["keyword_ids"] == ["kw_policy_pinned", "kw_policy_normal"]
    assert plan["due_count"] == 2


def test_imported_legacy_running_jobs_do_not_block_new_hub_batch(settings):
    _keyword(settings, "kw_new_hub")
    with connect(settings) as con:
        con.execute(
            """INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,command_id,trigger_type,status,
                requested_count,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,NULL,'manual','running',1,?,?,?)""",
            (
                "legacy_running_without_command",
                "wechat-search",
                "wechat-search",
                "2026-06-14T00:00:00Z",
                "2026-07-16T00:00:00Z",
                "web_refresh_all",
            ),
        )
    result = WechatRefreshService(
        settings,
        provider=FakeWechatRefreshProvider(),
    ).refresh_batch(
        keyword_ids=["kw_new_hub"],
        key="hub-after-imported-running",
    )
    assert result["status"] == "completed"
    assert result["success_count"] == 1


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
        assert result["status"] == "completed"


def test_stale_running_projection_cannot_resurrect_cancelled_batch(settings):
    _keyword(settings, "kw_stale_projection")
    result = WechatRefreshService(
        settings,
        provider=FakeWechatRefreshProvider(),
    ).refresh_batch(
        keyword_ids=["kw_stale_projection"],
        key="stale-running-projection",
    )
    batch_id = result["batch_id"]

    with connect(settings) as con:
        projection = con.execute(
            """SELECT payload_json FROM wechat_legacy_projections
               WHERE projection_kind='runtime' AND subject_id=?""",
            (batch_id,),
        ).fetchone()
        assert projection is not None
        payload = json.loads(projection["payload_json"])
        payload.update(
            {
                "status": "running",
                "hub_status": "running",
                "is_active": False,
                "is_finished": False,
                "finished_at": None,
            }
        )
        con.execute(
            """UPDATE search_refresh_jobs
               SET status='cancelled',cancel_requested=1,cancelled_at=?,
                   finished_at=?,updated_at=?
               WHERE refresh_job_id=?""",
            (
                "2026-07-16T02:00:00Z",
                "2026-07-16T02:00:00Z",
                "2026-07-16T02:00:00Z",
                batch_id,
            ),
        )
        con.execute(
            """UPDATE wechat_legacy_projections
               SET payload_json=?
               WHERE projection_kind='runtime' AND subject_id=?""",
            (json.dumps(payload, ensure_ascii=False), batch_id),
        )

    repository = WechatLegacyRepository(settings)
    assert repository.active_batch_runtime() is None
    assert repository.runtime(batch_id, subtype="batch")["status"] == "cancelled"


def test_successful_snapshot_touches_keyword_creator_and_runs_linkage(settings):
    _keyword(settings, "kw_linkage")
    provider = FakeWechatRefreshProvider({
        "kw_linkage": {
            "captured_at": "2026-07-18T01:00:00Z",
            "result_count": 1,
            "hits": [{
                "rank": 1,
                "title_raw": "联动文章",
                "url_raw": "https://example.invalid/linkage",
                "account": "联动账号",
                "markdown_body": "正文阅读123\n\n阅读42\n\n# 文章底部\n\n点赞7\n\nEndFragment",
            }],
            "source_ref": "provider:test-linkage",
        }
    })
    result = WechatRefreshService(settings, provider=provider).refresh_batch(keyword_ids=["kw_linkage"], key="linkage-success")
    assert result["hub_status"] == "succeeded"
    assert result["post_refresh_linkage"]["status"] == "succeeded"
    assert result["post_refresh_linkage"]["steps"]["metrics_backfill"]["status"] != "skipped"
    linkage_json = json.dumps(result["post_refresh_linkage"], ensure_ascii=False, separators=(",", ":"))
    assert len(linkage_json.encode("utf-8")) <= signals_module.POST_REFRESH_LINKAGE_MAX_BYTES
    assert "full" not in result["post_refresh_linkage"]["steps"]["live_projection"].get("result", {})
    with connect(settings, readonly=True) as con:
        keyword = con.execute("SELECT updated_at,payload_json FROM keywords WHERE keyword_id='kw_linkage'").fetchone()
        creator = con.execute("SELECT updated_at,payload_json FROM creators WHERE canonical_name='联动账号'").fetchone()
        assert keyword["updated_at"] == "2026-07-18T01:00:00Z"
        assert json.loads(keyword["payload_json"])["latest_snapshot_id"]
        assert creator["updated_at"] == "2026-07-18T01:00:00Z"
        assert json.loads(creator["payload_json"])["latest_snapshot_id"]
        metric = con.execute(
            """SELECT numeric_value FROM metric_observations
               WHERE metric_key='wechat.article.read_count'
               ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        assert metric["numeric_value"] == 42
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat.refresh_all.linkage' AND outcome='succeeded'").fetchone()[0] == 1


def test_scheduler_budget_recomputes_business_date_instead_of_payload_date(settings):
    service = WechatRefreshService(settings, provider=FakeWechatRefreshProvider())
    with connect(settings) as con:
        con.execute("INSERT INTO search_scheduler_state(system_key,platform,enabled,updated_at,payload_json) VALUES(?,?,?,?,?)", ("wechat-search", "wechat-search", 1, "2026-07-17T00:00:00Z", json.dumps({"daily_keyword_budget": 10, "budget": {"date": "1999-01-01", "used_count": 99}})))
        config = json.loads(con.execute("SELECT payload_json FROM search_scheduler_state WHERE system_key='wechat-search' AND platform='wechat-search'").fetchone()["payload_json"])
        plan = service._scheduler_plan(con, config=config)
    assert plan["budget"]["date"] == datetime.now().astimezone().date().isoformat()
    assert plan["budget"]["used_count"] == 0


def test_post_refresh_linkage_failure_is_observable(settings, monkeypatch):
    _keyword(settings, "kw_linkage_failure")
    def fail(**_kwargs):
        raise RuntimeError("metrics backfill exploded")
    import content_hub.services.wechat_article_metrics as module
    monkeypatch.setattr(module, "backfill", fail)
    result = WechatRefreshService(settings, provider=FakeWechatRefreshProvider()).refresh_batch(keyword_ids=["kw_linkage_failure"], key="linkage-failure")
    assert result["hub_status"] == "partial_failed"
    assert result["post_refresh_linkage"]["status"] == "failed"
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat.refresh_all.linkage' AND outcome='failed'").fetchone()[0] == 1
        runtime = con.execute("SELECT payload_json FROM wechat_legacy_projections WHERE projection_kind='runtime' AND subject_id=? ORDER BY updated_at DESC LIMIT 1", (result["batch_id"],)).fetchone()
        assert "metrics backfill exploded" in runtime["payload_json"]


def test_post_refresh_projection_summary_is_bounded_and_keeps_contract() -> None:
    huge = {
        "full": {"generated_at": "2026-07-18T19:43:00Z", "blob": "x" * (80 << 20)},
        "bootstrap": {"generated_at": "2026-07-18T19:43:00Z", "blob": "x" * (80 << 20)},
        **{
            f"keyword:kw_{index}": {"generated_at": "2026-07-18T19:43:00Z", "blob": "x" * 1024}
            for index in range(1000)
        },
    }
    summary = signals_module._summarize_optional_result(
        "content_hub.services.wechat_live_projection",
        huge,
    )
    encoded = json.dumps(
        {"status": "succeeded", "module": "content_hub.services.wechat_live_projection", "result": summary},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(encoded.encode("utf-8")) <= signals_module.POST_REFRESH_LINKAGE_MAX_BYTES
    assert summary["projection_count"] == 1002
    assert summary["projection_counts"]["keyword"] == 1000
    assert summary["generated_at"] == "2026-07-18T19:43:00Z"
    assert "blob" not in json.dumps(summary, ensure_ascii=False)
