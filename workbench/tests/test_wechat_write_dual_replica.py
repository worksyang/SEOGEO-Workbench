from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.repositories.wechat_state import WechatStateRepository

from wechat_write_replica import (
    FREEZE_DATABASE,
    LegacyWriteReplica,
    normalized_body,
    projected_state,
    seed_hub_from_legacy,
)


pytestmark = pytest.mark.skipif(
    not FREEZE_DATABASE.is_file(),
    reason="微信冻结 payload 不在当前工作区",
)


def test_legacy_write_replica_uses_temp_code_and_preserves_freeze(
    tmp_path,
) -> None:
    legacy = LegacyWriteReplica(tmp_path)
    keyword = legacy.active_keywords(1)[0]
    response = legacy.request(
        "POST",
        f"/api/keywords/{keyword['keyword_id']}/pin",
        json_body={"keyword": keyword["keyword_text"]},
    )
    assert response.status_code == 200
    assert not list(legacy.isolated_code_root.rglob("*.pyc"))
    assert not list(legacy.isolated_code_root.rglob("__pycache__"))
    legacy.assert_source_unchanged()


def _flask_body(response):
    value = response.get_json(silent=True)
    return value if value is not None else response.get_data(as_text=True)


def _httpx_body(response: httpx.Response):
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    return response.text


async def _assert_pair(
    legacy: LegacyWriteReplica,
    hub: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict | None,
    key: str,
):
    legacy_body = dict(body or {})
    legacy_body["idempotency_key"] = key
    old = legacy.request(method, path, json_body=legacy_body)
    new = await hub.request(method, path, json=legacy_body)
    old_body = _flask_body(old)
    new_body = _httpx_body(new)
    assert new.status_code == old.status_code, (
        method,
        path,
        old.status_code,
        old_body,
        new.status_code,
        new_body,
    )
    assert normalized_body(new_body) == normalized_body(old_body), (
        method,
        path,
        old_body,
        new_body,
    )
    return old, new


def _find_keyword_node(payload: dict, keyword_id: str) -> dict:
    if payload.get("keyword_id") == keyword_id:
        return payload
    if isinstance(payload.get("keywords"), list):
        for item in payload["keywords"]:
            if isinstance(item, dict) and item.get("keyword_id") == keyword_id:
                return item
    for group in payload.get("groups", []):
        for item in group.get("keywords", []):
            if item.get("keyword_id") == keyword_id:
                return item
    raise AssertionError(f"projection missing keyword: {keyword_id}")


