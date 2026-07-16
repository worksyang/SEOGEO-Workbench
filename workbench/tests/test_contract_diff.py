from __future__ import annotations

from content_hub.db.connection import connect
from content_hub.services.contract_diff import (
    ContractRule,
    ContractRuleRegistry,
    HTTPMetadata,
    PayloadWithHTTPMetadata,
    compare_values,
    default_registry,
    payload_hash,
)
from content_hub.services.migration import MigrationResolver


def test_normalized_hash_and_numeric_tolerance() -> None:
    assert payload_hash({"b": 1.0, "a": 1}) == payload_hash({"a": 1, "b": 1.0})
    assert not compare_values({"v": 1.0}, {"v": 1.0 + 1e-6}).diffs
    assert compare_values({"v": 1.0}, {"v": 1.0 + 1.1e-6}).diffs


def test_iso_timezone_and_strict_text_semantics() -> None:
    assert payload_hash({"at": "2026-07-16T08:00:00Z"}) == payload_hash(
        {"at": "2026-07-16T16:00:00+08:00"}
    )
    assert not compare_values({"at": "2026-07-16T08:00:00Z"}, {"at": "2026-07-16T16:00:00+08:00"}).diffs
    assert compare_values({"v": None}, {"v": ""}).diffs
    assert compare_values({"v": 0}, {"v": None}).diffs
    assert compare_values({"v": " text "}, {"v": "text"}).diffs


def test_keyword_manage_updated_at_has_bounded_contract_tolerance() -> None:
    registry = default_registry("wechat-search", "keyword-manage")
    assert not compare_values(
        {"updated_at": "2026-07-16T11:08:08Z"},
        {"updated_at": "2026-07-16T11:08:09Z"},
        registry=registry,
    ).diffs
    six_seconds = compare_values(
        {"updated_at": "2026-07-16T11:08:08Z"},
        {"updated_at": "2026-07-16T11:08:14Z"},
        registry=registry,
    )
    assert six_seconds.diffs[0]["rule"] == "keyword-manage-request-clock"
    assert compare_values(
        {"updated_at": "2026-07-16T11:08:08Z"},
        {},
        registry=registry,
    ).diffs
    assert compare_values(
        {"updated_at": "not-an-iso-time"},
        {"updated_at": "2026-07-16T11:08:09Z"},
        registry=registry,
    ).diffs
    assert compare_values(
        {"updated_at": "2026-07-16T11:08:08Z"},
        {"updated_at": "2026-07-16T11:08:09Z"},
        registry=default_registry("wechat-search", "monitor-data"),
    ).diffs


def test_pointer_order_and_explicit_unordered_rule() -> None:
    ordered = compare_values({"items": [{"id": "a"}, {"id": "b"}]}, {"items": [{"id": "b"}, {"id": "a"}]})
    assert {item["json_pointer"] for item in ordered.diffs} == {"/items/0/id", "/items/1/id"}
    registry = ContractRuleRegistry((ContractRule("/items", unordered_by="id", name="business-key"),))
    assert not compare_values(
        {"items": [{"id": "a", "v": 1}, {"id": "b", "v": 2}]},
        {"items": [{"id": "b", "v": 2}, {"id": "a", "v": 1}]},
        registry=registry,
    ).diffs


def test_different_length_arrays_compare_each_index() -> None:
    result = compare_values({"items": [1, 2]}, {"items": [1, 2, 3]})
    assert [(item["json_pointer"], item["kind"]) for item in result.diffs] == [
        ("/items/2", "added")
    ]


def test_recursive_ignore_rule_and_metadata_headers() -> None:
    registry = ContractRuleRegistry((
        ContractRule("/**/request_id", ignore=True, name="recursive-audit"),
    ))
    result = compare_values(
        {"meta": {"request_id": "a", "status": "old"}},
        {"meta": {"request_id": "b", "status": "new"}},
        registry=registry,
    )
    assert [item["json_pointer"] for item in result.diffs] == ["/meta/status"]
    metadata = compare_values(
        HTTPMetadata.from_mapping({"Status-Code": 200, "Content-Type": "application/json"}).to_dict(),
        HTTPMetadata.from_mapping({"status_code": 304, "content-type": "text/plain"}).to_dict(),
    )
    assert {item["json_pointer"] for item in metadata.diffs} >= {"/status_code", "/content_type"}


def test_nested_pointer_missing_null_and_bounded_values() -> None:
    result = compare_values(
        {"nested": {"x/y": "a" * 40}},
        {"nested": {"x/y": "b" * 40, "new": None}},
        max_value_bytes=16,
    )
    assert {item["json_pointer"] for item in result.diffs} == {"/nested/x~1y", "/nested/new"}
    assert result.truncated is True
    assert any("truncated" in item["legacy_value_json"] or "truncated" in item["hub_value_json"] for item in result.diffs)


