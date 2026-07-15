from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_hub.adapters.wiki import scan_directory
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.services.wiki import WikiService


@pytest.fixture
def wiki_hub(tmp_path):
    settings = Settings.load()
    from dataclasses import replace

    settings = replace(settings, database_path=tmp_path / "hub.sqlite")
    migrate(settings)
    connection = connect(settings)
    try:
        yield connection, settings, tmp_path
    finally:
        connection.close()


def test_wiki_import_projects_manifest_and_baseline_versions_idempotently(wiki_hub):
    connection, _settings, tmp_path = wiki_hub
    source = tmp_path / "output_md"
    (source / "其他").mkdir(parents=True)
    (source / "wiki" / "知识库").mkdir(parents=True)
    (source / "其他" / "母文章.md").write_text("# 母文章\n\n正文", encoding="utf-8")
    (source / "wiki" / "知识库" / "规则.md").write_text("# 规则\n\n知识", encoding="utf-8")
    asset = tmp_path / "asset_store"
    service = WikiService(
        connection=connection,
        asset_root=asset,
        source_roots=[source],
        lock_path=tmp_path / "wiki.lock",
    )

    first = service.import_wiki(confirm=True, max_files=10, operator="test")
    second = service.import_wiki(confirm=True, max_files=10, operator="test")

    assert first["scanned"] == first["accepted"] == 2
    assert first["baseline_versions"] == 2
    assert second["baseline_versions"] == 2
    assert connection.execute("SELECT COUNT(*) FROM source_manifests").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM source_manifest_entries").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM wiki_file_versions").fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM wiki_file_versions WHERE version_status='baseline'"
    ).fetchone()[0] == 2

    public = json.dumps({"first": first, "tree": service.tree()}, ensure_ascii=False)
    assert str(source) not in public
    assert str(asset) not in public
    assert all(
        not str(row[0]).startswith("/")
        for row in connection.execute(
            "SELECT source_ref FROM wiki_file_versions"
        ).fetchall()
    )

    # 工作副本存在后重放导入不得覆盖人工副本。
    content_id = connection.execute(
        "SELECT content_id FROM contents WHERE title='母文章'"
    ).fetchone()[0]
    workspace = asset / "wiki-workspace" / "files" / "其他" / "母文章.md"
    workspace.write_text("# 母文章\n\n保留的工作副本", encoding="utf-8")
    service.import_wiki(confirm=True, max_files=10, operator="test")
    assert "保留的工作副本" in workspace.read_text(encoding="utf-8")
    assert connection.execute(
        "SELECT COUNT(*) FROM wiki_file_versions WHERE content_id=?",
        (content_id,),
    ).fetchone()[0] == 1


def test_wiki_real_source_scan_reports_all_markdown_and_explicit_exclusions():
    source = Path("/Users/works14/Documents/output_md")
    if not source.is_dir():
        return
    scan = scan_directory(source, max_files=2000)
    assert scan.scanned == 1051
    assert scan.accepted == 1045
    assert scan.truncated is False