def test_w02_w16_write_dual_replica_happy_path_projection_restart_and_audit(
    settings, tmp_path
) -> None:
    legacy = LegacyWriteReplica(tmp_path)
    seed_hub_from_legacy(settings, legacy)
    fixtures = legacy.active_keywords(3)
    keyword = fixtures[0]
    keyword_id = keyword["keyword_id"]
    keyword_text = keyword["keyword_text"]
    original_group = keyword["group_id"]
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://hub-write-replica",
            ) as hub:
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/pin",
                    {"keyword": "前端传来的显示文本可以不同"},
                    "dual-w02",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/pin",
                    {"keyword": keyword_text},
                    "dual-w02-repeat",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/unpin",
                    {"keyword": keyword_text},
                    "dual-w03",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/unpin",
                    {"keyword": keyword_text},
                    "dual-w03-repeat",
                )
                assert legacy.rebuild_calls == []
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/topic",
                    {"keyword": keyword_text, "topic": "  双副本主题  "},
                    "dual-w04",
                )
                assert legacy.rebuild_calls == [
                    {"verbose": False, "full": False}
                ]
                await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/note",
                    {"keyword": keyword_text, "note": "  Unicode 备注🙂  "},
                    "dual-w05",
                )
                assert legacy.rebuild_calls == [
                    {"verbose": False, "full": False}
                ]
                old_bucket, new_bucket = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    f"/api/keywords/{keyword_id}/bucket",
                    {"keyword": keyword_text, "keyword_bucket": "  双副本桶  "},
                    "dual-w06",
                )
                assert legacy.rebuild_calls == [
                    {"verbose": False, "full": False},
                    {"verbose": False, "full": False},
                ]
                assert json.loads(
                    legacy.rebuild_probe_path.read_text(encoding="utf-8")
                ) == legacy.rebuild_calls

                # 旧 rebuild 调用由 Hub 的单事务 DB + 四类兼容投影替代。
                # 在后续其他写操作介入前，单独验证 topic/bucket 的等价结果。
                expected_after_bucket = projected_state(old_bucket.get_json())
                assert projected_state(new_bucket.json()) == expected_after_bucket
                with connect(settings, readonly=True) as con:
                    assert projected_state(
                        WechatStateRepository(con)._state(keyword_id)
                    ) == expected_after_bucket
                    stored = con.execute(
                        """SELECT k.topic,k.keyword_bucket,s.payload_json
                           FROM keywords k
                           JOIN search_keyword_settings s
                             ON s.keyword_id=k.keyword_id
                            AND s.system_key='wechat-search'
                            AND s.platform='wechat-search'
                           WHERE k.keyword_id=?""",
                        (keyword_id,),
                    ).fetchone()
                    assert stored["topic"] == "双副本主题"
                    assert stored["keyword_bucket"] == "双副本桶"
                    setting_payload = json.loads(stored["payload_json"])
                    assert setting_payload["topic"] == "双副本主题"
                    assert setting_payload["keyword_bucket"] == "双副本桶"
                for path in (
                    "/api/monitor-data/bootstrap",
                    "/api/monitor-data",
                    f"/api/monitor-data/keyword/{keyword_id}",
                    "/api/keyword-manage",
                ):
                    node = _find_keyword_node(
                        (await hub.get(path)).json(),
                        keyword_id,
                    )
                    assert projected_state(node) == expected_after_bucket

                _, group_a = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    "/api/keyword-manage/groups",
                    {"label": "双副本验收组 A"},
                    "dual-w08-a",
                )
                group_a_id = group_a.json()["group_id"]
                _, group_b = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    "/api/keyword-manage/groups",
                    {"label": "双副本验收组 B"},
                    "dual-w08-b",
                )
                group_b_id = group_b.json()["group_id"]
                await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/groups/{group_a_id}",
                    {"label": "双副本验收组 A2", "order": 70},
                    "dual-w09",
                )

                _, created = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    "/api/keyword-manage/keywords",
                    {
                        "group_id": group_a_id,
                        "keyword_text": "双副本验收新词",
                        "note": "初始",
                    },
                    "dual-w11",
                )
                created_id = created.json()["keyword_id"]
                await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/keywords/{created_id}",
                    {"group_id": group_b_id, "note": "  移动后备注  "},
                    "dual-w12",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "DELETE",
                    f"/api/keyword-manage/groups/{group_a_id}",
                    {},
                    "dual-w10-a",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "DELETE",
                    f"/api/keyword-manage/keywords/{created_id}",
                    {},
                    "dual-w13",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "DELETE",
                    f"/api/keyword-manage/groups/{group_b_id}",
                    {},
                    "dual-w10-b",
                )

                _, rename_source = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    "/api/keyword-manage/keywords",
                    {
                        "group_id": original_group,
                        "keyword_text": "双副本改名前词",
                        "note": "改名前",
                    },
                    "dual-w11-rename-source",
                )
                rename_source_id = rename_source.json()["keyword_id"]
                _, renamed = await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/keywords/{rename_source_id}",
                    {"keyword_text": "双副本改名后词", "note": "改名后"},
                    "dual-w12-rename",
                )
                renamed_id = renamed.json()["keyword_id"]
                assert renamed_id != rename_source_id
                old_detail = await hub.get(
                    f"/api/monitor-data/keyword/{rename_source_id}"
                )
                assert old_detail.status_code == 404
                renamed_detail = await hub.get(
                    f"/api/monitor-data/keyword/{renamed_id}"
                )
                assert renamed_detail.status_code == 200
                assert renamed_detail.json()["keyword_id"] == renamed_id
                assert renamed_detail.json()["keyword_text"] == "双副本改名后词"
                await _assert_pair(
                    legacy,
                    hub,
                    "DELETE",
                    f"/api/keyword-manage/keywords/{renamed_id}",
                    {},
                    "dual-w13-renamed",
                )
                assert (
                    await hub.get(f"/api/monitor-data/keyword/{renamed_id}")
                ).status_code == 404

                await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/keywords/{keyword_id}/refresh-policy",
                    {"source": "manual", "refresh_frequency_days": 7},
                    "dual-w14",
                )
                await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/keywords/{keyword_id}/commercial-value",
                    {"score": 8, "reason": "  双副本人工评分  "},
                    "dual-w15",
                )
                old_final, new_final = await _assert_pair(
                    legacy,
                    hub,
                    "PATCH",
                    f"/api/keyword-manage/keywords/{keyword_id}/auto-archive-lock",
                    {"locked": "yes"},
                    "dual-w16",
                )
                expected_state = projected_state(old_final.get_json())
                assert projected_state(new_final.json()) == expected_state

                # 显式幂等：第二次只重放回执，不新增命令；同键异输入必须 409。
                replay_payload = {
                    "keyword": keyword_text,
                    "note": "显式幂等备注",
                    "idempotency_key": "dual-explicit-replay",
                }
                legacy.request(
                    "POST",
                    f"/api/keywords/{keyword_id}/note",
                    json_body=replay_payload,
                )
                first = await hub.post(
                    f"/api/keywords/{keyword_id}/note", json=replay_payload
                )
                replay = await hub.post(
                    f"/api/keywords/{keyword_id}/note", json=replay_payload
                )
                assert replay.status_code == 200
                assert replay.json() == first.json()
                conflict = await hub.post(
                    f"/api/keywords/{keyword_id}/note",
                    json={**replay_payload, "note": "同键异输入"},
                )
                assert conflict.status_code == 409
                assert conflict.json() == {
                    "error": "idempotency key 已用于不同请求"
                }

                # 缺少显式幂等键必须在 Hub/router 边界拒绝，不生成 implicit-*。
                no_key_payload = {"keyword": keyword_text, "note": "无键连续备注"}
                before_commands = _command_count(settings, "implicit-%")
                first_implicit = await hub.post(
                    f"/api/keywords/{keyword_id}/note", json=no_key_payload
                )
                second_implicit = await hub.post(
                    f"/api/keywords/{keyword_id}/note", json=no_key_payload
                )
                assert first_implicit.status_code == second_implicit.status_code == 400
                assert first_implicit.json() == second_implicit.json() == {
                    "error": "状态命令必须提供非空 idempotency_key。"
                }
                assert _command_count(settings, "implicit-%") == before_commands

                # A 管理页与 B 管理投影的分组、顺序和旧字段一致。
                legacy_manage = legacy.request("GET", "/api/keyword-manage").get_json()
                hub_manage = (await hub.get("/api/keyword-manage")).json()
                assert _manage_contract(hub_manage) == _manage_contract(legacy_manage)

                # 主列表、全量、详情和管理投影同步，同时保留冻结动态字段。
                final_state = projected_state(
                    legacy.request(
                        "POST",
                        f"/api/keywords/{keyword_id}/note",
                        json_body={
                            "keyword": keyword_text,
                            "note": "投影最终备注",
                            "idempotency_key": "dual-final-note",
                        },
                    ).get_json()
                )
                hub_note = await hub.post(
                    f"/api/keywords/{keyword_id}/note",
                    json={
                        "keyword": keyword_text,
                        "note": "投影最终备注",
                        "idempotency_key": "dual-final-note",
                    },
                )
                assert hub_note.status_code == 200
                with connect(settings, readonly=True) as con:
                    assert projected_state(
                        WechatStateRepository(con)._state(keyword_id)
                    ) == final_state
                for path in (
                    "/api/monitor-data/bootstrap",
                    "/api/monitor-data",
                    f"/api/monitor-data/keyword/{keyword_id}",
                    "/api/keyword-manage",
                ):
                    payload = (await hub.get(path)).json()
                    node = _find_keyword_node(payload, keyword_id)
                    assert projected_state(node) == final_state
                    if path in {"/api/monitor-data", f"/api/monitor-data/keyword/{keyword_id}"}:
                        assert node["runs"] == [{"marker": "frozen-dynamic"}]
                    elif path == "/api/monitor-data/bootstrap":
                        # Bootstrap deliberately omits full run history; the
                        # keyword detail route above remains the source for it.
                        assert "runs" not in node

        # 重建应用模拟进程重启，状态、投影、命令与审计必须仍在。
        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://hub-write-restarted",
            ) as client:
                node = _find_keyword_node(
                    (await client.get("/api/monitor-data/bootstrap")).json(),
                    keyword_id,
                )
                assert node["note"] == "投影最终备注"
                assert node["auto_archive_locked"] is True
                assert node["commercial_value_score"] == 8
                assert node["refresh_frequency_days"] == 7

    try:
        asyncio.run(scenario())
    finally:
        legacy.assert_source_unchanged()

    expected_actions = {
        "wechat.keyword.pin",
        "wechat.keyword.unpin",
        "wechat.keyword.topic",
        "wechat.keyword.note",
        "wechat.keyword.bucket",
        "wechat.group.create",
        "wechat.group.update",
        "wechat.group.delete",
        "wechat.keyword.create",
        "wechat.keyword.update",
        "wechat.keyword.archive",
        "wechat.keyword.refresh_policy",
        "wechat.keyword.commercial_value",
        "wechat.keyword.auto_archive_lock",
    }
    with connect(settings, readonly=True) as con:
        actions = {
            row["action"]
            for row in con.execute(
                "SELECT DISTINCT action FROM audit_log WHERE action LIKE 'wechat.%'"
            )
        }
        assert expected_actions <= actions
        command_types = {
            row["command_type"]
            for row in con.execute(
                "SELECT DISTINCT command_type FROM command_runs WHERE module_key='wechat-search'"
            )
        }
        assert {item.removeprefix("wechat.") for item in expected_actions} <= command_types
        assert con.execute(
            """SELECT COUNT(*) FROM command_runs
               WHERE idempotency_key='dual-explicit-replay'"""
        ).fetchone()[0] == 1
        assert con.execute(
            """SELECT COUNT(*) FROM dual_write_receipts
               WHERE module_key='wechat-search' AND reconcile_status='matched'"""
        ).fetchone()[0] >= len(expected_actions)
        assert con.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE action='wechat.keyword.note' AND subject_id=?""",
            (keyword_id,),
        ).fetchone()[0] >= 1


def test_w02_w16_write_dual_replica_error_status_and_body(settings, tmp_path) -> None:
    legacy = LegacyWriteReplica(tmp_path)
    seed_hub_from_legacy(settings, legacy)
    first, second = legacy.active_keywords(2)
    keyword_id = first["keyword_id"]
    keyword_text = first["keyword_text"]
    group_id = first["group_id"]
    with legacy.connection() as con:
        group_label = con.execute(
            "SELECT label FROM keyword_groups WHERE group_id=?", (group_id,)
        ).fetchone()["label"]
    app = create_app(settings)

    cases = [
        ("POST", f"/api/keywords/{keyword_id}/pin", {}, "missing-keyword"),
        (
            "POST",
            "/api/keywords/missing/pin",
            {"keyword": "missing"},
            "legacy-500-missing-id",
        ),
        (
            "POST",
            "/api/keyword-manage/groups",
            {"label": group_label},
            "duplicate-group",
        ),
        (
            "PATCH",
            "/api/keyword-manage/groups/missing",
            {"label": "x"},
            "missing-group-update",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/groups/{group_id}",
            {"order": "bad"},
            "bad-group-order",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/groups/{group_id}",
            {"label": " "},
            "empty-group-label",
        ),
        (
            "DELETE",
            f"/api/keyword-manage/groups/{group_id}",
            {},
            "nonempty-group-delete",
        ),
        (
            "DELETE",
            "/api/keyword-manage/groups/missing",
            {},
            "missing-group-delete",
        ),
        (
            "POST",
            "/api/keyword-manage/keywords",
            {"group_id": "missing", "keyword_text": "新词"},
            "create-missing-group",
        ),
        (
            "POST",
            "/api/keyword-manage/keywords",
            {"group_id": group_id, "keyword_text": keyword_text},
            "duplicate-keyword",
        ),
        (
            "PATCH",
            "/api/keyword-manage/keywords/missing",
            {"note": "x"},
            "update-missing-keyword",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}",
            {"group_id": "missing"},
            "move-missing-group",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}",
            {"keyword_text": " "},
            "empty-keyword-text",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}",
            {"keyword_text": second["keyword_text"]},
            "duplicate-renamed-keyword",
        ),
        (
            "DELETE",
            "/api/keyword-manage/keywords/missing",
            {},
            "archive-missing-keyword",
        ),
        (
            "PATCH",
            "/api/keyword-manage/keywords/missing/refresh-policy",
            {"source": "manual", "refresh_frequency_days": 7},
            "policy-missing-keyword",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/refresh-policy",
            {"source": "manual"},
            "policy-missing-days",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/refresh-policy",
            {"source": "bad", "refresh_frequency_days": 7},
            "policy-bad-source",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/refresh-policy",
            {"source": "manual", "refresh_frequency_days": 2},
            "policy-bad-days",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/refresh-policy",
            {"source": "manual", "refresh_frequency_days": "bad"},
            "policy-bad-int",
        ),
        (
            "PATCH",
            "/api/keyword-manage/keywords/missing/commercial-value",
            {"score": 5},
            "commercial-missing-keyword",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/commercial-value",
            {"score": "bad"},
            "commercial-bad-int",
        ),
        (
            "PATCH",
            f"/api/keyword-manage/keywords/{keyword_id}/commercial-value",
            {"score": 11},
            "commercial-bad-range",
        ),
        (
            "PATCH",
            "/api/keyword-manage/keywords/missing/auto-archive-lock",
            {"locked": True},
            "lock-missing-keyword",
        ),
    ]

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://hub-write-errors",
            ) as hub:
                for method, path, body, key in cases:
                    if key in {
                        "legacy-500-missing-id",
                        "missing-group-update",
                        "missing-group-delete",
                        "create-missing-group",
                        "update-missing-keyword",
                        "move-missing-group",
                        "archive-missing-keyword",
                        "policy-missing-keyword",
                        "commercial-missing-keyword",
                        "lock-missing-keyword",
                    }:
                        request_body = {**(body or {}), "idempotency_key": key}
                        response = await hub.request(
                            method, path, json=request_body
                        )
                        assert response.status_code == 404, (key, response.text)
                        assert response.json()["error"].startswith(
                            ("keyword not found:", "group not found:")
                        )
                        continue
                    await _assert_pair(legacy, hub, method, path, body, key)

    try:
        asyncio.run(scenario())
    finally:
        legacy.assert_source_unchanged()


def test_archived_group_same_label_recreate_returns_json_400_and_preserves_state(
    settings,
    tmp_path,
) -> None:
    legacy = LegacyWriteReplica(tmp_path)
    seed_hub_from_legacy(settings, legacy)
    app = create_app(settings)
    label = "归档同名组双副本契约"

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://hub-archived-group-contract",
            ) as hub:
                _, created = await _assert_pair(
                    legacy,
                    hub,
                    "POST",
                    "/api/keyword-manage/groups",
                    {"label": label},
                    "archived-group-create",
                )
                group_id = created.json()["group_id"]
                await _assert_pair(
                    legacy,
                    hub,
                    "DELETE",
                    f"/api/keyword-manage/groups/{group_id}",
                    {},
                    "archived-group-delete",
                )
                new = await hub.post(
                    "/api/keyword-manage/groups",
                    json={
                        "label": label,
                        "idempotency_key": "archived-group-recreate",
                    },
                )
                assert new.status_code == 400
                assert new.json() == {"error": f"分组已存在：{label}"}

                with legacy.connection() as old_con, connect(
                    settings,
                    readonly=True,
                ) as new_con:
                    old_row = old_con.execute(
                        """SELECT group_id,label,archived_at
                           FROM keyword_groups WHERE label=?""",
                        (label,),
                    ).fetchone()
                    new_row = new_con.execute(
                        """SELECT group_id,group_name,archived_at
                           FROM search_keyword_groups
                           WHERE system_key='wechat-search'
                             AND platform='wechat-search'
                             AND group_name=?""",
                        (label,),
                    ).fetchone()
                    assert old_row["group_id"] == new_row["group_id"] == group_id
                    assert old_row["label"] == new_row["group_name"] == label
                    assert old_row["archived_at"]
                    assert new_row["archived_at"]
                    command = new_con.execute(
                        """SELECT status,error_json FROM command_runs
                           WHERE idempotency_key='archived-group-recreate'"""
                    ).fetchone()
                    assert command["status"] == "failed"
                    assert (
                        json.loads(command["error_json"])["code"]
                        == "VALIDATION_ERROR"
                    )

    try:
        asyncio.run(scenario())
    finally:
        legacy.assert_source_unchanged()


def _command_count(settings, pattern: str) -> int:
    with connect(settings, readonly=True) as con:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM command_runs WHERE idempotency_key LIKE ?",
                (pattern,),
            ).fetchone()[0]
        )


def _manage_contract(payload: dict) -> dict:
    return {
        "groups": [
            {
                "group_id": group.get("group_id"),
                "label": group.get("label"),
                "order": group.get("order"),
                "total": group.get("total"),
                "ranked_count": group.get("ranked_count"),
                "not_ranked_count": group.get("not_ranked_count"),
                "keywords": [
                    {
                        key: item.get(key)
                        for key in (
                            "keyword_id",
                            "keyword_text",
                            "note",
                            "batch_default_selected",
                            "refresh_frequency_days",
                            "effective_refresh_interval_hours",
                            "refresh_frequency_source",
                            "refresh_policy_reason",
                            "last_refresh_at",
                            "last_refresh_attempt_at",
                            "last_refresh_status",
                            "next_refresh_at",
                            "refresh_age_days",
                            "is_refresh_due",
                            "commercial_value_score",
                            "commercial_value_source",
                            "commercial_value_reason",
                            "lifecycle_stage",
                            "observation_started_at",
                            "observation_deadline_at",
                            "discovery_candidate_id",
                            "auto_archive_locked",
                            "archive_reason_code",
                            "archive_reason_detail",
                            "today_best",
                            "coverage_days",
                            "tracked_accounts",
                            "article_count",
                            "seo_status",
                        )
                    }
                    for item in group.get("keywords", [])
                ],
            }
            for group in payload.get("groups", [])
        ],
        "total": payload.get("total"),
        "ranked_total": payload.get("ranked_total"),
        "not_ranked_total": payload.get("not_ranked_total"),
    }
