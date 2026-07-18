from __future__ import annotations

import asyncio
import json

import httpx

from content_hub.app import create_app
from content_hub.db.connection import connect


def _seed(settings) -> None:
    with connect(settings) as connection:
        connection.execute(
            """INSERT INTO keywords(
                keyword_id,platform,keyword,status,topic,keyword_bucket,
                first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "xhs_keyword_1",
                "xiaohongshu",
                "香港保险",
                "active",
                "港险",
                "保险",
                "2026-07-15T00:00:00Z",
                "2026-07-16T00:00:00Z",
                json.dumps({"source_keyword_id": "kw_1"}),
            ),
        )
        connection.execute(
            """INSERT INTO creators(
                creator_id,canonical_name,platform,external_id,
                first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "xhs_creator_1",
                "测试博主",
                "xiaohongshu",
                "account_1",
                "2026-07-15T00:00:00Z",
                "2026-07-16T00:00:00Z",
                json.dumps({"headimg_url": "https://sns-na-i11.xhscdn.com/avatar"}),
            ),
        )
        connection.execute(
            """INSERT INTO contents(
                content_id,content_type,title,canonical_url,creator_id,
                author_name,published_at,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "xhs_tk_note_1",
                "social_note",
                "测试笔记",
                "https://www.xiaohongshu.com/explore/note_1",
                "xhs_creator_1",
                "测试博主",
                "2026-07-16T08:00:00Z",
                "2026-07-15T00:00:00Z",
                "2026-07-16T00:00:00Z",
                json.dumps(
                    {
                        "summary": "笔记摘要",
                        "work_type": "normal",
                        "cover_url": "https://sns-na-i11.xhscdn.com/test-cover",
                        "liked_count": 11,
                        "collected_count": 12,
                        "comment_count": 3,
                        "shared_count": 2,
                        "read_count": None,
                    }
                ),
            ),
        )
        connection.execute(
            """INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)""",
            (
                "xiaohongshu_article",
                "note_1",
                "xhs_tk_note_1",
                "2026-07-15T00:00:00Z",
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO search_snapshots(
                snapshot_id,platform,keyword,keyword_id,captured_at,
                trigger_type,result_count,payload_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "xhs_snapshot_1",
                "xiaohongshu",
                "香港保险",
                "xhs_keyword_1",
                "2026-07-16T09:00:00Z",
                "import",
                1,
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO search_hits(
                hit_id,snapshot_id,rank,content_id,title_raw,url_raw,
                creator_name_raw,payload_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "xhs_hit_1",
                "xhs_snapshot_1",
                1,
                "xhs_tk_note_1",
                "测试笔记",
                "https://www.xiaohongshu.com/explore/note_1",
                "测试博主",
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO metric_definitions(
                metric_key,platform,subject_type,display_name
            ) VALUES(?,?,?,?)""",
            ("xhs.note.like", "xiaohongshu", "content", "小红书点赞"),
        )
        connection.execute(
            """INSERT INTO metric_observations(
                observation_id,subject_type,subject_id,metric_key,
                observed_at,numeric_value,snapshot_id,payload_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "xhs_observation_1",
                "content",
                "xhs_tk_note_1",
                "xhs.note.like",
                "2026-07-16T09:00:00Z",
                11,
                "xhs_snapshot_1",
                "{}",
            ),
        )


def test_xhs_remaining_reads_are_hub_projected_and_markdown_is_blocked(settings):
    _seed(settings)

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                headers = {
                    "Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"
                }
                articles = await client.get(
                    "/api/articles?page=1&page_size=50&time_range=0",
                    headers=headers,
                )
                assert articles.status_code == 200
                assert articles.json()["source"] == "hub_db"
                assert articles.json()["articles"][0]["article_id"] == "xhs_tk_note_1"
                assert articles.json()["articles"][0]["hit_keywords"] == ["香港保险"]
                assert articles.json()["articles"][0]["account_id"] == "account_1"

                accounts = await client.get(
                    "/api/articles/accounts",
                    headers=headers,
                )
                assert accounts.status_code == 200
                assert accounts.json()["accounts"] == [
                    {
                        "account_id": "account_1",
                        "name": "测试博主",
                        "headimg_url": "https://sns-na-i11.xhscdn.com/avatar",
                        "article_count": 1,
                    }
                ]

                detail = await client.get(
                    "/api/article-hit-detail?article_id=xhs_tk_note_1",
                    headers=headers,
                )
                assert detail.status_code == 200
                detail_payload = detail.json()
                assert detail_payload["source_status"]["source"] == "hub_db"
                assert detail_payload["keyword_count"] == 1
                assert detail_payload["hit_count"] == 1
                assert detail_payload["content_files"] == []

                by_url = await client.get(
                    "/api/article-hit-detail?url=https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fnote_1%3Fxsec_token%3Dredacted",
                    headers=headers,
                )
                assert by_url.status_code == 200
                assert by_url.json()["article"]["article_id"] == "xhs_tk_note_1"

                covers = await client.post(
                    "/api/article-covers",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"articles": [{"article_id": "xhs_tk_note_1"}, {"article_id": "missing"}]},
                )
                assert covers.status_code == 200
                assert covers.json() == {
                    "items": [
                        {
                            "article_id": "xhs_tk_note_1",
                            "cover_url": "https://sns-na-i11.xhscdn.com/test-cover",
                            "status": "found",
                        },
                        {"article_id": "missing", "cover_url": None, "status": "not_found"},
                    ],
                    "source": "hub_db",
                }

                content = await client.get(
                    "/api/article-content?path=xhs_tk_note_1",
                    headers=headers,
                )
                assert content.status_code == 409
                assert content.json()["error"]["code"] == "LEGACY_XHS_READ_BLOCKED"
                assert content.json()["upstream_called"] is False

    asyncio.run(scenario())
