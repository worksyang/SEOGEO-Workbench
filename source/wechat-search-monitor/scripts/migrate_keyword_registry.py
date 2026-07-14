#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_registry_repo import KeywordRegistryRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="合并关键词配置、历史索引和人工状态")
    parser.add_argument("--db", default=str(ROOT / "data/state/app.db"))
    parser.add_argument("--config", required=True, help="迁移前关键词配置备份")
    parser.add_argument("--normalized", required=True, help="迁移前历史关键词索引备份")
    parser.add_argument("--drop-legacy-settings", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    backup_dir = ROOT / "临时产物" / f"{datetime.now():%y%m%d_%H%M%S}_关键词注册表迁移备份"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        shutil.copy2(db_path, backup_dir / "app.db")
    shutil.copy2(args.config, backup_dir / "keywords.config.json")
    shutil.copy2(args.normalized, backup_dir / "keywords.normalized.json")

    repo = KeywordRegistryRepository(db_path)
    counts = repo.migrate_legacy(
        Path(args.config),
        Path(args.normalized),
        drop_legacy_settings=args.drop_legacy_settings,
    )
    print(json.dumps({"counts": counts, "backup_dir": str(backup_dir)}, ensure_ascii=False))
    return 0 if counts == {"total": 200, "active": 151, "archived": 49} else 1


if __name__ == "__main__":
    raise SystemExit(main())
