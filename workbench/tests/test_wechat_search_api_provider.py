from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from content_hub.app import create_app
from content_hub.adapters.wechat_search_api import (
    RemoteWechatSearchProvider,
    canonicalize_url,
    normalize_search_response,
    parse_article_markdown,
    parse_search_markdown,
)
from content_hub.db.connection import connect
from content_hub.services.wechat_refresh import FakeWechatRefreshProvider


def _insert_keyword(settings, keyword_id: str, text: str) -> None:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO keywords(
                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (keyword_id, "wechat-search", text, "active", "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z", "{}"),
        )


def _enable_refresh_switch(settings) -> None:
    with connect(settings) as con:
        for index, contract in enumerate(("refresh", "refresh-status")):
            con.execute(
                """INSERT OR REPLACE INTO migration_switches(
                    switch_id,module_key,contract_key,data_mode,updated_at,updated_by
                ) VALUES(?,?,?,?,?,?)""",
                (
                    f"sw_test_refresh_api_{index}",
                    "wechat-search",
                    contract,
                    "hub",
                    "2026-07-16T00:00:00Z",
                    "test",
                ),
            )


def test_converter_canonicalizes_and_deduplicates_urls():
    assert canonicalize_url("HTTPS://Example.COM/a#fragment") == "https://example.com/a"
    result = normalize_search_response(
        {
            "status": "completed",
            "total": 3,
            "results": [
                {"rank": 1, "title": "A", "url": "https://Example.COM/a#x"},
                {"rank": 2, "title": "A duplicate", "url": "https://example.com/a"},
                {"rank": 3, "title": "invalid", "url": "javascript:bad"},
            ],
        },
        keyword="测试",
        request_id="remote-1",
        source_ref="remote:test",
    )
    assert result["result_count"] == 3
    assert result["markdown_count"] == 0
    assert result["invalid_hit_count"] == 1
    assert [item["url_raw"] for item in result["hits"]] == ["https://example.com/a"]
    assert result["hits"][0]["content_id"].startswith("wechat_article_")


def test_full_markdown_parser_extracts_rank_url_creator_and_article_body():
    markdown = """# 关键词

#### 文章列表
01. 第一篇
    文章简介：摘要
    公众号：公众号甲
    时间：2026-07-16 10:00
    https://mp.weixin.qq.com/s/one
02. 第二篇
    公众号：公众号乙
    https://mp.weixin.qq.com/s/two

#### 文章内容
##### 01. 第一篇
链接：https://mp.weixin.qq.com/s/one
StartFragment
# 第一篇

正文一
EndFragment
##### 02. 第二篇
链接：https://mp.weixin.qq.com/s/two
StartFragment
# 第二篇

正文二
EndFragment
"""
    hits = parse_search_markdown(markdown)
    assert [(item["rank"], item["title_raw"], item["url_raw"], item["creator_name_raw"]) for item in hits] == [
        (1, "第一篇", "https://mp.weixin.qq.com/s/one", "公众号甲"),
        (2, "第二篇", "https://mp.weixin.qq.com/s/two", "公众号乙"),
    ]
    assert parse_article_markdown(markdown) == {
        "https://mp.weixin.qq.com/s/one": "# 第一篇\n\n正文一",
        "https://mp.weixin.qq.com/s/two": "# 第二篇\n\n正文二",
    }
    result = normalize_search_response(
        {"status": "completed", "request_id": "r", "result": {"data": [{"article_count": 2}], "markdown": markdown}},
        keyword="关键词",
        request_id="r",
        source_ref="remote:test:r",
    )
    assert len(result["hits"]) == 2
    assert result["hits"][0]["markdown_body"].startswith("# 第一篇")
    assert result["raw_payload"].get("markdown") is None


