"""Wiki / Writing / Publishing 服务层单元测试。
对应矩阵 T146-T165。
"""
from __future__ import annotations

import asyncio
import sqlite3
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from content_hub.app import create_app
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.config import Settings
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.services.wiki import WikiService
from content_hub.services.writing import WritingService, FakeProvider, backfill_writing_runtime
from content_hub.services.publishing import PublishingService, PublishAccount
from content_hub.adapters.wiki import scan_directory


@pytest.fixture
def hub(tmp_path: Path):
    db = tmp_path / "h.sqlite"
    settings = Settings.load()
    from dataclasses import replace
    settings = replace(settings, database_path=db)
    migrate(settings)
    conn = connect(settings, readonly=False)
    conn.row_factory = sqlite3.Row
    yield conn, settings, tmp_path
    conn.close()


# ── Wiki ────────────────────────────────────────────


def test_t146_wiki_tree_scans_directories(hub):
    conn, _settings, tmp_path = hub
    # 在 tmp_path 创建一个模拟 Wiki 库
    wiki = tmp_path / "wiki"
    (wiki / "产品母页").mkdir(parents=True)
    (wiki / "产品母页" / "友邦财富盈活.md").write_text("# 友邦财富盈活\n\n> 测试", encoding="utf-8")
    (wiki / "保司母页").mkdir(parents=True)
    (wiki / "保司母页" / "保诚信守明天.md").write_text("# 保诚信守明天\n\n> 测试", encoding="utf-8")
    # asset_root 放另一边
    asset_root = tmp_path / "asset"
    asset_root.mkdir()
    (asset_root / "wiki").mkdir()
    svc = WikiService(connection=conn, asset_root=asset_root, source_roots=[tmp_path])
    tree = svc.tree()
    assert len(tree) >= 1
    files = svc.collect()
    assert any("友邦" in f["title"] for f in files)


def test_t147_wiki_search_finds_file(hub):
    conn, _settings, tmp_path = hub
    wiki = tmp_path / "wiki" / "产品母页"
    wiki.mkdir(parents=True)
    (wiki / "友邦财富盈活.md").write_text("# 友邦财富盈活\n\n正文", encoding="utf-8")
    asset_root = tmp_path / "asset"
    asset_root.mkdir()
    (asset_root / "wiki").mkdir()
    svc = WikiService(connection=conn, asset_root=asset_root, source_roots=[tmp_path])
    results = svc.search("友邦")
    assert any(item["title"].startswith("友邦") for item in results)


def test_t148_wiki_save_writes_versioned_workspace_not_original(hub):
    conn, _settings, tmp_path = hub
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    target = wiki / "测试.md"
    target.write_text("---\nschema_version: content-md/1.1\ncontent_id: cnt_wiki00000001\n---\n\n# 测试\n\n旧文本\n", encoding="utf-8")
    asset_root = tmp_path / "asset"
    asset_root.mkdir()
    (asset_root / "wiki").mkdir()
    svc = WikiService(connection=conn, asset_root=asset_root, source_roots=[tmp_path])
    cid = svc.collect()[0]["content_id"]
    new_body = "---\nschema_version: content-md/1.1\ncontent_id: cnt_wiki00000001\n---\n\n# 测试\n\n新内容\n"
    result = svc.save(cid, body=new_body, operator="test")
    assert result["content_id"] == cid
    assert "旧文本" in target.read_text(encoding="utf-8")
    workspace = asset_root / result["workspace_ref"]
    assert workspace.read_text(encoding="utf-8").endswith("新内容\n")
    assert result["original_written"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM wiki_file_versions WHERE content_id=?",
        (cid,),
    ).fetchone()[0] == 2
    second = svc.save(cid, body=new_body.replace("新内容", "再次保存"), operator="test")
    assert second["version_id"] != result["version_id"]
    assert workspace.read_text(encoding="utf-8").endswith("再次保存\n")
    assert str(tmp_path) not in json.dumps(result, ensure_ascii=False)


