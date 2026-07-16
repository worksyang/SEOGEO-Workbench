from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from content_hub.app import create_app
from content_hub.legacy_proxy import legacy_referer_kind


def test_legacy_referer_requires_same_origin_8799_and_explicit_legacy_path() -> None:
    assert legacy_referer_kind("http://127.0.0.1:8799/legacy/xhs/monitor.html") == "xhs"
    assert legacy_referer_kind("https://localhost:8799/legacy/mp/index.html") == "mp"
    assert legacy_referer_kind("https://evil.example/legacy/xhs/monitor.html") is None
    assert legacy_referer_kind("http://127.0.0.1:8774/legacy/xhs/monitor.html") is None
    assert legacy_referer_kind("http://127.0.0.1:8799/not-legacy/xhs/monitor.html") is None


def test_unknown_wechat_api_is_fail_closed_without_upstream(settings, monkeypatch) -> None:
    calls: list[tuple] = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unknown API must not call any upstream")

    monkeypatch.setattr("content_hub.legacy_proxy.urllib.request.urlopen", fail)

    async def scenario() -> None:
        app = create_app(replace(settings, wechat_source_url="http://127.0.0.1:8774"))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8799"
            ) as client:
                for method in ("get", "post"):
                    response = await getattr(client, method)(
                        "/api/foo-bar",
                        headers={"Referer": "https://evil.example/legacy/xhs/monitor.html"},
                    )
                    assert response.status_code == 404
                    assert response.json()["error"]["code"] == "LEGACY_ENDPOINT_NOT_ALLOWED"

    asyncio.run(scenario())
    assert calls == []


def test_account_score_pages_are_rendered_without_jinja_markers(settings) -> None:
    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8799"
            ) as client:
                for path, title, marker in (
                    ("/account-score-analysis?window_days=15", "账号研究价值判断稿", "必加"),
                    ("/account-score-formula?window_days=15", "账号分公式说明", "公式总览"),
                ):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert title in response.text
                    assert "{{" not in response.text
                    assert "{%" not in response.text
                    assert marker in response.text

    asyncio.run(scenario())
