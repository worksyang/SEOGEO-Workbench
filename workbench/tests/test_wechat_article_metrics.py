from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.services.wechat_article_metrics import (
    WechatArticleMetricsService,
    backfill,
    parse_article_metrics,
)


def _settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    db = tmp_path / "hub.sqlite"
    return replace(base, database_path=db, lock_path=tmp_path / "hub.lock", asset_store_path=tmp_path / "asset_store")


def _insert_content(settings: Settings, content_id: str, md_path: str) -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,md_path,payload_json)
               VALUES(?,?,?,?,?,?,?)""",
            (content_id, "wechat_article", "测试文章", "2026-07-18T01:00:00Z", "2026-07-18T01:00:00Z", md_path, "{}"),
        )
        con.commit()


def test_parse_new_article_20260718_uses_legacy_footer_rules():
    text = (
        "正文里有阅读999，不应取这里。\n"
        "阅读1234\n"
        "5篇原创内容\n"
        "#\n"
        "12个朋友关注\n"
        "赞34\n"
        "EndFragment"
    )
    assert parse_article_metrics(text).present() == {
        "read_count": 1234,
        "like_count": 34,
        "friends_follow_count": 12,
        "original_article_count": 5,
    }


def test_backfill_updates_payload_and_observations_idempotently(tmp_path: Path):
    settings = _settings(tmp_path)
    migrate(settings)
    asset = settings.asset_store_path / "wechat" / "2026-07-18-new.md"
    asset.parent.mkdir(parents=True)
    asset.write_text("阅读100\n3篇原创内容\n#\n8个朋友关注\n关注6推荐\nEndFragment", encoding="utf-8")
    content_id = "wechat_article_" + "a" * 32
    _insert_content(settings, content_id, "wechat/2026-07-18-new.md")

    service = WechatArticleMetricsService(settings)
    first = service.backfill_contents(observed_at="2026-07-18T08:00:00Z")
    second = service.backfill_contents(observed_at="2026-07-18T08:00:00Z")
    assert first["observations"] == 4
    assert second["observations"] == 4

    with connect(settings, readonly=True) as con:
        payload = json.loads(con.execute("SELECT payload_json FROM contents WHERE content_id=?", (content_id,)).fetchone()[0])
        assert payload == {"friends_follow_count": 8, "like_count": 6, "original_article_count": 3, "read_count": 100}
        assert con.execute("SELECT COUNT(*) FROM metric_observations WHERE subject_type='content' AND subject_id=?", (content_id,)).fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM metric_observations").fetchone()[0] == 4


def test_markdown_without_metrics_is_not_written_as_zero(tmp_path: Path):
    settings = _settings(tmp_path)
    migrate(settings)
    asset = settings.asset_store_path / "wechat" / "no-metrics.md"
    asset.parent.mkdir(parents=True)
    asset.write_text("# 2026-07-18 新文章\n\n只有正文，没有底部指标。\n", encoding="utf-8")
    content_id = "wechat_article_" + "b" * 32
    _insert_content(settings, content_id, "wechat/no-metrics.md")

    stats = WechatArticleMetricsService(settings).backfill_contents(observed_at="2026-07-18T09:00:00Z")
    assert stats["skipped"] == 1
    with connect(settings, readonly=True) as con:
        payload = json.loads(con.execute("SELECT payload_json FROM contents WHERE content_id=?", (content_id,)).fetchone()[0])
        assert payload == {}
        assert con.execute("SELECT COUNT(*) FROM metric_observations").fetchone()[0] == 0


def test_connection_aware_backfill_uses_canonical_identity_and_outer_transaction(tmp_path: Path):
    settings = _settings(tmp_path)
    migrate(settings)
    asset = settings.asset_store_path / "wechat" / "legacy-id.md"
    asset.parent.mkdir(parents=True)
    asset.write_text(("正文\n" * 200) + "阅读77\n#\nEndFragment", encoding="utf-8")
    content_id = "imported-article-without-prefix"
    _insert_content(settings, content_id, "wechat/legacy-id.md")
    with connect(settings) as con:
        con.execute(
            """INSERT INTO content_identifiers(
                   namespace,external_id,content_id,first_seen_at,payload_json
               ) VALUES(?,?,?,?,?)""",
            ("wechat_article", "gh_1", content_id, "2026-07-18T10:00:00Z", "{}"),
        )
        con.commit()

    with connect(settings) as con:
        with con:
            result = backfill(
                con, settings, observed_at="2026-07-18T10:00:00Z",
                source_ref="test:outer", content_ids=[content_id],
            )
            assert result["with_metrics"] == 1
            assert con.execute(
                "SELECT numeric_value FROM metric_observations WHERE subject_id=?",
                (content_id,),
            ).fetchone()[0] == 77