def test_t149_wiki_save_validation_blocks_external_path(hub):
    conn, _settings, tmp_path = hub
    svc = WikiService(connection=conn, asset_root=tmp_path, source_roots=[tmp_path])
    with pytest.raises(FileNotFoundError):
        svc.save("cnt_never00000001", body="body", operator="test")


def test_t149b_wiki_source_read_and_save_keep_version_lock(hub):
    conn, settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "wiki").mkdir(parents=True)
    original = root / "wiki" / "规则.md"
    original.write_text("# 规则\n\n原文\n", encoding="utf-8")
    asset = tmp_path / "asset"
    asset.mkdir()
    from dataclasses import replace
    app_settings = replace(settings, wiki_allowed_roots=(root,), asset_store_path=asset)

    async def scenario():
        app = create_app(app_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                loaded = await client.get("/api/v1/wiki/source", params={"source_ref": "wiki/规则.md"})
                assert loaded.status_code == 200
                assert loaded.json()["data"]["body"].endswith("原文\n")
                assert loaded.json()["data"]["version_id"] is None
                saved = await client.put(
                    "/api/v1/wiki/source",
                    json={"source_ref": "wiki/规则.md", "body": "# 规则\n\n工作副本\n"},
                )
                assert saved.status_code == 200
                version_id = saved.json()["data"]["version_id"]
                reread = await client.get("/api/v1/wiki/source", params={"source_ref": "wiki/规则.md"})
                assert reread.json()["data"]["body"].endswith("工作副本\n")
                assert reread.json()["data"]["version_id"] == version_id
                conflict = await client.put(
                    "/api/v1/wiki/source",
                    json={
                        "source_ref": "wiki/规则.md",
                        "body": "# 规则\n\n冲突内容\n",
                        "base_version_id": "wfv_not_current",
                    },
                )
                assert conflict.status_code == 409

    asyncio.run(scenario())
    assert original.read_text(encoding="utf-8").endswith("原文\n")


def test_t150_wiki_rejects_path_traversal(hub):
    conn, _settings, tmp_path = hub
    svc = WikiService(connection=conn, asset_root=tmp_path, source_roots=[tmp_path])
    # 路径不在允许根的写入入口被 resolve_within 拦截
    from content_hub.validation.paths import resolve_within
    with pytest.raises(Exception):
        resolve_within(Path("/etc/passwd"), [tmp_path])


def test_t153_wiki_workspace_rejects_symlink_escape(hub):
    conn, _settings, tmp_path = hub
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "测试.md"
    target.write_text("# 测试\n\n旧文本\n", encoding="utf-8")
    asset_root = tmp_path / "asset"
    (asset_root / "wiki-workspace").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (asset_root / "wiki-workspace" / "files").symlink_to(outside, target_is_directory=True)
    svc = WikiService(connection=conn, asset_root=asset_root, source_roots=[tmp_path])
    cid = svc.collect()[0]["content_id"]
    with pytest.raises(Exception):
        svc.save(cid, body="# 测试\n\n新文本\n", operator="test")
    assert not list(outside.iterdir())


def test_t166_wiki_import_dry_run_has_no_database_writes(hub):
    conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "母文章.md").write_text("# 母文章\n\n正文", encoding="utf-8")
    (root / "wiki" / "知识库").mkdir(parents=True)
    (root / "wiki" / "知识库" / "规则.md").write_text("# 规则\n\n知识", encoding="utf-8")
    asset = tmp_path / "asset"
    (asset / "wiki").mkdir(parents=True)
    svc = WikiService(connection=conn, asset_root=asset, source_roots=[root], lock_path=tmp_path / "lock")
    before = conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
    result = svc.import_wiki(confirm=False, max_files=10)
    assert result["status"] == "dry_run"
    assert result["accepted"] == 2
    assert result["processed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0] == 0


def test_t167_wiki_import_confirm_is_idempotent_and_classified(hub):
    conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "母文章.md").write_text("# 母文章\n\n正文", encoding="utf-8")
    (root / "wiki").mkdir()
    (root / "wiki" / "规则.md").write_text("# 规则\n\n知识", encoding="utf-8")
    asset = tmp_path / "asset"
    (asset / "wiki").mkdir(parents=True)
    svc = WikiService(connection=conn, asset_root=asset, source_roots=[root], lock_path=tmp_path / "lock")
    first = svc.import_wiki(confirm=True, max_files=10, operator="test")
    second = svc.import_wiki(confirm=True, max_files=10, operator="test")
    assert first["status"] == "succeeded"
    assert first["processed"] == 2
    assert first["classification"]["mother_article"] == 1
    assert first["classification"]["knowledge_article"] == 1
    assert second["status"] == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM content_identifiers").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM content_discoveries WHERE discovery_system='wiki'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM ingestion_checkpoints WHERE adapter_key='wiki'").fetchone()[0] == 1
    connection = conn.execute(
        "SELECT status, base_url, details_json FROM system_connections WHERE system_key='wiki'"
    ).fetchone()
    assert connection["status"] == "healthy"
    assert connection["base_url"] is None
    assert json.loads(connection["details_json"])["accepted"] == 2
    audit = conn.execute("SELECT details_json FROM audit_log WHERE action='wiki.import' ORDER BY occurred_at DESC LIMIT 1").fetchone()[0]
    assert str(root) not in audit
    assert "configured/wiki-source" in audit
    tree = svc.tree()
    entries = svc.collect(tree)
    mother = next(item for item in entries if item["source_ref"] == "其他/母文章.md")
    knowledge = next(item for item in entries if item["source_ref"] == "wiki/规则.md")
    assert mother["content_id"] == conn.execute(
        "SELECT content_id FROM contents WHERE title='母文章'"
    ).fetchone()[0]
    assert knowledge["content_id"] == conn.execute(
        "SELECT content_id FROM contents WHERE title='规则'"
    ).fetchone()[0]
    assert svc.search("母文章")[0]["content_id"] == mother["content_id"]
    detail = svc.read(mother["content_id"])
    assert detail and detail["entry"]["source_ref"] == "其他/母文章.md"
    assert "正文" in detail["body"]
    public_data = json.dumps({"tree": tree, "detail": detail}, ensure_ascii=False)
    assert str(root) not in public_data
    assert str(asset) not in public_data

    saved = svc.save(mother["content_id"], body="# 母文章\n\n编辑后的正文", operator="test")
    assert "正文" in (root / "其他" / "母文章.md").read_text(encoding="utf-8")
    assert (asset / saved["workspace_ref"]).read_text(encoding="utf-8").endswith("编辑后的正文\n")
    assert saved["original_written"] is False
    row = conn.execute(
        "SELECT content_id, content_hash FROM contents WHERE content_id=?",
        (mother["content_id"],),
    ).fetchone()
    assert row["content_id"] == mother["content_id"]
    assert row["content_hash"]
    save_audit = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='wiki.workspace_save' ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()[0]
    assert str(root) not in save_audit
    assert "其他/母文章.md" in save_audit
    # 保存必须提交：工作台请求会使用独立连接，重开只读连接后仍要看到新索引。
    reader = sqlite3.connect(_settings.database_path)
    try:
        persisted = reader.execute(
            "SELECT content_hash FROM contents WHERE content_id=?",
            (mother["content_id"],),
        ).fetchone()
        assert persisted and persisted[0] == row["content_hash"]
    finally:
        reader.close()


