from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx

from content_hub.app import create_app
from content_hub.config import Settings


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "source" / "wechat-search-monitor" / "app"
MIRROR = ROOT / "workbench" / "legacy_mirrors" / "wechat"
PUBLIC = ROOT / "workbench" / "frontend" / "public" / "legacy" / "wechat"
DIST = ROOT / "workbench" / "frontend" / "dist" / "legacy" / "wechat"
WECHAT_ISLAND = ROOT / "workbench" / "frontend" / "src" / "features" / "wechat" / "WechatIslandPage.tsx"


def test_wechat_auxiliary_files_are_mirrored() -> None:
    for name in (
        "monitor.html",
        "keyword_turnover.html",
        "article_hit_detail.html",
        "account_score_analysis.html",
        "account_score_formula.html",
    ):
        source = (SOURCE / "templates" / name).read_text(encoding="utf-8")
        mirror = (MIRROR / "source" / "templates" / name).read_text(encoding="utf-8")
        public = (PUBLIC / name).read_text(encoding="utf-8")
        if name in {"monitor.html", "keyword_turnover.html", "article_hit_detail.html"}:
            for asset_name in (
                "css/monitor.css",
                "js/turnover-utils.js",
                "js/keyword-turnover.js",
                "js/article-hit-detail.js",
                "js/monitor.js",
                "js/article-list.js",
            ):
                source = source.replace(
                    f"{{{{ asset_url('{asset_name}') }}}}",
                    f"{{{{ asset_url('{asset_name}') }}}}?wbv=wechat-v1",
                )
            expected_public = source
            for asset_name, public_path in (
                ("css/monitor.css", "/legacy/wechat/static/css/monitor.css"),
                ("js/turnover-utils.js", "/legacy/wechat/static/js/turnover-utils.js"),
                ("js/keyword-turnover.js", "/legacy/wechat/static/js/keyword-turnover.js"),
                ("js/article-hit-detail.js", "/legacy/wechat/static/js/article-hit-detail.js"),
                ("js/monitor.js", "/legacy/wechat/static/js/monitor.js"),
                ("js/article-list.js", "/legacy/wechat/static/js/article-list.js"),
            ):
                expected_public = expected_public.replace(
                    f"{{{{ asset_url('{asset_name}') }}}}?wbv=wechat-v1",
                    f"{public_path}?wbv=wechat-v1",
                )
            expected_mirror = source
            if name in {"keyword_turnover.html", "article_hit_detail.html"}:
                expected_mirror = expected_public
            assert mirror == expected_mirror
            assert public == expected_public
        else:
            assert mirror == source
            assert public == mirror

    for name in ("keyword-turnover.js", "article-hit-detail.js", "article-list-demo.js"):
        mirror = (MIRROR / "source" / "static" / "js" / name).read_bytes()
        public = (PUBLIC / "static" / "js" / name).read_bytes()
        assert public == mirror
        if name == "article-list-demo.js":
            assert mirror == (SOURCE / "static" / "js" / name).read_bytes()


def test_wechat_aux_navigation_uses_stable_version_and_keeps_business_params() -> None:
    island = WECHAT_ISLAND.read_text(encoding="utf-8")
    assert 'src="/legacy/wechat/monitor.html?wbv=wechat-v1"' in island
    dist_bundles = list((ROOT / "workbench" / "frontend" / "dist" / "assets").glob("*.js"))
    assert any("/legacy/wechat/monitor.html?wbv=wechat-v1" in bundle.read_text(encoding="utf-8") for bundle in dist_bundles)

    monitor_html = (PUBLIC / "monitor.html").read_text(encoding="utf-8")
    for asset in (
        "/legacy/wechat/static/css/monitor.css?wbv=wechat-v1",
        "/legacy/wechat/static/js/turnover-utils.js?wbv=wechat-v1",
        "/legacy/wechat/static/js/monitor.js?wbv=wechat-v1",
        "/legacy/wechat/static/js/article-list.js?wbv=wechat-v1",
    ):
        assert asset in monitor_html

    monitor = (PUBLIC / "static" / "js" / "monitor.js").read_text(encoding="utf-8")
    turnover = (PUBLIC / "static" / "js" / "keyword-turnover.js").read_text(encoding="utf-8")
    detail = (PUBLIC / "static" / "js" / "article-hit-detail.js").read_text(encoding="utf-8")
    assert (DIST / "static" / "js" / "monitor.js").read_bytes() == (PUBLIC / "static" / "js" / "monitor.js").read_bytes()
    assert (DIST / "static" / "js" / "keyword-turnover.js").read_bytes() == (PUBLIC / "static" / "js" / "keyword-turnover.js").read_bytes()
    assert (DIST / "static" / "js" / "article-hit-detail.js").read_bytes() == (PUBLIC / "static" / "js" / "article-hit-detail.js").read_bytes()

    assert "const WECHAT_AUX_VERSION = 'wechat-v1';" in monitor
    assert "params.set('keyword_id', k.keyword_id)" in monitor
    assert "params.set('keyword', k.keyword || '')" in monitor
    assert "params.set('wbv', WECHAT_AUX_VERSION)" in monitor
    assert "function wechatAuxUrl(path, params = {})" in turnover
    assert "function wechatAuxUrl(path, params = {})" in detail
    for script in (monitor, turnover, detail):
        assert "query.set('wbv', WECHAT_AUX_VERSION)" in script
        assert "wbv=Date.now" not in script
        assert "wbv=${Date.now" not in script
        assert "/article-hit-detail?" not in script

    # These two pages are standalone explanations and currently contain no
    # navigation generators; the route/header tests below cover their roots.
    for name in ("account_score_analysis.html", "account_score_formula.html"):
        html = (PUBLIC / name).read_text(encoding="utf-8")
        assert 'href="/account-score-analysis' not in html
        assert 'href="/account-score-formula' not in html
        assert "window.location" not in html
        assert "window.open" not in html


