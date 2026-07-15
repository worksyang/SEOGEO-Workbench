from __future__ import annotations

import json
import asyncio
import hashlib
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
import httpx

from content_hub.adapters.xhs import XhsAdapter, XhsSourceError, RemoteResponse
from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.features.xhs.service import XhsService
from content_hub.errors import ConflictError


def _source(tmp_path: Path, *, changed: bool = False) -> Path:
    root = tmp_path / "normalized"
    root.mkdir(parents=True)
    rows = {
        "keywords.json": [{"keyword_id": "kw_1", "keyword_text": "香港保险", "is_active": True, "first_seen_at": "2026-07-01T00:00:00+08:00", "last_seen_at": "2026-07-01T00:00:00+08:00"}],
        "snapshots.json": [{"snapshot_id": "snap_1", "keyword_id": "kw_1", "captured_at": "2026-07-01T00:00:00+08:00", "result_count": 1}],
        "snapshot_terms.json": [],
        "ranking_hits.json": [{"hit_id": "hit_1", "snapshot_id": "snap_1", "rank": 1, "article_id": "xhs_tk_" + "a" * 24, "account_id": "b" * 24, "title_raw": "标题", "url_raw": "https://xhslink.com/a"}],
        "articles.json": [{"article_id": "xhs_tk_" + "a" * 24, "account_id": "b" * 24, "title": "标题", "normalized_url": "https://www.xiaohongshu.com/explore/" + "a" * 24, "raw_url": "https://evil.example/note", "published_at": "2026-07-01T00:00:00+08:00", "work_type": "normal", "read_count": None, "is_relevant": True, "relevance_score": 1.0}],
        "accounts.json": [{"account_id": "b" * 24, "canonical_name": "作者", "last_seen_at": "2026-07-01T00:00:00+08:00", "fans": 10, "total_works": 2, "likes": 3, "collects": 4, "follows": 5, "platform_payload": {"red_id": "red-1", "red_official_verified": True}}],
        "note_metric_observations.json": [{"observation_id": "obs_1", "article_id": "xhs_tk_" + "a" * 24, "snapshot_id": "snap_1", "captured_at": "2026-07-01T00:00:00+08:00", "liked_count": 1, "collected_count": 2, "comment_count": 3, "shared_count": 4, "read_count": None}],
    }
    for name, value in rows.items():
        (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if changed:
        (root / "keywords.json").write_text("not-json", encoding="utf-8")
    return root


@pytest.fixture
def xhs_settings(settings, tmp_path):
    return replace(settings, xhs_normalized_root=_source(tmp_path), xhs_settings_db_path=tmp_path / "isolated-settings.db")


def test_xhs_dry_run_and_full_mapping(xhs_settings):
    service = XhsService(xhs_settings)
    dry = service.import_history(dry_run=True)
    assert dry["counts"]["keywords"] == 1
    assert dry["counts"]["snapshot_terms"] == 0
    assert dry["audit"]["manifest_id"]
    result = service.import_history(dry_run=False)
    assert not result["audit"]["rejected"]
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM contents WHERE content_type='social_note'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM metric_definitions WHERE platform='xiaohongshu'").fetchone()[0] == 9
        assert con.execute("SELECT count(*) FROM metric_observations WHERE subject_type='content'").fetchone()[0] == 4
        payload = json.loads(con.execute("SELECT payload_json FROM contents").fetchone()[0])
        assert payload["read_count"] is None
        assert payload["is_relevant"] is None


def test_xhs_scoped_keyword_and_creator_ids_preserve_cross_platform_rows(xhs_settings):
    with connect(xhs_settings) as con:
        con.execute(
            "INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("kw_1", "wechat-search", "微信原词", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", '{"keep":"yes"}'),
        )
        con.execute(
            "INSERT INTO creators(creator_id,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("wechat_creator", "wechat-search", "b" * 24, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", '{"keep":"yes"}'),
        )
        before = tuple(con.execute("SELECT * FROM keywords WHERE keyword_id='kw_1'").fetchone())
    XhsService(xhs_settings).import_history(dry_run=False)
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM keywords WHERE keyword_id='kw_1' AND platform='wechat-search'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM keywords WHERE platform='xiaohongshu'").fetchone()[0] == 1
        assert tuple(con.execute("SELECT * FROM keywords WHERE keyword_id='kw_1'").fetchone()) == before
        assert con.execute("SELECT count(*) FROM creators WHERE platform='wechat-search' AND external_id=?", ("b" * 24,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM creators WHERE platform='xiaohongshu' AND external_id=?", ("b" * 24,)).fetchone()[0] == 1
        xhs_keyword = con.execute("SELECT keyword_id,payload_json FROM keywords WHERE platform='xiaohongshu'").fetchone()
        assert xhs_keyword["keyword_id"].startswith("xhs_keyword_")
        assert json.loads(xhs_keyword["payload_json"])["source_keyword_id"] == "kw_1"


def test_xhs_legacy_nickname_account_is_preserved(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    accounts = json.loads((root / "accounts.json").read_text())
    accounts[0]["account_id"] = "legacy-nickname"
    (root / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
    articles = json.loads((root / "articles.json").read_text())
    articles[0]["account_id"] = "legacy-nickname"
    (root / "articles.json").write_text(json.dumps(articles), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert not any(item.get("reason") == "invalid_account_id" for item in result["audit"]["rejected"])
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM creators WHERE platform='xiaohongshu' AND external_id='legacy-nickname'").fetchone()[0] == 1
        assert con.execute("SELECT author_name FROM contents WHERE content_type='social_note'").fetchone()[0] == "作者"


def test_xhs_multiple_identity_owners_and_scoped_placeholders_do_not_create_orphans(xhs_settings):
    from content_hub.features.xhs.service import _scoped
    with connect(xhs_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,canonical_url,title,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            ("owner-a", "social_note", None, "A", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"),
        )
        con.execute(
            "INSERT INTO contents(content_id,content_type,canonical_url,title,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            ("owner-b", "social_note", "https://www.xiaohongshu.com/explore/" + "a" * 24, "B", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"),
        )
        con.execute("INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) VALUES(?,?,?,?,?)", ("xiaohongshu_article", "xhs_tk_" + "a" * 24, "owner-a", "2026-01-01T00:00:00Z", "{}"))
        con.execute("INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)", (_scoped("xhs_keyword", "kw_1"), "douyin", "占位", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", '{"source_keyword_id":"other"}'))
        con.execute("INSERT INTO creators(creator_id,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)", (_scoped("xhs_creator", "b" * 24), "douyin", "other", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"))
        con.execute("INSERT INTO search_snapshots(snapshot_id,platform,keyword,captured_at,payload_json) VALUES(?,?,?,?,?)", ("snap_1", "douyin", "占位", "2026-01-01T00:00:00Z", "{}"))
    result = XhsService(xhs_settings).import_history(dry_run=False)
    reasons = {item["reason"] for item in result["audit"]["rejected"]}
    assert "multiple_identity_owners" in reasons
    assert "scoped_id_conflict" in reasons
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM contents WHERE content_id=?", ("xhs_tk_" + "a" * 24,)).fetchone()[0] == 0
        assert con.execute("SELECT platform FROM keywords WHERE keyword_id=?", (_scoped("xhs_keyword", "kw_1"),)).fetchone()[0] == "douyin"
        assert con.execute("SELECT platform FROM search_snapshots WHERE snapshot_id='snap_1'").fetchone()[0] == "douyin"


def _core_hashes(settings):
    tables = (
        "contents", "creators", "content_identifiers", "content_discoveries",
        "search_snapshots", "search_hits", "metric_definitions", "metric_observations",
        "comments", "comment_events", "geo_answers", "geo_source_relations",
        "signals", "production_jobs",
    )
    result = {}
    with connect(settings, readonly=True) as con:
        for table in tables:
            rows = [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            result[table] = hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()
    return result


def test_xhs_dry_run_has_zero_hub_writes(xhs_settings):
    before_file = hashlib.sha256(xhs_settings.database_path.read_bytes()).hexdigest()
    before_tables = _core_hashes(xhs_settings)
    with connect(xhs_settings, readonly=True) as con:
        before_batches = con.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0]
        before_audits = con.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        before_connections = con.execute("SELECT count(*) FROM system_connections").fetchone()[0]
    XhsService(xhs_settings).import_history(dry_run=True)
    after_file = hashlib.sha256(xhs_settings.database_path.read_bytes()).hexdigest()
    assert after_file == before_file
    assert _core_hashes(xhs_settings) == before_tables
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0] == before_batches
        assert con.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before_audits
        assert con.execute("SELECT count(*) FROM system_connections").fetchone()[0] == before_connections


def test_xhs_idempotent_replay_and_conflict(xhs_settings):
    service = XhsService(xhs_settings)
    service.import_history(dry_run=False)
    before = {}
    with connect(xhs_settings, readonly=True) as con:
        for table in ("keywords", "creators", "contents", "search_snapshots", "search_hits", "metric_observations"):
            before[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    before_core = _core_hashes(xhs_settings)
    service.import_history(dry_run=False)
    with connect(xhs_settings, readonly=True) as con:
        assert {table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before} == before
    assert _core_hashes(xhs_settings) == before_core
    source = xhs_settings.xhs_normalized_root / "note_metric_observations.json"
    data = json.loads(source.read_text())
    data[0]["liked_count"] = 99
    source.write_text(json.dumps(data), encoding="utf-8")
    result = service.import_history(dry_run=False)
    assert any(item["reason"] == "value_conflict" for item in result["audit"]["rejected"])


def test_xhs_dangling_relations_are_rejected(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    snapshots = json.loads((root / "snapshots.json").read_text())
    snapshots.append({"snapshot_id": "snap_orphan", "keyword_id": "kw_missing", "captured_at": "2026-07-02T00:00:00+08:00"})
    (root / "snapshots.json").write_text(json.dumps(snapshots), encoding="utf-8")
    hits = json.loads((root / "ranking_hits.json").read_text())
    hits.append({"hit_id": "hit_orphan", "snapshot_id": "snap_orphan", "rank": 1, "article_id": "xhs_tk_" + "c" * 24})
    (root / "ranking_hits.json").write_text(json.dumps(hits), encoding="utf-8")
    observations = json.loads((root / "note_metric_observations.json").read_text())
    observations.append({"observation_id": "obs_orphan", "article_id": "xhs_tk_" + "c" * 24, "snapshot_id": "snap_orphan", "captured_at": "2026-07-02T00:00:00+08:00", "liked_count": 1})
    (root / "note_metric_observations.json").write_text(json.dumps(observations), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    reasons = {item["reason"] for item in result["audit"]["rejected"]}
    assert {"dangling_keyword", "dangling_snapshot_or_article"} <= reasons


def test_xhs_blank_keyword_rejects_snapshot_and_batch_is_partial_failed(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    keywords = json.loads((root / "keywords.json").read_text())
    keywords[0]["keyword_text"] = ""
    (root / "keywords.json").write_text(json.dumps(keywords), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert any(item["reason"] == "missing_keyword" for item in result["audit"]["rejected"])
    assert any(item["reason"] == "dangling_keyword" for item in result["audit"]["rejected"])
    with connect(xhs_settings, readonly=True) as con:
        batch = con.execute("SELECT status,records_written,records_failed,payload_json FROM ingestion_batches").fetchone()
        assert batch["status"] == "partial_failed"
        assert batch["records_written"] >= 0 and batch["records_failed"] > 0
        assert "count_semantics" in batch["payload_json"]
        assert con.execute("SELECT count(*) FROM system_connections WHERE system_key='xhs-search'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM system_connections").fetchone()[0] == 7
        assert con.execute("SELECT count(*) FROM audit_log WHERE action='xhs.import'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ingestion_checkpoints WHERE adapter_key='xiaohongshu'").fetchone()[0] == 1


def test_xhs_hit_conflict_does_not_delete_existing_fact(xhs_settings):
    service = XhsService(xhs_settings)
    service.import_history(dry_run=False)
    with connect(xhs_settings, readonly=True) as con:
        original = tuple(con.execute("SELECT hit_id, title_raw, url_raw FROM search_hits WHERE snapshot_id='snap_1' AND rank=1").fetchone())
    path = xhs_settings.xhs_normalized_root / "ranking_hits.json"
    rows = json.loads(path.read_text())
    rows[0]["title_raw"] = "冲突标题"
    rows[0]["hit_id"] = "hit_conflict"
    path.write_text(json.dumps(rows), encoding="utf-8")
    result = service.import_history(dry_run=False)
    assert any(item["reason"] == "snapshot_rank_conflict" for item in result["audit"]["rejected"])
    with connect(xhs_settings, readonly=True) as con:
        assert tuple(con.execute("SELECT hit_id, title_raw, url_raw FROM search_hits WHERE snapshot_id='snap_1' AND rank=1").fetchone()) == original


def test_xhs_same_hit_id_rank_change_and_invalid_metric_time_are_rejected(xhs_settings):
    service = XhsService(xhs_settings)
    service.import_history(dry_run=False)
    hit_path = xhs_settings.xhs_normalized_root / "ranking_hits.json"
    hits = json.loads(hit_path.read_text())
    hits[0]["rank"] = 2
    hit_path.write_text(json.dumps(hits), encoding="utf-8")
    obs_path = xhs_settings.xhs_normalized_root / "note_metric_observations.json"
    observations = json.loads(obs_path.read_text())
    observations[0]["captured_at"] = "bad-time"
    obs_path.write_text(json.dumps(observations), encoding="utf-8")
    result = service.import_history(dry_run=False)
    reasons = {item["reason"] for item in result["audit"]["rejected"]}
    assert "hit_identity_conflict" in reasons or "snapshot_rank_conflict" in reasons
    assert "invalid_captured_at" in reasons


def test_xhs_source_change_and_root_security(settings, tmp_path):
    root = _source(tmp_path)
    isolated = replace(settings, xhs_normalized_root=root, xhs_settings_db_path=tmp_path / "settings.db")
    adapter = XhsAdapter(isolated)
    assert adapter.read_records()[1]["keywords"]["size"] > 0
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "keywords.json").unlink()
    (root / "keywords.json").symlink_to(outside / "keywords.json")
    with pytest.raises(XhsSourceError) as error:
        adapter.file_manifest()
    assert error.value.kind in {"path_traversal", "missing_source_file"}
    changed = _source(tmp_path / "changed")
    (changed / "keywords.json").write_text("not-json", encoding="utf-8")
    bad = XhsAdapter(replace(settings, xhs_normalized_root=changed))
    with pytest.raises(XhsSourceError):
        bad.read_records()


def test_xhs_refresh_is_blocked_without_calling_legacy_write(settings, tmp_path, monkeypatch):
    service = XhsService(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    with connect(service.settings) as con:
        con.execute(
            "INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("xhs_keyword_1", "xiaohongshu", "香港保险", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", '{"source_keyword_id":"kw_1"}'),
        )
    called = False

    def legacy_write(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应调用旧小红书写接口")

    monkeypatch.setattr(service.adapter, "refresh", legacy_write)
    blocked = service.refresh("xhs_keyword_1", True)
    assert blocked["http_status"] == 409
    assert blocked["blocked"] is True
    assert blocked["upstream_called"] is False
    assert blocked["source_status"]["source"] == "hub_policy"
    assert called is False
    with connect(service.settings, readonly=True) as con:
        audit = con.execute(
            "SELECT outcome,details_json FROM audit_log WHERE action='xhs.refresh' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        assert audit["outcome"] == "blocked"
        assert json.loads(audit["details_json"])["upstream_called"] is False
        connection = con.execute(
            "SELECT capabilities_json FROM system_connections WHERE system_key='xhs-search'"
        ).fetchone()
        assert "keyword_refresh_dry_run" in json.loads(connection["capabilities_json"])


def test_xhs_live_bootstrap_fallback_and_empty_terms(settings, tmp_path, monkeypatch):
    service = XhsService(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    with connect(service.settings) as con:
        con.execute(
            "INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("xhs_keyword_1", "xiaohongshu", "香港保险", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", '{"source_keyword_id":"kw_1"}'),
        )
    class Response:
        def __init__(self, status, payload):
            self.status, self.payload = status, payload

    monkeypatch.setattr(service.adapter, "bootstrap", lambda: Response(200, {"keywords": [], "snapshot_terms": None, "token": "secret"}))
    degraded = service.bootstrap()
    assert degraded["source_status"]["source"] == "hub_db"
    assert degraded["counts"]["keywords"] == 1
    payload = {"keywords": [], "accounts": [], "snapshots": [], "ranking_hits": [], "articles": [], "snapshot_terms": [], "counts": {"keywords": 9}}
    monkeypatch.setattr(service.adapter, "bootstrap", lambda: Response(200, payload))
    assert service.bootstrap()["counts"] == {"keywords": 9}


def test_xhs_refresh_does_not_forward_legacy_identifier(settings, tmp_path, monkeypatch):
    service = XhsService(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    with connect(service.settings) as con:
        con.execute(
            "INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("xhs_keyword_1", "xiaohongshu", "香港保险", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", '{"source_keyword_id":"kw_1"}'),
        )
    called = False

    def legacy_write(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应调用旧小红书写接口")

    monkeypatch.setattr(service.adapter, "refresh", legacy_write)
    result = service.refresh("xhs_keyword_1", True)
    assert result["http_status"] == 409
    assert result["keyword_id"] == "xhs_keyword_1"
    assert result["upstream_called"] is False
    assert called is False


def test_xhs_adapter_refresh_payload_contains_keyword_and_confirm(settings, tmp_path, monkeypatch):
    adapter = XhsAdapter(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    seen = {}
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda path, *, method, payload, allowed_error_statuses: seen.update(
            path=path, method=method, payload=payload, allowed_error_statuses=allowed_error_statuses
        ) or RemoteResponse({}, 202),
    )
    response = adapter.refresh("kw/1", " 香港保险 ")
    assert response.status == 202
    assert seen == {
        "path": "/api/keywords/kw%2F1/refresh",
        "method": "POST",
        "payload": {"keyword": "香港保险", "confirm": True},
        "allowed_error_statuses": {409},
    }


def test_xhs_live_account_normalizes_name_and_score_preserving_raw(settings, tmp_path, monkeypatch):
    service = XhsService(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    monkeypatch.setattr(service.adapter, "account", lambda _: RemoteResponse({"accountNickname": "  作者 ", "accountScore": "98.5", "token": "secret"}, 200))
    result = service.account("acct-1")
    account = result["account"]
    assert account["name"] == "作者"
    assert account["canonical_name"] == "作者"
    assert account["score"] == 98.5
    assert account["raw_evidence"]["accountNickname"] == "  作者 "
    assert account["raw_evidence"]["token"] == "[REDACTED]"


def test_xhs_empty_settings_uses_normalized_active_and_audits_warning(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    keywords = json.loads((root / "keywords.json").read_text())
    keywords[0]["is_active"] = False
    (root / "keywords.json").write_text(json.dumps(keywords), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert result["audit"]["warnings"][0]["kind"] == "settings_db_empty"
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT status FROM keywords WHERE platform='xiaohongshu'").fetchone()[0] == "paused"


def test_xhs_refresh_legacy_failure_path_is_not_used(settings, tmp_path, monkeypatch):
    service = XhsService(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    with connect(service.settings) as con:
        con.execute(
            "INSERT INTO keywords(keyword_id,platform,keyword,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("xhs_keyword_1", "xiaohongshu", "香港保险", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", '{"source_keyword_id":"kw_1"}'),
        )
    monkeypatch.setattr(
        service.adapter,
        "refresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应调用旧小红书写接口")),
    )
    result = service.refresh("xhs_keyword_1", True)
    assert result["http_status"] == 409
    assert result["blocked"] is True


def test_xhs_live_account_proxy_and_hub_fallback(xhs_settings, monkeypatch):
    service = XhsService(xhs_settings)
    service.import_history(dry_run=False)
    class Response:
        status = 200
        payload = {"account_id": "b" * 24, "token": "secret"}
    monkeypatch.setattr(service.adapter, "account", lambda _: Response())
    remote = service.account("b" * 24)
    assert remote["source_status"]["source"] == "legacy_http"
    assert remote["account"]["token"] == "[REDACTED]"
    monkeypatch.setattr(service.adapter, "account", lambda _: (_ for _ in ()).throw(XhsSourceError("offline")))
    fallback = service.account("b" * 24)
    assert fallback["source_status"]["source"] == "hub_db"
    assert isinstance(fallback["account"]["payload"], dict)


def test_xhs_url_secret_matrix_terms_and_settings_merge(xhs_settings, tmp_path):
    import sqlite3
    settings_db = tmp_path / "settings.db"
    db = sqlite3.connect(settings_db)
    db.execute("CREATE TABLE keyword_settings(keyword_id TEXT PRIMARY KEY, keyword_text TEXT NOT NULL, is_pinned INTEGER, pin_order INTEGER, is_active INTEGER, topic TEXT, keyword_bucket TEXT, note TEXT, batch_default_selected INTEGER, updated_at TEXT)")
    db.execute("INSERT INTO keyword_settings VALUES(?,?,?,?,?,?,?,?,?,?)", ("kw_1", "香港保险", 1, 3, 1, "真实主题", "真实分组", "说明", 1, "2026-07-01"))
    db.commit()
    db.close()
    xhs_settings = replace(xhs_settings, xhs_settings_db_path=settings_db)
    root = xhs_settings.xhs_normalized_root
    keywords = json.loads((root / "keywords.json").read_text())
    keywords[0]["is_active"] = False
    (root / "keywords.json").write_text(json.dumps(keywords), encoding="utf-8")
    articles = json.loads((root / "articles.json").read_text())
    articles[0]["normalized_url"] = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?z=2&xsec_token=SECRET&xsec_source=s&token=t&a=1#frag"
    (root / "articles.json").write_text(json.dumps(articles), encoding="utf-8")
    hits = json.loads((root / "ranking_hits.json").read_text())
    hits[0]["url_raw"] = "https://xhslink.com/a?token=t&b=2&xsec_token=s&a=1#frag"
    (root / "ranking_hits.json").write_text(json.dumps(hits), encoding="utf-8")
    terms = [{"term_id": "term_1", "snapshot_id": "snap_1", "term_type": "suggestion", "position": 1, "term_text": "相关词", "token": "secret"}, {"term_id": "term_orphan", "snapshot_id": "missing", "term_type": "related", "position": 1, "term_text": "孤儿"}]
    (root / "snapshot_terms.json").write_text(json.dumps(terms), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert any(item["reason"] == "dangling_snapshot" for item in result["audit"]["rejected"])
    with connect(xhs_settings, readonly=True) as con:
        keyword = con.execute("SELECT topic,keyword_bucket,payload_json FROM keywords WHERE platform='xiaohongshu'").fetchone()
        assert (keyword["topic"], keyword["keyword_bucket"]) == ("真实主题", "真实分组")
        assert con.execute("SELECT status FROM keywords WHERE platform='xiaohongshu'").fetchone()[0] == "active"
        assert json.loads(keyword["payload_json"])["settings"]["is_active"] == 1
        assert "SECRET" not in con.execute("SELECT payload_json FROM contents").fetchone()[0]
        assert "token" not in con.execute("SELECT url_raw FROM search_hits").fetchone()[0]
        features = json.loads(con.execute("SELECT features_json FROM search_snapshots").fetchone()[0])
        assert features["suggestions"][0]["term_text"] == "相关词"


def test_xhs_term_rejected_when_source_snapshot_is_rejected(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    snapshots = json.loads((root / "snapshots.json").read_text())
    snapshots[0]["captured_at"] = "bad-time"
    (root / "snapshots.json").write_text(json.dumps(snapshots), encoding="utf-8")
    terms = [{"term_id": "term_1", "snapshot_id": "snap_1", "term_type": "suggestion", "position": 1, "term_text": "会被拒"}]
    (root / "snapshot_terms.json").write_text(json.dumps(terms), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert any(item["reason"] == "dangling_snapshot" for item in result["audit"]["rejected"])


def test_xhs_empty_account_is_rejected_but_article_survives_without_creator(xhs_settings):
    root = xhs_settings.xhs_normalized_root
    accounts = json.loads((root / "accounts.json").read_text())
    accounts[0]["account_id"] = ""
    (root / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
    articles = json.loads((root / "articles.json").read_text())
    articles[0]["account_id"] = ""
    (root / "articles.json").write_text(json.dumps(articles), encoding="utf-8")
    result = XhsService(xhs_settings).import_history(dry_run=False)
    assert any(item["reason"] == "missing_account_id" for item in result["audit"]["rejected"])
    with connect(xhs_settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM contents WHERE content_type='social_note'").fetchone()[0] == 1
        assert con.execute("SELECT creator_id FROM contents WHERE content_type='social_note'").fetchone()[0] is None


def test_xhs_bootstrap_offline_uses_hub_fallback(xhs_settings):
    XhsService(xhs_settings).import_history(dry_run=False)
    xhs_settings = replace(xhs_settings, xhs_source_url="http://127.0.0.1:1")
    result = XhsService(xhs_settings).bootstrap()
    assert result["source_status"]["status"] == "degraded"
    assert result["counts"]["keywords"] == 1
    assert isinstance(result["snapshot_terms"], list)
    assert isinstance(result["articles"][0]["payload"], dict)


def test_xhs_keyword_offline_returns_real_history(xhs_settings):
    XhsService(xhs_settings).import_history(dry_run=False)
    xhs_settings = replace(xhs_settings, xhs_source_url="http://127.0.0.1:1")
    result = XhsService(xhs_settings).keyword("kw_1")
    assert result["source_status"]["status"] == "degraded"
    assert len(result["snapshots"]) == 1
    assert len(result["hits"]) == 1
    assert len(result["articles"]) == 1
    assert isinstance(result["snapshots"][0]["features"], dict)
    assert isinstance(result["hits"][0]["payload"], dict)
    assert isinstance(result["articles"][0]["payload"], dict)


def test_xhs_non_xhs_social_note_is_not_mixed_into_api(xhs_settings):
    XhsService(xhs_settings).import_history(dry_run=False)
    with connect(xhs_settings) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
            ("douyin-note", "social_note", "抖音", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "{}"),
        )
        con.execute(
            "INSERT INTO search_snapshots(snapshot_id,platform,keyword,captured_at,payload_json) VALUES(?,?,?,?,?)",
            ("douyin-snapshot", "douyin", "其他", "2026-01-02T00:00:00Z", "{}"),
        )
        con.execute(
            "INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,payload_json) VALUES(?,?,?,?,?)",
            ("douyin-hit", "douyin-snapshot", 1, "xhs_tk_" + "a" * 24, "{}"),
        )
    service = XhsService(xhs_settings)
    assert all(item["content_id"] != "douyin-note" for item in service.articles()["articles"])
    with pytest.raises(Exception):
        service.article("douyin-note")
    assert all(item["hit_id"] != "douyin-hit" for item in service.article("xhs_tk_" + "a" * 24)["hits"])


def test_xhs_settings_db_errors_are_not_silent(settings, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite", encoding="utf-8")
    adapter = XhsAdapter(replace(settings, xhs_settings_db_path=bad))
    with pytest.raises(XhsSourceError) as error:
        adapter.file_manifest()
    assert error.value.kind == "settings_db_error"
    directory = tmp_path / "settings-dir"
    directory.mkdir()
    with pytest.raises(XhsSourceError):
        XhsAdapter(replace(settings, xhs_settings_db_path=directory)).file_manifest()


def test_xhs_api_articles_article_and_account_fallback(xhs_settings, monkeypatch):
    XhsService(xhs_settings).import_history(dry_run=False)
    xhs_settings = replace(xhs_settings, xhs_source_url="http://127.0.0.1:1")
    app = create_app(xhs_settings)
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                account = await client.get("/api/v1/xhs/accounts/" + "b" * 24)
                articles = await client.get("/api/v1/xhs/articles")
                article = await client.get("/api/v1/xhs/articles/xhs_tk_" + "a" * 24)
                dry = await client.post("/api/v1/xhs/import", json={"dry_run": True})
                assert account.status_code == 200 and account.json()["data"]["source_status"]["status"] == "degraded"
                assert isinstance(articles.json()["data"]["articles"][0]["payload"], dict)
                assert isinstance(article.json()["data"]["article"]["payload"], dict)
                assert isinstance(article.json()["data"]["hits"][0]["payload"], dict)
                assert isinstance(article.json()["data"]["observations"][0]["payload"], dict)
                assert dry.status_code == 200 and dry.json()["data"]["dry_run"] is True
    asyncio.run(run())


def test_xhs_adapter_http_errors_and_secret_scrub(settings, tmp_path, monkeypatch):
    adapter = XhsAdapter(replace(settings, xhs_settings_db_path=tmp_path / "settings.db"))
    class FakeResponse:
        status = 500
        def read(self):
            return b'{"token":"secret","xsec_token":"secret","access_token":"secret","refresh_token":"secret","api_key":"secret","apikey":"secret","secret":"secret","password":"secret","message":"bad"}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(XhsSourceError) as error:
        adapter.bootstrap()
    assert error.value.status == 500
    for key in ("token", "xsec_token", "access_token", "refresh_token", "api_key", "apikey", "secret", "password"):
        assert error.value.payload[key] == "[REDACTED]"
    class Fake429(FakeResponse):
        status = 429
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Fake429())
    with pytest.raises(XhsSourceError) as error:
        adapter.keyword("kw_1")
    assert error.value.status == 429
    class Fake409(FakeResponse):
        status = 409
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Fake409())
    response = adapter.refresh("kw_1", "香港保险")
    assert response.status == 409


def test_xhs_api_confirm_required(xhs_settings):
    app = create_app(xhs_settings)
    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/xhs/keywords/kw_1/refresh", json={"confirm": False})
                assert response.status_code == 422
    asyncio.run(run())