def test_t168_wiki_scan_rejects_symlink_invalid_utf8_and_excluded_dirs(hub):
    _conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "ok.md").write_text("# OK", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "hidden.md").write_text("# hidden", encoding="utf-8")
    (root / "wiki-viewer").mkdir()
    (root / "wiki-viewer" / "tool.md").write_text("# tool", encoding="utf-8")
    (root / "其他" / "bad.md").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside", encoding="utf-8")
    (root / "其他" / "link.md").symlink_to(outside)
    scan = scan_directory(root, max_files=10)
    reasons = {item["reason"] for item in scan.rejected}
    refs = {item["source_ref"] for item in scan.rejected}
    assert scan.accepted == 1
    assert "invalid_utf8" in reasons
    assert "symlink_or_not_regular_file" in reasons
    assert "其他/bad.md" in refs
    assert "其他/link.md" in refs
    assert "excluded_directory:.hidden" in reasons
    assert "excluded_directory:wiki-viewer" in reasons


def test_t168b_wiki_import_audits_expected_rejections_without_false_failure(hub):
    conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "ok.md").write_text("# OK", encoding="utf-8")
    (root / "wiki-viewer").mkdir()
    (root / "wiki-viewer" / "tool.md").write_text("# tool", encoding="utf-8")
    (root / "wiki").mkdir()
    asset = tmp_path / "asset"
    (asset / "wiki").mkdir(parents=True)
    svc = WikiService(connection=conn, asset_root=asset, source_roots=[root], lock_path=tmp_path / "lock")
    result = svc.import_wiki(confirm=True, max_files=10, operator="test")
    assert result["status"] == "degraded"
    outcome, details_json = conn.execute(
        "SELECT outcome, details_json FROM audit_log WHERE action='wiki.import' ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()
    assert outcome == "succeeded"
    assert json.loads(details_json)["status"] == "degraded"
    connection = conn.execute(
        "SELECT status, details_json FROM system_connections WHERE system_key='wiki'"
    ).fetchone()
    assert connection["status"] == "degraded"
    assert json.loads(connection["details_json"])["rejected"] == 1


def test_t169_wiki_scan_reports_truncation_and_real_root_shape(hub):
    _conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    for index in range(3):
        (root / "其他" / f"{index}.md").write_text(f"# {index}", encoding="utf-8")
    scan = scan_directory(root, max_files=2)
    assert scan.scanned == 3
    assert scan.accepted == 2
    assert scan.truncated is True


def test_t170_wiki_tree_read_skip_symlink_dirs_and_files(hub):
    conn, _settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "正常.md").write_text("# 正常\n\n正文", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "逃逸.md").write_text("# 逃逸", encoding="utf-8")
    (root / "软链目录").symlink_to(outside, target_is_directory=True)
    (root / "其他" / "软链文件.md").symlink_to(outside / "逃逸.md")
    (root / "wiki").mkdir()
    asset = tmp_path / "asset"
    (asset / "wiki").mkdir(parents=True)
    svc = WikiService(connection=conn, asset_root=asset, source_roots=[root], lock_path=tmp_path / "lock")
    entries = svc.collect()
    refs = {entry["source_ref"] for entry in entries}
    assert "其他/正常.md" in refs
    assert "软链目录/逃逸.md" not in refs
    assert "其他/软链文件.md" not in refs
    assert svc.read("cnt_not_a_real_article") is None


