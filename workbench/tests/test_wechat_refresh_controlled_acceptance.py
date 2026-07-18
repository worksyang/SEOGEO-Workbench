from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock

import httpx
import pytest

from content_hub.adapters.wechat import WechatAdapter
from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.services.backup import BackupService
from content_hub.services.wechat_refresh import (
    FakeWechatRefreshProvider,
    WechatRefreshService,
)
from content_hub.services.migration import WECHAT_CUTOVER_READ_CONTRACTS


@pytest.fixture(autouse=True)
def _forbid_legacy_or_external_provider(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("controlled acceptance must not call legacy 8765/8774 or external HTTP")

    monkeypatch.setattr(WechatAdapter, "_request_response", fail)


def _keyword(settings, keyword_id: str) -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO keywords(
                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                keyword_id,
                "wechat-search",
                f"受控关键词-{keyword_id}",
                "active",
                "2026-07-16T00:00:00Z",
                "2026-07-16T00:00:00Z",
                "{}",
            ),
        )


def _switch(settings, contract: str, mode: str) -> None:
    with connect(settings) as con:
        con.execute(
            """UPDATE migration_switches
               SET data_mode=?,updated_by='controlled-acceptance'
               WHERE module_key='wechat-search' AND contract_key=?""",
            (mode, contract),
        )


def _seed_cutover_readiness(settings) -> None:
    now = datetime.now(UTC)
    finished_at = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    evidence_at = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    payload = {
        "manifest_id": "controlled-ready-manifest",
        "audit": {"reconcile": {"status": "matched", "verified": True}},
        "asset_integrity": {"verified": True},
    }
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO ingestion_batches(
                batch_id,adapter_key,source_scope,status,started_at,finished_at,
                source_ref,payload_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "controlled-ready-batch",
                "wechat-search",
                "history",
                "succeeded",
                finished_at,
                finished_at,
                "manifest://wechat-search/controlled-ready-manifest",
                json.dumps(payload),
                finished_at,
                finished_at,
            ),
        )
        con.execute(
            """
            INSERT INTO ingestion_checkpoints(
                adapter_key,checkpoint_key,source_hash,last_success_at,batch_id,payload_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                "wechat-search",
                "normalized",
                "controlled-ready-manifest",
                evidence_at,
                "controlled-ready-batch",
                json.dumps(payload),
            ),
        )
        for index, contract in enumerate(WECHAT_CUTOVER_READ_CONTRACTS):
            con.execute(
                """
                INSERT INTO contract_comparisons(
                    comparison_id,module_key,contract_key,request_fingerprint,
                    legacy_hash,hub_hash,status,compared_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    f"controlled-ready-{index}",
                    "wechat-search",
                    contract,
                    f"controlled-ready-{index}",
                    "same",
                    "same",
                    "matched",
                    evidence_at,
                ),
            )
        con.execute(
            """
            INSERT INTO audit_log(
                audit_id,occurred_at,actor_type,actor_id,action,
                subject_type,subject_id,outcome,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "controlled-ready-snapshot",
                evidence_at,
                "test",
                "controlled",
                "backup.snapshot",
                "backup",
                "controlled-snapshot",
                "succeeded",
                "{}",
            ),
        )
        con.execute(
            """
            INSERT INTO audit_log(
                audit_id,occurred_at,actor_type,actor_id,action,
                subject_type,subject_id,outcome,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "controlled-ready-restore",
                evidence_at,
                "test",
                "controlled",
                "backup.restore_drill",
                "backup",
                "controlled-restore",
                "succeeded",
                json.dumps({"runtime_database_unchanged": True}),
            ),
        )


