from __future__ import annotations

import hashlib
import os
from pathlib import Path

from flask import Flask, Response, request, url_for

from app.config import Config


def _load_parent_env() -> None:
    """从父目录 ../.env 加载环境变量（如 TIKHUB_API_TOKEN），方便 1:1 启动。

    不覆盖已经存在的进程环境变量；只在父目录 `.env` 存在时尝试加载。
    """
    parent_env = Config.PROJECT_ROOT.parent / ".env"
    if not parent_env.exists():
        return
    try:
        for raw in parent_env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def _asset_fingerprint(app: Flask, filename: str) -> str:
    """内容指纹 — 与源项目一致，避免 mtime 不可靠。"""
    path = os.path.join(app.static_folder, filename)
    try:
        with open(path, "rb") as fp:
            digest = hashlib.md5(fp.read()).hexdigest()
    except OSError:
        return "missing"
    return digest[:10]


def create_app() -> Flask:
    _load_parent_env()

    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    from app.web.routes import bp as web_bp
    from app.web.api import bp as api_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    # launchd/机器重启后，若有未主动取消的 TikHub 批次，先恢复 runner/guard；
    # 之后再启动每日调度，避免调度器误以为没有活跃任务而并发开新一批。
    from app.services.refresh_service import recover_orphaned_batches
    recover_orphaned_batches()

    # 初始化 MonitorFastStore（预热 154MB 数据）
    from app.services.monitor_fast_service import init_fast_store
    init_fast_store(
        monitor_data_path=app.config["MONITOR_DATA_FILE"],
        sqlite_path=app.config["SQLITE_PATH"],
        keywords_config_path=app.config["KEYWORDS_CONFIG_FILE"],
    )

    if not app.config.get("DISABLE_SCHEDULER", False):
        from app.services import scheduler_service

        scheduler_service.start(
            base_url=f"http://127.0.0.1:{app.config.get('PORT', 8766)}",
            interval_hours=float(app.config.get("AUTO_REFRESH_INTERVAL_HOURS", 24.0)),
            enabled=bool(app.config.get("AUTO_REFRESH_ENABLED", False)),
        )

    @app.context_processor
    def inject_globals():
        def asset_url(filename: str) -> str:
            base = url_for("static", filename=filename)
            return f"{base}?v={_asset_fingerprint(app, filename)}"

        return {"asset_url": asset_url}

    @app.after_request
    def add_no_cache_headers(response: Response) -> Response:
        # 对高性能端点和带内容指纹的静态资源做精确豁免
        _path = request.path if request else ""
        _is_fast_endpoint = _path.startswith("/api/monitor-data/bootstrap") or                             _path.startswith("/api/monitor-data/keyword/") or                             _path.startswith("/api/monitor-data/account/")
        _is_fingerprinted_static = _path.startswith("/static/") and "?v=" in (request.url if request else "")
        if _is_fast_endpoint or _is_fingerprinted_static:
            return response
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    return app
