from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from content_hub import __version__
from content_hub.config import Settings
from content_hub.db.migrations import migrate
from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import AppError
from content_hub.services.migration import MigrationResolver, wechat_http_operation
from content_hub.features.overview.router import router as overview_router
from content_hub.features.system.router import router as system_router
from content_hub.features.wechat.router import router as wechat_router
from content_hub.features.wechat.legacy_aux_router import router as wechat_legacy_aux_router
from content_hub.features.wechat.legacy_read_router import router as wechat_legacy_read_router
from content_hub.features.mp.router import router as mp_router
from content_hub.features.xhs.router import router as xhs_router
from content_hub.features.geo.router import router as geo_router
from content_hub.features.wiki.router import router as wiki_router
from content_hub.features.writing.router import router as writing_router
from content_hub.features.publishing.router import router as publishing_router
from content_hub.features.contents.router import router as contents_router
from content_hub.features.jobs.router import router as jobs_router
from content_hub.features.signals.router import router as signals_router
from content_hub.features.governance.router import router as governance_router
from content_hub.legacy_proxy import (
    legacy_referer_kind,
    proxy_legacy_geo_page,
    proxy_legacy_xhs_page,
    proxy_legacy_static,
    proxy_legacy_wechat_api,
)
from content_hub.legacy_pages import (
    wechat_account_score_analysis,
    wechat_account_score_formula,
    wechat_article_hit_detail,
    wechat_article_detail_demo,
    wechat_article_detail_demo_root,
    wechat_keyword_turnover,
)

from content_hub.logging import configure_logging
from content_hub.services.writing import backfill_writing_runtime

logger = logging.getLogger("content_hub.http")

