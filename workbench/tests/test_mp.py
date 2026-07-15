from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from content_hub.adapters.mp import MpAdapter, MpSourceError, RemoteResponse, parse_markdown
from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.features.mp.service import MpService


def _configured(settings, tmp_path: Path):
    root = tmp_path / "output_md"
    category = root / "热门产品"
    category.mkdir(parents=True)
    (category / "260712_文章.md").write_text("# 标题\n正文\n公众号：测试号\n", encoding="utf-8")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    return replace(settings, mp_source_root=root, mp_source_url="http://127.0.0.1:1", mp_rejected_csv_path=root / "rejected_articles.csv", mp_metadata_root=metadata)


def test_mp_parse_and_root_guards(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    record = parse_markdown(configured.mp_source_root / "热门产品/260712_文章.md", configured.mp_source_root, "热门产品")
    assert record["title"] == "标题"
    assert record["author"] == "测试号"
    assert record["published_at"] is None
    assert record["ingested_date_hint"] == "260712"
    outside = tmp_path / "outside.md"
    outside.write_text("# outside", encoding="utf-8")
    (configured.mp_source_root / "热门产品" / "escape.md").symlink_to(outside)
    (configured.mp_source_root / "热门产品" / "link-dir").symlink_to(tmp_path, target_is_directory=True)
    records, manifest = MpAdapter(configured).scan()
    assert len(records) == 1
    assert any(item["reason"] == "symlink" for item in manifest["skipped"])


def test_mp_missing_metadata_root_is_safe_empty(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    configured = replace(configured, mp_metadata_root=tmp_path / "does-not-exist")
    records, manifest = MpAdapter(configured).scan()
    assert len(records) == 1
    assert manifest["metadata_files"] == []
    assert manifest["reconcile"]["csv_rows_raw"] == 0


def test_mp_complex_filename_recovers_only_filename_mp_and_not_publish_time(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    path = configured.mp_source_root / "热门产品/260712_测试号_标题_175305_20260516_121112.md"
    path.write_text("# 标题\n正文\n", encoding="utf-8")
    record = parse_markdown(path, configured.mp_source_root, "热门产品", metadata_names=["测试号"])
    assert record["mp_name"] == "测试号"
    assert record["published_at"] is None
    assert record["ingested_date_hint"] == "260712"


def test_mp_dry_run_has_no_hub_writes(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    service = MpService(configured)

    def snapshot(table: str) -> list[tuple[object, ...]]:
        with connect(configured, readonly=True) as con:
            return [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]

    before = {
        "audit_log": snapshot("audit_log"),
        "system_connections": snapshot("system_connections"),
        "contents": snapshot("contents"),
    }
    result = service.import_history(dry_run=True, limit=None)
    assert result["dry_run"] is True
    after = {
        "audit_log": snapshot("audit_log"),
        "system_connections": snapshot("system_connections"),
        "contents": snapshot("contents"),
    }
    assert after == before


def test_mp_import_is_idempotent_and_rejected_not_content(settings, tmp_path, monkeypatch):
    configured = _configured(settings, tmp_path)
    rejected = configured.mp_source_root / "rejected_articles.csv"
    rejected.write_text("title,reason\n拒绝,invalid\n", encoding="utf-8")
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": [{"mp_id": "mp1", "mp_name": "测试号", "password": "never"}]}, 200))
    service = MpService(configured)
    first = service.import_history(dry_run=False, limit=None)
    second = service.import_history(dry_run=False, limit=None)
    assert first["batch_id"] == second["batch_id"]
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM content_discoveries WHERE discovery_system='wechat-mp'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0] == 1
        assert "password" not in con.execute("SELECT payload_json FROM creators").fetchone()[0]
    assert first["audit"]["rejected_articles"]["rows"] == 1


def test_mp_cross_system_url_reuses_content_but_adds_mp_discovery(settings, tmp_path, monkeypatch):
    configured = _configured(settings, tmp_path)
    with connect(configured) as con:
        con.execute("INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)", ("wechat_content", "external_article", "旧标题", "https://mp.weixin.qq.com/s/abc", "2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z", "{}"))
        con.commit()
    original = MpAdapter.scan
    def scan(self, *, limit=None, max_consistency_attempts=3):
        rows, manifest = original(self, limit=limit, max_consistency_attempts=max_consistency_attempts)
        rows[0]["canonical_url"] = "https://mp.weixin.qq.com/s/abc"
        return rows, manifest
    monkeypatch.setattr(MpAdapter, "scan", scan)
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": []}, 200))
    MpService(configured).import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
        assert con.execute("SELECT content_type FROM contents WHERE content_id='wechat_content'").fetchone()[0] == "external_article"
        rows = [tuple(row) for row in con.execute("SELECT discovery_system,discovery_channel FROM content_discoveries WHERE content_id='wechat_content'").fetchall()]
        assert rows == [("wechat-mp", "account-feed")]
    assert MpService(configured).articles()["articles"][0]["content_id"] == "wechat_content"


def test_mp_late_url_enrichment_repoints_placeholder_without_duplicate(settings, tmp_path, monkeypatch):
    configured = _configured(settings, tmp_path)
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": []}, 200))
    service = MpService(configured)
    service.import_history(dry_run=False, limit=None)
    with connect(configured) as con:
        placeholder_id = con.execute(
            "SELECT content_id FROM content_identifiers WHERE namespace='mp_markdown_path'"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            (
                "wechat_content",
                "external_article",
                "搜索标题",
                "https://mp.weixin.qq.com/s/late",
                "2026-07-11T00:00:00Z",
                "2026-07-11T00:00:00Z",
                "{}",
            ),
        )
        con.commit()
    original = MpAdapter.scan

    def scan(self, *, limit=None, max_consistency_attempts=3):
        rows, manifest = original(self, limit=limit, max_consistency_attempts=max_consistency_attempts)
        rows[0]["canonical_url"] = "https://mp.weixin.qq.com/s/late"
        return rows, manifest

    monkeypatch.setattr(MpAdapter, "scan", scan)
    service.import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 1
        assert con.execute(
            "SELECT content_id FROM content_identifiers WHERE namespace='mp_markdown_path'"
        ).fetchone()[0] == "wechat_content"
        assert con.execute(
            "SELECT COUNT(*) FROM content_discoveries WHERE discovery_system='wechat-mp'"
        ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM contents WHERE content_id=?", (placeholder_id,)).fetchone()[0] == 0


def test_mp_csv_identity_ignores_source_mtime_and_url_is_canonical(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    csv_path = configured.mp_metadata_root / "rows.csv"
    csv_path.write_text(
        "title,publish_time,mp_name,url,mp_id\n"
        "无URL,2026-07-01 10:00:00,甲号,,mp-a\n"
        "有URL,2026-07-02 10:00:00,乙号,https://mp.weixin.qq.com/s/canonical#fragment,mp-b\n",
        encoding="utf-8",
    )
    first, _ = MpAdapter(configured).scan()
    first_csv = MpAdapter(configured)._load_metadata()[0]
    stat = csv_path.stat()
    os.utime(csv_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    second_csv = MpAdapter(configured)._load_metadata()[0]
    assert [row["_identity"] for row in first_csv] == [row["_identity"] for row in second_csv]
    csv_records = [MpService._csv_record(row) for row in second_csv]
    by_title = {row["title"]: row for row in csv_records}
    assert by_title["有URL"]["canonical_url"] == "https://mp.weixin.qq.com/s/canonical"
    assert len(first) == 1


def test_mp_upstream_status_and_confirmation(settings, monkeypatch):
    from content_hub.adapters.mp import MpAdapter
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: (_ for _ in ()).throw(MpSourceError("bad gateway", status=502, kind="remote_http", payload={"token": "secret"})))
    service = MpService(settings)
    with pytest.raises(MpSourceError) as error:
        service.auth_check()
    assert error.value.status == 502
    assert "secret" not in json.dumps(error.value.payload)
    with pytest.raises(Exception):
        service.create_job({"confirm": False, "token": "x"}, False)


def test_mp_routes(settings, monkeypatch):
    configured = settings
    monkeypatch.setattr(MpAdapter, "health", lambda self: RemoteResponse({"status": "ok"}, 200))
    monkeypatch.setattr(MpAdapter, "runtime_overview", lambda self: RemoteResponse({"runtime": "ok"}, 200))
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": [{"mp_id": "1", "mp_name": "A"}]}, 200))
    monkeypatch.setattr(MpAdapter, "categories_remote", lambda self: RemoteResponse({"categories": []}, 200))
    monkeypatch.setattr(MpAdapter, "jobs", lambda self: RemoteResponse({"jobs": []}, 200))
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse({"authenticated": True}, 200))
    async def run():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.get("/api/v1/mp/bootstrap")
                assert response.status_code == 200
                assert response.json()["data"]["summary"]["account_count"] == 1
                categories = await client.get("/api/v1/mp/categories")
                assert categories.status_code == 200
                blocked = await client.post("/api/v1/mp/jobs", json={"type": "sync"})
                assert blocked.status_code == 422
    asyncio.run(run())


def test_mp_auth_exact_upstream_paths_and_binary_qrcode(settings, tmp_path):
    calls: list[tuple[str, str, bytes]] = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def _json(self, payload, status=200):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            calls.append(("POST", self.path, body))
            if self.path == "/api/auth/wechat/check":
                return self._json({"logged_in": True})
            if self.path == "/api/auth/wechat/qrcode":
                return self._json({"already_logged_in": False, "auth_status": "waiting", "image_url": "/api/auth/wechat/qrcode/image/qr-1", "raw": "https://temporary.example/secret"})
            if self.path == "/api/auth/wechat/qrcode/finish":
                return self._json({"finished": True})
            return self._json({"error": "wrong path"}, 404)
        def do_GET(self):
            calls.append(("GET", self.path, b""))
            if self.path == "/api/auth/wechat/qrcode/image/qr-1":
                raw = b"\x89PNG\r\n\x1a\n"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/auth/wechat/qrcode/image/not-image":
                raw = b"not an image"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_response(404)
                self.end_headers()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        configured = replace(settings, mp_source_url=f"http://127.0.0.1:{server.server_port}")
        adapter = MpAdapter(configured)
        assert adapter.auth_check().payload["logged_in"] is True
        assert adapter.auth_qrcode().payload["image_url"].endswith("/qr-1")
        assert adapter.auth_qrcode_finish().payload["finished"] is True
        assert [call[:2] for call in calls] == [
            ("POST", "/api/auth/wechat/check"),
            ("POST", "/api/auth/wechat/qrcode"),
            ("POST", "/api/auth/wechat/qrcode/finish"),
        ]
        service_qr = MpService(configured).auth_qrcode()
        assert service_qr.payload["qr_id"] == "qr-1"
        assert service_qr.payload["image_url"] == "/api/v1/mp/auth/qrcode/image/qr-1"
        assert "raw" not in service_qr.payload
        MpService(configured).auth_qrcode_finish({"token": "secret"})
        assert calls[-1][2] == b"{}"
        image = adapter.auth_qrcode_image("qr-1")
        assert image.content.startswith(b"\x89PNG") and image.content_type == "image/png"
        with pytest.raises(MpSourceError) as error:
            adapter.auth_qrcode_image("missing")
        assert error.value.status == 404
        with pytest.raises(MpSourceError) as error:
            adapter.auth_qrcode_image("not-image")
        assert error.value.status == 502
        async def check_routes():
            app = create_app(configured)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    image_response = await client.get("/api/v1/mp/auth/qrcode/image/qr-1")
                    missing_response = await client.get("/api/v1/mp/auth/qrcode/image/missing")
                    assert image_response.status_code == 200
                    assert image_response.headers["content-type"].startswith("image/png")
                    assert image_response.content.startswith(b"\x89PNG")
                    assert missing_response.status_code == 404
        asyncio.run(check_routes())
    finally:
        server.shutdown()
        server.server_close()


def test_mp_bootstrap_honours_runtime_inconsistent(settings, monkeypatch):
    runtime = {"wechat_status": {"inconsistent": True, "logged_in": False, "display_status": "未登录", "message": "扫码失效"}}
    monkeypatch.setattr(MpAdapter, "health", lambda self: RemoteResponse({"ok": True}, 200))
    monkeypatch.setattr(MpAdapter, "runtime_overview", lambda self: RemoteResponse(runtime, 200))
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": []}, 200))
    monkeypatch.setattr(MpAdapter, "categories_remote", lambda self: RemoteResponse({"categories": []}, 200))
    monkeypatch.setattr(MpAdapter, "jobs", lambda self: RemoteResponse({"jobs": []}, 200))
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse({"logged_in": False}, 200))
    result = MpService(settings).bootstrap()
    assert result["source_status"]["status"] == "degraded"
    assert result["source_status"]["inconsistent"] is True
    assert result["source_status"]["logged_in"] is False
    assert result["source_status"]["display_status"] == "未登录"
    assert result["source_status"]["message"] == "扫码失效"


def test_mp_rejected_csv_hash_changes_even_when_row_count_same(settings, tmp_path, monkeypatch):
    configured = _configured(settings, tmp_path)
    csv_path = configured.mp_rejected_csv_path
    csv_path.write_text("title,reason\nA,one\n", encoding="utf-8")
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": []}, 200))
    first = MpService(configured).import_history(dry_run=True, limit=None)
    csv_path.write_text("title,reason\nA,two\n", encoding="utf-8")
    second = MpService(configured).import_history(dry_run=True, limit=None)
    assert first["batch_id"] != second["batch_id"]
    assert first["audit"]["rejected_articles"]["rows"] == second["audit"]["rejected_articles"]["rows"] == 1
    assert first["audit"]["rejected_articles"]["sha256"] != second["audit"]["rejected_articles"]["sha256"]
    assert first["audit"]["rejected_articles"]["samples"][0]["reason"] == "one"


def test_mp_category_env_and_traversal_are_whitelisted_and_sorted(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    other = configured.mp_source_root / "其他"
    other.mkdir()
    (other / "260713_b.md").write_text("# B\n公众号：B\n", encoding="utf-8")
    (configured.mp_source_root / "secret").mkdir()
    (configured.mp_source_root / "secret/260714_x.md").write_text("# X\n公众号：X\n", encoding="utf-8")
    configured = replace(configured, mp_categories=("其他", "../secret", "热门产品"))
    adapter = MpAdapter(configured)
    assert adapter.categories == ("其他", "热门产品")
    records, _ = adapter.scan()
    assert {row["category"] for row in records} == {"其他", "热门产品"}


def test_mp_metadata_csv_recovers_url_mp_id_precise_time_and_cross_system_reuse(settings, tmp_path, monkeypatch):
    root = tmp_path / "md"
    (root / "热门产品").mkdir(parents=True)
    (root / "热门产品/260712_测试号_CSV标题_123456.md").write_text("# CSV标题\n正文无 footer\n", encoding="utf-8")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "测试号.csv").write_text(
        "title,publish_time,mp_name,url,mp_id,publish_date,id\n"
        "CSV标题,2026-07-12 12:34:56,测试号,https://mp.weixin.qq.com/s/csv1,mp-csv,2026-07-12,row1\n",
        encoding="utf-8",
    )
    configured = replace(settings, mp_source_root=root, mp_metadata_root=metadata, mp_rejected_csv_path=tmp_path / "rejected.csv")
    records, manifest = MpAdapter(configured).scan()
    assert records[0]["author"] == "测试号"
    assert records[0]["mp_id"] == "mp-csv"
    assert records[0]["canonical_url"] == "https://mp.weixin.qq.com/s/csv1"
    assert records[0]["published_at"] == "2026-07-12T04:34:56Z"
    assert "missing_author" not in records[0]["integrity_warnings"]
    assert manifest["metadata_files"][0]["sha256"]
    with connect(configured) as con:
        con.execute("INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)", ("wechat_content", "external_article", "旧", "https://mp.weixin.qq.com/s/csv1", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "{}"))
        con.execute("INSERT INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,payload_json) VALUES(?,?,?,?,?,?)", ("search-disc", "wechat_content", "wechat-search", "keyword-rank", "2026-07-01T00:00:00Z", "{}"))
        con.commit()
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": [{"mp_id": "mp-csv", "mp_name": "测试号"}]}, 200))
    first = MpService(configured).import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        row = dict(con.execute("SELECT content_id,content_type,canonical_url,published_at,updated_at,file_hash,content_hash,payload_json FROM contents WHERE content_id='wechat_content'").fetchone())
        discoveries = {tuple(item) for item in con.execute("SELECT discovery_system,discovery_channel FROM content_discoveries WHERE content_id='wechat_content'").fetchall()}
    second = MpService(configured).import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        replay = dict(con.execute("SELECT content_id,content_type,canonical_url,published_at,updated_at,file_hash,content_hash,payload_json FROM contents WHERE content_id='wechat_content'").fetchone())
    assert first["batch_id"] == second["batch_id"]
    assert row == replay
    assert discoveries == {("wechat-search", "keyword-rank"), ("wechat-mp", "account-feed")}
    assert MpService(configured).articles()["articles"][0]["content_id"] == "wechat_content"


def test_mp_offline_metadata_mp_id_reuses_creator(settings, tmp_path, monkeypatch):
    root = tmp_path / "md"
    (root / "热门产品").mkdir(parents=True)
    (root / "热门产品/260712_测试号_标题_123456.md").write_text("# 标题\n正文\n", encoding="utf-8")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "rows.csv").write_text(
        "title,publish_time,mp_name,url,mp_id,id\n"
        "标题,2026-07-01 10:00:00,测试号,https://mp.weixin.qq.com/s/offline,mp-offline,id-offline\n",
        encoding="utf-8",
    )
    configured = replace(settings, mp_source_root=root, mp_metadata_root=metadata, mp_rejected_csv_path=tmp_path / "rejected.csv")
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: (_ for _ in ()).throw(MpSourceError("offline")))
    MpService(configured).import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        creator = con.execute("SELECT creator_id,external_id,payload_json FROM creators WHERE external_id='mp-offline'").fetchone()
        content = con.execute("SELECT content_id,creator_id FROM contents WHERE canonical_url='https://mp.weixin.qq.com/s/offline'").fetchone()
        discovery = con.execute("SELECT discovered_at FROM content_discoveries WHERE content_id=?", (content[0],)).fetchone()
    assert creator is not None
    assert "unresolved" not in creator[2]
    assert content[1] == creator[0]
    assert discovery[0] != ""


def test_mp_csv_is_independent_and_md_ambiguity_never_guesses(settings, tmp_path, monkeypatch):
    root = tmp_path / "md"
    category = root / "热门产品"
    category.mkdir(parents=True)
    (category / "260712_甲号_可匹配_123456.md").write_text("# 可匹配\n正文\n", encoding="utf-8")
    (category / "260712_歧义标题.md").write_text("# 歧义标题\n正文\n", encoding="utf-8")
    (category / "260712_无覆盖.md").write_text("# 无覆盖\n正文\n", encoding="utf-8")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "rows.csv").write_text(
        "title,publish_time,mp_name,url,mp_id,id\n"
        "可匹配,2026-07-01 10:00:00,甲号,https://mp.weixin.qq.com/s/matched,mp-a,a1\n"
        "歧义标题,2026-07-01 10:00:00,甲号,https://mp.weixin.qq.com/s/amb-a,mp-a,a2\n"
        "歧义标题,2026-07-02 10:00:00,乙号,https://mp.weixin.qq.com/s/amb-b,mp-b,b2\n"
        "CSV独立,2026-07-03 10:00:00,丙号,https://mp.weixin.qq.com/s/csv-only,mp-c,c3\n"
        "CSV独立,2026-07-03 10:00:00,丙号,https://mp.weixin.qq.com/s/csv-only,mp-c,c3\n",
        encoding="utf-8",
    )
    configured = replace(settings, mp_source_root=root, mp_metadata_root=metadata, mp_rejected_csv_path=tmp_path / "rejected.csv")
    records, manifest = MpAdapter(configured).scan()
    by_title = {record["title"]: record for record in records}
    assert by_title["可匹配"]["metadata_match_status"] == "matched"
    assert by_title["可匹配"]["canonical_url"].endswith("/matched")
    assert by_title["歧义标题"]["metadata_match_status"] == "ambiguous"
    assert by_title["歧义标题"]["canonical_url"] is None
    assert by_title["歧义标题"]["published_at"] is None
    assert by_title["无覆盖"]["metadata_match_status"] == "unmatched"
    assert by_title["无覆盖"]["published_at"] is None
    assert manifest["reconcile"] == {
        "csv_rows_raw": 5, "csv_unique": 4, "md_matched": 1,
        "md_ambiguous": 1, "md_unmatched": 1, "csv_only": 3,
    }
    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse({"accounts": []}, 200))
    result = MpService(configured).import_history(dry_run=False, limit=None)
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 6
        csv_only = con.execute("SELECT COUNT(*) FROM contents WHERE md_path IS NULL AND canonical_url IS NOT NULL").fetchone()[0]
        assert csv_only == 3
        unmatched = con.execute("SELECT published_at FROM contents WHERE title='无覆盖'").fetchone()[0]
        assert unmatched is None
    assert result["audit"]["reconcile"]["csv_only"] == 3