def test_w17_w18_w19_w21_controlled_refresh_replay_cancel_and_recovery(settings):
    for keyword_id in ("kw_w17", "kw_w18_a", "kw_w18_b"):
        _keyword(settings, keyword_id)
    for contract in ("refresh-status", "refresh-all-status"):
        _switch(settings, contract, "hub")

    calls: list[str] = []
    provider = FakeWechatRefreshProvider(fail_ids={"kw_w18_b"})

    async def run() -> None:
        app = create_app(settings)
        app.state.wechat_refresh_provider = provider
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # W17: one keyword, normal response is only a start receipt.
                one = await client.post(
                    "/api/keywords/kw_w17/refresh",
                    json={"keyword": "受控关键词-kw_w17", "idempotency_key": "w17"},
                )
                assert one.status_code == 200
                assert one.json()["status"] == "running"
                job_id = one.json()["job_id"]
                assert (
                    await client.get(f"/api/refresh-status/{job_id}")
                ).json()["status"] == "completed"

                # W18: duplicates are normalized, one provider failure remains auditable.
                batch = await client.post(
                    "/api/refresh-all",
                    json={
                        "keyword_ids": ["kw_w18_a", "kw_w18_a", "kw_w18_b"],
                        "incremental": True,
                        "idempotency_key": "w18",
                    },
                )
                assert batch.status_code == 409
                batch_body = batch.json()
                assert batch_body["total"] == 2
                assert batch_body["status"] == "completed_with_failures"
                assert batch_body["hub_status"] == "partial_failed"
                assert all(
                    isinstance(item, dict) and {"keyword", "reason"} <= set(item)
                    for item in batch_body["failed_keywords"]
                )
                batch_id = batch_body["batch_id"]

                # Explicit replay is stable; same key with changed input is a 409.
                replay = await client.post(
                    "/api/refresh-all",
                    json={
                        "keyword_ids": ["kw_w18_a", "kw_w18_a", "kw_w18_b"],
                        "incremental": True,
                        "idempotency_key": "w18",
                    },
                )
                assert replay.status_code == 409
                assert replay.json()["batch_id"] == batch_id
                changed = await client.post(
                    "/api/refresh-all",
                    json={
                        "keyword_ids": ["kw_w18_a"],
                        "incremental": True,
                        "idempotency_key": "w18",
                    },
                )
                assert changed.status_code == 409

                # W19: a persisted running batch can be cancelled without a provider call.
                with connect(settings) as con:
                    now = "2026-07-16T02:00:00Z"
                    con.execute(
                        """INSERT INTO command_runs(
                            command_id,module_key,command_type,idempotency_key,actor_id,status,
                            input_json,output_json,error_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)""",
                        (
                            "cmd_cancel_controlled",
                            "wechat-search",
                            "wechat.refresh_all",
                            "cancel-seed",
                            "test",
                            "{}",
                            "{}",
                            "{}",
                            now,
                            now,
                        ),
                    )
                    con.execute(
                        """INSERT INTO search_refresh_jobs(
                            refresh_job_id,system_key,platform,command_id,trigger_type,status,
                            requested_count,created_at,updated_at,trigger_source
                        ) VALUES(?,?,?,?,?,'running',1,?,?,?)""",
                        (
                            "srj_cancel_controlled",
                            "wechat-search",
                            "wechat-search",
                            "cmd_cancel_controlled",
                            "manual",
                            now,
                            now,
                            "web_refresh_all",
                        ),
                    )
                    con.execute(
                        """INSERT INTO search_refresh_items(
                            refresh_item_id,refresh_job_id,keyword_id,ordinal,status,
                            attempt_count,current_phase
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            "sri_cancel_controlled",
                            "srj_cancel_controlled",
                            "kw_w17",
                            0,
                            "queued",
                            0,
                            "queued",
                        ),
                    )
                cancelled = await client.post(
                    "/api/refresh-all/cancel",
                    json={
                        "batch_id": "srj_cancel_controlled",
                        "idempotency_key": "cancel-controlled",
                    },
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "running"
                assert cancelled.json()["hub_status"] == "cancelling"
                status = await client.get(
                    "/api/refresh-all/status?batch_id=srj_cancel_controlled"
                )
                assert status.json()["status"] == "cancelled"
                assert status.json()["cancel_reason"] == "user_requested"
                assert status.json()["snapshot_count"] == 0

                # W21: failure survives restart, and a new key is an independent recovery attempt.
                failed = await client.post(
                    "/api/keywords/kw_w18_b/refresh",
                    json={"keyword": "受控关键词-kw_w18_b", "idempotency_key": "w21-fail"},
                )
                assert failed.status_code == 409
                failed_job = failed.json()["job_id"]
                assert (
                    await client.get(f"/api/refresh-status/{failed_job}")
                ).json()["status"] == "failed"

    asyncio.run(run())

    recovered = WechatRefreshService(
        settings,
        provider=FakeWechatRefreshProvider(),
    ).refresh_one(
        keyword_id="kw_w18_b",
        request_keyword="受控关键词-kw_w18_b",
        key="w21-recovery",
    )
    assert recovered["status"] == "completed"
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action IN "
            "('wechat.refresh','wechat.refresh_all','wechat.refresh_all.cancel')"
        ).fetchone()[0] >= 5
        assert con.execute(
            "SELECT COUNT(*) FROM search_refresh_events"
        ).fetchone()[0] >= 8
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_w20_scheduler_state_commands_and_disabled_provider_are_persistent(settings):
    _keyword(settings, "kw_scheduler_controlled")
    _switch(settings, "scheduler-status", "hub")

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                configured = await client.post(
                    "/api/scheduler/config",
                    json={
                        "enabled": True,
                        "interval_hours": 1,
                        "daily_keyword_budget": 3,
                        "max_keywords_per_batch": 1,
                        "idempotency_key": "scheduler-controlled",
                    },
                )
                assert configured.status_code == 200
                assert configured.json()["enabled"] is True
                status = await client.get("/api/scheduler/status")
                assert status.status_code == 200
                assert status.json()["max_keywords_per_batch"] == 1
                trigger = await client.post(
                    "/api/scheduler/trigger",
                    json={"idempotency_key": "scheduler-trigger-controlled"},
                )
                assert trigger.status_code == 409
                assert trigger.json()["blocked"] is True

        # A new app instance reads commands/status from SQLite, not process memory.
        app2 = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://test"
        ) as client:
            after_restart = await client.get("/api/scheduler/status")
            assert after_restart.status_code == 200
            assert after_restart.json()["enabled"] is True

    asyncio.run(run())


def test_r19_r20_exact_projection_and_switch_rollback_without_network(
    settings, monkeypatch
):
    _keyword(settings, "kw_switch_controlled")
    provider = FakeWechatRefreshProvider()
    service = WechatRefreshService(settings, provider=provider)
    batch = service.refresh_batch(
        keyword_ids=["kw_switch_controlled"],
        key="switch-batch",
    )
    scheduler = service.scheduler_config(
        payload={"enabled": False, "interval_hours": 2},
        key="switch-scheduler",
    )

    with connect(settings, readonly=True) as con:
        batch_projection = json.loads(
            con.execute(
                "SELECT payload_json FROM wechat_legacy_projections "
                "WHERE projection_kind='runtime' AND subject_id=?",
                (batch["batch_id"],),
            ).fetchone()["payload_json"]
        )
        history_expected = [dict(batch_projection)]
        history_expected[0].pop("runtime_subtype", None)
        scheduler_projection = json.loads(
            con.execute(
                "SELECT payload_json FROM wechat_legacy_projections "
                "WHERE projection_kind='runtime' AND subject_id='scheduler' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
        scheduler_expected = dict(scheduler_projection)
        scheduler_expected.pop("runtime_subtype", None)

    # This is a recorded reference response; it is deliberately not an HTTP call.
    from content_hub.features.wechat import legacy_read_router
    from content_hub.services.contract_diff import HTTPMetadata, PayloadWithHTTPMetadata

    def recorded_remote(_request, path, **kwargs):
        if path == "/api/refresh-all/history":
            assert kwargs.get("allow_any_json") is True
            value = history_expected
        elif path == "/api/scheduler/status":
            assert kwargs.get("allow_any_json", False) is False
            value = scheduler_expected
        else:
            raise AssertionError(f"unexpected reference read: {path}")
        return PayloadWithHTTPMetadata(
            value,
            HTTPMetadata(status_code=200, content_type="application/json"),
        )

    monkeypatch.setattr(legacy_read_router, "_remote", recorded_remote)

    def assert_group_mode(expected: str) -> None:
        with connect(settings, readonly=True) as con:
            rows = con.execute(
                """
                SELECT contract_key,data_mode
                FROM migration_switches
                WHERE module_key='wechat-search'
                  AND contract_key IN ({})
                """.format(",".join("?" for _ in WECHAT_CUTOVER_READ_CONTRACTS)),
                WECHAT_CUTOVER_READ_CONTRACTS,
            ).fetchall()
        assert len(rows) == len(WECHAT_CUTOVER_READ_CONTRACTS)
        assert {row["data_mode"] for row in rows} == {expected}

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                async def assert_reads() -> None:
                    for contract, path in (
                        ("refresh-all-history", "/api/refresh-all/history"),
                        ("scheduler-status", "/api/scheduler/status"),
                    ):
                        read = await client.get(path)
                        assert read.status_code == 200
                        assert "runtime_subtype" not in json.dumps(read.json())
                        expected = (
                            history_expected
                            if contract == "refresh-all-history"
                            else scheduler_expected
                        )
                        assert read.json() == expected

                assert_group_mode("legacy")
                await assert_reads()
                for mode, expected_mode in (
                    ("compare", "legacy"),
                    ("hub", "compare"),
                    ("legacy", "hub"),
                ):
                    if mode == "hub":
                        # compare 读取已完成后再落 readiness 证据，确保门禁看到
                        # 每个契约最新的一条 matched comparison。
                        _seed_cutover_readiness(settings)
                    response = await client.post(
                        "/api/v1/governance/switches/wechat-search/cutover",
                        json={
                            "data_mode": mode,
                            "expected_mode": expected_mode,
                            "confirm": mode == "hub",
                            "actor": "controlled-acceptance",
                            "reason": "R19/R20 原子切换与回滚验收",
                        },
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["data_mode"] == mode
                    assert response.json()["data"]["changed_count"] == len(
                        WECHAT_CUTOVER_READ_CONTRACTS
                    )
                    assert_group_mode(mode)
                    await assert_reads()

        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://test",
            ) as client:
                assert_group_mode("legacy")
                for path, expected in (
                    ("/api/refresh-all/history", history_expected),
                    ("/api/scheduler/status", scheduler_expected),
                ):
                    read = await client.get(path)
                    assert read.status_code == 200
                    assert read.json() == expected

    asyncio.run(run())


def test_refresh_same_key_concurrent_replay_and_distinct_keys(settings):
    _keyword(settings, "kw_concurrent")
    calls = 0
    calls_lock = Lock()

    class CountingProvider(FakeWechatRefreshProvider):
        kind = "recorded-counting"

        def fetch(self, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            return super().fetch(**kwargs)

    def invoke(key: str):
        return WechatRefreshService(
            settings, provider=CountingProvider()
        ).refresh_one(
            keyword_id="kw_concurrent",
            request_keyword="受控关键词-kw_concurrent",
            key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(lambda _: invoke("same-concurrent"), range(2)))
    assert same[0]["job_id"] == same[1]["job_id"]
    assert calls == 1
    independent = [invoke("independent-a"), invoke("independent-b")]
    assert independent[0]["job_id"] != independent[1]["job_id"]
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM command_runs WHERE module_key='wechat-search'"
        ).fetchone()[0] == 3


def test_sqlite_backup_restore_keeps_refresh_runtime_and_audit_projection(settings):
    _keyword(settings, "kw_backup_controlled")
    result = WechatRefreshService(
        settings, provider=FakeWechatRefreshProvider()
    ).refresh_one(
        keyword_id="kw_backup_controlled",
        request_keyword="受控关键词-kw_backup_controlled",
        key="backup-refresh",
    )
    before = {}
    with connect(settings, readonly=True) as con:
        for table in ("command_runs", "search_refresh_jobs", "audit_log", "wechat_legacy_projections"):
            before[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    backup = BackupService.from_settings(settings).snapshot(
        label="wechat-controlled", reuse=False, actor_id="test"
    )
    drill = BackupService.from_settings(settings).restore_drill(
        backup.name, actor_id="test"
    )
    assert drill["integrity"] == "ok"
    target = settings.database_path.parent / "backups" / drill["target"]
    with sqlite3.connect(target) as con:
        for table, count in before.items():
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert con.execute(
            "SELECT status FROM search_refresh_jobs WHERE refresh_job_id=?",
            (result["job_id"],),
        ).fetchone()[0] == "succeeded"