def test_resolver_records_field_diffs_and_keeps_legacy_result(settings) -> None:
    resolver = MigrationResolver(settings, module_key="wechat-search", contract_key="bootstrap")
    value, metadata = resolver.compare(
        request_fingerprint="test-fingerprint",
        legacy=lambda: PayloadWithHTTPMetadata(
            {"items": [{"id": "a"}], "request_id": "legacy"},
            HTTPMetadata(status_code=200, content_type="application/json", etag="a"),
        ),
        hub=lambda: PayloadWithHTTPMetadata(
            {"items": [{"id": "b"}], "request_id": "hub"},
            HTTPMetadata(status_code=200, content_type="application/json", etag="b"),
        ),
        max_value_bytes=64,
    )
    assert value["items"][0]["id"] == "a"
    assert metadata["comparison"]["status"] == "different"
    with connect(settings, readonly=True) as con:
        comparison = con.execute(
            "SELECT diff_json,diff_count,diffs_truncated FROM contract_comparisons WHERE comparison_id=?",
            (metadata["comparison"]["comparison_id"],),
        ).fetchone()
        diffs = con.execute(
            "SELECT json_pointer,kind FROM contract_comparison_diffs WHERE comparison_id=? ORDER BY diff_id",
            (metadata["comparison"]["comparison_id"],),
        ).fetchall()
    assert '"legacy"' not in comparison["diff_json"]
    assert comparison["diff_count"] == 2
    assert [(row["json_pointer"], row["kind"]) for row in diffs] == [
        ("/items/0/id", "changed"),
        ("/@response/etag", "changed"),
    ]
    summary = __import__("json").loads(comparison["diff_json"])
    assert summary["comparison_basis"] == "payload+http_metadata"
    assert summary["hash_equal"] is False


def test_tolerance_can_match_with_different_hash(settings) -> None:
    resolver = MigrationResolver(settings, module_key="wechat-search", contract_key="monitor-data")
    _, metadata = resolver.compare(
        request_fingerprint="tolerance",
        legacy=lambda: {"value": 1.0},
        hub=lambda: {"value": 1.0 + 1e-7},
    )
    assert metadata["comparison"]["status"] == "matched"
    assert metadata["comparison"]["hash_equal"] is False


def test_resolver_error_status_and_legacy_result(settings) -> None:
    resolver = MigrationResolver(settings, module_key="wechat-search", contract_key="bootstrap")
    value, metadata = resolver.compare(
        request_fingerprint="error-fingerprint",
        legacy=lambda: {"ok": True},
        hub=lambda: (_ for _ in ()).throw(RuntimeError("hub down")),
    )
    assert value == {"ok": True}
    assert metadata["comparison"]["status"] == "hub_error"
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT diff_json FROM contract_comparisons WHERE comparison_id=?",
            (metadata["comparison"]["comparison_id"],),
        ).fetchone()
    assert '"type": "RuntimeError"' in row["diff_json"]
    assert "hub down" in row["diff_json"]
    assert metadata["comparison"]["diff_count"] == 0


def test_compare_writes_sanitized_audit_evidence_for_all_outcomes(settings) -> None:
    resolver = MigrationResolver(settings, module_key="wechat-search", contract_key="bootstrap")
    cases = [
        ("matched-audit-body-secret", lambda: {"value": 1}, lambda: {"value": 1}, "matched"),
        ("different-audit-body-secret", lambda: {"value": 1}, lambda: {"value": 2}, "different"),
        (
            "legacy-error-audit-body-secret",
            lambda: (_ for _ in ()).throw(RuntimeError("legacy secret body")),
            lambda: {"value": 1},
            "legacy_error",
        ),
        (
            "hub-error-audit-body-secret",
            lambda: {"value": 1},
            lambda: (_ for _ in ()).throw(RuntimeError("hub secret body")),
            "hub_error",
        ),
    ]
    for fingerprint, legacy, hub, expected_status in cases:
        try:
            _, result = resolver.compare(
                request_fingerprint=fingerprint,
                legacy=legacy,
                hub=hub,
            )
        except Exception:
            with connect(settings, readonly=True) as con:
                row = con.execute(
                    "SELECT comparison_id FROM contract_comparisons "
                    "WHERE request_fingerprint=? ORDER BY compared_at DESC LIMIT 1",
                    (fingerprint,),
                ).fetchone()
            comparison_id = row["comparison_id"]
        else:
            comparison_id = result["comparison"]["comparison_id"]

        with connect(settings, readonly=True) as con:
            audit = con.execute(
                "SELECT details_json, outcome FROM audit_log WHERE action='migration.compare' "
                "AND subject_id=?",
                (comparison_id,),
            ).fetchone()
        details = __import__("json").loads(audit["details_json"])
        assert details["module"] == "wechat-search"
        assert details["contract"] == "bootstrap"
        assert details["comparison_id"] == comparison_id
        assert details["status"] == expected_status
        assert details["request_fingerprint"] != fingerprint
        assert "secret body" not in audit["details_json"]
        assert audit["outcome"] == ("failed" if "error" in expected_status else "succeeded")


