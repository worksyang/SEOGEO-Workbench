from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.errors import ValidationAppError
from content_hub.services.wechat_state import StateCommandService


def _seed(settings: Settings) -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO search_keyword_groups(
               group_id,system_key,platform,group_name,sort_order,created_at,updated_at,archived_at
            ) VALUES(?,?,?,?,?,?,?,NULL)""",
            ("g_state", "wechat-search", "wechat-search", "状态测试", 0, "2026-01-01", "2026-01-01"),
        )
        con.execute(
            """INSERT INTO keywords(
               keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            ("kw_state", "wechat-search", "状态测试词", "active", "2026-01-01", "2026-01-01", "{}"),
        )
        con.execute(
            """INSERT INTO search_keyword_settings(
               setting_id,system_key,platform,keyword_id,group_id,pinned,
               refresh_strategy,refresh_interval_minutes,commercial_value,note,
               archived_at,updated_at,payload_json,refresh_policy_reason,
               commercial_value_source,commercial_value_reason,auto_archive_locked,
               keyword_order,batch_default_selected
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wechat-search:kw_state", "wechat-search", "wechat-search", "kw_state",
                "g_state", 0, "manual", 1440, 5, "", None, "2026-01-01", "{}",
                "新词：观察期每日刷新", "auto", "", 0, 1, 1,
            ),
        )
        projections = {
            "keyword_manage": {
                "groups": [{
                    "group_id": "g_state", "label": "状态测试", "order": 0,
                    "keywords": [{
                        "keyword_id": "kw_state", "keyword_text": "状态测试词",
                        "today_best": 2, "coverage_days": 11, "kw_score": {"heat": 9},
                    }],
                }],
                "keywords": [{"keyword_id": "kw_state", "keyword_text": "状态测试词", "today_best": 2}],
                "total": 1, "ranked_total": 1, "not_ranked_total": 0,
            },
            "bootstrap": {
                "scope": {"total": 1, "pinned": 0},
                "keywords": [{"keyword_id": "kw_state", "keyword": "状态测试词", "runs": [{"id": "r1"}], "today_best": 2}],
            },
            "full": {
                "keywords": [{"keyword_id": "kw_state", "keyword": "状态测试词", "runs": [{"id": "r1"}], "today_best": 2}],
                "pinned_keyword_count": 0, "keyword_bucket_options": [],
            },
            "keyword": {
                "keyword_id": "kw_state", "keyword": "状态测试词",
                "runs": [{"id": "r1"}], "features": {"coverage_days": 11},
            },
        }
        for kind, payload in projections.items():
            subject = "kw_state" if kind == "keyword" else ""
            con.execute(
                """INSERT INTO wechat_legacy_projections(
                   projection_id,projection_kind,subject_id,payload_json,source_hash,
                   source_manifest_id,source_ref,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (f"p_{kind}", kind, subject, json.dumps(payload, ensure_ascii=False),
                 f"hash-{kind}", "freeze-test", "freeze-test", "2026-01-01T00:00:00Z"),
            )
        for contract in ("keyword-manage", "bootstrap", "keyword", "monitor-data"):
            con.execute(
                """INSERT INTO migration_switches(
                   switch_id,module_key,contract_key,data_mode,updated_at,updated_by
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(module_key,contract_key) DO UPDATE SET
                        data_mode=excluded.data_mode,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by""",
                (f"sw_{contract}", "wechat-search", contract, "hub", "2026-01-01T00:00:00Z", "test"),
            )


def test_state_command_service_rejects_missing_or_blank_idempotency_key(settings) -> None:
    service = StateCommandService(settings)
    for key in (None, "", "   "):
        with pytest.raises(ValidationAppError, match="非空 idempotency_key"):
            service.execute(
                "keyword.note",
                {"command": {"keyword_id": "kw_state", "note": "x"}},
                lambda _: {"ok": True},
                idempotency_key=key,
            )


def test_wechat_state_commands_cover_w02_w06_w08_w16(settings) -> None:
    _seed(settings)
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                base = {"keyword": "状态测试词"}
                for index, path in enumerate((
                    "/api/keywords/kw_state/pin",
                    "/api/keywords/kw_state/unpin",
                    "/api/keywords/kw_state/topic",
                    "/api/keywords/kw_state/note",
                    "/api/keywords/kw_state/bucket",
                )):
                    missing_keyword = await client.post(
                        path, json={"idempotency_key": f"missing-keyword-{index}"}
                    )
                    assert missing_keyword.status_code == 400
                    assert missing_keyword.json() == {"error": "keyword is required"}
                missing_group = await client.post(
                    "/api/keyword-manage/keywords",
                    json={"keyword_text": "缺分组", "idempotency_key": "missing-group"},
                )
                assert missing_group.status_code == 400
                assert missing_group.json() == {"error": "group_id is required"}
                missing_key = await client.post(
                    "/api/keywords/kw_state/pin",
                    json={"keyword": "状态测试词"},
                )
                assert missing_key.status_code == 400
                assert missing_key.json() == {
                    "error": "状态命令必须提供非空 idempotency_key。"
                }
                blank_key = await client.post(
                    "/api/keywords/kw_state/pin",
                    json={"keyword": "状态测试词", "idempotency_key": "   "},
                )
                assert blank_key.status_code == 400
                explicit_pin = await client.post(
                    "/api/keywords/kw_state/pin",
                    json={"keyword": "状态测试词", "idempotency_key": "w02"},
                )
                assert explicit_pin.status_code == 200
                for path, body, method in (
                    ("/api/keywords/kw_state/unpin", {**base, "idempotency_key": "w03"}, "post"),
                    ("/api/keywords/kw_state/topic", {**base, "topic": "主题", "idempotency_key": "w04"}, "post"),
                    ("/api/keywords/kw_state/note", {**base, "note": "备注", "idempotency_key": "w05"}, "post"),
                    ("/api/keywords/kw_state/bucket", {**base, "keyword_bucket": "热门单品", "idempotency_key": "w06"}, "post"),
                    ("/api/keyword-manage/keywords/kw_state/refresh-policy", {"refresh_frequency_days": 7, "source": "manual", "idempotency_key": "w14"}, "patch"),
                    ("/api/keyword-manage/keywords/kw_state/commercial-value", {"score": 8, "reason": "人工", "idempotency_key": "w15"}, "patch"),
                    ("/api/keyword-manage/keywords/kw_state/auto-archive-lock", {"locked": True, "idempotency_key": "w16"}, "patch"),
                ):
                    response = await getattr(client, method)(path, json=body)
                    assert response.status_code == 200, (path, response.text)
                state = (await client.get("/api/keyword-manage")).json()
                keyword = state["groups"][0]["keywords"][0]
                assert keyword["topic"] == "主题"
                assert keyword["keyword_bucket"] == "热门单品"
                assert keyword["note"] == "备注"
                assert keyword["refresh_frequency_days"] == 7
                assert keyword["commercial_value_score"] == 8
                assert keyword["auto_archive_locked"] is True
                assert keyword["today_best"] == 2
                assert keyword["coverage_days"] == 11
                bootstrap = (await client.get("/api/monitor-data/bootstrap")).json()
                assert bootstrap["keywords"][0]["topic"] == "主题"
                # Bootstrap is intentionally compact; full run details are
                # loaded by the keyword detail endpoint.
                assert "runs" not in bootstrap["keywords"][0]
                detail = (await client.get("/api/monitor-data/keyword/kw_state")).json()
                assert detail["topic"] == "主题"
                assert detail["runs"] == [{"id": "r1"}]
                note_string = await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    json={"note": 12345, "idempotency_key": "w12-note-string"},
                )
                assert note_string.status_code == 200
                assert note_string.json()["note"] == "12345"
                lock_truthy = await client.patch(
                    "/api/keyword-manage/keywords/kw_state/auto-archive-lock",
                    json={"locked": "yes", "idempotency_key": "w16-yes"},
                )
                assert lock_truthy.status_code == 200
                assert lock_truthy.json()["auto_archive_locked"] is True
                for index, value in enumerate(("false", "no", "off", "maybe", None)):
                    lock_false = await client.patch(
                        "/api/keyword-manage/keywords/kw_state/auto-archive-lock",
                        json={"locked": value, "idempotency_key": f"w16-false-{index}"},
                    )
                    assert lock_false.status_code == 200
                    assert lock_false.json()["auto_archive_locked"] is False

                created = await client.post(
                    "/api/keyword-manage/groups",
                    json={"label": "新组", "idempotency_key": "w08"},
                )
                assert created.status_code == 200
                group_id = created.json()["group_id"]
                renamed = await client.patch(
                    f"/api/keyword-manage/groups/{group_id}",
                    json={"label": "新组2", "order": 4, "idempotency_key": "w09"},
                )
                assert renamed.status_code == 200
                moved_existing = await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    json={"group_id": group_id, "idempotency_key": "w12-existing"},
                )
                assert moved_existing.status_code == 200
                after_move = (await client.get("/api/keyword-manage")).json()
                moved_node = next(
                    item for group in after_move["groups"] for item in group["keywords"]
                    if item["keyword_id"] == "kw_state"
                )
                assert moved_node["today_best"] == 2
                assert moved_node["coverage_days"] == 11
                assert next(group for group in after_move["groups"] if group["group_id"] == group_id)["ranked_count"] == 1
                assert after_move["ranked_total"] == 1
                new_keyword = await client.post(
                    "/api/keyword-manage/keywords",
                    json={"group_id": group_id, "keyword_text": "新增测试词", "note": "n", "idempotency_key": "w11"},
                )
                assert new_keyword.status_code == 200
                new_id = new_keyword.json()["keyword_id"]
                with connect(settings) as con:
                    con.execute(
                        """INSERT INTO search_snapshots(
                           snapshot_id,platform,keyword,keyword_id,captured_at,result_count
                        ) VALUES(?,?,?,?,?,?)""",
                        ("snapshot_before_archive", "wechat-search", "新增测试词", new_id, "2026-01-02T00:00:00Z", 0),
                    )
                after_create = (await client.get("/api/keyword-manage")).json()
                assert any(item["keyword_id"] == new_id for group in after_create["groups"] for item in group["keywords"])
                moved = await client.patch(
                    f"/api/keyword-manage/keywords/{new_id}",
                    json={"note": "n2", "group_id": "g_state", "idempotency_key": "w12"},
                )
                assert moved.status_code == 200
                archived = await client.request(
                    "DELETE",
                    f"/api/keyword-manage/keywords/{new_id}",
                    json={"idempotency_key": "w13"},
                )
                assert archived.status_code == 200
                with connect(settings, readonly=True) as con:
                    assert con.execute(
                        "SELECT status FROM keywords WHERE keyword_id=?", (new_id,)
                    ).fetchone()["status"] == "archived"
                    assert con.execute(
                        "SELECT COUNT(*) FROM search_snapshots WHERE keyword_id=?", (new_id,)
                    ).fetchone()[0] == 1
                after_archive = (await client.get("/api/keyword-manage")).json()
                assert all(item["keyword_id"] != new_id for group in after_archive["groups"] for item in group["keywords"])
                await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    json={"group_id": "g_state", "idempotency_key": "w12-back"},
                )
                assert (await client.request("DELETE", f"/api/keyword-manage/groups/{group_id}", json={"idempotency_key": "w10"})).status_code == 200
                recreated = await client.post(
                    "/api/keyword-manage/groups",
                    json={"label": "新组2", "idempotency_key": "w17"},
                )
                assert recreated.status_code == 400
                assert recreated.json() == {"error": "分组已存在：新组2"}

    asyncio.run(scenario())


