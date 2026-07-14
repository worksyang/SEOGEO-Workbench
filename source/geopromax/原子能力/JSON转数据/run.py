#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RAW = ROOT / "data/raw"
TEMP = Path.home() / ".geopromax/tmp"
DATABASE = ROOT / "data/index/geopromax.sqlite"


def call(name: str, *args: str) -> dict:
    command = [sys.executable, str(HERE / name / "run.py"), *args]
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout).strip() or f"{name} 执行失败") from exc
    return json.loads(result.stdout.strip().splitlines()[-1])


def archive(content: bytes, raw_root: Path = RAW) -> tuple[Path, bool]:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 文件无效：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("JSON 缺少 results 数组")
    value = payload.get("started_at") or payload.get("finished_at")
    try:
        captured_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        captured_at = datetime.now().astimezone()
    row_mode = next((row.get("mode") for row in payload["results"] if row.get("mode")), None)
    mode = re.sub(r"[^a-z0-9_-]+", "-", str(payload.get("mode") or row_mode or "quick").lower()).strip("-") or "quick"
    path = raw_root / "豆包" / "mobile" / mode / f"{captured_at:%Y-%m-%d-%H-%M-%S}.json"
    if path.exists():
        if path.read_bytes() == content:
            return path, False
        raise RuntimeError(f"同一采集时间已有不同文件：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_bytes(content)
    temp.replace(path)
    return path, True


def ingest(path: Path, output_root: Path = ROOT, database: Path = DATABASE) -> dict:
    TEMP.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    normalized = TEMP / f"{path.stem}-{key}.normalized.json"
    try:
        unpacked = call("拆解JSON", "--input", str(path), "--output", str(normalized))
        markdown = call("保存Markdown正文", "--input", str(normalized), "--root", str(output_root))
        result = call("写入SQLite", "--input", str(normalized), "--database", str(database))
        return {**result, **unpacked, **markdown, "raw_file": str(path)}
    finally:
        normalized.unlink(missing_ok=True)


def ingest_bytes(content: bytes, raw_root: Path = RAW, output_root: Path = ROOT, database: Path = DATABASE) -> dict:
    path, archived = archive(content, raw_root)
    return {**ingest(path, output_root, database), "archived": archived}


def main() -> int:
    files = sorted(RAW.glob("**/*.json"))
    if not files:
        raise SystemExit(f"未找到原始 JSON：{RAW}")
    imported = skipped = 0
    for path in files:
        result = ingest(path)
        imported += result["status"] == "imported"
        skipped += result["status"] == "skipped"
    print(json.dumps({"files": len(files), "imported": imported, "skipped": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
