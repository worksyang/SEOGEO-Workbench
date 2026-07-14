from __future__ import annotations
import os as _os_for_paths
_WIKI_DEFAULT_ROOTS = (
    "/Users/works14/Documents/output_md",
    "/Users/works14/Documents/zkcode",
)

import os
from dataclasses import dataclass, replace
from pathlib import Path

_MP_ALLOWED_CATEGORIES = (
    "热门产品", "z产品对比", "z香港vs内地", "港险优惠", "美联储降息", "保司盘点",
    "什么是香港保险", "香港储蓄险", "z非热门产品", "其他", "新加坡保险",
)

def _split_paths(raw: str | None) -> tuple[Path, ...]:
    if not raw:
        return ()
    return tuple(Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    workbench_root: Path
    database_path: Path
    lock_path: Path
    migration_dir: Path
    schema_dir: Path
    frontend_dist: Path
    host: str
    port: int
    log_level: str
    allowed_roots: tuple[Path, ...]
    cors_origins: tuple[str, ...]
    wechat_source_url: str
    wechat_source_root: Path
    wechat_source_timeout_seconds: float
    mp_source_url: str
    mp_source_root: Path
    mp_source_timeout_seconds: float
    mp_categories: tuple[str, ...]
    mp_rejected_csv_path: Path
    mp_metadata_root: Path
    xhs_source_url: str
    xhs_normalized_root: Path
    xhs_settings_db_path: Path
    xhs_source_timeout_seconds: float
    geo_source_root: Path
    geo_database_path: Path
    geo_platforms_path: Path
    geo_redfox_root: Path
    geo_redfox_api_key_configured: bool
    asset_store_path: Path
    wiki_allowed_roots: tuple[Path, ...]
    publish_accounts: tuple[dict, ...] = ()

    @classmethod
    def load(cls, *, host: str | None = None, port: int | None = None) -> "Settings":
        workbench_root = Path(__file__).resolve().parents[2]
        project_root = workbench_root.parent
        database_path = Path(
            os.getenv("HUB_DATABASE_PATH", project_root / "data/hub/content_hub.sqlite")
        ).expanduser().resolve()
        configured_roots = _split_paths(os.getenv("HUB_ALLOWED_ROOTS"))
        default_roots = (
            project_root.resolve(),
            Path("/Users/works14/Documents/output_md").resolve(),
            Path("/Users/works14/Documents/zkcode").resolve(),
            Path("/Users/works14/.claude/监控").resolve(),
        )
        resolved_host = host or os.getenv("HUB_HOST", "127.0.0.1")
        if resolved_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("首版工作台只允许监听本机回环地址。")
        resolved_port = port if port is not None else int(os.getenv("HUB_PORT", "8799"))
        if not 1 <= resolved_port <= 65535:
            raise ValueError("HUB_PORT 必须在 1–65535 之间。")
        cors_origins = _split_csv(os.getenv("HUB_CORS_ORIGINS")) or (
            f"http://127.0.0.1:{resolved_port}",
            f"http://localhost:{resolved_port}",
            "http://127.0.0.1:5174",
            "http://localhost:5174",
        )
        _asset_store_path_local = Path(_os_for_paths.getenv("HUB_ASSET_STORE_PATH", project_root / "asset_store")).resolve()
        _wiki_roots = tuple(Path(p).resolve() for p in _WIKI_DEFAULT_ROOTS if Path(p).exists()) + (_asset_store_path_local,)
        settings = cls(
            project_root=project_root,
            workbench_root=workbench_root,
            database_path=database_path,
            lock_path=database_path.with_suffix(".lock"),
            migration_dir=workbench_root / "backend/content_hub/db/migrations",
            schema_dir=workbench_root / "schemas",
            frontend_dist=workbench_root / "frontend/dist",
            host=resolved_host,
            port=resolved_port,
            log_level=os.getenv("HUB_LOG_LEVEL", "INFO").upper(),
            allowed_roots=configured_roots or default_roots,
            cors_origins=cors_origins,
            wechat_source_url=os.getenv("HUB_WECHAT_SOURCE_URL", "http://127.0.0.1:8765"),
            wechat_source_root=Path(
                os.getenv(
                    "HUB_WECHAT_SOURCE_ROOT",
                    "/Users/works14/.claude/监控/wechat-ybxhyyh-top3",
                )
            ).expanduser().resolve(),
            wechat_source_timeout_seconds=float(
                os.getenv("HUB_WECHAT_SOURCE_TIMEOUT_SECONDS", "3")
            ),
            mp_source_url=os.getenv("HUB_MP_SOURCE_URL", "http://127.0.0.1:28765"),
            mp_source_root=Path(
                os.getenv("HUB_MP_SOURCE_ROOT", "/Users/works14/Documents/output_md")
            ).expanduser().absolute(),
            mp_source_timeout_seconds=float(os.getenv("HUB_MP_SOURCE_TIMEOUT_SECONDS", "5")),
            mp_categories=tuple(sorted(set(_split_csv(os.getenv(
                "HUB_MP_CATEGORIES", ",".join(_MP_ALLOWED_CATEGORIES)
            ))) & set(_MP_ALLOWED_CATEGORIES))),
            mp_rejected_csv_path=Path(os.getenv(
                "HUB_MP_REJECTED_CSV_PATH",
                "/Users/works14/Documents/zkcode/250626_mpGUI/rejected_articles.csv",
            )).expanduser().absolute(),
            mp_metadata_root=Path(os.getenv(
                "HUB_MP_METADATA_ROOT",
                "/Users/works14/Documents/zkcode/250626_mpGUI/output",
            )).expanduser().absolute(),
            xhs_source_url=os.getenv("HUB_XHS_SOURCE_URL", "http://127.0.0.1:8766").rstrip("/"),
            xhs_normalized_root=Path(os.getenv(
                "HUB_XHS_NORMALIZED_ROOT",
                "/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor/normalized",
            )).expanduser().resolve(),
            xhs_settings_db_path=Path(os.getenv(
                "HUB_XHS_SETTINGS_DB_PATH",
                "/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor/data/state/app.db",
            )).expanduser().resolve(),
            xhs_source_timeout_seconds=float(os.getenv("HUB_XHS_SOURCE_TIMEOUT_SECONDS", "5")),
            geo_source_root=Path(os.getenv(
                "HUB_GEO_SOURCE_ROOT", "/Users/works14/Documents/zkcode/GEOProMax"
            )).expanduser().resolve(),
            geo_database_path=Path(os.getenv(
                "HUB_GEO_DATABASE_PATH",
                "/Users/works14/Documents/zkcode/GEOProMax/data/index/geopromax.sqlite",
            )).expanduser().resolve(),
            geo_platforms_path=Path(os.getenv(
                "HUB_GEO_PLATFORMS_PATH",
                "/Users/works14/Documents/zkcode/GEOProMax/data/platforms.json",
            )).expanduser().resolve(),
            geo_redfox_root=Path(os.getenv(
                "HUB_GEO_REDFOX_ROOT",
                "/Users/works14/Documents/zkcode/GEOProMax/data/redfox",
            )).expanduser().resolve(),
            geo_redfox_api_key_configured=bool(os.getenv("HUB_GEO_REDFOX_API_KEY", "").strip()),
            asset_store_path=_asset_store_path_local,
            wiki_allowed_roots=_wiki_roots,

        )
        settings.ensure_directories()
        return settings

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        (self.database_path.parent / "backups").mkdir(parents=True, exist_ok=True)
        (self.database_path.parent / "reports/reconcile").mkdir(parents=True, exist_ok=True)

    def with_database(self, database_path: Path) -> "Settings":
        resolved = database_path.resolve()
        updated = replace(
            self,
            database_path=resolved,
            lock_path=resolved.with_suffix(".lock"),
        )
        updated.ensure_directories()
        return updated