def test_t171_wiki_api_entries_and_reads_do_not_expose_absolute_paths(hub):
    conn, settings, tmp_path = hub
    root = tmp_path / "source"
    (root / "其他").mkdir(parents=True)
    (root / "其他" / "接口文章.md").write_text("# 接口文章\n\n正文", encoding="utf-8")
    (root / "wiki").mkdir()
    asset = tmp_path / "asset"
    (asset / "wiki").mkdir(parents=True)
    svc = WikiService(connection=conn, asset_root=asset, source_roots=[root], lock_path=tmp_path / "lock")
    imported = svc.import_wiki(confirm=True, max_files=10, operator="test")
    assert imported["status"] == "succeeded"
    content_id = conn.execute("SELECT content_id FROM contents WHERE title='接口文章'").fetchone()[0]
    from dataclasses import replace
    app = create_app(replace(
        settings,
        asset_store_path=asset,
        wiki_allowed_roots=(root, asset),
        lock_path=tmp_path / "lock",
    ))

    async def request_json(path: str) -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(path)
            assert response.status_code == 200
            return response.json()

    tree = asyncio.run(request_json("/api/v1/wiki/tree"))
    detail = asyncio.run(request_json(f"/api/v1/wiki/{content_id}"))
    async def confirm_import() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/wiki/import",
                json={"confirm": True, "max_files": 10, "operator": "test"},
            )
            assert response.status_code == 200
            return response.json()

    confirmed = asyncio.run(confirm_import())
    assert confirmed["ok"] is True
    assert confirmed["data"]["status"] == "succeeded"
    payload = json.dumps({"tree": tree, "detail": detail}, ensure_ascii=False)
    assert str(root) not in payload
    assert str(asset) not in payload
    assert detail["data"]["entry"]["source_ref"] == "其他/接口文章.md"


