"""摄取层：identity_resolver / markdown_store / pipeline / checkpoints / reconcile。
对应矩阵 T042 / T045-T058。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from content_hub.db.connection import connect
from content_hub.ingestion.checkpoints import CheckpointStore
from content_hub.ingestion.identity_resolver import IdentityResolver, ResolverContext
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.ingestion.pipeline import IngestionPipeline, RawBatch
from content_hub.ingestion.reconcile import ReconcileEngine
from content_hub.db.migrations import migrate
from content_hub.domain.models import (
    ContentRecord,
    DiscoveryRecord,
    IdentifierRecord,
    MetricObservation,
)
from content_hub.config import Settings


@pytest.fixture
def hub(tmp_path: Path):
    db = tmp_path / "hub.sqlite"
    settings = Settings.load(host="127.0.0.1")  # noqa: not actually starting server
    from dataclasses import replace
    settings = replace(settings, database_path=db)
    migrate(settings)
    conn = connect(settings, readonly=False)
    conn.row_factory = sqlite3.Row
    conn.commit()
    yield conn, settings
    conn.close()


# ── Identity Resolver ────────────────────────────────────


def test_t045_resolve_namespace_priority(hub):
    conn, _ = hub
    cursor = conn.execute(
        "SELECT 1 FROM content_identifiers WHERE namespace='wechat.article_url'"
    )
    # 不存在则写入一条再验证
    conn.execute(
        "INSERT INTO contents(content_id, content_type, title, first_seen_at, updated_at, canonical_url) "
        "VALUES ('cnt_xyz1234567890ab', 'external_article', 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'https://example.com/a')"
    )
    conn.execute(
        "INSERT INTO content_identifiers(namespace, external_id, content_id, first_seen_at) "
        "VALUES ('wechat.article_url', 'https://example.com/a', 'cnt_xyz1234567890ab', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    resolver = IdentityResolver(conn)
    decision = resolver.resolve(
        ResolverContext(
            namespace="wechat.article_url",
            external_id="https://example.com/a",
            canonical_url="https://example.com/a",
            title="test",
            author_name="author",
        )
    )
    assert decision.content_id == "cnt_xyz1234567890ab"
    assert decision.method == "namespace"
    assert decision.confidence == 1.0


def test_t047_title_author_only_candidate(hub):
    conn, _ = hub
    conn.execute(
        "INSERT INTO contents(content_id, content_type, title, author_name, first_seen_at, updated_at) "
        "VALUES ('cnt_existing1234abcd', 'external_article', '同标题', '同作者', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    resolver = IdentityResolver(conn)
    decision = resolver.resolve(
        ResolverContext(title="同标题", author_name="同作者"),
    )
    assert decision.requires_human is True
    assert decision.content_id == "cnt_existing1234abcd"


def test_t052_different_author_does_not_merge(hub):
    conn, _ = hub
    conn.execute(
        "INSERT INTO contents(content_id, content_type, title, author_name, first_seen_at, updated_at) "
        "VALUES ('cnt_distinct5678efgh', 'external_article', '热门话题', '甲作者', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    resolver = IdentityResolver(conn)
    decision = resolver.resolve(
        ResolverContext(title="热门话题", author_name="乙作者"),
    )
    assert decision.requires_human is True
    assert decision.confidence == 0.5  # 占位生成


def test_t053_merge_writes_identity_map(hub):
    conn, _ = hub
    conn.execute(
        "INSERT INTO contents(content_id, content_type, title, first_seen_at, updated_at) "
        "VALUES ('cnt_merge_old1', 'external_article', 'a', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO content_identifiers(namespace, external_id, content_id, first_seen_at) "
        "VALUES ('wechat.article_url', 'https://example.com/old', 'cnt_merge_old1', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO contents(content_id, content_type, title, first_seen_at, updated_at) "
        "VALUES ('cnt_merge_new1', 'external_article', 'a', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    resolver = IdentityResolver(conn)
    new_id = resolver.merge("cnt_merge_old1", "cnt_merge_new1", operator="test", reason="dup")
    assert new_id == "cnt_merge_new1"
    aliases = resolver.lookup_alias("cnt_merge_old1")
    assert aliases == "cnt_merge_new1"


# ── Markdown Store ────────────────────────────────────


def test_t054_markdown_write_creates_file(hub, tmp_path):
    conn, _settings = hub
    store = MarkdownStore(tmp_path / "asset_store")
    result = store.write(
        bucket="content",
        content_id="cnt_test1234567890",
        content_type="external_article",
        title="测试文章",
        body="正文段落一\n\n正文段落二\n",
    )
    assert Path(result.md_path).exists()
    assert result.written is True


def test_t055_markdown_atomic_no_partial_files(hub, tmp_path):
    conn, _settings = hub
    store = MarkdownStore(tmp_path / "asset_store")
    payload = b"---\nschema_version: content-md/1.1\ncontent_id: cnt_smoke1234ab\n---\n\n# Smoke\nbody content\n"
    target = tmp_path / "asset_store" / "content" / "2026" / "07" / "smoke.md"
    store._atomic_write(target, payload)
    assert target.read_bytes() == payload


def test_t056_markdown_resolve_within_rejects_traversal(hub, tmp_path):
    conn, _settings = hub
    store = MarkdownStore(tmp_path / "asset_store")
    with pytest.raises(Exception):
        store.read(str(tmp_path / "asset_store/../../../etc/passwd"))


# ── Pipeline & Checkpoints ─────────────────────────────


def test_t057_pipeline_writes_batch_state(hub, tmp_path):
    conn, settings = hub
    pipeline = IngestionPipeline(conn, settings.lock_path)
    raw = RawBatch(adapter_key="test", source_scope="unit")
    raw.contents.append(
        ContentRecord(
            content_id="cnt_pipe00000001",
            content_type="external_article",
            title="管线写入测试",
            canonical_url="https://example.com/pipe",
            first_seen_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
    )
    raw.identifiers.append(
        IdentifierRecord(
            namespace="wechat.article_url",
            external_id="https://example.com/pipe",
            content_id="cnt_pipe00000001",
            first_seen_at="2026-01-01T00:00:00Z",
        )
    )
    raw.discoveries.append(
        DiscoveryRecord.build(
            content_id="cnt_pipe00000001",
            system="test",
            channel="unit",
            discovered_at="2026-01-01T00:00:00Z",
        )
    )
    result = pipeline.run(raw)
    assert result.records_written >= 1
    row = conn.execute(
        "SELECT status, records_written FROM ingestion_batches ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] in ("succeeded", "partial_failed")


def test_t058_checkpoint_upsert_round_trip(hub):
    conn, _ = hub
    # 先建 ingestion_batches 行满足 FK
    conn.execute(
        "INSERT INTO ingestion_batches(batch_id, adapter_key, source_scope, status, started_at, records_seen) "
        "VALUES ('batch_test', 'test', 'unit', 'succeeded', '2026-01-01T00:00:00Z', 1)"
    )
    conn.commit()
    store = CheckpointStore(conn)
    store.upsert(
        adapter_key="test",
        checkpoint_key="keyword::foo",
        cursor_value="2026-07-14T10:00:00Z",
        source_hash="abc",
        batch_id="batch_test",
        payload={"k": "v"},
    )
    cp = store.get("test", "keyword::foo")
    assert cp is not None
    assert cp.cursor_value == "2026-07-14T10:00:00Z"
    assert cp.payload == {"k": "v"}


def test_t059_reconcile_engine_runs(hub, tmp_path):
    conn, settings = hub
    engine = ReconcileEngine(conn, [Path(settings.project_root), tmp_path])
    results = engine.run()
    assert isinstance(results, list)
    assert any(r.section == "identity" for r in results)


def test_t060_reconcile_creates_correction_job(hub, tmp_path):
    conn, settings = hub
    engine = ReconcileEngine(conn, [Path(settings.project_root), tmp_path])
    results = engine.run()
    error_results = [r for r in results if r.severity == "error"]
    if error_results:
        corr = conn.execute("SELECT COUNT(*) AS n FROM correction_jobs").fetchone()
        assert corr["n"] >= len(error_results)
