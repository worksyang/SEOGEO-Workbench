"""Wiki 适配器：只读扫描母文章 Markdown，并通过统一摄取管道入库。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import ContentRecord, DiscoveryRecord, IdentifierRecord
from ..domain.taxonomy import normalize_entity_list, normalize_intent_list
from ..domain.ids import content_id_from_text
from ..ingestion.identity_resolver import IdentityResolver, ResolverContext
from ..ingestion.pipeline import IngestionPipeline, RawBatch
from ..validation.timestamps import utc_now_iso
from .base import AdapterStatus, AdapterTask

ADAPTER_KEY = "wiki"
IDENTIFIER_NAMESPACE = "wiki.source_ref"
DEFAULT_MAX_FILES = 2000
EXCLUDED_DIRS = {
    ".git", ".obsidian", ".claude", ".codex", ".playwright-mcp", ".tmp",
    "__pycache__", "wiki-viewer", "WritingMoney", "output", "候选区",
    "排除流", "微信搜索结果", "缓存", "cache", "tmp", "temp", "runtime",
}
NON_ARTICLE_NAME_HINTS = ("prompt", "readme", "license", "changelog", "requirements")
NON_ARTICLE_NAMES = {"agents.md", "claude.md", ".backup-log.md"}


class WikiIngestionPipeline(IngestionPipeline):
    """沿用统一 pipeline，但兼容当前仓库旧版 identity_merge_candidates 表。"""

    def _ingest_contents(self, contents, resolver, errors) -> int:
        written = 0
        for record in contents:
            try:
                decision = resolver.resolve(
                    ResolverContext(
                        title=record.title,
                        author_name=record.author_name,
                        published_at=record.published_at,
                        canonical_url=record.canonical_url,
                    )
                )
                entities = normalize_entity_list(record.entities)
                intents = normalize_intent_list(record.intents)
                if not record.content_id:
                    record.content_id = decision.content_id
                self._conn.execute(
                    """
                    INSERT INTO contents(
                        content_id, content_type, title, canonical_url, creator_id,
                        author_name, published_at, first_seen_at, updated_at,
                        md_path, file_hash, content_hash, domain,
                        entities_json, intents_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(content_id) DO UPDATE SET
                        title=COALESCE(excluded.title, contents.title),
                        updated_at=excluded.updated_at,
                        md_path=COALESCE(excluded.md_path, contents.md_path),
                        file_hash=COALESCE(excluded.file_hash, contents.file_hash),
                        content_hash=COALESCE(excluded.content_hash, contents.content_hash),
                        domain=COALESCE(excluded.domain, contents.domain),
                        entities_json=excluded.entities_json,
                        intents_json=excluded.intents_json,
                        payload_json=excluded.payload_json
                    """,
                    (
                        record.content_id, record.content_type, record.title or None,
                        record.canonical_url or None, record.creator_id or None,
                        record.author_name or None, record.published_at or None,
                        record.first_seen_at or utc_now_iso(),
                        record.updated_at or record.first_seen_at or utc_now_iso(),
                        record.md_path or None, record.file_hash or None,
                        record.content_hash or None, record.domain or None,
                        json.dumps(entities, ensure_ascii=False),
                        json.dumps(intents, ensure_ascii=False),
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                # 旧表没有新 resolver 的 evidence 列，内容本身仍由稳定 identifier 保护幂等。
                written += 1
            except Exception as exc:
                errors.append({"scope": "content", "content_id": record.content_id, "error": str(exc)})
        return written

    def _ingest_discoveries(self, discoveries, errors) -> int:
        written = 0
        for record in discoveries:
            try:
                self._conn.execute(
                    """
                    INSERT INTO content_discoveries(
                        discovery_id, content_id, discovery_system, discovery_channel,
                        discovered_at, snapshot_id, source_ref, payload_json
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(content_id, discovery_system, discovery_channel, COALESCE(snapshot_id, 'no-snapshot'))
                    DO UPDATE SET source_ref=excluded.source_ref, payload_json=excluded.payload_json
                    """,
                    (
                        record.discovery_id, record.content_id, record.discovery_system,
                        record.discovery_channel, record.discovered_at,
                        record.source_ref or "",
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                written += 1
            except Exception as exc:
                errors.append({"scope": "discovery", "error": str(exc)})
        return written


@dataclass(slots=True)
class WikiScan:
    root: Path
    files: list[tuple[Path, str, str, str, str]] = field(default_factory=list)
    scanned: int = 0
    rejected: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    @property
    def accepted(self) -> int:
        return len(self.files)


def _relative_ref(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_candidate(path: Path, root: Path) -> tuple[Path | None, str | None]:
    try:
        normalized = path.absolute()
        normalized.relative_to(root)
    except ValueError:
        return None, "path_outside_allowed_root"
    try:
        info = os.lstat(normalized)
    except OSError as exc:
        return None, f"stat_failed:{exc.__class__.__name__}"
    if os.path.islink(normalized) or not os.path.isfile(normalized):
        return None, "symlink_or_not_regular_file"
    try:
        resolved = normalized.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None, "path_outside_allowed_root"
    if resolved != normalized:
        return None, "symlink_or_not_regular_file"
    return normalized, None


def scan_directory(wiki_root: Path, *, max_files: int = DEFAULT_MAX_FILES) -> WikiScan:
    """扫描根内普通 UTF-8 Markdown；不读取或修改根外文件。"""
    root = Path(wiki_root).expanduser().resolve()
    result = WikiScan(root=root)
    if max_files < 1:
        raise ValueError("max_files 必须大于 0")
    try:
        os.lstat(root)
        if os.path.islink(root) or not root.is_dir():
            result.rejected.append({"source_ref": "", "reason": "wiki_root_not_directory"})
            return result
    except OSError as exc:
        result.rejected.append({"source_ref": "", "reason": f"wiki_root_unavailable:{exc.__class__.__name__}"})
        return result

    candidates = sorted(
        (path for path in root.rglob("*") if path.suffix.lower() == ".md"),
        key=lambda item: _relative_ref(item, root),
    )
    for candidate in candidates:
        result.scanned += 1
        source_ref = _relative_ref(candidate, root)
        parts = Path(source_ref).parts[:-1]
        excluded_part = next(
            (part for part in parts if part in EXCLUDED_DIRS or part.startswith(".")),
            None,
        )
        if excluded_part:
            result.rejected.append(
                {"source_ref": source_ref, "reason": f"excluded_directory:{excluded_part}"}
            )
            continue
        if (
            candidate.name.casefold() in NON_ARTICLE_NAMES
            or candidate.name.casefold().startswith(NON_ARTICLE_NAME_HINTS)
        ):
            result.rejected.append({"source_ref": source_ref, "reason": "non_article_prompt_or_tool_file"})
            continue
        safe_path, reason = _safe_candidate(candidate, root)
        if not safe_path:
            result.rejected.append({"source_ref": source_ref, "reason": reason or "rejected"})
            continue
        try:
            text = safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            result.rejected.append({"source_ref": source_ref, "reason": "invalid_utf8"})
            continue
        except OSError as exc:
            result.rejected.append({"source_ref": source_ref, "reason": f"read_failed:{exc.__class__.__name__}"})
            continue
        if len(result.files) >= max_files:
            result.truncated = True
            continue
        title = _extract_title(text) or safe_path.stem
        category = _extract_category(safe_path, root)
        result.files.append(
            (safe_path, title, category, hashlib.sha256(text.encode("utf-8")).hexdigest(), text)
        )
    return result


def _extract_title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return ""


def _extract_category(path: Path, root: Path) -> str:
    parts = path.relative_to(root).parts[:-1]
    return "/".join(parts) if parts else "wiki"


def _existing_content_id(connection: sqlite3.Connection, source_ref: str, content_hash: str) -> str:
    row = connection.execute(
        "SELECT content_id FROM content_identifiers WHERE namespace=? AND external_id=?",
        (IDENTIFIER_NAMESPACE, source_ref),
    ).fetchone()
    if row:
        return str(row[0])
    row = connection.execute(
        "SELECT content_id FROM contents WHERE content_hash=? AND content_type IN ('mother_article', 'knowledge_article') LIMIT 1",
        (content_hash,),
    ).fetchone()
    if row:
        return str(row[0])
    return content_id_from_text(IDENTIFIER_NAMESPACE, source_ref)


def make_batch(
    connection: sqlite3.Connection,
    *,
    wiki_root: Path,
    max_files: int = DEFAULT_MAX_FILES,
) -> tuple[RawBatch, AdapterStatus, WikiScan]:
    scan = scan_directory(wiki_root, max_files=max_files)
    raw = RawBatch(adapter_key=ADAPTER_KEY, source_scope="wiki")
    status = AdapterStatus(adapter_key=ADAPTER_KEY, source_scope="wiki")
    status.total = scan.accepted
    status.errors = [f"{item['source_ref']}: {item['reason']}" for item in scan.rejected]
    now = utc_now_iso()
    for path, title, category, content_hash, text in scan.files:
        source_ref = _relative_ref(path, scan.root)
        content_id = _existing_content_id(connection, source_ref, content_hash)
        try:
            stat = path.stat()
        except OSError:
            stat = None
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        asset_kind = "knowledge_article" if source_ref == "wiki" or source_ref.startswith("wiki/") else "mother_article"
        content = ContentRecord(
            content_id=content_id,
            content_type=asset_kind,
            title=title,
            first_seen_at=now,
            updated_at=now,
            md_path=source_ref,
            file_hash=file_hash,
            content_hash=content_hash,
            domain=category,
            payload={
                "asset_kind": asset_kind,
                "wiki_category": category,
                "source_ref": source_ref,
                "size": stat.st_size if stat else 0,
            },
        )
        raw.contents.append(content)
        raw.identifiers.append(
            IdentifierRecord(
                namespace=IDENTIFIER_NAMESPACE,
                external_id=source_ref,
                content_id=content_id,
                first_seen_at=now,
            )
        )
        raw.discoveries.append(
            DiscoveryRecord.build(
                content_id=content_id,
                system=ADAPTER_KEY,
                channel="directory_scan",
                discovered_at=now,
                source_ref=source_ref,
                payload={"category": category, "file_hash": file_hash},
            )
        )
        status.written += 1
    status.last_seen_at = now
    return raw, status, scan


def run(
    connection: sqlite3.Connection,
    pipeline: IngestionPipeline,
    *,
    wiki_root: Path,
    max_files: int = DEFAULT_MAX_FILES,
) -> AdapterTask:
    task = AdapterTask(adapter_key=ADAPTER_KEY, status=AdapterStatus(adapter_key=ADAPTER_KEY, source_scope="wiki"))
    raw, status, _scan = make_batch(connection, wiki_root=wiki_root, max_files=max_files)
    task.status = status
    runner = (
        pipeline
        if isinstance(pipeline, WikiIngestionPipeline)
        else WikiIngestionPipeline(connection, getattr(pipeline, "_lock_path", Path("content_hub.lock")))
    )
    result = runner.run(raw)
    task.records_written = result.records_written
    task.records_failed = result.records_failed
    task.finished_at = utc_now_iso()
    return task