def test_t151_settings_default_wiki_roots_exclude_zkcode_source(hub):
    _conn, settings, _tmp_path = hub
    roots = {path.resolve() for path in settings.wiki_allowed_roots}
    assert Path("/Users/works14/Documents/output_md").resolve() in roots
    assert settings.asset_store_path.resolve() in roots
    assert Path("/Users/works14/Documents/zkcode").resolve() not in roots
    assert all("source" not in str(path).split("/") for path in roots)


def test_t152_settings_publish_accounts_safe_json_and_demo_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_PUBLISH_ACCOUNTS", raising=False)
    monkeypatch.setenv("HUB_DATABASE_PATH", str(tmp_path / "hub.sqlite"))
    fallback = Settings.load().publish_accounts
    assert fallback == (
        {
            "account_id": "demo",
            "display_name": "演示账号（不可发布）",
            "profile_dir": "",
            "cookie_file": "",
            "token_file": "",
            "enabled": False,
            "publishable": False,
        },
    )

    monkeypatch.setenv(
        "HUB_PUBLISH_ACCOUNTS",
        json.dumps(
            [
                {
                    "account_id": "real",
                    "display_name": "公开名称",
                    "profile_dir": "/private/profile",
                    "cookie_file": "/private/cookie",
                    "token_file": "/private/token",
                    "enabled": True,
                    "publishable": True,
                }
            ]
        ),
    )
    parsed = Settings.load().publish_accounts
    assert parsed[0]["account_id"] == "real"
    assert parsed[0]["publishable"] is True

    monkeypatch.setenv("HUB_PUBLISH_ACCOUNTS", '{"not": "an array"}')
    with pytest.raises(ValueError, match="JSON 数组"):
        Settings.load()
    monkeypatch.setenv("HUB_PUBLISH_ACCOUNTS", '[{"account_id": "x", "display_name": "x", "enabled": "yes"}]')
    with pytest.raises(ValueError, match="布尔值"):
        Settings.load()


# ── Writing ─────────────────────────────────────────


