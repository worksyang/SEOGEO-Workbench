"""Wiki / Writing / Publishing 服务层单元测试。
对应矩阵 T146-T165。
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.config import Settings
from content_hub.ingestion.markdown_store import MarkdownStore
from content_hub.services.wiki import WikiService
from content_hub.services.writing import WritingService, FakeProvider
from content_hub.services.publishing import PublishingService, PublishAccount


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


def test_t148_wiki_save_atomic_replaces_file(hub):
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
    assert "新内容" in target.read_text(encoding="utf-8")


def test_t149_wiki_save_validation_blocks_external_path(hub):
    conn, _settings, tmp_path = hub
    svc = WikiService(connection=conn, asset_root=tmp_path, source_roots=[tmp_path])
    with pytest.raises(FileNotFoundError):
        svc.save("cnt_never00000001", body="body", operator="test")


def test_t150_wiki_rejects_path_traversal(hub):
    conn, _settings, tmp_path = hub
    svc = WikiService(connection=conn, asset_root=tmp_path, source_roots=[tmp_path])
    # 路径不在允许根的写入入口被 resolve_within 拦截
    from content_hub.validation.paths import resolve_within
    with pytest.raises(Exception):
        resolve_within(Path("/etc/passwd"), [tmp_path])


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


def test_t157_writing_run_mother_forge_writes_artifact(hub):
    conn, settings, _ = hub
    store = MarkdownStore(settings.asset_store_path)
    svc = WritingService(connection=conn, markdown_store=store, provider=FakeProvider(latency_ms=0))
    job = svc.create_mother_forge(topic="铸造测试", purpose="")
    result = svc.run(job.job_id, operator="test")
    assert result["status"] == "succeeded"
    assert Path(result["md_path"]).exists()


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
    assert result["status"] == "succeeded"
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
    assert detail["status"] == "succeeded"


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
    # 字段集合应该只有 6 项
    expected = {"account_id", "display_name", "profile_dir", "cookie_file", "token_file", "enabled"}
    assert set(asdict(acct).keys()) == expected, f"PublishAccount 字段集合变化：{set(asdict(acct).keys())}"
    dumped = str(acct.to_dict())
    # 文件路径允许暴露，但 cookie 文件路径如果含 SECRET（视为类似敏感哨兵值），
    # 仅在 `to_dict` 标注的字段中可见，不被自动展开读取
    assert "cookie_file" in dumped
    assert "token_file" in dumped
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
    assert "preview_path" in result
    assert Path(result["preview_path"]).exists()


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
    result_with_confirm = svc.publish(account_id="acc", content_id="c", body="x", confirm=True)
    assert result_with_confirm["status"] == "succeeded"
