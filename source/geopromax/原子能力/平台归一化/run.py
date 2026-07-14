#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "data/platforms.json"
DEFAULT_DATABASE = ROOT / "data/index/geopromax.sqlite"


def load(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    platforms = data.get("platforms") or {}
    aliases: dict[str, str] = {}
    for canonical, meta in platforms.items():
        names = [canonical, *(meta.get("aliases") or [])]
        for name in names:
            key = str(name).strip()
            if not key:
                raise ValueError(f"平台 {canonical} 包含空名称")
            if key in aliases and aliases[key] != canonical:
                raise ValueError(f"平台别名重复：{key}")
            aliases[key] = canonical
    return {"version": data.get("version", 1), "platforms": platforms, "aliases": aliases}


def canonical(name: str | None, rules: dict[str, Any]) -> str:
    raw = str(name or "").strip()
    return rules["aliases"].get(raw, raw or "其他网页")


def metadata(name: str | None, rules: dict[str, Any]) -> dict[str, str]:
    raw = str(name or "").strip()
    platform = canonical(raw, rules)
    meta = rules["platforms"].get(platform) or {}
    domain = str(meta.get("icon_domain") or "").strip()
    return {
        "raw_platform": raw or "其他网页",
        "platform": platform,
        "icon_url": f"https://www.google.com/s2/favicons?sz=64&domain_url=https://{domain}" if domain else "",
    }


def aggregate(database: str | Path, rules: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    db = sqlite3.connect(Path(database).expanduser())
    raw = list(db.execute("SELECT platform,COUNT(*) FROM sources GROUP BY platform"))
    db.close()
    counts: Counter[str] = Counter()
    mapped_sources = 0
    for name, count in raw:
        platform = canonical(name, rules)
        counts[platform] += count
        if platform != name:
            mapped_sources += count
    rows = [
        {"platform": name, "sources": count, "aliases": sorted(
            alias for alias, target in rules["aliases"].items() if target == name and alias != name
        )}
        for name, count in counts.most_common(limit)
    ]
    return {
        "sources": sum(counts.values()),
        "raw_platforms": len(raw),
        "platforms": len(counts),
        "mapped_sources": mapped_sources,
        "configured_platforms": len(rules["platforms"]),
        "top": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把采集到的平台名称映射为内容分析使用的标准平台。")
    parser.add_argument("platform", nargs="?", help="要归一化的单个平台名称；省略时统计 SQLite。")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    rules = load(args.config)
    result = metadata(args.platform, rules) if args.platform else aggregate(args.database, rules, args.limit)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
