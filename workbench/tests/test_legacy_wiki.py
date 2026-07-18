from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from content_hub.app import create_app
from content_hub.config import Settings
from content_hub.db.migrations import migrate


def _run(coro):
    return asyncio.run(coro)


def test_legacy_wiki_mirror_and_read_contract(settings) -> None:
    browser_settings = replace(
        settings,
        frontend_dist=Settings.load().frontend_dist,
    )

    async def scenario() -> None:
        app = create_app(browser_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                wiki_page = await client.get("/legacy/wiki/wiki.html")
                writing_page = await client.get("/legacy/writing/index.html")
                assert wiki_page.status_code == 200
                assert "Wiki 知识库" in wiki_page.text
                assert writing_page.status_code == 200
                assert "WritingMoney" in writing_page.text

                root = await client.get("/api/list")
                assert root.status_code == 200
                assert "dirs" in root.json()
                assert any(item["name"] == "wiki" for item in root.json()["dirs"])

                hits = await client.get("/api/search", params={"q": "环宇盈活"})
                assert hits.status_code == 200
                assert any("环宇盈活" in path for path in hits.json()["files"])

                article = await client.get(
                    "/api/file",
                    params={"path": "wiki/产品母页/友邦环宇盈活.md"},
                )
                assert article.status_code == 200
                assert "友邦环宇盈活" in article.json()["content"]

                traversal = await client.get("/api/file", params={"path": "../AGENTS.md"})
                assert traversal.status_code == 404

    _run(scenario())


def test_legacy_wiki_save_writes_the_real_output_md_source(settings, tmp_path: Path) -> None:
    source_root = tmp_path / "output_md"
    source_root.joinpath("wiki").mkdir(parents=True)
    source_file = source_root / "wiki" / "sample.md"
    source_file.write_text("# 原标题\n\n旧正文\n", encoding="utf-8")
    asset_root = tmp_path / "asset_store"
    test_settings = replace(
        settings,
        wiki_allowed_roots=(source_root,),
        asset_store_path=asset_root,
    )
    migrate(test_settings)

    async def scenario() -> None:
        app = create_app(test_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                saved = await client.post(
                    "/api/save",
                    json={"path": "wiki/sample.md", "content": "# 新标题\n\n新正文"},
                )
                assert saved.status_code == 200
                data = saved.json()
                assert data["ok"] is True
                assert data["data"]["original_written"] is True
                assert source_file.read_text(encoding="utf-8") == "# 新标题\n\n新正文\n"

                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as second:
                    tree = await second.get("/api/v1/wiki/tree")
                    assert tree.status_code == 200
                with test_settings.database_path.open("rb"):
                    pass

    _run(scenario())

    import sqlite3

    connection = sqlite3.connect(test_settings.database_path)
    try:
        audit = connection.execute(
            "SELECT action, outcome FROM audit_log WHERE action='wiki.source_save' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        assert audit == ("wiki.source_save", "succeeded")
    finally:
        connection.close()


def test_legacy_wiki_bulk_image_delete_updates_all_markdown_and_ocr_db(
    settings, tmp_path: Path
) -> None:
    source_root = tmp_path / "output_md"
    (source_root / "wiki").mkdir(parents=True)
    (source_root / "wiki-viewer").mkdir()
    image = "https://mmbiz.qpic.cn/demo/image-id/640?wx_fmt=png"
    core = "https://mmbiz.qpic.cn/demo/image-id"
    first = source_root / "wiki" / "一.md"
    second = source_root / "wiki" / "二.md"
    first.write_text(
        f"# 一\n\n![图]({image})\n\n<!-- OCR内容：文字 -->\n\n---\n\n正文\n",
        encoding="utf-8",
    )
    second.write_text(f"# 二\n\n![图]({image})\n\n结尾\n", encoding="utf-8")
    ocr_db = source_root / "wiki-viewer" / "ocr-db.json"
    ocr_db.write_text(
        json.dumps({core: {"ocr": "文字", "source": "wiki/一.md"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    asset_root = tmp_path / "asset_store"
    test_settings = replace(
        settings,
        wiki_allowed_roots=(source_root,),
        asset_store_path=asset_root,
    )
    migrate(test_settings)

    async def scenario() -> None:
        app = create_app(test_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                deleted = await client.post(
                    "/api/bulk-delete-image",
                    json={"core": core},
                )
                assert deleted.status_code == 200
                result = deleted.json()
                assert result["ok"] is True
                assert result["deleted_files"] == 2
                assert result["deleted_images"] == 2
                assert result["ocr_db_updated"] is True

    _run(scenario())
    assert image not in first.read_text(encoding="utf-8")
    assert image not in second.read_text(encoding="utf-8")
    assert core not in json.loads(ocr_db.read_text(encoding="utf-8"))
