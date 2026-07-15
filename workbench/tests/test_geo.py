from __future__ import annotations

from dataclasses import replace
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from content_hub.adapters.geo import GeoAdapter, RedfoxAdapter, parse_markdown, scrub, stable_source_id
from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.errors import AppError
from content_hub.features.geo.service import GeoService, _file_manifest, _utc


REAL_DB = Path("/Users/works14/Documents/zkcode/GEOProMax/data/index/geopromax.sqlite")


def configured(settings, tmp_path: Path):
    return replace(
        settings,
        geo_database_path=REAL_DB,
        geo_source_root=REAL_DB.parents[2],
        geo_redfox_root=REAL_DB.parents[2] / "data/redfox",
    )


def test_geo_schema_and_real_counts(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    status = service.status()
    assert status["source_status"]["read_only"] is True
    assert status["source"]["counts"]["answers"] == 1164
    assert status["source"]["counts"]["sources"] == 7811
    assert status["source"]["counts"]["source_url_distinct"] == 7078
    assert status["source"]["counts"]["source_duplicate_groups"] == 702
    assert status["source"]["counts"]["source_duplicate_rows"] == 1435
    assert stable_source_id("手机网易网", "https://example.com") == "4e64f5d7d4132f1929e4"
    assert status["redfox"]["snapshot_count"] == 603
    assert _utc("2026-07-07 10:00:00") == "2026-07-07T02:00:00.000Z"
    assert _utc("2026-07-07T10:00:00+08:00") == "2026-07-07T02:00:00.000Z"
    assert status["redfox"]["snapshot_count"] == 603


def test_geo_all_source_identity_and_raw_canonical_platforms(settings, tmp_path):
    adapter = GeoAdapter(configured(settings, tmp_path))
    data = adapter.records()
    assert len(data["batches"]) == 6
    assert len(data["answers"]) == 1164
    assert len(data["tools"]) == 1071
    assert len(data["keywords"]) == 3557
    assert len(data["sources"]) == 7811
    assert len(data["relations"]) == 20028
    assert len(data["suggestions"]) == 1816
    assert len(data["metrics"]) == 9135
    assert all(stable_source_id(row["platform"], row["url"]) == row["id"] for row in data["sources"])
    assert any(row["raw_platform"] != row["canonical_platform"] for row in data["sources"])
    assert data["answers"][0]["app"] == "豆包"
    assert data["answers"][0]["channel"] == "mobile"


def test_geo_limit_is_an_answer_associated_subset(settings, tmp_path):
    data = GeoAdapter(configured(settings, tmp_path)).records(limit=2)
    answer_ids = {row["id"] for row in data["answers"]}
    tool_ids = {row["id"] for row in data["tools"]}
    source_ids = {row["source_id"] for row in data["relations"]}
    assert len(answer_ids) == 2
    assert all(row["answer_id"] in answer_ids for row in data["tools"])
    assert all(row["tool_id"] in tool_ids for row in data["keywords"])
    assert all(row["answer_id"] in answer_ids for row in data["relations"])
    assert all(row["answer_id"] in answer_ids for row in data["suggestions"])
    assert all(row["answer_id"] in answer_ids for row in data["metrics"])
    assert all(row["id"] in source_ids for row in data["sources"])


def test_geo_questions_aggregate_real_repeated_answers(settings, tmp_path):
    result = GeoService(configured(settings, tmp_path)).questions(limit=10000)
    assert result["total"] == 194
    assert result["excluded_answer_count"] == 1
    assert sum(item["answer_count"] for item in result["items"]) == 1163
    assert max(item["answer_count"] for item in result["items"]) >= 2
    for item in result["items"]:
        captured = [snapshot["captured_at"] for snapshot in item["snapshots"]]
        assert all(captured)
        assert captured == sorted(captured)


def test_geo_question_summary_contract_and_bulk_query(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    connects = 0
    original = service.adapter._connect

    def counted():
        nonlocal connects
        connects += 1
        return original()

    monkeypatch.setattr(service.adapter, "_connect", counted)
    result = service.questions(limit=10000)
    assert connects == 1
    item = next(row for row in result["items"] if row["question"] == "225提领靠谱吗？")
    assert item["question_id"] == f"geo_question_{hashlib.sha256(item['question'].encode()).hexdigest()[:24]}"
    assert item["answer_count"] == 6
    assert item["latest_answer_id"] == 971
    assert item["status_counts"] == {"answered": 3, "failed": 2, "input_not_ready": 1}
    assert item["first_captured_at"] <= item["latest_captured_at"]
    assert item["answers"] == item["snapshots"]
    assert all(
        {
            "markdown_available", "relation_count", "source_count", "platform_count",
            "creator_count", "relation_type_counts",
        } <= set(snapshot)
        for snapshot in item["snapshots"]
    )


def test_geo_question_detail_uses_real_relation_positions(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    question = "225提领靠谱吗？"
    question_id = f"geo_question_{hashlib.sha256(question.encode()).hexdigest()[:24]}"
    result = service.question_detail(question_id)
    columns = result["citation_matrix"]["columns"]
    rows = result["citation_matrix"]["rows"]
    assert [column["answer_id"] for column in columns] == [1, 195, 389, 583, 777, 971]
    assert result["totals"] == {
        "snapshot_count": 6,
        "source_count": 34,
        "platform_count": 8,
        "creator_count": 8,
        "relation_count": 51,
    }
    missing_markdown = next(item for item in result["snapshots"] if item["id"] == 389)
    assert missing_markdown["markdown_available"] is False
    with service.adapter._connect() as con:
        expected = {
            (row["source_id"], row["answer_id"]): row["rank"]
            for row in con.execute(
                """
                SELECT r.source_id,r.answer_id,MIN(r.position) AS rank
                FROM source_relations r JOIN answers a ON a.id=r.answer_id
                WHERE a.question=? AND r.source_id IS NOT NULL
                GROUP BY r.source_id,r.answer_id
                """,
                (question,),
            )
        }
    for source in rows:
        assert len(source["ranks"]) == len(columns)
        assert source["ranks"] == [
            expected.get((source["source_id"], column["answer_id"]))
            for column in columns
        ]
        assert source["hit_snapshots"] == sum(rank is not None for rank in source["ranks"])
        assert source["best_rank"] == min(rank for rank in source["ranks"] if rank is not None)
        assert source["relation_types"]
        assert {"raw_platform", "canonical_platform", "platform_mapped", "author", "author_profile_link"} <= set(source)
    answer_without_markdown = service.detail("answer", 389)
    assert answer_without_markdown["markdown"]["exists"] is False
    assert answer_without_markdown["markdown"]["error"] == "missing_path"
    with pytest.raises(AppError):
        service.question_detail("geo_question_not_found")


def test_geo_answer_detail_preserves_batch_tools_suggestions_relations_and_metrics(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    detail = service.detail("answer", 1)
    assert detail["app"] == "豆包"
    assert detail["channel"] == "mobile"
    assert detail["raw_batch"]["input_file"]
    assert detail["raw_batch"]["output_file"]
    assert detail["tools"][0]["position"] == 1
    assert detail["keywords"][0]["position"] == 1
    assert detail["suggested_questions"][0]["position"] == 1
    assert detail["markdown"]["exists"] is True
    assert detail["markdown"]["content"].strip()
    relation = next(item for item in service.detail("answer", 991)["relations"] if item["type"] == "image_reference")
    assert "image_url" in relation and "tool_id" in relation and "anchor_index" in relation and "error" in relation
    assert {"search_result", "text_reference", "image_reference"} <= {item["type"] for item in service.detail("answer", 991)["relations"]}
    assert {"read_count", "like_count", "comment_count", "favorite_count", "share_count"} <= set(detail["metrics"][0])
    missing_markdown = service.detail("answer", 389)["markdown"]
    assert missing_markdown["exists"] is False
    assert missing_markdown["error"] == "missing_path"


def test_geo_answer_detail_nested_tools_citations_and_real_types(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    detail = service.detail("answer", 1)
    assert [tool["position"] for tool in detail["tools_nested"]] == sorted(tool["position"] for tool in detail["tools_nested"])
    for tool in detail["tools_nested"]:
        assert [item["position"] for item in tool["search_keywords"]] == sorted(item["position"] for item in tool["search_keywords"])
    assert detail["metrics_observed_at"] == detail["captured_at"]
    assert detail["platform_summary"]["denominator"] == "relation_count"
    assert detail["platform_summary"]["relation_count"] == len(detail["relations"])
    citation = next(item for item in detail["citations"] if item["source"])
    assert {"raw_platform", "canonical_platform", "platform_mapped", "author", "author_profile_link", "url", "markdown", "file_hash", "content_hash"} <= set(citation["source"])
    assert set(citation["metrics"]) == {"read_count", "like_count", "comment_count", "favorite_count", "share_count"}
    assert citation["metrics_observed_at"] == detail["captured_at"]
    sampled = [service.detail("answer", item_id) for item_id in (1, 991, 913)]
    assert {"search_result", "text_reference", "image_reference", "related_video"} == {
        citation["type"] for answer in sampled for citation in answer["citations"]
    }
    image = next(citation for citation in sampled[1]["citations"] if citation["type"] == "image_reference")
    assert {"image_url", "error", "tool_id", "anchor_index"} <= set(image)


def test_geo_source_overview_real_totals_filters_and_creators(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    result = service.source_overview(limit=5)
    assert {
        "identifier_count": 7811,
        "source_count": 7811,
        "source_row_count": 7811,
        "url_count": 7078,
        "distinct_url_count": 7078,
        "citation_count": 20028,
        "question_count": 194,
        "creator_count": 674,
        "author_count": 674,
    }.items() <= result["totals"].items()
    assert result["total"] == 674
    assert result["count"] == 5
    assert sum(item["citation_count"] for item in result["platforms"]) == 20028
    assert pytest.approx(sum(item["share_of_citations"] for item in result["platforms"])) == 1
    assert all(item["name"] and item["creator_id"].startswith("creator_") for item in result["creators"])
    platform = result["platforms"][0]["canonical_platform"]
    filtered = service.source_overview(platform=platform, limit=1000)
    assert filtered["platforms"] and all(item["canonical_platform"] == platform for item in filtered["platforms"])
    assert all(item["canonical_platform"] == platform for item in filtered["creators"])
    alias_filtered = service.source_overview(platform="手机网易网", limit=1000)
    assert [item["canonical_platform"] for item in alias_filtered["platforms"]] == ["网易"]
    assert alias_filtered["filters"]["platform"] == "手机网易网"
    assert alias_filtered["filters"]["platform_canonical"] == "网易"
    creator = result["creators"][0]
    searched = service.source_overview(q=creator["name"], limit=1000)
    assert any(item["creator_id"] == creator["creator_id"] for item in searched["creators"])
    assert any(item["canonical_platform"] == creator["canonical_platform"] for item in searched["platforms"])
    alias_search = service.source_overview(q="手机网易网", limit=1000)
    assert any(
        item["canonical_platform"] == "网易" and "手机网易网" in item["raw_platforms"]
        for item in alias_search["platforms"]
    )
    author_search = service.source_overview(q="紫荆保险规划", limit=1000)
    author = next(item for item in author_search["creators"] if item["name"] == "紫荆保险规划")
    assert any(item["canonical_platform"] == "抖音" for item in author_search["platforms"])
    profile_search = service.source_overview(q=author["profile_url"], limit=1000)
    assert any(item["creator_id"] == author["creator_id"] for item in profile_search["creators"])
    assert author_search["search_fields"] == [
        "canonical_platform", "raw_platforms", "creator_name", "creator_profile_url",
    ]
    empty = service.source_overview(q="不存在的GEO平台或作者_019f602c", limit=10, offset=0)
    assert empty["platforms"] == []
    assert empty["creators"] == []
    assert empty["total"] == empty["count"] == 0
    assert empty["totals"] == result["totals"]
    assert empty["filters"]["q"] == "不存在的GEO平台或作者_019f602c"
    for kwargs in ({"limit": 0}, {"limit": 1001}, {"limit": True}, {"offset": -1}, {"offset": True}):
        with pytest.raises(AppError):
            service.source_overview(**kwargs)


def test_geo_source_author_domain_and_five_metrics(settings, tmp_path):
    data = GeoAdapter(configured(settings, tmp_path)).records(limit=3)
    source = next(item for item in data["sources"] if item["author"])
    assert source["author_profile_link"] is None or source["author_profile_link"].startswith(("http://", "https://"))
    assert "summary" in source and "favicon" in source and "cover_image" in source
    assert Path(source["markdown_path"]).name.endswith(".md")
    assert urlparse(source["url"]).hostname
    assert {"read_count", "like_count", "comment_count", "favorite_count", "share_count"} <= set(data["metrics"][0])


def test_geo_dry_run_has_no_hub_write_and_reports_mapping(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    before = service.status()["hub"]
    assert service.bootstrap()["hub_import_status"]["status"] == "not_checked"
    result = service.preview(limit=2)
    assert result["dry_run"] is True
    assert result["writes"] is False
    assert result["source"]["database"] == "[REDACTED_PATH]"
    assert result["importable"]["answers"] == 2
    assert service.status()["hub"] == before


def test_geo_confirm_gate_idempotent_and_sensitive_filter(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    with pytest.raises(AppError):
        service.import_history(confirm=False, limit=1)
    first = service.import_history(confirm=True, limit=1)
    second = service.import_history(confirm=True, limit=1)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    filtered = scrub({"token": "secret", "nested": {"api_key": "x"}})
    assert filtered["token"] == "[REDACTED]"
    assert filtered["nested"]["api_key"] == "[REDACTED]"
    assert scrub({"next": "https://example.test/x?token=abc&ok=1"})["next"] == "https://example.test/x?token=%5BREDACTED%5D&ok=1"


def test_geo_scrub_masks_absolute_local_paths_but_preserves_relative_source_refs():
    value = scrub({
        "absolute": "/Users/alice/Documents/GEOProMax/data/answer.md",
        "private": "/private/var/folders/x/cache.json",
        "home": "~/Library/Application Support/geopromax.sqlite",
        "source_ref": "geopromax:123",
        "url": "https://example.test/a?secret=abc&ok=1",
    })
    assert value["absolute"] == "[REDACTED_PATH]"
    assert value["private"] == "[REDACTED_PATH]"
    assert value["home"] == "[REDACTED_PATH]"
    assert value["source_ref"] == "geopromax:123"
    assert value["url"] == "https://example.test/a?secret=%5BREDACTED%5D&ok=1"


def test_geo_import_persists_no_absolute_paths_and_uses_canonical_connection(settings, tmp_path):
    configured_settings = configured(settings, tmp_path)
    service = GeoService(configured_settings)
    with connect(configured_settings) as con:
        migration = con.execute(
            """
            SELECT details_json
            FROM audit_log
            WHERE audit_id='audit_migration_0004_geo_connection'
            """
        ).fetchone()
        assert migration is not None
        assert con.execute("SELECT 1 FROM system_connections WHERE system_key='geopromax'").fetchone() is None
        geo = con.execute(
            "SELECT details_json FROM system_connections WHERE system_key='geo'"
        ).fetchone()
        assert json.loads(geo["details_json"]) == {
            "source_kind": "canonical_registry",
            "migrated_from": "geopromax",
        }
        assert "/Users/" not in migration["details_json"]
        assert "/private/" not in migration["details_json"]
        con.commit()
    result = service.import_history(confirm=True, limit=1)
    assert result["status"] == "succeeded"
    with connect(configured_settings, readonly=True) as con:
        assert con.execute("SELECT 1 FROM system_connections WHERE system_key='geopromax'").fetchone() is None
        assert con.execute("SELECT status FROM system_connections WHERE system_key='geo'").fetchone()[0] == "healthy"
        rows = con.execute(
            "SELECT source_ref,error_json,payload_json FROM ingestion_batches "
            "UNION ALL SELECT source_ref,'',payload_json FROM geo_answers"
        ).fetchall()
        assert rows
        audit = con.execute(
            "SELECT details_json FROM audit_log WHERE action='geo_import' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        texts = [row["source_ref"] + row["error_json"] + row["payload_json"] for row in rows]
        texts.append(audit["details_json"])
        assert all("/Users/" not in text and "/private/" not in text for text in texts)
        assert "legacy_connection_migrated" not in audit["details_json"]


def test_geo_missing_markdown_is_partial_failed_and_not_healthy(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    original = service.adapter.records

    def missing_markdown(*, limit=None):
        data = original(limit=limit)
        data["answers"][0]["markdown_path"] = "missing-from-import.md"
        return data

    monkeypatch.setattr(service.adapter, "records", missing_markdown)
    result = service.import_history(confirm=True, limit=1)
    assert result["status"] == "partial_failed"
    assert service.bootstrap()["hub_import_status"]["status"] == "degraded"
    assert service.status()["hub_import_status"]["records_failed"] >= 1
    with connect(service.settings, readonly=True) as con:
        assert con.execute("SELECT status FROM system_connections WHERE system_key='geo'").fetchone()[0] == "degraded"
        audit = con.execute(
            "SELECT outcome,details_json FROM audit_log WHERE action='geo_import' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        assert audit["outcome"] == "failed"
        assert "missing_answer_markdown" in audit["details_json"] or "missing_markdown" in audit["details_json"]


def test_geo_manifest_changes_create_new_batch_even_when_counts_match(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    first = service.import_history(confirm=True, limit=1)
    original = service.adapter.records

    def changed(*, limit=None):
        data = original(limit=limit)
        data["answers"][0]["error"] = "same-count-change"
        return data

    monkeypatch.setattr(service.adapter, "records", changed)
    second = service.import_history(confirm=True, limit=1)
    assert first["manifest_id"] != second["manifest_id"]
    assert second["idempotent"] is False
    with connect(service.settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0] == 2


def test_geo_replay_after_manifest_change_keeps_canonical_reuse(settings, tmp_path, monkeypatch):
    configured_settings = configured(settings, tmp_path)
    source = GeoAdapter(configured_settings).records(limit=1)["sources"][0]
    with connect(configured_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            ("shared_content", "external_article", "共享事实", source["url"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"),
        )
        con.commit()
    service = GeoService(configured_settings)
    first = service.import_history(confirm=True, limit=1)
    original = service.adapter.records

    def changed(*, limit=None):
        data = original(limit=limit)
        data["answers"][0]["error"] = "manifest-change"
        return data

    monkeypatch.setattr(service.adapter, "records", changed)
    second = service.import_history(confirm=True, limit=1)
    assert first["status"] == second["status"] == "succeeded"
    with connect(configured_settings, readonly=True) as con:
        assert con.execute("SELECT content_id FROM content_identifiers WHERE namespace='geopromax_sqlite_source' AND external_id=?", (source["id"],)).fetchone()[0] == "shared_content"


def test_geo_hash_semantics_file_vs_normalized_content_and_invalid_time(settings, tmp_path):
    path = tmp_path / "answer.md"
    raw = b"---\r\ntitle: x\r\n---\r\nBody  \r\nline\r\n"
    path.write_bytes(raw)
    parsed = parse_markdown(path, tmp_path)
    assert parsed["file_hash"] == hashlib.sha256(raw).hexdigest()
    assert parsed["content_hash"] == hashlib.sha256("Body\nline".encode()).hexdigest()
    assert parsed["answer_hash"] == parsed["content_hash"]
    assert parsed["file_hash"] != parsed["answer_hash"]
    assert parse_markdown(path, tmp_path)["file_hash"] == parsed["file_hash"]
    assert _utc("not-a-time") is None


def test_geo_preview_warnings_separate_source_and_answer_rejects(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    original = service.adapter.records

    def broken(*, limit=None):
        data = original(limit=limit)
        data["sources"][0]["id"] = "bad-source-id"
        data["answers"][0]["markdown_path"] = "missing.md"
        return data

    monkeypatch.setattr(service.adapter, "records", broken)
    warnings = service.preview(limit=1)["warnings"]
    assert any("source.id" in item for item in warnings)
    assert any("回答 Markdown" in item for item in warnings)


def test_geo_cross_system_content_reuse_preserves_other_fact_and_geo_identifier(settings, tmp_path):
    configured_settings = configured(settings, tmp_path)
    source = GeoAdapter(configured_settings).records(limit=1)["sources"][0]
    with connect(configured_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            ("other_content", "external_article", "其他系统标题", source["url"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", '{"origin":"other"}'),
        )
        con.commit()
    result = GeoService(configured_settings).import_history(confirm=True, limit=1)
    assert result["status"] == "succeeded"
    with connect(configured_settings, readonly=True) as con:
        row = con.execute("SELECT title FROM contents WHERE content_id='other_content'").fetchone()
        assert row["title"] == "其他系统标题"
        identifier = con.execute("SELECT content_id,payload_json FROM content_identifiers WHERE namespace='geopromax_sqlite_source' AND external_id=?", (source["id"],)).fetchone()
        assert identifier["content_id"] == "other_content"
        assert json.loads(identifier["payload_json"])["raw_platform"] == source["platform"]
        relation_payload = con.execute("SELECT payload_json FROM geo_source_relations LIMIT 1").fetchone()
        assert json.loads(relation_payload["payload_json"])["source_fact"]["raw_url"]


def test_geo_identifier_conflict_is_rejected_without_remap(settings, tmp_path):
    configured_settings = configured(settings, tmp_path)
    source = GeoAdapter(configured_settings).records(limit=1)["sources"][0]
    with connect(configured_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("wrong_content", "external_article", "既有", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"),
        )
        con.execute(
            "INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?)",
            ("geopromax_sqlite_source", source["id"], "wrong_content", "2026-01-01T00:00:00Z", "{}"),
        )
        con.commit()
    service = GeoService(configured_settings)
    canonical, _ = service.adapter.canonical_platform(source["platform"])
    creator_id = service._creator_id(canonical, source["author"], source.get("author_profile_link"))[0] if source.get("author") else None
    result = service.import_history(confirm=True, limit=1)
    assert result["status"] == "partial_failed"
    with connect(configured_settings, readonly=True) as con:
        assert con.execute("SELECT content_id FROM content_identifiers WHERE namespace='geopromax_sqlite_source' AND external_id=?", (source["id"],)).fetchone()[0] == "wrong_content"
        batch = con.execute("SELECT records_seen,records_written,records_failed FROM ingestion_batches").fetchone()
        assert batch["records_failed"] <= batch["records_seen"]
        assert con.execute("SELECT details_json FROM audit_log WHERE action='geo_import'").fetchone()
        if creator_id:
            assert con.execute("SELECT 1 FROM creators WHERE creator_id=?", (creator_id,)).fetchone() is None


def test_geo_http_routes_and_parameter_errors(settings, tmp_path):
    configured_settings = configured(settings, tmp_path)
    app = create_app(configured_settings)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/v1/geo/status")).status_code == 200
            assert (await client.get("/api/v1/geo/batches?limit=1")).json()["data"]["count"] == 1
            questions = await client.get("/api/v1/geo/questions?limit=2")
            assert questions.status_code == 200
            question_items = questions.json()["data"]["items"]
            assert question_items and all(item["answer_count"] >= 1 for item in question_items)
            assert all(
                item["answers"][index]["captured_at"] <= item["answers"][index + 1]["captured_at"]
                for item in question_items
                for index in range(len(item["answers"]) - 1)
            )
            question_detail = await client.get(f"/api/v1/geo/questions/{question_items[0]['question_id']}")
            assert question_detail.status_code == 200
            assert "citation_matrix" in question_detail.json()["data"]
            assert (await client.get("/api/v1/geo/questions/geo_question_missing")).status_code == 404
            answer = await client.get("/api/v1/geo/answers/1")
            assert answer.status_code == 200 and answer.json()["data"]["app"] == "豆包"
            assert "citations" in answer.json()["data"]
            assert (await client.get("/api/v1/geo/answers/999999")).status_code == 404
            assert (await client.get("/api/v1/geo/answers/not-a-number")).status_code == 422
            assert (await client.post("/api/v1/geo/answers/999999/refresh/preview")).status_code == 404
            assert (await client.post("/api/v1/geo/answers/1/refresh/confirm", json={"confirm": False})).status_code == 409
            assert (await client.post("/api/v1/geo/answers/1/refresh/confirm", json={"confirm": True})).status_code == 409
            sources = await client.get("/api/v1/geo/sources?limit=1")
            assert "raw_platform" in sources.json()["data"]["items"][0]
            overview = await client.get("/api/v1/geo/source-overview?limit=2")
            assert overview.status_code == 200 and overview.json()["data"]["totals"]["citation_count"] == 20028
            alias_overview = await client.get("/api/v1/geo/source-overview", params={"q": "手机网易网", "limit": 1000})
            assert any(item["canonical_platform"] == "网易" for item in alias_overview.json()["data"]["platforms"])
            alias_filter = await client.get("/api/v1/geo/source-overview", params={"platform": "手机网易网", "limit": 1000})
            assert [item["canonical_platform"] for item in alias_filter.json()["data"]["platforms"]] == ["网易"]
            assert alias_filter.json()["data"]["filters"]["platform_canonical"] == "网易"
            empty_overview = await client.get("/api/v1/geo/source-overview", params={"q": "不存在的GEO平台或作者_019f602c"})
            assert empty_overview.json()["data"]["platforms"] == []
            assert empty_overview.json()["data"]["creators"] == []
            assert (await client.get("/api/v1/geo/source-overview?limit=0")).status_code == 422
            assert (await client.get("/api/v1/geo/source-overview?limit=1001")).status_code == 422
            assert (await client.get("/api/v1/geo/source-overview?offset=-1")).status_code == 422
            assert (await client.post("/api/v1/geo/dry-run", json={"limit": 1})).status_code == 200
            assert (await client.post("/api/v1/geo/dry-run", json={"limit": True})).status_code == 422
            assert (await client.post("/api/v1/geo/import", json={"limit": "1", "confirm": False})).status_code == 422
            assert (await client.post("/api/v1/geo/import", json={"limit": 0, "confirm": False})).status_code == 422
            assert (await client.post("/api/v1/geo/import", json={"confirm": False})).status_code == 409

    asyncio.run(run())


def test_geo_import_updates_connection_checkpoint_and_source_files_unchanged(settings, tmp_path):
    configured_settings = configured(settings, tmp_path)
    tracked = [
        configured_settings.geo_database_path,
        configured_settings.geo_platforms_path,
        configured_settings.geo_source_root / "data/answers/豆包/225提领靠谱吗？/2026-07-07-10-17-00_mobile_quick.md",
        configured_settings.geo_source_root / "data/sources/手机网易网/6e3b36dd6b8e4411348b.md",
    ]
    before = [(path, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in tracked]
    result = GeoService(configured_settings).import_history(confirm=True, limit=1)
    after = [(path, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in tracked]
    assert before == after
    with connect(configured_settings, readonly=True) as con:
        batch = con.execute("SELECT status,records_seen,records_written,records_failed,error_json FROM ingestion_batches WHERE batch_id=?", (result["batch_id"],)).fetchone()
        assert tuple(batch)[:4] == ("succeeded", result["records_seen"], result["records_imported"], 0)
        assert con.execute("SELECT COUNT(*) FROM contents WHERE content_type='ai_answer'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ingestion_checkpoints WHERE adapter_key='geopromax-sqlite'").fetchone()[0] == 1
        assert con.execute("SELECT status FROM system_connections WHERE system_key='geo'").fetchone()[0] == "healthy"


def test_geo_missing_bad_markdown_unknown_platform_and_redfox_guard(settings, tmp_path):
    service = GeoService(configured(settings, tmp_path))
    adapter = GeoAdapter(configured(settings, tmp_path))
    assert adapter.markdown("data/answers/not-found.md")["error"] == "missing"
    local = replace(configured(settings, tmp_path), geo_source_root=tmp_path)
    adapter = GeoAdapter(local)
    bad = tmp_path / "bad.md"
    bad.write_text("---\nno closing delimiter\n", encoding="utf-8")
    assert adapter.markdown("bad.md")["error"] == "bad_frontmatter"
    canonical, mapped = adapter.canonical_platform("未登记平台")
    assert canonical == "未登记平台"
    assert mapped is False
    with pytest.raises(AppError):
        service.refresh_confirm(1, True)
    with pytest.raises(Exception, match="禁止"):
        RedfoxAdapter(configured(settings, tmp_path)).batch_refresh()


def test_geo_refresh_contract_uses_boolean_configuration_only(settings, tmp_path, monkeypatch):
    no_key = GeoService(replace(configured(settings, tmp_path), geo_redfox_api_key_configured=False))
    expected = {
        "configured": False,
        "available": False,
        "blocked_reason": "missing_api_key",
        "paid": True,
        "requires_confirm": True,
    }
    assert expected.items() <= no_key.bootstrap()["refresh"].items()
    assert expected.items() <= no_key.preview(limit=1)["refresh"].items()
    assert no_key.refresh_preview(1)["blocked_reason"] == "missing_api_key"
    with pytest.raises(AppError):
        no_key.refresh_confirm(1, False)
    with pytest.raises(AppError):
        no_key.refresh_confirm(1, True)
    configured_key = GeoService(replace(configured(settings, tmp_path), geo_redfox_api_key_configured=True))
    refresh = configured_key.bootstrap()["refresh"]
    assert refresh["configured"] is True
    assert refresh["blocked_reason"] == "not_integrated"
    assert configured_key.preview(limit=1)["refresh"]["blocked_reason"] == "not_integrated"
    assert configured_key.refresh_preview(1)["blocked_reason"] == "not_integrated"
    env_example = (settings.workbench_root / ".env.example").read_text(encoding="utf-8")
    assert "# HUB_GEO_REDFOX_API_KEY" in env_example
    assert "HUB_GEO_REDFOX_API_KEY=" not in env_example
    assert not hasattr(settings, "geo_redfox_api_key")
    monkeypatch.delenv("HUB_GEO_REDFOX_API_KEY", raising=False)
    assert Settings.load().geo_redfox_api_key_configured is False
    monkeypatch.setenv("HUB_GEO_REDFOX_API_KEY", "configured")
    loaded = Settings.load()
    assert loaded.geo_redfox_api_key_configured is True
    assert not hasattr(loaded, "geo_redfox_api_key")


def test_geo_api_key_never_leaks_in_http_responses(settings, tmp_path, monkeypatch):
    secret = "placeholder-redfox-test-value"
    monkeypatch.setenv("HUB_GEO_REDFOX_API_KEY", secret)
    loaded = Settings.load()
    assert loaded.geo_redfox_api_key_configured is True
    configured_settings = replace(
        configured(settings, tmp_path),
        geo_redfox_api_key_configured=loaded.geo_redfox_api_key_configured,
    )
    app = create_app(configured_settings)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            responses = [
                await client.get("/api/v1/geo/bootstrap"),
                await client.get("/api/v1/geo/status"),
                await client.get("/api/v1/geo/redfox/read-only"),
                await client.post("/api/v1/geo/dry-run", json={"limit": 1}),
                await client.post("/api/v1/geo/answers/1/refresh/preview"),
                await client.post("/api/v1/geo/answers/1/refresh/confirm", json={"confirm": False}),
                await client.post("/api/v1/geo/answers/1/refresh/confirm", json={"confirm": True}),
            ]
            assert responses[-3].status_code == 200
            assert [response.status_code for response in responses[-2:]] == [409, 409]
            for response in responses:
                assert secret not in response.text
                assert "geo-redfox-secret-019f602c-do-not-return" not in response.text
            assert responses[0].json()["data"]["refresh"]["configured"] is True
            assert responses[0].json()["data"]["refresh"]["blocked_reason"] == "not_integrated"

    asyncio.run(run())


def test_geo_markdown_manifest_and_redfox_paths_are_root_contained(settings, tmp_path):
    local = replace(configured(settings, tmp_path), geo_source_root=tmp_path, geo_redfox_root=tmp_path / "redfox")
    local.geo_redfox_root.mkdir()
    inside = tmp_path / "inside.md"
    outside = tmp_path.parent / "outside.md"
    inside.write_text("# inside", encoding="utf-8")
    outside.write_text("# outside", encoding="utf-8")
    adapter = GeoAdapter(local)
    assert adapter.markdown("inside.md")["exists"] is True
    assert adapter.markdown("../outside.md")["error"] == "outside_root"
    assert _file_manifest(outside, root=tmp_path)["error"] == "outside_root"
    redfox = local.geo_redfox_root / "snapshot.md"
    redfox.write_text("# redfox", encoding="utf-8")
    assert adapter.markdown("snapshot.md", redfox=True)["exists"] is True


def test_geo_service_limit_and_markdown_cache(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    with pytest.raises(AppError):
        service.preview(limit=True)
    with pytest.raises(AppError):
        service.preview(limit=10001)
    calls = []
    original = service.adapter.markdown

    def counted(relative, *, redfox=False):
        calls.append(str(relative))
        return original(relative, redfox=redfox)

    monkeypatch.setattr(service.adapter, "markdown", counted)
    service.preview(limit=1)
    assert len(calls) == len(set(calls))


def test_geo_manifest_reuses_markdown_bytes_hash(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))
    monkeypatch.setattr(service, "_redfox_summary", lambda: {"snapshot_count": 0, "manifest_id": "test"})
    reads = {}
    original = Path.read_bytes

    def counted(path):
        value = original(path)
        if str(path).startswith(str(service.settings.geo_source_root)):
            reads[str(path)] = reads.get(str(path), 0) + 1
        return value

    monkeypatch.setattr(Path, "read_bytes", counted)
    service.preview(limit=1)
    assert reads
    assert all(count == 1 for count in reads.values())


def test_geo_failed_import_is_persisted_after_business_rollback(settings, tmp_path, monkeypatch):
    service = GeoService(configured(settings, tmp_path))

    def explode(*args, **kwargs):
        raise RuntimeError("injected geo failure")

    monkeypatch.setattr(service, "_upsert_source", explode)
    with pytest.raises(RuntimeError, match="injected geo failure"):
        service.import_history(confirm=True, limit=1)
    with connect(service.settings, readonly=True) as con:
        batch = con.execute("SELECT status,error_json FROM ingestion_batches").fetchone()
        assert batch["status"] == "failed"
        assert "injected geo failure" in batch["error_json"]
        audit = con.execute("SELECT outcome FROM audit_log WHERE action='geo_import' ORDER BY occurred_at DESC").fetchone()
        assert audit["outcome"] == "failed"
        assert con.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0


def test_geo_full_temp_import_preserves_verified_counts_and_single_deduplications(settings, tmp_path, monkeypatch):
    configured_settings = configured(settings, tmp_path)
    service = GeoService(configured_settings)
    source_rows = service.adapter.records()["sources"]
    by_url = {}
    for source in source_rows:
        by_url.setdefault(source["url"], []).append(source)
    first_source, second_source = next(group for group in by_url.values() if len(group) > 1)[:2]
    result = service.import_history(confirm=True)
    assert result["status"] == "partial_failed"
    assert result["records_failed"] == 54
    assert len(result["deduplications"]) == 733
    assert result["conflicts"] == []
    original_records = service.adapter.records

    def changed(*, limit=None):
        data = original_records(limit=limit)
        data["answers"][0]["error"] = "manifest-replay"
        return data

    monkeypatch.setattr(service.adapter, "records", changed)
    replay = service.import_history(confirm=True)
    assert len(replay["deduplications"]) == 733
    assert replay["conflicts"] == []
    with connect(configured_settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM geo_answers").fetchone()[0] == 1110
        assert con.execute("SELECT COUNT(*) FROM content_identifiers").fetchone()[0] == 7811
        assert con.execute("SELECT COUNT(DISTINCT canonical_url) FROM contents WHERE content_type='external_article'").fetchone()[0] == 7078
        assert con.execute("SELECT COUNT(*) FROM geo_source_relations").fetchone()[0] == 20028
        assert con.execute("SELECT COUNT(*) FROM metric_observations").fetchone()[0] == 18287
        first_identifier = con.execute(
            "SELECT content_id FROM content_identifiers WHERE namespace='geopromax_sqlite_source' AND external_id=?",
            (first_source["id"],),
        ).fetchone()
        second_identifier = con.execute(
            "SELECT content_id FROM content_identifiers WHERE namespace='geopromax_sqlite_source' AND external_id=?",
            (second_source["id"],),
        ).fetchone()
        first_content = con.execute("SELECT title FROM contents WHERE content_id=?", (first_identifier["content_id"],)).fetchone()
        assert first_identifier["content_id"] == second_identifier["content_id"]
        assert first_content["title"] == first_source["title"]


def test_geo_owned_source_updates_on_replay_but_shared_content_does_not(settings, tmp_path, monkeypatch):
    owned_settings = replace(settings, database_path=tmp_path / "owned.sqlite")
    migrate(owned_settings)
    owned = GeoService(configured(owned_settings, tmp_path))
    original_records = owned.adapter.records
    original_markdown = owned.adapter.markdown
    first_source = original_records(limit=1)["sources"][0]
    original_source_md = original_markdown(first_source["markdown_path"])
    owned.import_history(confirm=True, limit=1)

    def changed_records(*, limit=None):
        data = original_records(limit=limit)
        data["sources"][0]["title"] = "GEO 更新标题"
        return data

    def changed_markdown(relative, *, redfox=False):
        parsed = original_markdown(relative, redfox=redfox)
        if relative == first_source["markdown_path"]:
            parsed = {**parsed, "file_hash": "changed-file-hash", "content_hash": "changed-content-hash"}
        return parsed

    monkeypatch.setattr(owned.adapter, "records", changed_records)
    monkeypatch.setattr(owned.adapter, "markdown", changed_markdown)
    owned.import_history(confirm=True, limit=1)
    content_id = _id_for_test("content", f"geopromax-sqlite:{first_source['id']}")
    with connect(owned_settings, readonly=True) as con:
        row = con.execute("SELECT title,file_hash,content_hash FROM contents WHERE content_id=?", (content_id,)).fetchone()
        assert tuple(row) == ("GEO 更新标题", "changed-file-hash", "changed-content-hash")

    shared_settings = replace(settings, database_path=tmp_path / "shared.sqlite")
    migrate(shared_settings)
    shared = GeoService(configured(shared_settings, tmp_path))
    shared_source = shared.adapter.records(limit=1)["sources"][0]
    with connect(shared_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,file_hash,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            ("shared", "external_article", "共享原标题", shared_source["url"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "shared-hash", '{"origin":"other-system"}'),
        )
        con.commit()
    shared_original_records = shared.adapter.records
    shared_original_markdown = shared.adapter.markdown
    shared.import_history(confirm=True, limit=1)
    shared_source_md = shared_original_markdown(shared_source["markdown_path"])

    def changed_shared_records(*, limit=None):
        data = shared_original_records(limit=limit)
        data["sources"][0]["title"] = "不得覆盖共享标题"
        return data

    def changed_shared_markdown(relative, *, redfox=False):
        parsed = shared_original_markdown(relative, redfox=redfox)
        if relative == shared_source["markdown_path"]:
            parsed = {**parsed, "file_hash": "must-not-overwrite", "content_hash": "must-not-overwrite"}
        return parsed

    monkeypatch.setattr(shared.adapter, "records", changed_shared_records)
    monkeypatch.setattr(shared.adapter, "markdown", changed_shared_markdown)
    shared.import_history(confirm=True, limit=1)
    with connect(shared_settings, readonly=True) as con:
        row = con.execute("SELECT title,file_hash,payload_json FROM contents WHERE content_id='shared'").fetchone()
        assert tuple(row) == ("共享原标题", "shared-hash", '{"origin":"other-system"}')
        identifier = con.execute("SELECT payload_json FROM content_identifiers WHERE content_id='shared'").fetchone()
        assert json.loads(identifier["payload_json"])["title"] == "不得覆盖共享标题"


def test_geo_relation_and_metric_identity_conflicts_degrade_batch(settings, tmp_path, monkeypatch):
    configured_settings = configured(settings, tmp_path)
    service = GeoService(configured_settings)
    first = service.import_history(confirm=True, limit=1)
    original = service.adapter.records
    with connect(configured_settings) as con:
        relation = con.execute("SELECT relation_id FROM geo_source_relations LIMIT 1").fetchone()
        con.execute("UPDATE geo_source_relations SET relation_id='legacy-replayed-relation' WHERE relation_id=?", (relation["relation_id"],))
        observation = con.execute("SELECT observation_id FROM metric_observations WHERE source_ref='geopromax:1' LIMIT 1").fetchone()
        if observation:
            con.execute("UPDATE metric_observations SET observation_id='legacy-replayed-observation' WHERE observation_id=?", (observation["observation_id"],))
        con.commit()

    def changed(*, limit=None):
        data = original(limit=limit)
        data["answers"][0]["error"] = "force-conflict-replay"
        if data["relations"]:
            data["relations"][0]["id"] = 999999
        return data

    monkeypatch.setattr(service.adapter, "records", changed)
    result = service.import_history(confirm=True, limit=1)
    assert result["status"] == "partial_failed"
    assert {item["kind"] for item in result["conflicts"]} >= {"relation", "metric"}
    with connect(configured_settings, readonly=True) as con:
        audit = con.execute("SELECT outcome FROM audit_log WHERE action='geo_import' ORDER BY occurred_at DESC").fetchone()
        assert audit["outcome"] == "failed"
        assert con.execute("SELECT status FROM ingestion_batches WHERE batch_id=?", (result["batch_id"],)).fetchone()[0] == "partial_failed"


def _id_for_test(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:24]}"
