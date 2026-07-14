#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path("/Users/works14/Documents/output_md")
PLAN_CSV = ROOT / "output_md_rename_plan.csv"
APPLIED_CSV = ROOT / "output_md_rename_applied.csv"
BACKLINK_CSV = ROOT / "output_md_backlink_fix_report.csv"

OLD_NAME_RE = re.compile(r"^(?P<mp>.+)_(?P<title>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.md$")
NEW_NAME_RE = re.compile(r"^\d{6}_.+_\d{6}\.md$")

FIXED_NAMES = {"index.md", "logs.md"}
SKIP_SUFFIXES = ("-字幕.md",)
SKIP_PREFIXES = ("error_report_",)


@dataclass(frozen=True)
class RenameItem:
    status: str
    old_path: Path
    new_path: Path
    reason: str


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def is_fixed_file(path: Path) -> bool:
    name = path.name
    if name in FIXED_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in SKIP_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in SKIP_PREFIXES):
        return True
    return False


def iter_markdown_files() -> Iterable[Path]:
    return sorted(OUTPUT_DIR.rglob("*.md"))


def build_plan() -> list[RenameItem]:
    items: list[RenameItem] = []
    wiki_dir = OUTPUT_DIR / "wiki"
    obsidian_dir = OUTPUT_DIR / ".obsidian"

    for old_path in iter_markdown_files():
        if is_relative_to(old_path, wiki_dir):
            items.append(RenameItem("skip_wiki", old_path, old_path, "wiki目录整体跳过"))
            continue
        if is_relative_to(old_path, obsidian_dir):
            items.append(RenameItem("skip_obsidian", old_path, old_path, ".obsidian目录整体跳过"))
            continue
        if is_fixed_file(old_path):
            items.append(RenameItem("skip_fixed", old_path, old_path, "固定文件或非文章文件"))
            continue
        if NEW_NAME_RE.match(old_path.name):
            items.append(RenameItem("skip_already_new", old_path, old_path, "已经是目标格式"))
            continue

        match = OLD_NAME_RE.match(old_path.name)
        if not match:
            items.append(RenameItem("skip_unmatched", old_path, old_path, "无法解析旧文章文件名"))
            continue

        date_part = match.group("date")[2:]
        new_name = f"{date_part}_{match.group('mp')}_{match.group('title')}_{match.group('time')}.md"
        new_path = old_path.with_name(new_name)
        if new_path.exists() and new_path != old_path:
            reason = "目标文件已存在"
            try:
                if old_path.read_bytes() == new_path.read_bytes():
                    reason = "目标文件已存在且内容一致"
            except OSError:
                pass
            items.append(RenameItem("conflict_exists", old_path, new_path, reason))
            continue

        items.append(RenameItem("ready", old_path, new_path, "可改名"))

    return items


def write_items_csv(path: Path, items: Iterable[RenameItem]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["status", "old_path", "new_path", "reason"])
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "status": item.status,
                    "old_path": rel(item.old_path),
                    "new_path": rel(item.new_path),
                    "reason": item.reason,
                }
            )


def load_applied_map() -> dict[str, str]:
    if not APPLIED_CSV.exists():
        return {}

    mapping: dict[str, str] = {}
    with APPLIED_CSV.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row.get("status") != "renamed":
                continue
            old_stem = Path(row["old_path"]).stem
            new_stem = Path(row["new_path"]).stem
            mapping[old_stem] = new_stem
    return mapping


def apply_renames(items: list[RenameItem]) -> list[RenameItem]:
    applied: list[RenameItem] = []
    for item in items:
        if item.status != "ready":
            continue
        item.old_path.rename(item.new_path)
        applied.append(RenameItem("renamed", item.old_path, item.new_path, "已改名"))
    write_items_csv(APPLIED_CSV, applied)
    return applied


def fix_backlinks(mapping: dict[str, str]) -> list[dict[str, str]]:
    if not mapping:
        return []

    wiki_dir = OUTPUT_DIR / "wiki"
    rows: list[dict[str, str]] = []
    link_re = re.compile(r"(!?)\[\[([^\]\|\#]+)(#[^\]\|]+)?(\|[^\]]+)?\]\]")

    def replace_link(match: re.Match[str]) -> str:
        target = match.group(2)
        new_target = mapping.get(target)
        if not new_target:
            return match.group(0)
        return f"{match.group(1)}[[{new_target}{match.group(3) or ''}{match.group(4) or ''}]]"

    for md_path in iter_markdown_files():
        if is_relative_to(md_path, wiki_dir):
            continue
        try:
            old_text = md_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            rows.append({"path": rel(md_path), "replacements": "0", "status": "skip_decode_error"})
            continue

        new_text, count = link_re.subn(replace_link, old_text)
        if count and new_text != old_text:
            md_path.write_text(new_text, encoding="utf-8")
            rows.append({"path": rel(md_path), "replacements": str(count), "status": "updated"})

    with BACKLINK_CSV.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["path", "replacements", "status"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def print_summary(items: list[RenameItem]) -> None:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    for status in sorted(counts):
        print(f"{status}: {counts[status]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只生成改名计划，不修改文件")
    parser.add_argument("--apply", action="store_true", help="执行改名")
    parser.add_argument("--fix-backlinks", action="store_true", help="根据已执行改名记录修复非wiki反链")
    args = parser.parse_args()

    if args.fix_backlinks:
        rows = fix_backlinks(load_applied_map())
        print(f"backlink_updated_files: {len(rows)}")
        print(f"report: {rel(BACKLINK_CSV)}")
        return

    items = build_plan()
    write_items_csv(PLAN_CSV, items)
    print_summary(items)
    print(f"plan: {rel(PLAN_CSV)}")

    if args.apply:
        applied = apply_renames(items)
        print(f"renamed: {len(applied)}")
        print(f"applied: {rel(APPLIED_CSV)}")


if __name__ == "__main__":
    main()