def test_converter_reads_nested_full_markdown_and_normalizes_metadata():
    markdown = """#### 文章列表
01. 嵌套文章
    公众号：嵌套公众号
    发布时间：2026-07-16 12:34
    https://mp.weixin.qq.com/s/nested#fragment

#### 文章内容
##### 01. 嵌套文章
链接：https://mp.weixin.qq.com/s/nested
StartFragment
正文
EndFragment
"""
    result = normalize_search_response(
        {
            "status": "completed",
            "result": {
                "data": [{"article_count": 1, "markdown": markdown}],
            },
        },
        keyword="嵌套",
        request_id="nested-1",
        source_ref="remote:test:nested",
    )
    assert result["hits"] == [
        {
            "rank": 1,
            "title_raw": "嵌套文章",
            "url_raw": "https://mp.weixin.qq.com/s/nested",
            "creator_name_raw": "嵌套公众号",
            "published_at": "2026-07-16 12:34",
            "summary_raw": None,
            "canonical_url": "https://mp.weixin.qq.com/s/nested",
            "content_id": result["hits"][0]["content_id"],
            "markdown_body": "正文",
        }
    ]
    assert result["markdown_count"] == 0


@dataclass
class _Response:
    value: dict
    status: int = 200

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_remote_provider_uses_legacy_synchronous_workload_and_waits_for_response():
    calls = []
    responses = iter(
        [
            _Response(
                {
                    "status": "success",
                    "request_id": "r-1",
                    "completed_at": "2026-07-16T01:00:00Z",
                    "results": [{"title": "A", "url": "https://example.com/a"}],
                }
            ),
        ]
    )

    def opener(request, timeout):
        calls.append((request.full_url, request.method, json.loads(request.data) if request.data else None, timeout))
        return next(responses)

    provider = RemoteWechatSearchProvider(
        "http://192.168.31.238:8000",
        timeout_seconds=7,
        poll_interval_seconds=0,
        max_wait_seconds=1,
        opener=opener,
        sleep=lambda _: None,
    )
    result = provider.fetch(keyword_id="kw", keyword="测试")
    assert result["source_ref"].endswith(":r-1")
    assert result["markdown_count"] == 0
    assert len(result["hits"]) == 1
    assert calls[0][0].endswith("/search")
    assert calls[0][1] == "POST"
    assert "async_mode" not in calls[0][2]
    assert calls[0][2]["fetch_depth"] == 1
    assert calls[0][2]["fetch_max_count"] == 5
    assert calls[0][3] == 1
    assert len(calls) == 1


def test_remote_provider_polls_same_request_when_server_returns_queue_receipt():
    calls = []
    responses = iter(
        [
            _Response({"status": "queued", "request_id": "r-queued"}),
            _Response({"status": "processing", "request_id": "r-queued"}),
            _Response(
                {
                    "status": "completed",
                    "request_id": "r-queued",
                    "results": [{"title": "A", "url": "https://example.com/a"}],
                }
            ),
        ]
    )

    def opener(request, timeout):
        calls.append(request.full_url)
        return next(responses)

    provider = RemoteWechatSearchProvider(
        "http://192.168.31.238:8000",
        poll_interval_seconds=0,
        max_wait_seconds=1,
        opener=opener,
        sleep=lambda _: None,
    )
    assert provider.fetch(keyword_id="kw", keyword="测试")["remote_request_id"] == "r-queued"
    assert calls == [
        "http://192.168.31.238:8000/search",
        "http://192.168.31.238:8000/search/result/r-queued",
        "http://192.168.31.238:8000/search/result/r-queued",
    ]


