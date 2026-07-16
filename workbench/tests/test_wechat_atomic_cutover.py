from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from typing import Any

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.services.migration import (
    WECHAT_CUTOVER_READ_CONTRACTS,
    WECHAT_FIXED_HUB_CONTRACTS,
)


def _post(settings, payload: dict[str, Any]) -> httpx.Response:
    async def scenario() -> httpx.Response:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/v1/governance/switches/wechat-search/cutover",
                json=payload,
            )

    return asyncio.run(scenario())


def _switch_rows(settings, contracts: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in contracts)
    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            f"""
            SELECT contract_key,data_mode,enabled,rollback_mode,updated_at,updated_by,reason
            FROM migration_switches
            WHERE module_key='wechat-search'
              AND contract_key IN ({placeholders})
            ORDER BY contract_key
            """,
            contracts,
        ).fetchall()
    return {str(row["contract_key"]): dict(row) for row in rows}


def _assert_all_read_modes(settings, expected: str) -> None:
    rows = _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS)
    assert len(rows) == 22
    assert {row["data_mode"] for row in rows.values()} == {expected}
    assert {row["enabled"] for row in rows.values()} == {1}


def _seed_readiness(
    settings,
    *,
    batch_status: str = "succeeded",
    checkpoint_batch_id: str = "batch-ready",
    checkpoint_source_hash: str = "manifest-ready",
    checkpoint_last_success_at: str | None = "2026-01-01T00:00:01Z",
    missing_comparisons: tuple[str, ...] = (),
    with_snapshot: bool = True,
    with_restore_drill: bool = True,
    asset_verified: bool = True,
    reconcile_status: str = "matched",
    reconcile_verified: bool = True,
    newer_bad_status: str | None = None,
) -> None:
    finished_at = "2026-01-01T00:00:00Z"
    payload = {
        "manifest_id": "manifest-ready",
        "audit": {"reconcile": {"status": reconcile_status, "verified": reconcile_verified}},
        "asset_integrity": {"verified": asset_verified},
    }
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_batches(
                batch_id,adapter_key,source_scope,status,started_at,finished_at,
                source_ref,payload_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "batch-ready", "wechat-search", "history", batch_status,
                finished_at, finished_at, "manifest://wechat-search/manifest-ready",
                json.dumps(payload), finished_at, finished_at,
            ),
        )
        if newer_bad_status:
            connection.execute(
                """
                INSERT INTO ingestion_batches(
                    batch_id,adapter_key,source_scope,status,started_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    "batch-newer-bad", "wechat-search", "history", newer_bad_status,
                    "2026-01-01T00:00:02Z", "2026-01-01T00:00:02Z", "2026-01-01T00:00:02Z",
                ),
            )
        connection.execute(
            """
            INSERT INTO ingestion_checkpoints(
                adapter_key,checkpoint_key,source_hash,last_success_at,batch_id,payload_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                "wechat-search", "normalized", checkpoint_source_hash,
                checkpoint_last_success_at, checkpoint_batch_id, json.dumps(payload),
            ),
        )
        for index, contract in enumerate(WECHAT_CUTOVER_READ_CONTRACTS):
            if contract in missing_comparisons:
                continue
            connection.execute(
                """
                INSERT INTO contract_comparisons(
                    comparison_id,module_key,contract_key,request_fingerprint,
                    legacy_hash,hub_hash,status,compared_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    f"cmp-ready-{index}", "wechat-search", contract, f"ready-{index}",
                    "same", "same", "matched", "2026-01-01T00:00:01Z",
                ),
            )
        if with_snapshot:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id,occurred_at,actor_type,actor_id,action,
                    subject_type,subject_id,outcome,details_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "audit-ready-snapshot", "2026-01-01T00:00:02Z", "test", "test",
                    "backup.snapshot", "backup", "snapshot-ready", "succeeded", "{}",
                ),
            )
        if with_restore_drill:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id,occurred_at,actor_type,actor_id,action,
                    subject_type,subject_id,outcome,details_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "audit-ready-restore", "2026-01-01T00:00:02Z", "test", "test",
                    "backup.restore_drill", "backup", "restore-ready", "succeeded",
                    json.dumps({"runtime_database_unchanged": True}),
                ),
            )
        connection.commit()


def test_cutover_contract_inventories_are_exact_and_disjoint() -> None:
    assert len(WECHAT_CUTOVER_READ_CONTRACTS) == 22
    assert len(set(WECHAT_CUTOVER_READ_CONTRACTS)) == 22
    assert len(WECHAT_FIXED_HUB_CONTRACTS) == 21
    assert len(set(WECHAT_FIXED_HUB_CONTRACTS)) == 21
    assert set(WECHAT_CUTOVER_READ_CONTRACTS).isdisjoint(WECHAT_FIXED_HUB_CONTRACTS)


def test_atomic_cutover_legacy_compare_hub_legacy_audit_and_restart(settings) -> None:
    fixed_before = _switch_rows(settings, WECHAT_FIXED_HUB_CONTRACTS)

    compare = _post(
        settings,
        {
            "data_mode": "compare",
            "expected_mode": "legacy",
            "actor": "cutover-test",
            "reason": "start full comparison",
        },
    )
    assert compare.status_code == 200
    assert compare.json()["data"]["changed_count"] == 22
    _assert_all_read_modes(settings, "compare")

    missing_confirm = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "compare",
            "actor": "cutover-test",
            "reason": "should remain compare",
        },
    )
    assert missing_confirm.status_code == 409
    _assert_all_read_modes(settings, "compare")

    _seed_readiness(settings)
    hub = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "compare",
            "confirm": True,
            "actor": "cutover-test",
            "reason": "comparison accepted",
        },
    )
    assert hub.status_code == 200
    assert hub.json()["data"]["previous_mode"] == "compare"
    assert hub.json()["data"]["changed_count"] == 22
    _assert_all_read_modes(settings, "hub")

    rollback = _post(
        settings,
        {
            "data_mode": "legacy",
            "expected_mode": "hub",
            "actor": "cutover-test",
            "reason": "controlled rollback",
        },
    )
    assert rollback.status_code == 200
    assert rollback.json()["data"]["changed_count"] == 22
    _assert_all_read_modes(settings, "legacy")
    assert _switch_rows(settings, WECHAT_FIXED_HUB_CONTRACTS) == fixed_before

    with connect(settings, readonly=True) as connection:
        audits = connection.execute(
            """
            SELECT actor_id,outcome,details_json
            FROM audit_log
            WHERE action='migration_switch.wechat_cutover'
            ORDER BY occurred_at,audit_id
            """
        ).fetchall()
    assert len(audits) == 3
    last = json.loads(audits[-1]["details_json"])
    assert audits[-1]["actor_id"] == "cutover-test"
    assert audits[-1]["outcome"] == "succeeded"
    assert last["previous_mode"] == "hub"
    assert last["data_mode"] == "legacy"
    assert last["reason"] == "controlled rollback"
    assert last["changed_count"] == 22
    assert len(last["contracts"]) == 22
    assert {
        (item["before_mode"], item["after_mode"]) for item in last["contracts"]
    } == {("hub", "legacy")}

    async def restart_readback() -> None:
        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://testserver",
            ) as client:
                response = await client.get("/api/v1/governance/switches")
                assert response.status_code == 200
                items = response.json()["data"]["items"]
                reads = {
                    item["contract_key"]: item["data_mode"]
                    for item in items
                    if item["module_key"] == "wechat-search"
                    and item["contract_key"] in WECHAT_CUTOVER_READ_CONTRACTS
                }
                writes = {
                    item["contract_key"]: item["data_mode"]
                    for item in items
                    if item["module_key"] == "wechat-search"
                    and item["contract_key"] in WECHAT_FIXED_HUB_CONTRACTS
                }
                assert set(reads.values()) == {"legacy"}
                assert set(writes.values()) == {"hub"}

    asyncio.run(restart_readback())


def test_hub_cutover_empty_database_is_blocked_without_changes(settings) -> None:
    before = _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS)
    response = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "legacy",
            "confirm": True,
            "actor": "readiness-test",
            "reason": "empty database",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["readiness"]["ready"] is False
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"batch_status": "partial_failed"}, "history_batch_not_succeeded"),
        ({"checkpoint_batch_id": None}, "normalized_checkpoint_mismatch"),
        ({"missing_comparisons": (WECHAT_CUTOVER_READ_CONTRACTS[0],)}, "contract_comparisons_incomplete"),
        ({"with_snapshot": False}, "backup_snapshot_missing"),
        ({"with_restore_drill": False}, "backup_restore_drill_missing"),
    ],
)
def test_hub_cutover_readiness_blockers_are_atomic(settings, kwargs, code) -> None:
    _seed_readiness(settings, **kwargs)
    before = _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS)
    preflight = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "legacy",
            "confirm": True,
            "dry_run": True,
            "actor": "readiness-test",
            "reason": "preflight",
        },
    )
    assert preflight.status_code == 200
    readiness = preflight.json()["data"]["readiness"]
    assert readiness["ready"] is False
    assert any(item["code"] == code for item in readiness["blockers"])
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before

    blocked = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "legacy",
            "confirm": True,
            "actor": "readiness-test",
            "reason": "blocked cutover",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["readiness"]["ready"] is False
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before


def test_hub_cutover_newer_bad_batch_and_unverified_evidence_are_blocked(settings) -> None:
    _seed_readiness(settings, newer_bad_status="running", asset_verified=False)
    response = _post(
        settings,
        {
            "data_mode": "hub",
            "expected_mode": "legacy",
            "confirm": True,
            "dry_run": True,
            "actor": "readiness-test",
            "reason": "preflight",
        },
    )
    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["data"]["readiness"]["blockers"]}
    assert {"history_batch_not_succeeded", "asset_integrity_not_verified"} <= codes


def test_cutover_missing_contract_fails_atomically_and_writes_no_audit(settings) -> None:
    missing = WECHAT_CUTOVER_READ_CONTRACTS[0]
    with connect(settings) as connection:
        connection.execute(
            "DELETE FROM migration_switches WHERE module_key='wechat-search' AND contract_key=?",
            (missing,),
        )
        connection.commit()
    before = _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS)

    response = _post(
        settings,
        {
            "data_mode": "compare",
            "expected_mode": "legacy",
            "actor": "cutover-test",
            "reason": "must fail",
        },
    )
    assert response.status_code == 409
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before
    with connect(settings, readonly=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='migration_switch.wechat_cutover'"
        ).fetchone()[0]
    assert count == 0


def test_cutover_expected_mode_conflict_and_dry_run_do_not_write(settings) -> None:
    before = _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS)
    dry_run = _post(
        settings,
        {
            "data_mode": "compare",
            "expected_mode": "legacy",
            "dry_run": True,
            "actor": "preflight",
            "reason": "preflight only",
        },
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["data"]["changed_count"] == 0
    assert dry_run.json()["data"]["would_change_count"] == 22
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before

    conflict = _post(
        settings,
        {
            "data_mode": "compare",
            "expected_mode": "hub",
            "actor": "stale-client",
            "reason": "stale expected mode",
        },
    )
    assert conflict.status_code == 409
    assert _switch_rows(settings, WECHAT_CUTOVER_READ_CONTRACTS) == before


def test_cutover_audit_failure_rolls_back_all_reads(settings, monkeypatch) -> None:
    governance_router = importlib.import_module(
        "content_hub.features.governance.router"
    )

    def fail_audit(*args, **kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(governance_router.AuditService, "record", fail_audit)
    response = _post(
        settings,
        {
            "data_mode": "compare",
            "expected_mode": "legacy",
            "actor": "audit-failure-test",
            "reason": "must rollback",
        },
    )
    assert response.status_code == 500
    _assert_all_read_modes(settings, "legacy")
    with connect(settings, readonly=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='migration_switch.wechat_cutover'"
        ).fetchone()[0]
    assert count == 0


def test_fixed_hub_contract_cannot_be_changed_by_single_switch_api(settings) -> None:
    contract = WECHAT_FIXED_HUB_CONTRACTS[0]

    async def scenario() -> httpx.Response:
        app = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.put(
                f"/api/v1/governance/switches/wechat-search/{contract}",
                json={
                    "data_mode": "legacy",
                    "rollback_mode": "legacy",
                    "operator": "unsafe-test",
                    "reason": "must be blocked",
                },
            )

    response = asyncio.run(scenario())
    assert response.status_code == 409
    row = _switch_rows(settings, (contract,))[contract]
    assert row["data_mode"] == row["rollback_mode"] == "hub"


def test_read_contract_cannot_be_changed_by_generic_put(settings) -> None:
    contract = WECHAT_CUTOVER_READ_CONTRACTS[0]

    async def scenario() -> httpx.Response:
        app = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.put(
                f"/api/v1/governance/switches/wechat-search/{contract}",
                json={
                    "data_mode": "compare",
                    "operator": "unsafe-test",
                    "reason": "must use group cutover",
                },
            )

    response = asyncio.run(scenario())
    assert response.status_code == 409
    assert _switch_rows(settings, (contract,))[contract]["data_mode"] == "legacy"


def test_cutover_writer_lock_conflict_returns_409(settings) -> None:
    script = """
import fcntl, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a+") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    print("ready", flush=True)
    time.sleep(3)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(settings.lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        response = _post(
            settings,
            {
                "data_mode": "compare",
                "expected_mode": "legacy",
                "actor": "concurrency-test",
                "reason": "lock conflict",
            },
        )
        assert response.status_code == 409
        _assert_all_read_modes(settings, "legacy")
    finally:
        process.terminate()
        process.wait(timeout=5)
