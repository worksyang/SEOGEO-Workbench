from __future__ import annotations

import asyncio
from contextlib import contextmanager

import httpx

from content_hub import legacy_proxy
from content_hub.app import create_app


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


def test_legacy_proxy_whitelist_and_query(settings, monkeypatch) -> None:
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
                allowed = await client.get(
                    "/api/monitor-data/bootstrap?cache=no",
                    headers={"Accept": "application/json"},
                )
                assert allowed.status_code == 200
                assert allowed.json()["keywords"] == []
                assert seen and "/api/monitor-data/bootstrap?cache=no" in seen[0]

                xhs = await client.get(
                    "/api/monitor-data/bootstrap",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert xhs.status_code == 200
                assert "127.0.0.1:8766" in seen[-1]

                cover = await client.get(
                    "/api/article-cover-image?url=https%3A%2F%2Fsns-na-i11.xhscdn.com%2Fcover",
                    headers={"Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html"},
                )
                assert cover.status_code == 200
                assert "127.0.0.1:8766" in seen[-1]

                blocked = await client.get("/api/not-registered")
                assert blocked.status_code == 404
                assert blocked.json()["error"]["code"] == "LEGACY_ENDPOINT_NOT_ALLOWED"

    asyncio.run(run())


def test_xhs_legacy_write_is_blocked_before_upstream(settings, monkeypatch) -> None:
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
                response = await client.post(
                    "/api/keywords/kw_1/refresh",
                    headers={
                        "Referer": "http://127.0.0.1:8799/legacy/xhs/monitor.html",
                        "Content-Type": "application/json",
                    },
                    json={"keyword": "香港保险"},
                )
                assert response.status_code == 409
                body = response.json()
                assert body["blocked"] is True
                assert body["upstream_called"] is False
                assert body["error"]["code"] == "LEGACY_XHS_WRITE_BLOCKED"
                assert not seen

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
