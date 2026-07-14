from __future__ import annotations

import hashlib
import os

from flask import Flask, Response, request, url_for

from app.config import Config


def _asset_fingerprint(app: Flask, filename: str) -> str:
    """Return a short fingerprint derived from file *content*.

    Content hash is the only correct cache-busting signal:
    - file content changes -> hash changes -> URL changes -> browser must re-fetch
    - file content unchanged -> hash unchanged -> URL unchanged -> browser may reuse
    mtime is unreliable: touching a file or copying it changes mtime without changing
    content, and conversely a file's content can change without mtime moving.
    """
    path = os.path.join(app.static_folder, filename)
    try:
        with open(path, "rb") as fp:
            digest = hashlib.md5(fp.read()).hexdigest()
    except OSError:
        return "missing"
    return digest[:10]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    from app.repositories.keyword_registry_repo import KeywordRegistryRepository
    keyword_registry = KeywordRegistryRepository(app.config["SQLITE_PATH"])
    if keyword_registry.is_empty():
        raise RuntimeError(
            "keyword registry is empty; run scripts/migrate_keyword_registry.py first"
        )
    # 只做幂等建表/补列，不会在 Flask 启动时发起搜索。
    from app.repositories.keyword_discovery_repo import KeywordDiscoveryRepository
    KeywordDiscoveryRepository(app.config["SQLITE_PATH"])

    from app.web.routes import bp as web_bp
    from app.web.api import bp as api_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    from app.services.monitor_fast_service import init_fast_store
    init_fast_store(
        monitor_data_path=app.config["MONITOR_DATA_FILE"],
        sqlite_path=app.config["SQLITE_PATH"],
        keyword_read_deltas_path=app.config["KEYWORD_READ_DELTAS_FILE"],
        article_metric_meta_path=app.config["ARTICLE_METRIC_META_FILE"],
    )

    if not app.config.get("DISABLE_SCHEDULER", False):
        from app.services import scheduler_service
        scheduler_service.start(
            base_url=f"http://127.0.0.1:{app.config.get('PORT', 8765)}",
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
        path = request.path
        is_monitor_data = (
            path == "/api/monitor-data"
            or path == "/api/monitor-data/bootstrap"
            or path.startswith("/api/monitor-data/keyword/")
            or path.startswith("/api/monitor-data/account/")
        )
        is_fingerprinted_static = (
            path.startswith("/static/")
            and bool(request.args.get("v"))
        )
        if is_monitor_data or is_fingerprinted_static:
            return response
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    return app
