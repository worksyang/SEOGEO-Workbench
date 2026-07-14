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
from content_hub.errors import AppError
from content_hub.features.overview.router import router as overview_router
from content_hub.features.system.router import router as system_router
from content_hub.features.wechat.router import router as wechat_router
from content_hub.features.mp.router import router as mp_router
from content_hub.features.xhs.router import router as xhs_router
from content_hub.features.geo.router import router as geo_router
from content_hub.logging import configure_logging

logger = logging.getLogger("content_hub.http")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.load()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(resolved_settings)
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
        allow_headers=["Content-Type", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "请求处理失败",
                extra={"request_id": request_id, "method": request.method, "path": request.url.path},
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
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
    app.include_router(mp_router)
    app.include_router(xhs_router)
    app.include_router(geo_router)
    _mount_frontend(app, resolved_settings.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index_path = frontend_dist / "index.html"
    assets_path = frontend_dist / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

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
