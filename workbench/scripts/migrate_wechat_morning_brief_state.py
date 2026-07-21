#!/usr/bin/env python3
"""把旧港险晨报业务状态一次性复制到 Workbench 允许写入区。

只复制编辑记忆、噪音库、Wiki 和历史记录；旧目录永远只读。重复执行时，已存在且
内容不同的目标文件会被拒绝，避免覆盖新系统已经产生的业务状态。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

OLD_ROOT = Path("/Users/works14/.claude/监控/wechat-ybxhyyh-top3")
PROJECT_ROOT = Path("/Users/works14/Documents/zkcode/260712_SEO-GEO")
TARGET = PROJECT_ROOT / "data/agent/morning-brief"
SOURCES = (Path("MEMORY.md"), Path("账号噪音库.md"), Path("wiki"), Path("历史记录"))


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    if not OLD_ROOT.is_dir() or OLD_ROOT.is_symlink():
        raise RuntimeError("old business root is missing or is a symlink")
    TARGET.mkdir(parents=True, exist_ok=True)
    for directory in ("decisions", "discovery", "temporary"):
        (TARGET / directory).mkdir(exist_ok=True)

    entries = []
    for relative in SOURCES:
        source = OLD_ROOT / relative
        if source.is_symlink():
            raise RuntimeError(f"source cannot be a symlink: {relative}")
        files = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.is_file())
        for path in files:
            if path.is_symlink():
                raise RuntimeError(f"source file cannot be a symlink: {path}")
            nested = path.relative_to(OLD_ROOT)
            target = TARGET / nested
            source_hash = digest(path)
            if target.exists():
                if target.is_symlink() or digest(target) != source_hash:
                    raise RuntimeError(f"target already differs: {nested}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            entries.append(
                {
                    "path": nested.as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": source_hash,
                }
            )

    manifest = {
        "schema_version": "morning_brief_state_migration_v1",
        "source": "legacy-wechat-morning-brief-business-state",
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "file_count": len(entries),
        "files": entries,
    }
    (TARGET / "migration-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"target": str(TARGET), "file_count": len(entries)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
