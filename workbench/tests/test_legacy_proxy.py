from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx

from content_hub import legacy_proxy
from content_hub.app import create_app
from content_hub.db.connection import connect


class _Headers:
    def get_content_type(self) -> str:
        return "application/json"


class _Response:
    status = 200
    headers = _Headers()

    def read(self) -> bytes:
        return b'{"keywords":[]}'

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _SensitiveResponse(_Response):
    def read(self) -> bytes:
        return b'{"username":"demo","password":"dont-return","nested":{"token":"secret"}}'


def test_legacy_proxy_whitelist_and_query(settings, monkeypatch, tmp_path: Path) -> None:
    seen: list[str] = []
    settings = replace(
        settings,
        project_root=tmp_path,
        xhs_normalized_root=tmp_path / "normalized",
    )

    @contextmanager
    def fake_urlopen(request, timeout):
        seen.append(f"{request.full_url}|{request.method}|{timeout}")
        yield _Response()

    monkeypatch.setattr(legacy_proxy.urllib.request, "urlopen", fake_urlopen)

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                allowed = await client.get(
                    "/api/monitor-data/bootstrap?cache=no",
                    headers={"Accept": "application/json"},
                )
                assert allowed.status_code == 200
                assert allowed.json()["keywords"] == []
                assert seen and "/api/monitor-data/bootstrap?cache=no" in seen[0]

                with connect(settings) as con:
                    con.execute(
                        """INSERT INTO keywords(
                            keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            "xhs_keyword_1",
                            "xiaohongshu",
                            "香港保险",
                            "active",
                            "2026-07-16T00:00:00Z",
                            "2026-07-16T00:00:00Z",
                            '{"source_keyword_id":"kw_1"}',
                        ),
                    )
                    con.execute(
                        """INSERT INTO creators(
                            creator_id,platform,external_id,canonical_name,first_seen_at,updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            "xhs_creator_1",
                            "xiaohongshu",
                            "account_1",
                            "测试博主",
                            "2026-07-16T00:00:00Z",
                            "2026-07-16T00:00:00Z",
                            "{}",
                        ),
                    )
                xhs = await client.get(
                    "/api/monitor-data/bootstrap",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert xhs.status_code == 200
                assert xhs.json()["counts"]["keywords"] == 1
                assert xhs.json()["accounts"][0]["name"] == "测试博主"
                assert not seen or "127.0.0.1:8766" not in seen[-1]
                summary = await client.get(
                    "/api/monitor-data/bootstrap?summary=true",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert summary.status_code == 200
                assert summary.json()["counts"]["keywords"] == 1
                assert "keywords" not in summary.json()
                assert summary.json()["source_status"]["source"] == "hub_db"

                for legacy_path in (
                    "/api/monitor-data/keyword/kw_1",
                    "/api/monitor-data/account/account_1",
                    "/api/keyword-manage",
                ):
                    routed = await client.get(
                        legacy_path,
                        headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                    )
                    assert routed.status_code == 200
                    assert "127.0.0.1:8766" not in seen[-1]

                cover = await client.get(
                    "/api/article-cover-image?url=https%3A%2F%2Fsns-na-i11.xhscdn.com%2Fcover",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert cover.status_code == 200
                assert "https://sns-na-i11.xhscdn.com/cover" in seen[-1]

                blocked = await client.get("/api/not-registered")
                assert blocked.status_code == 404
                assert blocked.json()["error"]["code"] == "LEGACY_ENDPOINT_NOT_ALLOWED"

    asyncio.run(run())


def test_xhs_legacy_writes_are_frozen_without_upstream_or_hub_task(settings, monkeypatch) -> None:
    seen: list[str] = []

    @contextmanager
    def fake_urlopen(request, timeout):
        seen.append(f"{request.full_url}|{request.method}|{timeout}")
        yield _Response()

    monkeypatch.setattr(legacy_proxy.urllib.request, "urlopen", fake_urlopen)

    async def run() -> None:
        with connect(settings) as con:
            con.execute(
                """INSERT INTO keywords(
                    keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "xhs_keyword_1",
                    "xiaohongshu",
                    "香港保险",
                    "active",
                    "2026-07-16T00:00:00Z",
                    "2026-07-16T00:00:00Z",
                    '{"source_keyword_id":"kw_1"}',
                ),
            )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                headers = {
                    "Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html",
                    "Content-Type": "application/json",
                }
                for path, payload in (
                    ("/api/keywords/kw_1/refresh", {"keyword": "香港保险"}),
                    ("/api/keywords/kw_1/pin", {}),
                    ("/api/keywords/kw_1/note", {"note": "迁移验收"}),
                    ("/api/refresh-all", {"keyword_ids": ["kw_1"]}),
                    ("/api/refresh-all/cancel", {"batch_id": "missing"}),
                    ("/api/refresh-all/resume", {"batch_id": "missing"}),
                    ("/api/accounts", {}),
                ):
                    blocked = await client.post(path, headers=headers, json=payload)
                    assert blocked.status_code == 409
                    body = blocked.json()
                    assert body["blocked"] is True
                    assert body["upstream_called"] is False
                    assert body["freeze_state"] == "all_frozen"
                    assert body["error"]["code"] == "XHS_MIGRATION_FROZEN"
                assert not seen

                covers = await client.post(
                    "/api/article-covers",
                    headers=headers,
                    json={"articles": [{"article_id": "missing"}]},
                )
                assert covers.status_code == 200
                assert covers.json() == {
                    "items": [{"article_id": "missing", "cover_url": None, "status": "not_found"}],
                    "source": "hub_db",
                }
                assert not seen

                with connect(settings, readonly=True) as con:
                    assert con.execute(
                        "SELECT count(*) FROM search_snapshots WHERE platform='xiaohongshu'"
                    ).fetchone()[0] == 0
                    note_row = con.execute(
                        "SELECT note FROM search_keyword_settings WHERE keyword_id='xhs_keyword_1'"
                    ).fetchone()
                    assert note_row is None or note_row["note"] in (None, "")
                    pinned_row = con.execute(
                        "SELECT pinned FROM search_keyword_settings WHERE keyword_id='xhs_keyword_1'"
                    ).fetchone()
                    assert pinned_row is None or pinned_row["pinned"] in (None, 0)
                    assert con.execute(
                        "SELECT count(*) FROM search_refresh_jobs WHERE system_key='xhs-search'"
                    ).fetchone()[0] == 0

                unsupported_read = await client.get(
                    "/api/article-content?path=legacy.md",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert unsupported_read.status_code == 409
                assert unsupported_read.json()["error"]["code"] == "LEGACY_XHS_READ_BLOCKED"
                assert not seen

    asyncio.run(run())


def test_xhs_legacy_batch_refresh_and_recovery_are_frozen(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        legacy_proxy.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("小红书冻结期间不应访问旧系统")
        ),
    )

    with connect(settings) as con:
        for suffix, source_id, text in (
            ("1", "kw_1", "香港保险"),
            ("2", "kw_2", "澳门保险"),
        ):
            con.execute(
                """INSERT INTO keywords(
                    keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    f"xhs_keyword_{suffix}",
                    "xiaohongshu",
                    text,
                    "active",
                    "2026-07-16T00:00:00Z",
                    "2026-07-16T00:00:00Z",
                    f'{{"source_keyword_id":"{source_id}"}}',
                ),
            )

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                headers = {"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"}
                for path, payload in (
                    ("/api/refresh-all", {"keyword_ids": ["kw_1", "kw_2"]}),
                    ("/api/refresh-all/cancel", {"batch_id": "missing"}),
                    ("/api/refresh-all/resume", {"batch_id": "missing"}),
                ):
                    response = await client.post(path, headers=headers, json=payload)
                    assert response.status_code == 409
                    assert response.json()["error"]["code"] == "XHS_MIGRATION_FROZEN"

                status = await client.get("/api/refresh-all/status", headers=headers)
                assert status.status_code == 200
                assert status.json()["status"] == "idle"

                history = await client.get("/api/refresh-all/history", headers=headers)
                assert history.status_code == 200
                assert history.json() == []

    asyncio.run(run())


def test_mp_legacy_proxy_uses_mp_upstream_and_limits_static_files(settings, monkeypatch) -> None:
    seen: list[str] = []

    @contextmanager
    def fake_urlopen(request, timeout):
        seen.append(f"{request.full_url}|{request.method}|{timeout}")
        yield _Response()

    monkeypatch.setattr(legacy_proxy.urllib.request, "urlopen", fake_urlopen)

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accounts = await client.get(
                    "/api/accounts",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/mp/index.html"},
                )
                assert accounts.status_code == 200
                assert f"{settings.mp_source_url}/api/accounts" in seen[-1]

                logo = await client.get(
                    "/static/logo.svg",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/mp/index.html"},
                )
                assert logo.status_code == 200
                assert logo.headers["content-type"].startswith("image/svg+xml")

                blocked = await client.get(
                    "/static/private.txt",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/mp/index.html"},
                )
                assert blocked.status_code == 404
                assert blocked.json()["error"]["code"] == "LEGACY_STATIC_NOT_ALLOWED"

    asyncio.run(run())


def test_mp_settings_response_redacts_credentials(settings, monkeypatch) -> None:
    @contextmanager
    def fake_urlopen(request, timeout):
        yield _SensitiveResponse()

    monkeypatch.setattr(legacy_proxy.urllib.request, "urlopen", fake_urlopen)

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get(
                    "/api/settings",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/mp/index.html"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["password"] == "[REDACTED]"
                assert body["nested"]["token"] == "[REDACTED]"

    asyncio.run(run())


def test_geo_legacy_page_and_api_use_geo_upstream(settings, monkeypatch) -> None:
    seen: list[str] = []

    @contextmanager
    def fake_urlopen(request, timeout):
        seen.append(f"{request.full_url}|{request.method}|{timeout}")
        yield _Response()

    monkeypatch.setattr(legacy_proxy.urllib.request, "urlopen", fake_urlopen)

    async def run() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                page = await client.get("/legacy/geo/index.html")
                assert page.status_code == 200
                assert f"{settings.geo_source_url}/" in seen[-1]

                data = await client.get(
                    "/api/data",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/geo/index.html"},
                )
                assert data.status_code == 200
                assert f"{settings.geo_source_url}/api/data" in seen[-1]

                blocked = await client.get("/legacy/geo/other.html")
                assert blocked.status_code == 404
                assert blocked.json()["error"]["code"] == "LEGACY_GEO_PAGE_NOT_ALLOWED"

    asyncio.run(run())


def test_xhs_auxiliary_pages_are_mirrored_without_root_navigation(settings) -> None:
    """小红书原版从榜单跳转的两个无扩展名页面必须留在业务岛屿内。"""
    from dataclasses import replace
    from content_hub.config import Settings

    browser_settings = replace(settings, frontend_dist=Settings.load().frontend_dist)

    async def scenario() -> None:
        app = create_app(browser_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                for path, marker in (
                    ("/legacy/xhs/monitor.html", "小红书关键词监控系统"),
                    ("/legacy/xhs/article-hit-detail", "文章命中详情"),
                    ("/legacy/xhs/keyword-turnover", "上榜文章换新热力图"),
                ):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert marker in response.text

                static = await client.get("/legacy/xhs/static/js/article-hit-detail.js")
                assert static.status_code == 200
                assert "DETAIL_API_URL" in static.text

    asyncio.run(scenario())