def test_zero_diff_budget_marks_truncated_and_both_errors_are_kept(settings) -> None:
    resolver = MigrationResolver(settings, module_key="wechat-search", contract_key="monitor-data")
    _, limited = resolver.compare(
        request_fingerprint="zero-budget",
        legacy=lambda: {"a": 1},
        hub=lambda: {"a": 2},
        max_diffs=0,
    )
    assert limited["comparison"]["status"] == "different"
    assert limited["comparison"]["diff_count"] == 0
    assert limited["comparison"]["diffs_truncated"] is True

    def fail_legacy() -> dict:
        raise ValueError("legacy down")

    def fail_hub() -> dict:
        raise OSError("hub down")

    try:
        resolver.compare(request_fingerprint="both-errors", legacy=fail_legacy, hub=fail_hub)
    except Exception:
        pass
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT diff_json,diff_count,diffs_truncated FROM contract_comparisons "
            "WHERE request_fingerprint='both-errors'"
        ).fetchone()
    assert row["diff_count"] == 0
    assert row["diffs_truncated"] == 0
    assert "legacy down" in row["diff_json"] and "hub down" in row["diff_json"]


def test_default_registry_does_not_ignore_business_nested_request_id() -> None:
    """Plan §8.2 只允许 ``/request_id``、``/compared_at``、``/meta/...`` 等
    绝对审计路径被吞掉；``/**/`` 全深度通配会把 ``/audit/request_id``、
    ``/search/request_id`` 等业务字段错误忽略，导致 compare 假 matched。
    """
    registry = default_registry("wechat-search", "monitor-data")
    business_paths = (
        "/audit/request_id",
        "/search/request_id",
        "/foo/bar/request_id",
        "/data/request_id",
    )
    for pointer in business_paths:
        rule = registry.for_path(pointer)
        assert rule is None or rule.ignore is False, (
            f"{pointer!r} should not be ignored by default registry, "
            f"got {rule!r}"
        )
        # And the compare must surface the difference as a real diff.
        result = compare_values(
            {"audit": {"request_id": "a"}},
            {"audit": {"request_id": "b"}},
            registry=registry,
        )
        assert any(
            item["json_pointer"] == "/audit/request_id" and item["kind"] == "changed"
            for item in result.diffs
        ), f"missing /audit/request_id diff: {result.diffs!r}"


def test_default_registry_does_not_ignore_business_nested_compared_at() -> None:
    """``/search/compared_at`` 这类业务时间戳必须参与 compare，不能被通配
    ignore 吞掉（否则两个不同时间的快照会判 matched）。"""
    registry = default_registry("wechat-search", "monitor-data")
    result = compare_values(
        {"search": {"compared_at": "2026-07-16T00:00:00Z"}},
        {"search": {"compared_at": "2026-07-16T00:00:01Z"}},
        registry=registry,
    )
    assert any(
        item["json_pointer"] == "/search/compared_at" and item["kind"] == "changed"
        for item in result.diffs
    ), f"missing /search/compared_at diff: {result.diffs!r}"


def test_default_registry_ignores_explicit_audit_paths_only() -> None:
    """默认 registry 仅忽略 plan §8.2 列出的绝对审计路径。"""
    registry = default_registry("wechat-search", "monitor-data")
    expected_ignored = {
        "/request_id",
        "/compared_at",
        "/meta/request_id",
        "/meta/compared_at",
    }
    actual_ignored = {
        rule.pointer
        for rule in registry.rules
        if rule.ignore and rule.name == "request-audit"
    }
    assert actual_ignored == expected_ignored


def test_keyword_manage_request_clock_tolerance_still_holds() -> None:
    """``/updated_at`` 在 keyword-manage 上有专属 5 秒时钟容差，缩窄
    request-audit 通配后必须仍生效。"""
    registry = default_registry("wechat-search", "keyword-manage")
    result = compare_values(
        {"updated_at": "2026-07-16T11:08:08Z"},
        {"updated_at": "2026-07-16T11:08:09Z"},
        registry=registry,
    )
    assert result.diffs == []


def test_explicit_recursive_ignore_rule_still_works_for_custom_registry() -> None:
    """自定义 registry 仍然可以使用 ``/**/`` 显式通配（仅在 default 中
    被禁）。这是 plan §8.2 没明文禁止的扩展点，保留向后兼容。"""
    registry = ContractRuleRegistry(
        (ContractRule("/**/request_id", ignore=True, name="recursive-audit"),)
    )
    result = compare_values(
        {"meta": {"request_id": "a", "status": "old"}},
        {"meta": {"request_id": "b", "status": "new"}},
        registry=registry,
    )
    assert [item["json_pointer"] for item in result.diffs] == ["/meta/status"]
