#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def write(path: Path, content: str) -> bool:
    text = content.strip() + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="把统一数据中的回答和来源正文保存为纯 Markdown。")
    parser.add_argument("--input", required=True)
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    data = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    answer_files = source_files = 0
    for answer in data.get("answers") or []:
        if answer.get("markdown_path") and answer.get("content"):
            answer_files += write(root / answer["markdown_path"], answer["content"])
    for source in (data.get("sources") or {}).values():
        if source.get("markdown_path") and source.get("content"):
            source_files += write(root / source["markdown_path"], source["content"])
    print(json.dumps({"answer_files": answer_files, "source_files": source_files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