def test_t155_writing_create_mother_forge_returns_job(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_mother_forge(
        topic="测试选题",
        purpose="为测试而创建的母文章",
        urls=[],
        recommended_mothers=["母文章 A · 0", "母文章 B · 1"],
    )
    assert job.job_type == "mother_forge"
    assert job.job_id.startswith("job_")
    assert job.topic == "测试选题"
    project_id = conn.execute(
        "SELECT wm_project_id FROM production_jobs WHERE job_id=?",
        (job.job_id,),
    ).fetchone()[0]
    project = conn.execute(
        "SELECT title, purpose, stage, status FROM wm_projects WHERE wm_project_id=?",
        (project_id,),
    ).fetchone()
    assert tuple(project) == ("测试选题", "为测试而创建的母文章", "decision", "active")
    assert conn.execute(
        "SELECT COUNT(*) FROM wm_project_events WHERE wm_project_id=?",
        (project_id,),
    ).fetchone()[0] == 1


def test_t156_writing_create_batch_returns_job(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_batch(
        topic="香港储蓄险",
        source="manual",
        requirements={"style": "concise"},
        keywords=["友邦财富盈活", "保诚信守明天", "安盛盛利2"],
        target_article_count=3,
    )
    assert job.job_type == "batch_production"
    assert "keywords" in job.payload
    batch_id = conn.execute(
        "SELECT wm_batch_id FROM production_jobs WHERE job_id=?",
        (job.job_id,),
    ).fetchone()[0]
    assert tuple(conn.execute(
        "SELECT title, source FROM wm_batches WHERE wm_batch_id=?",
        (batch_id,),
    ).fetchone()) == ("香港储蓄险", "manual")
    assert conn.execute(
        "SELECT COUNT(*) FROM wm_batch_keywords WHERE wm_batch_id=?",
        (batch_id,),
    ).fetchone()[0] == 3


def test_t156b_writing_runtime_backfill_is_idempotent(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    legacy = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = legacy.create_batch(
        topic="待回填批次",
        source="manual",
        requirements={"brief": "历史任务"},
        keywords=["历史关键词"],
        target_article_count=1,
    )
    conn.execute(
        "UPDATE production_jobs SET wm_batch_id=NULL WHERE job_id=?",
        (job.job_id,),
    )
    conn.execute("DELETE FROM wm_batch_keywords")
    conn.execute("DELETE FROM wm_batches")
    assert backfill_writing_runtime(conn, asset_root=settings.asset_store_path) == 1
    assert backfill_writing_runtime(conn, asset_root=settings.asset_store_path) == 0
    assert conn.execute(
        "SELECT wm_batch_id FROM production_jobs WHERE job_id=?",
        (job.job_id,),
    ).fetchone()[0]


def test_t157_writing_run_mother_forge_writes_artifact(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_mother_forge(topic="铸造测试", purpose="")
    result = svc.run(job.job_id, operator="test")
    assert result["status"] == "demo_only"
    assert result["demo"] is True
    assert result["reason_code"] == "writing.demo_provider"
    assert result["asset_ref"] == result["md_path"]
    assert not Path(result["md_path"]).is_absolute()
    artifact = settings.asset_store_path / result["asset_ref"]
    assert artifact.exists()
    assert conn.execute("SELECT status FROM production_jobs WHERE job_id=?", (job.job_id,)).fetchone()[0] == "blocked"
    assert conn.execute(
        "SELECT COUNT(*) FROM wm_drafts WHERE wm_project_id=(SELECT wm_project_id FROM production_jobs WHERE job_id=?)",
        (job.job_id,),
    ).fetchone()[0] == 1
    assert "demo_mother_article" in artifact.read_text(encoding="utf-8")
    event = conn.execute(
        "SELECT payload_json FROM job_events WHERE job_id=? AND event_type='mother_forge.written'",
        (job.job_id,),
    ).fetchone()[0]
    audit = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='writing.run' ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()[0]
    assert str(settings.asset_store_path) not in event
    assert str(settings.asset_store_path) not in audit


def test_t158_writing_run_batch_emits_outputs(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_batch(
        topic="批量成稿测试",
        source="manual",
        requirements={"style": "concise"},
        keywords=["K1", "K2"],
        target_article_count=2,
    )
    result = svc.run(job.job_id, operator="test")
    assert result["status"] == "demo_only"
    assert result["count"] == 2


def test_t158a_batch_plan_count_overrides_default_target(hub):
    """页面已确认的逐关键词计划不能被创建时默认 1 篇覆盖。"""
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_batch(
        topic="逐关键词计划测试",
        source="manual",
        requirements={},
        keywords=["K1", "K2"],
        target_article_count=1,
    )
    detail = svc.mutate(
        job.job_id,
        action="batch_state",
        value={
            "state": {
                "name": "逐关键词计划测试",
                "source": "manual",
                "brief": "",
                "output_dir": "generated/test",
                "stage": "batch-config",
                "keywords": [
                    {"id": "kw-0", "keyword": "K1", "count": 1, "readiness": "needs-mother"},
                    {"id": "kw-1", "keyword": "K2", "count": 1, "readiness": "needs-mother"},
                ],
                "queue": [],
            }
        },
    )
    assert len(detail["payload"]["batch_state"]["keywords"]) == 2
    result = svc.run(job.job_id, operator="test")
    assert result["count"] == 2


def test_t159_writing_restart_recovery(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_batch(topic="Reboot", source="manual", requirements={}, keywords=["X"], target_article_count=1)
    # 第一次运行
    svc.run(job.job_id, operator="test")
    # 第二次 claim 应该被拒绝
    detail = svc.detail(job.job_id)
    assert detail["status"] == "blocked"


# ── Publishing ─────────────────────────────────────


def test_t160_publishing_does_not_leak_cookie(hub):
    """PublishAccount 不暴露 cookie / token 明文与文件内容读取入口。"""
    conn, _settings, tmp_path = hub
    acct = PublishAccount(
        account_id="acc1",
        display_name="acc1",
        profile_dir="/profiles/a",
        cookie_file="/cookies/SECRET",
        token_file="/tokens/SECRET",
        enabled=True,
    )
    # 内部对象保留配置字段，但服务输出不得泄露路径。
    expected = {
        "account_id", "display_name", "profile_dir", "cookie_file",
        "token_file", "enabled", "publishable", "bridge_kind", "bridge_status",
    }
    assert set(asdict(acct).keys()) == expected, f"PublishAccount 字段集合变化：{set(asdict(acct).keys())}"
    dumped = str(acct.to_dict())
    assert "SECRET" not in dumped
    assert "cookie_file" not in dumped
    assert "token_file" not in dumped
    assert set(acct.to_dict()) == {
        "account_id", "display_name", "enabled", "publishable",
        "bridge_kind", "bridge_status", "status", "reason_code",
    }
    # 不应有 cookie 内容读取的方法
    assert not hasattr(acct, "read_cookie")
    assert not hasattr(acct, "load_cookie_blob")


def test_t163_publishing_dry_run_no_publish(hub):
    conn, _settings, tmp_path = hub
    svc = PublishingService(
        connection=conn,
        publish_root=tmp_path / "publish",
        sensitive_words=[],
        accounts=[PublishAccount("acc", "A", "", "", "", True)],
    )
    result = svc.dry_run(account_id="acc", content_id="c", body="# body")
    assert result["status"] == "dry_run_only"
    assert result["preview_only"] is True
    assert "preview_path" not in result
    assert not Path(result["preview_ref"]).is_absolute()
    assert (tmp_path / result["preview_ref"]).exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM publish_accounts_runtime WHERE account_id='acc'"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM publish_queues").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM publish_queue_items").fetchone()[0] == "drafted"


def test_t164_publishing_idempotency_blocks_duplicate(hub):
    conn, _settings, tmp_path = hub
    svc = PublishingService(
        connection=conn,
        publish_root=tmp_path / "publish",
        sensitive_words=[],
        accounts=[PublishAccount("acc", "A", "", "", "", True)],
    )
    first = svc.save_draft(account_id="acc", content_id="c", body="# body")
    second = svc.save_draft(account_id="acc", content_id="c", body="# body")
    # 第二次因为内容哈希相同走幂等分支，attempt_id 应等于第一次
    assert first["attempt_id"] == second["attempt_id"]


def test_t165_publishing_real_requires_confirm(hub):
    conn, _settings, tmp_path = hub
    svc = PublishingService(
        connection=conn,
        publish_root=tmp_path / "publish",
        sensitive_words=[],
        accounts=[PublishAccount("acc", "A", "", "", "", True)],
    )
    result_no_confirm = svc.publish(account_id="acc", content_id="c", body="x")
    assert result_no_confirm["status"] == "needs_confirmation"
    assert result_no_confirm["ok"] is False
    result_with_confirm = svc.publish(account_id="acc", content_id="c", body="x", confirm=True)
    assert result_with_confirm["status"] == "blocked"
    assert result_with_confirm["ok"] is False
    assert result_with_confirm["reason_code"] == "publish.bridge_unavailable"
    assert not (tmp_path / "publish" / "acc" / "would_publish").exists()
    attempt = conn.execute("SELECT status, payload_json FROM publish_attempts").fetchone()
    assert attempt[0] == "blocked"
    assert "content_md_path" not in attempt[1]


def test_t181_writing_unconfigured_is_blocked_and_audited(hub):
    conn, settings, _ = hub
    svc = WritingService(connection=conn, markdown_store=MarkdownStore(settings.asset_store_path))
    job = svc.create_batch(topic="未配置", source="manual", requirements={}, keywords=["K"], target_article_count=1)
    result = svc.run(job.job_id, operator="test")
    assert result == {
        "status": "blocked",
        "job_id": job.job_id,
        "blocked": True,
        "demo": False,
        "provider_kind": "unconfigured",
        "provider_status": "unconfigured",
        "reason_code": "writing.provider_unconfigured",
        "mode": "batch_production",
    }
    assert not list((settings.asset_store_path / "generated").glob("*.md"))
    event = conn.execute("SELECT payload_json FROM job_events WHERE job_id=? AND event_type='blocked'", (job.job_id,)).fetchone()[0]
    audit = conn.execute("SELECT details_json FROM audit_log WHERE action='writing.run'").fetchone()[0]
    assert "writing.provider_unconfigured" in event and "writing.provider_unconfigured" in audit
    assert "/" not in event and "secret" not in event.lower()


def test_t182_publishing_confirm_is_blocked_without_bridge_and_never_succeeds(hub):
    conn, _settings, tmp_path = hub
    svc = PublishingService(
        connection=conn,
        publish_root=tmp_path / "publish",
        accounts=[PublishAccount("demo", "演示账号", "", "", "", False)],
    )
    result = svc.publish(account_id="demo", content_id="c182", body="# body", confirm=True)
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason_code"] == "publish.bridge_unavailable"
    assert not (tmp_path / "publish" / "demo" / "would_publish").exists()
    row = conn.execute("SELECT status FROM publish_attempts WHERE attempt_id=?", (result["attempt_id"],)).fetchone()
    assert row[0] == "blocked"
    audit = conn.execute("SELECT details_json FROM audit_log WHERE action='publishing.publish'").fetchone()[0]
    assert "publish.bridge_unavailable" in audit
    assert "SECRET" not in audit and "/" not in audit


def test_t183_publishing_refs_and_historical_attempt_api_are_safe(hub):
    conn, settings, tmp_path = hub
    asset = tmp_path / "asset_store"
    svc = PublishingService(
        connection=conn,
        publish_root=asset / "publish",
        accounts=[PublishAccount("acc", "演示账号", "", "", "", False)],
    )
    draft = svc.save_draft(account_id="acc", content_id="c183", body="# draft body")
    assert draft["status"] == "draft_only"
    assert draft["draft_only"] is True
    assert not Path(draft["draft_ref"]).is_absolute()
    assert (asset / draft["draft_ref"]).exists()
    payload = conn.execute(
        "SELECT payload_json FROM publish_attempts WHERE attempt_id=?",
        (draft["attempt_id"],),
    ).fetchone()[0]
    assert str(asset) not in payload
    assert "draft body" not in payload

    conn.execute(
        """INSERT INTO production_jobs(
            job_id, job_type, status, input_signal_ids_json,
            source_content_ids_json, created_at, updated_at, payload_json
        ) VALUES (?, ?, ?, '[]', '[]', ?, ?, '{}')""",
        (
            "job_legacy183",
            "publish_draft",
            "blocked",
            "2026-07-15T00:00:00+08:00",
            "2026-07-15T00:00:00+08:00",
        ),
    )
    conn.execute(
        """INSERT INTO publish_attempts(
            attempt_id, job_id, account_key, idempotency_key, mode, status,
            attempted_at, payload_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "pub_legacy183",
            "job_legacy183",
            "acc",
            "legacy183",
            "draft",
            "blocked",
            "2026-07-15T00:00:00+08:00",
            json.dumps({
                "draft_path": str(asset / "publish" / "acc" / "drafts" / "legacy.md"),
                "cookie_file": "/secret/cookie",
                "body": "不应出现在 API",
            }, ensure_ascii=False),
            str(asset / "publish" / "error.log"),
        ),
    )
    conn.commit()
    from dataclasses import replace
    app = create_app(replace(settings, asset_store_path=asset))

    async def read_attempts() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/publishing/attempts")
            assert response.status_code == 200
            return response.json()

    data = asyncio.run(read_attempts())
    dumped = json.dumps(data, ensure_ascii=False)
    assert str(asset) not in dumped
    assert "cookie" not in dumped.lower()
    assert "不应出现在 API" not in dumped
    assert "asset_ref" in dumped
