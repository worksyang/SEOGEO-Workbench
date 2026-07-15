#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

WORKBENCH_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = WORKBENCH_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from content_hub.app import create_app  # noqa: E402
from content_hub.config import Settings  # noqa: E402
from content_hub.db.backup import create_backup  # noqa: E402
from content_hub.db.connection import connect  # noqa: E402
from content_hub.db.migrations import migrate  # noqa: E402
from content_hub.services.signals import SignalsService, load_platform_rules  # noqa: E402
from content_hub.db.writer_lock import writer_lock  # noqa: E402
from content_hub.services.wiki import WikiService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动全域内容工作台")
    parser.add_argument("--host", help="监听地址；默认读取 HUB_HOST")
    parser.add_argument("--port", type=int, help="监听端口；默认读取 HUB_PORT")
    parser.add_argument("--api-only", action="store_true", help="允许前端尚未构建时仅启动 API")
    parser.add_argument("--migrate-only", action="store_true", help="仅执行数据库迁移")
    parser.add_argument("--backup", action="store_true", help="执行一次带完整性校验的在线备份")
    parser.add_argument("--check", action="store_true", help="检查配置、数据库和前端构建")
    parser.add_argument(
        "--backfill-core",
        action="store_true",
        help="受控回填 platforms 并重算 signals；不抓取评论、不创建生产任务",
    )
    parser.add_argument(
        "--wiki-import",
        action="store_true",
        help="扫描已配置 Wiki 允许根；默认仅 dry-run，不写入 Hub",
    )
    parser.add_argument(
        "--confirm-wiki-import",
        action="store_true",
        help="确认执行 Wiki 导入；只允许写入 Hub 与 asset_store 工作副本，不回写原目录",
    )
    parser.add_argument(
        "--wiki-max-files",
        type=int,
        default=2000,
        help="Wiki 导入最多接收的 Markdown 数量，默认 2000",
    )
    return parser.parse_args()


def ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex((host, port)) == 0:
            raise SystemExit(f"端口 {host}:{port} 已被占用，请先关闭占用进程或设置 HUB_PORT。")


def check(settings: Settings) -> dict[str, object]:
    migrate(settings)
    with connect(settings, readonly=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    return {
        "ok": integrity == "ok",
        "database": str(settings.database_path),
        "integrity": integrity,
        "schema_version": schema_version,
        "frontend_built": (settings.frontend_dist / "index.html").is_file(),
        "bind": f"{settings.host}:{settings.port}",
    }


def main() -> None:
    args = parse_args()
    settings = Settings.load(host=args.host, port=args.port)

    if args.migrate_only:
        applied = migrate(settings)
        print(json.dumps({"ok": True, "applied_migrations": applied}, ensure_ascii=False))
        return

    if args.backup:
        migrate(settings)
        backup_path = create_backup(settings)
        print(json.dumps({"ok": True, "backup_path": str(backup_path)}, ensure_ascii=False))
        return

    if args.backfill_core:
        migrate(settings)
        with writer_lock(settings.lock_path):
            with connect(settings, readonly=False) as connection:
                service = SignalsService(
                    connection,
                    platform_rules=load_platform_rules(settings.geo_platforms_path),
                )
                result = {
                    "platforms": service.backfill_platforms(),
                    "signals": service.recompute_all(),
                    "comments_written": 0,
                    "production_jobs_written": 0,
                }
                connection.commit()
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
        return

    if args.wiki_import:
        if args.wiki_max_files < 1:
            raise SystemExit("--wiki-max-files 必须是正整数")
        migrate(settings)
        confirming = bool(args.confirm_wiki_import)
        with connect(settings, readonly=not confirming) as connection:
            service = WikiService(
                connection=connection,
                asset_root=settings.asset_store_path,
                source_roots=settings.wiki_allowed_roots,
                lock_path=settings.lock_path,
            )
            result = service.import_wiki(
                confirm=confirming,
                max_files=args.wiki_max_files,
                operator="cli/wiki-import",
            )
        print(json.dumps({"ok": result.get("status") != "blocked", "data": result}, ensure_ascii=False, indent=2))
        return

    if args.check:
        result = check(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["ok"] else 1)

    if not args.api_only and not (settings.frontend_dist / "index.html").is_file():
        raise SystemExit(
            "前端构建不存在。请先运行 `cd workbench/frontend && npm install && npm run build`，"
            "或使用 `python3 workbench/run.py --api-only` 仅启动 API。"
        )

    ensure_port_available(settings.host, settings.port)
    migrate(settings)

    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
