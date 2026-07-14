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
    assert sum(item["answer_count"] for item in result["items"]) == 1163
    assert max(item["answer_count"] for item in result["items"]) >= 2


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
    relation = next(item for item in service.detail("answer", 991)["relations"] if item["type"] == "image_reference")
    assert "image_url" in relation and "tool_id" in relation and "anchor_index" in relation and "error" in relation
    assert {"search_result", "text_reference", "image_reference"} <= {item["type"] for item in service.detail("answer", 991)["relations"]}
    assert {"read_count", "like_count", "comment_count", "favorite_count", "share_count"} <= set(detail["metrics"][0])


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
    result = service.preview(limit=2)
    assert result["dry_run"] is True
    assert result["writes"] is False
    assert result["source"]["database"] == str(REAL_DB)
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
            answer = await client.get("/api/v1/geo/answers/1")
            assert answer.status_code == 200 and answer.json()["data"]["app"] == "豆包"
            sources = await client.get("/api/v1/geo/sources?limit=1")
            assert "raw_platform" in sources.json()["data"]["items"][0]
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