def test_mp_flags_strict_whitelist_and_types(settings, monkeypatch):
    captured = {}
    def update(self, mp_id, payload):
        captured.update(payload)
        return RemoteResponse({"ok": True}, 200)
    monkeypatch.setattr(MpAdapter, "update_flags", update)
    monkeypatch.setattr(MpAdapter, "categories_remote", lambda self: RemoteResponse({"categories": ["港险", "其他"]}, 200))
    service = MpService(settings)
    with pytest.raises(Exception):
        service.update_flags("mp-1", {}, True)
    with pytest.raises(Exception):
        service.update_flags("mp-1", {"other": True}, True)
    with pytest.raises(Exception):
        service.update_flags("mp-1", {"monitor_enabled": "yes"}, True)
    assert service.update_flags("mp-1", {"monitor_enabled": True}, True).payload["ok"] is True
    assert service.update_flags("mp-1", {"category_name": "港险"}, True).payload["ok"] is True
    with pytest.raises(Exception):
        service.update_flags("mp-1", {"category_name": "../secret"}, True)
    with pytest.raises(Exception):
        service.update_flags("mp-1", {"category_name": "热门产品"}, True)
    assert captured == {"monitor_enabled": True, "category_name": "港险"}


def test_mp_auth_invalid_is_blocked_with_safe_evidence(settings, monkeypatch):
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse(
        {"logged_in": False, "token": "secret", "message": "cookie invalid"}, 200
    ))
    result = MpService(settings).auth_check()
    assert result.payload["source_status"]["status"] == "blocked"
    assert result.payload["source_status"]["operation_hint"]
    assert "secret" not in json.dumps(result.payload)


