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
                    headers={"Referer": "http://testserver/legacy/xhs/monitor.html"},
                )
                assert xhs.status_code == 200
                assert "127.0.0.1:8766" in seen[-1]

                cover = await client.get(
                    "/api/article-cover-image?url=https%3A%2F%2Fsns-na-i11.xhscdn.com%2Fcover",
                    headers={"Referer": "http://testserver/legacy/xhs/monitor.html"},
                )
                assert cover.status_code == 200
                assert "127.0.0.1:8766" in seen[-1]

                blocked = await client.get("/api/not-registered")
                assert blocked.status_code == 404
                assert blocked.json()["error"]["code"] == "LEGACY_ENDPOINT_NOT_ALLOWED"

    asyncio.run(run())