def test_wechat_auxiliary_routes_and_root_mappings(settings) -> None:
    browser_settings = replace(settings, frontend_dist=Settings.load().frontend_dist)

    async def scenario() -> None:
        app = create_app(browser_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                for path, marker in (
                    ("/legacy/wechat/keyword-turnover?keyword=AI", "上榜文章换新热力图"),
                    ("/legacy/wechat/article-hit-detail?article_id=art_1", "文章命中详情"),
                    ("/keyword-turnover?keyword=AI", "上榜文章换新热力图"),
                    ("/article-hit-detail?article_id=art_1", "文章命中详情"),
                ):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert marker in response.text
                    assert "全域内容工作台" not in response.text

                business_island_csp = (
                    "default-src 'self'; img-src 'self' data: https: http://wx.qlogo.cn; "
                    "style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'self'"
                )
                for path in (
                    "/legacy/wechat/monitor.html?wbv=wechat-v1",
                    "/legacy/wechat/keyword_turnover.html?wbv=wechat-v1",
                    "/legacy/wechat/article_hit_detail.html?wbv=wechat-v1",
                    "/legacy/wechat/account_score_analysis.html?wbv=wechat-v1",
                    "/legacy/wechat/account_score_formula.html?wbv=wechat-v1",
                ):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert response.headers["cache-control"] == (
                        "no-store, no-cache, must-revalidate, max-age=0"
                    )
                    assert response.headers["pragma"] == "no-cache"
                    assert response.headers["expires"] == "0"

                for path in (
                    "/keyword-turnover?keyword=AI",
                    "/article-hit-detail?article_id=art_1",
                    "/article-hit-detail-demo?from=monitor",
                    "/account-score-analysis?window_days=15",
                    "/account-score-formula?window_days=15",
                ):
                    response = await client.get(path, follow_redirects=False)
                    assert response.headers["x-frame-options"] == "SAMEORIGIN"
                    assert response.headers["content-security-policy"] == business_island_csp
                    assert response.headers["cache-control"] == (
                        "no-store, no-cache, must-revalidate, max-age=0"
                    )
                    assert response.headers["pragma"] == "no-cache"
                    assert response.headers["expires"] == "0"
                assert "http://wx.qlogo.cn" in business_island_csp
                assert "http:" not in business_island_csp.replace(
                    "http://wx.qlogo.cn", ""
                )
                assert " *" not in business_island_csp

                ordinary = await client.get("/")
                assert ordinary.headers["x-frame-options"] == "DENY"
                assert ordinary.headers["content-security-policy"] != business_island_csp
                assert "cache-control" not in ordinary.headers

                demo = await client.get(
                    "/legacy/wechat/article-hit-detail-demo",
                    follow_redirects=False,
                )
                assert demo.status_code == 307
                assert demo.headers["location"] == (
                    "/legacy/wechat/article-hit-detail?article_id=art_749d447ea394&wbv=wechat-v1"
                )

                root_demo = await client.get(
                    "/article-hit-detail-demo?from=monitor",
                    follow_redirects=False,
                )
                assert root_demo.status_code == 307
                assert root_demo.headers["location"] == (
                    "/article-hit-detail?article_id=art_749d447ea394&wbv=wechat-v1"
                )

                static = await client.get("/legacy/wechat/static/js/article-list-demo.js")
                assert static.status_code == 200
                assert "文章List Demo" in static.text

                unknown = await client.get("/legacy/wechat/not-a-page")
                assert unknown.status_code == 404
                assert "index.html" not in unknown.text

    asyncio.run(scenario())


def test_account_score_pages_are_local_and_do_not_use_old_proxy(settings) -> None:
    async def scenario() -> None:
        app = create_app(replace(settings, frontend_dist=Settings.load().frontend_dist))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                for prefix, page in (
                    ("/legacy/wechat", "account-score-analysis"),
                    ("/legacy/wechat", "account-score-formula"),
                    ("", "account-score-analysis"),
                    ("", "account-score-formula"),
                ):
                    response = await client.get(f"{prefix}/{page}?window_days=15")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/html")
                    assert "<!DOCTYPE html>" in response.text
                    assert "账号" in response.text

    asyncio.run(scenario())