def test_mp_history_import_does_not_fake_live_health_after_auth_failure(settings, tmp_path, monkeypatch):
    configured = _configured(settings, tmp_path)
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse(
        {"logged_in": False, "token": "secret", "message": "cookie invalid"}, 200
    ))
    service = MpService(configured)
    auth_result = service.auth_check()
    assert auth_result.payload["source_status"]["status"] == "blocked"

    monkeypatch.setattr(MpAdapter, "accounts", lambda self: RemoteResponse(
        {"accounts": [{"mp_id": "mp1", "mp_name": "测试号", "password": "never"}]}, 200
    ))
    result = service.import_history(dry_run=False, limit=None)

    with connect(configured, readonly=True) as con:
        content_count = con.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
        connection = con.execute(
            "SELECT status FROM system_connections WHERE system_key='wechat-mp'"
        ).fetchone()
    assert content_count == 1
    assert connection["status"] == "blocked"
    assert result["source_status"]["status"] == "blocked"
    assert result["history_import"]["status"] == "succeeded"
    assert result["history_import"]["source"] == "markdown"
    assert result["source_status"]["verified"] is False
    assert "healthy" not in json.dumps(result["source_status"])
    assert "secret" not in json.dumps(result)


def test_mp_job_is_blocked_when_runtime_login_is_inconsistent(settings, monkeypatch):
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse({"logged_in": False}, 200))
    monkeypatch.setattr(MpAdapter, "runtime_overview", lambda self: RemoteResponse(
        {"wechat_status": {"inconsistent": True, "logged_in": False, "display_status": "未登录"}}, 200
    ))
    called = False
    def create_job(_self, _payload):
        nonlocal called
        called = True
        return RemoteResponse({"created": True}, 200)
    monkeypatch.setattr(MpAdapter, "create_job", create_job)
    with pytest.raises(Exception, match="登录状态"):
        MpService(settings).create_job({"type": "sync"}, True)
    assert called is False