def test_wechat_state_idempotency_concurrency_and_failure_persist(settings) -> None:
    _seed(settings)
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                responses = await asyncio.gather(
                    *[
                        client.post(
                            "/api/keywords/kw_state/topic",
                            json={"keyword": "状态测试词", "topic": "并发主题", "idempotency_key": "same-key"},
                        )
                        for _ in range(4)
                    ]
                )
                assert {response.status_code for response in responses} == {200}
                conflict = await client.post(
                    "/api/keywords/kw_state/topic",
                    json={"keyword": "状态测试词", "topic": "冲突", "idempotency_key": "same-key"},
                )
                assert conflict.status_code == 409
                replay_with_header = await client.post(
                    "/api/keywords/kw_state/topic",
                    headers={"Idempotency-Key": "same-key"},
                    json={"keyword": "状态测试词", "topic": "并发主题"},
                )
                assert replay_with_header.status_code == 200
                normalized_note = await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    json={"note": 123, "idempotency_key": "normalized-note"},
                )
                assert normalized_note.status_code == 200
                normalized_note_replay = await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    headers={"Idempotency-Key": "normalized-note"},
                    json={"note": "123"},
                )
                assert normalized_note_replay.status_code == 200
                legacy_false = await client.patch(
                    "/api/keyword-manage/keywords/kw_state/auto-archive-lock",
                    json={"locked": "maybe", "idempotency_key": "legacy-false"},
                )
                assert legacy_false.status_code == 200
                assert legacy_false.json()["auto_archive_locked"] is False
                missing = await client.post(
                    "/api/keywords/missing/topic",
                    json={"keyword": "missing", "topic": "x", "idempotency_key": "failed-key"},
                )
                assert missing.status_code == 404
                assert missing.json() == {"error": "keyword not found: missing"}
        with connect(settings, readonly=True) as con:
            command = con.execute(
                "SELECT status,error_json FROM command_runs WHERE idempotency_key='failed-key'"
            ).fetchone()
            assert command["status"] == "failed"
            assert con.execute(
                "SELECT COUNT(*) FROM dual_write_receipts WHERE idempotency_key='same-key'"
            ).fetchone()[0] == 1
            assert con.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='wechat.keyword.topic'"
            ).fetchone()[0] >= 1
            assert con.execute(
                "SELECT COUNT(*) FROM command_runs WHERE idempotency_key LIKE 'implicit-%'"
            ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_wechat_keyword_rename_and_archive_detail_projection_lifecycle(
    settings,
) -> None:
    _seed(settings)
    with connect(settings) as con:
        con.execute(
            """INSERT INTO search_snapshots(
               snapshot_id,platform,keyword,keyword_id,captured_at,result_count
            ) VALUES(?,?,?,?,?,?)""",
            (
                "snapshot_before_rename",
                "wechat-search",
                "状态测试词",
                "kw_state",
                "2026-01-02T00:00:00Z",
                1,
            ),
        )
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                before = await client.get(
                    "/api/monitor-data/keyword/kw_state"
                )
                assert before.status_code == 200
                before_run_ids = {item["id"] for item in before.json()["runs"]}
                assert {"snapshot_before_rename", "r1"} <= before_run_ids

                renamed = await client.patch(
                    "/api/keyword-manage/keywords/kw_state",
                    json={
                        "keyword_text": "状态测试改名词",
                        "idempotency_key": "rename-detail-lifecycle",
                    },
                )
                assert renamed.status_code == 200
                renamed_id = renamed.json()["keyword_id"]
                assert renamed_id != "kw_state"

                assert (
                    await client.get("/api/monitor-data/keyword/kw_state")
                ).status_code == 404
                renamed_detail = await client.get(
                    f"/api/monitor-data/keyword/{renamed_id}"
                )
                assert renamed_detail.status_code == 200
                assert renamed_detail.json()["keyword_id"] == renamed_id
                assert renamed_detail.json()["keyword_text"] == "状态测试改名词"
                assert renamed_detail.json()["runs"] == [{"id": "r1"}]
                assert renamed_detail.json()["features"] == {
                    "coverage_days": 11
                }

                bootstrap = (
                    await client.get("/api/monitor-data/bootstrap")
                ).json()
                ids = {
                    item["keyword_id"]
                    for item in bootstrap["keywords"]
                }
                assert "kw_state" not in ids
                assert renamed_id in ids

                archived = await client.request(
                    "DELETE",
                    f"/api/keyword-manage/keywords/{renamed_id}",
                    json={"idempotency_key": "archive-renamed-detail"},
                )
                assert archived.status_code == 200
                assert (
                    await client.get(
                        f"/api/monitor-data/keyword/{renamed_id}"
                    )
                ).status_code == 404

        with connect(settings, readonly=True) as con:
            statuses = {
                row["keyword_id"]: row["status"]
                for row in con.execute(
                    """SELECT keyword_id,status FROM keywords
                       WHERE keyword_id IN (?,?)""",
                    ("kw_state", renamed_id),
                )
            }
            assert statuses == {
                "kw_state": "archived",
                renamed_id: "archived",
            }
            assert con.execute(
                """SELECT COUNT(*) FROM search_snapshots
                   WHERE snapshot_id='snapshot_before_rename'
                     AND keyword_id='kw_state'"""
            ).fetchone()[0] == 1
            assert con.execute(
                """SELECT COUNT(*) FROM wechat_legacy_projections
                   WHERE projection_kind='keyword'
                     AND subject_id IN (?,?)""",
                ("kw_state", renamed_id),
            ).fetchone()[0] >= 2

    asyncio.run(scenario())
