"""Wiki 适配器：扫描母文章库，落 contents + content_discoveries。
依 dev-plan §5.5 仅做只读 first-pass，不修改原 markdown 文件。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from ..domain.ids import content_id_from_canonical_url, generate_ulid_like
from ..ingestion.pipeline import IngestionPipeline, RawBatch
from ..validation.timestamps import utc_now_iso
from .base import AdapterStatus, AdapterTask

ADAPTER_KEY = "wiki"


def scan_directory(wiki_root: Path, *, max_files: int = 400) -> tuple[list[tuple[Path, str, str]], list[str]]:
    files: list[tuple[Path, str, str]] = []
    errors: list[str] = []
    if not wiki_root.exists():
        return files, ["wiki_root 不存在"]
    count = 0
    for path in sorted(wiki_root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        title = _extract_title(text) or path.stem
        files.append((path, title, _extract_category(path, wiki_root)))
        count += 1
        if count >= max_files:
            break
    return files, errors


def _extract_title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return ""


def _extract_category(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts[:-1]
    return "/".join(parts) if parts else "wiki"


def make_batch(
    connection: sqlite3.Connection,
    *,
    wiki_root: Path,
    max_files: int = 400,
) -> tuple[RawBatch, AdapterStatus]:
    files, errors = scan_directory(wiki_root, max_files=max_files)
    raw = RawBatch(adapter_key=ADAPTER_KEY, source_scope=str(wiki_root))
    status = AdapterStatus(adapter_key=ADAPTER_KEY, source_scope=str(wiki_root))
    status.total = len(files)
    now = utc_now_iso()
    for path, title, category in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"{path}: 读取失败")
            continue
        url = f"file://{path}"
        content_id = content_id_from_canonical_url(url)
        from ..domain.models import ContentRecord, DiscoveryRecord, IdentifierRecord

        content = ContentRecord.from_url(
            url=url,
            content_type="mother_article",
            title=title,
            author_name="",
            published_at="",
            first_seen_at=now,
            updated_at=now,
            domain=str(path.parent.relative_to(wiki_root.parent)),
            payload={"wiki_category": category},
        )
        content.md_path = str(path)
        content.content_id = content_id
        raw.contents.append(content)
        raw.identifiers.append(
            IdentifierRecord(
                namespace="mother.frontmatter_asset_id",
                external_id=str(path.resolve()),
                content_id=content_id,
                first_seen_at=now,
            )
        )
        raw.discoveries.append(
            DiscoveryRecord.build(
                content_id=content_id,
                system="wiki",
                channel="directory_scan",
                discovered_at=now,
                source_ref=str(path),
                payload={"category": category},
            )
        )
        status.written += 1
    status.errors = errors
    return raw, status


def run(connection: sqlite3.Connection, pipeline: IngestionPipeline, *, wiki_root: Path) -> AdapterTask:
    task = AdapterTask(adapter_key=ADAPTER_KEY, status=AdapterStatus(adapter_key=ADAPTER_KEY, source_scope=str(wiki_root)))
    raw, status = make_batch(connection, wiki_root=wiki_root)
    task.status = status
    result = pipeline.run(raw)
    task.records_written = result.records_written
    task.records_failed = result.records_failed
    return task