def test_mp_job_command_is_persisted_before_upstream_collection(settings, monkeypatch):
    monkeypatch.setattr(MpAdapter, "auth_check", lambda self: RemoteResponse({"logged_in": True}, 200))
    monkeypatch.setattr(MpAdapter, "runtime_overview", lambda self: RemoteResponse(
        {"wechat_status": {"logged_in": True, "inconsistent": False}}, 200
    ))
    monkeypatch.setattr(MpAdapter, "create_job", lambda self, payload: RemoteResponse(
        {"status": "queued", "legacy_job_id": "old-1", "payload": payload}, 202
    ))
    result = MpService(settings).create_job(
        {"type": "sync", "accounts": ["mp-a"]},
        True,
        idempotency_key="test-mp-command-1",
    )
    assert result.payload["hub_collection_job_id"].startswith("mpj_")
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT status, account_count FROM mp_collection_jobs WHERE collection_job_id=?",
            (result.payload["hub_collection_job_id"],),
        ).fetchone()
    assert tuple(row) == ("queued", 1)


def test_mp_invalid_metadata_url_is_rejected_and_not_written(settings, tmp_path):
    configured = _configured(settings, tmp_path)
    (configured.mp_metadata_root / "bad.csv").write_text(
        "title,publish_time,mp_name,url,mp_id,id\n"
        "坏链接,2026-07-01 10:00:00,测试号,http://evil.example/a,mp-bad,bad-1\n",
        encoding="utf-8",
    )
    result = MpService(configured).import_history(dry_run=False, limit=None)
    assert result["counts"]["accepted"] == 1
    assert result["counts"]["rejected"] == 1
    assert result["counts"]["processed"] == 2
    assert any(item["reason"] == "invalid_url" for item in result["audit"]["rejected"])
    with connect(configured, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM contents WHERE title='坏链接'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] == 0