_WECHAT_ROOT_AUXILIARY_PATHS = frozenset({
    "/keyword-turnover",
    "/article-hit-detail",
    "/article-hit-detail-demo",
    "/account-score-analysis",
    "/account-score-formula",
})
_WECHAT_BUSINESS_ISLAND_CSP = (
    "default-src 'self'; img-src 'self' data: https: http://wx.qlogo.cn; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "connect-src 'self'; frame-ancestors 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.load()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(resolved_settings)
        with writer_lock(resolved_settings.lock_path):
            with connect(resolved_settings, readonly=False) as connection:
                backfilled = backfill_writing_runtime(
                    connection,
                    asset_root=Path(resolved_settings.asset_store_path),
                )
                connection.commit()
        if backfilled:
            logger.info("WritingMoney v3.3 运行层回填完成", extra={"count": backfilled})
        logger.info("全域内容工作台启动")
        yield
        logger.info("全域内容工作台停止")

    app = FastAPI(
        title="全域内容工作台 API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Request-ID",
            "Idempotency-Key",
            "X-Idempotency-Key",
            "X-Actor-ID",
        ],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started = time.perf_counter()
        try:
            referer_kind = legacy_referer_kind(request.headers.get("referer", ""))
            if referer_kind == "xhs" and (
                request.url.path == "/api/article-cover-image"
                or request.url.path == "/api/article-covers"
                or (
                    request.url.path.startswith("/api/keywords/")
                    and request.url.path.endswith("/refresh")
                )
            ):
                legacy_path = request.url.path.removeprefix("/api/")
                response = await proxy_legacy_wechat_api(legacy_path, request)
            else:
                operation = wechat_http_operation(request.method, request.url.path)
                if operation and operation["kind"] == "write":
                    MigrationResolver(
                        resolved_settings,
                        module_key="wechat-search",
                        contract_key=operation["contract_key"],
                    ).require_mode("hub")
                response = await call_next(request)
        except AppError as exc:
            # middleware 自身的迁移写护栏位于 FastAPI exception handler 外层；
            # 在这里保持统一错误契约，同时继续 fail-closed。
            response = JSONResponse(
                status_code=exc.status_code,
                content={
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "request_id": request_id,
                    },
                },
            )
        except Exception:
            logger.exception(
                "请求处理失败",
                extra={"request_id": request_id, "method": request.method, "path": request.url.path},
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        is_root_wechat_auxiliary = request.url.path in _WECHAT_ROOT_AUXILIARY_PATHS
        is_wechat_legacy_html = (
            request.url.path.startswith("/legacy/wechat/")
            and request.url.path.endswith(".html")
        )
        if request.url.path.startswith("/legacy/") or is_root_wechat_auxiliary:
            # 原系统页面含历史 inline handler 与 Chart.js/marked 依赖；
            # 微信旧辅助页是同源 business-island 页面；只对这批显式
            # 白名单放宽，不影响统一工作台主页面和其他根路径。
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = _WECHAT_BUSINESS_ISLAND_CSP
            if is_root_wechat_auxiliary or is_wechat_legacy_html:
                response.headers["Cache-Control"] = (
                    "no-store, no-cache, must-revalidate, max-age=0"
                )
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data: https:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
            )
        logger.info(
            "请求完成",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.headers.get("X-Request-ID"),
                },
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "工作台发生未预期错误，请查看本地日志。",
                    "request_id": request.headers.get("X-Request-ID"),
                },
            },
        )

    app.include_router(system_router)
    app.include_router(overview_router)
    app.include_router(wechat_router)
    # 必须先于 /api/{path:path} catch-all，保持旧微信 GET 响应形状。
    app.include_router(wechat_legacy_read_router)
    # AUX 读/写/外部动作必须先于 catch-all，避免被旧代理吞掉。
    app.include_router(wechat_legacy_aux_router)
    app.include_router(mp_router)
    app.include_router(xhs_router)
    app.include_router(geo_router)
    app.include_router(wiki_router)
    app.include_router(writing_router)
    app.include_router(publishing_router)
    app.include_router(contents_router)
    app.include_router(jobs_router)
    app.include_router(signals_router)
    app.include_router(governance_router)
    # 原微信关键词岛屿使用旧页面的原始 API 契约；先经工作台白名单代理，
    # 后续再按接口逐条切换到 Hub，不改动旧系统本身。
    app.add_api_route(
        "/api/{path:path}",
        proxy_legacy_wechat_api,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/geo/{path:path}",
        proxy_legacy_geo_page,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/xhs/article-hit-detail",
        proxy_legacy_xhs_page,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/xhs/keyword-turnover",
        proxy_legacy_xhs_page,
        methods=["GET"],
        include_in_schema=False,
    )
    # 微信旧监控页生成的是根路径；显式映射到原业务页面，不能落入 SPA。
    app.add_api_route(
        "/keyword-turnover",
        wechat_keyword_turnover,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/article-hit-detail",
        wechat_article_hit_detail,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/article-hit-detail-demo",
        wechat_article_detail_demo_root,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/account-score-analysis",
        wechat_account_score_analysis,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/account-score-formula",
        wechat_account_score_formula,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/wechat/keyword-turnover",
        wechat_keyword_turnover,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/wechat/article-hit-detail",
        wechat_article_hit_detail,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/wechat/article-hit-detail-demo",
        wechat_article_detail_demo,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/wechat/account-score-analysis",
        wechat_account_score_analysis,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/legacy/wechat/account-score-formula",
        wechat_account_score_formula,
        methods=["GET"],
        include_in_schema=False,
    )
    # 公众号旧控制台会返回 /static/logo.svg 这类绝对资源地址；
    # 仅为该业务岛屿登记 logo.svg，不开放任意静态文件代理。
    app.add_api_route(
        "/static/{path:path}",
        proxy_legacy_static,
        methods=["GET"],
        include_in_schema=False,
    )

    _mount_frontend(app, resolved_settings.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index_path = frontend_dist / "index.html"
    assets_path = frontend_dist / "assets"
    legacy_path = frontend_dist / "legacy"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")
    if legacy_path.is_dir():
        app.mount("/legacy", StaticFiles(directory=legacy_path), name="legacy")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith(("api/", "docs", "openapi.json", "redoc")):
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": {"code": "NOT_FOUND", "message": "接口不存在。"}},
            )
        candidate = (frontend_dist / path).resolve()
        if frontend_dist.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        if index_path.is_file():
            return FileResponse(index_path)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": {
                    "code": "FRONTEND_NOT_BUILT",
                    "message": "前端尚未构建，请在 workbench/frontend 运行 npm install && npm run build。",
                },
            },
        )