def test_refresh_api_shared_url_reuses_content_id_and_persists_history(settings):
    _insert_keyword(settings, "kw_one", "词一")
    _insert_keyword(settings, "kw_two", "词二")
    _enable_refresh_switch(settings)
    provider = FakeWechatRefreshProvider(
        {
            "kw_one": {
                "captured_at": "2026-07-16T02:00:00Z",
                "result_count": 1,
                "hits": [{"rank": 1, "title_raw": "共同文章", "url_raw": "https://example.com/shared#x"}],
                "markdown_count": 0,
                "source_ref": "remote:test:one",
            },
            "kw_two": {
                "captured_at": "2026-07-16T02:01:00Z",
                "result_count": 1,
                "hits": [{"rank": 1, "title_raw": "共同文章", "url_raw": "https://example.com/shared"}],
                "markdown_count": 0,
                "source_ref": "remote:test:two",
            },
        }
    )

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = provider
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post(
                    "/api/keywords/kw_one/refresh",
                    json={"confirm": True, "keyword": "词一", "idempotency_key": "provider-api-one"},
                )
                second = await client.post(
                    "/api/keywords/kw_two/refresh",
                    json={"confirm": True, "keyword": "词二", "idempotency_key": "provider-api-two"},
                )
                assert first.status_code == 200
                assert second.status_code == 200
                first_body = first.json()
                second_body = second.json()
                first_data = first_body.get("data", first_body)
                second_data = second_body.get("data", second_body)
                first_status = (await client.get(f"/api/refresh-status/{first_data['job_id']}")).json()
                second_status = (await client.get(f"/api/refresh-status/{second_data['job_id']}")).json()
                assert first_status["status"] == "completed"
                assert second_status["status"] == "completed"

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            """SELECT h.content_id
               FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
               WHERE s.keyword_id IN ('kw_one','kw_two') ORDER BY s.keyword_id"""
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["content_id"] == rows[1]["content_id"]
        assert con.execute("SELECT COUNT(*) FROM contents WHERE canonical_url='https://example.com/shared'").fetchone()[0] == 1
        snapshots = con.execute(
            "SELECT payload_json FROM search_snapshots WHERE keyword_id IN ('kw_one','kw_two')"
        ).fetchall()
        assert all(json.loads(row["payload_json"])["markdown_count"] == 0 for row in snapshots)
        assert con.execute("SELECT COUNT(*) FROM search_refresh_jobs WHERE status='succeeded'").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat.refresh' AND outcome='succeeded'").fetchone()[0] == 2


def test_refresh_persists_unique_article_markdown_and_never_search_markdown(settings):
    _insert_keyword(settings, "kw_full", "全文关键词")
    _enable_refresh_switch(settings)
    body = "# 正文标题\n\n正文内容"
    provider = FakeWechatRefreshProvider({
        "kw_full": {
            "captured_at": "2026-07-16T03:00:00Z",
            "result_count": 2,
            "hits": [
                {
                    "rank": 1,
                    "title_raw": "正文标题",
                    "url_raw": "https://mp.weixin.qq.com/s/full-one",
                    "creator_name_raw": "公众号甲",
                    "markdown_body": body,
                },
                {
                    "rank": 2,
                    "title_raw": "正文标题重复",
                    "url_raw": "https://mp.weixin.qq.com/s/full-one#fragment",
                    "creator_name_raw": "公众号甲",
                    "markdown_body": body,
                },
            ],
            "source_ref": "remote:full:test",
            "markdown_count": 0,
        }
    })

    async def run():
        app = create_app(settings)
        app.state.wechat_refresh_provider = provider
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/keywords/kw_full/refresh",
                    json={"confirm": True, "keyword": "全文关键词", "idempotency_key": "full-md-1"},
                )
                assert response.status_code == 200
                assert (await client.get(f"/api/refresh-status/{response.json()['job_id']}")).json()["status"] == "completed"

    asyncio.run(run())
    with connect(settings, readonly=True) as con:
        content = con.execute("SELECT content_id,md_path,content_hash FROM contents WHERE canonical_url=?", ("https://mp.weixin.qq.com/s/full-one",)).fetchone()
        assert content is not None
        assert con.execute("SELECT COUNT(*) FROM contents WHERE canonical_url=?", ("https://mp.weixin.qq.com/s/full-one",)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM content_identifiers WHERE namespace='wechat_url' AND external_id=?", ("https://mp.weixin.qq.com/s/full-one",)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM wechat_article_paths WHERE article_id=?", (content["content_id"],)).fetchone()[0] == 1
        snapshot = con.execute("SELECT payload_json FROM search_snapshots WHERE keyword_id='kw_full'").fetchone()
        stored = json.loads(snapshot["payload_json"])
        assert stored["markdown_count"] == 0
        assert all("markdown_body" not in hit for hit in stored["hits"])
        assert "markdown" not in json.dumps(stored.get("raw_payload", {}), ensure_ascii=False)
    asset = settings.asset_store_path / str(content["md_path"])
    assert asset.exists()
    assert asset.read_text(encoding="utf-8") == body + "\n"
    assert len(list((settings.asset_store_path / "wechat").glob(f"{content['content_hash']}.md"))) == 1
