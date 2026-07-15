"""Wiki 服务：母文章目录扫描、全文文件名搜索、正文读取与原子保存。

对应 dev-plan §5.5：Markdown 是唯一正文源；保存采用原子写入 + 路径解析防穿透。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..domain.ids import generate_ulid_like
from ..adapters.wiki import (
    DEFAULT_MAX_FILES,
    WikiIngestionPipeline,
    content_id_for_source,
    make_batch,
    safe_markdown_path,
    scan_directory,
)
from ..ingestion.checkpoints import CheckpointStore, checkpoint_key_for
from .audit import AuditService
from .jobs import JobsService
from ..validation.paths import resolve_within
from ..validation.timestamps import utc_now_iso


class WikiService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        asset_root: Path,
        source_roots: Iterable[Path],
        lock_path: Path | None = None,
    ):
        self._conn = connection
        self._asset_root = Path(asset_root).resolve()
        self._source_roots = [Path(root).resolve() for root in source_roots if root is not None]
        self._lock_path = Path(lock_path or (Path(tempfile.gettempdir()) / "content_hub_wiki.lock"))

    def import_root(self) -> Path | None:
        """选择允许根中的 output_md 业务根；不把 asset_store 当历史源。"""
        candidates = [
            root for root in self._source_roots
            if root != self._asset_root and root.is_dir() and (root / "wiki").is_dir()
        ]
        return sorted(candidates, key=lambda item: str(item))[0] if candidates else None

    def import_wiki(
        self,
        *,
        confirm: bool = False,
        max_files: int = DEFAULT_MAX_FILES,
        operator: str = "user",
    ) -> dict[str, Any]:
        if max_files < 1:
            raise ValueError("max_files 必须大于 0")
        root = self.import_root()
        if root is None:
            return {
                "status": "blocked",
                "reason": "没有可用的已配置 Wiki 允许根",
                "source_root": "",
                "scanned": 0, "accepted": 0, "rejected": 0, "processed": 0,
                "truncated": False, "rejections": [],
            }

        raw, adapter_status, scan = make_batch(self._conn, wiki_root=root, max_files=max_files)
        result: dict[str, Any] = {
            "status": "degraded" if scan.rejected else "ready",
            "source_root": "configured/wiki-source",
            "scanned": scan.scanned,
            "accepted": scan.accepted,
            "rejected": len(scan.rejected),
            "processed": 0,
            "truncated": scan.truncated,
            "max_files": max_files,
            "rejections": list(scan.rejected),
            "classification": {
                "mother_article": sum(
                    1 for item in scan.files
                    if not (item[0].relative_to(scan.root).as_posix() == "wiki"
                            or item[0].relative_to(scan.root).as_posix().startswith("wiki/"))
                ),
                "knowledge_article": sum(
                    1 for item in scan.files
                    if item[0].relative_to(scan.root).as_posix() == "wiki"
                    or item[0].relative_to(scan.root).as_posix().startswith("wiki/")
                ),
                "excluded_directories": "hidden、wiki-viewer、WritingMoney、运行/缓存目录及历史候选/排除目录",
            },
        }
        if not scan.accepted:
            result["status"] = "blocked"
            result["reason"] = "没有可导入的 UTF-8 普通 Markdown"
            return result
        if not confirm:
            result["status"] = "dry_run"
            result["preview"] = [
                {"source_ref": item[0].relative_to(scan.root).as_posix(), "title": item[1]}
                for item in scan.files[:20]
            ]
            return result

        jobs = JobsService(self._conn)
        job_id = jobs.create(
            job_type="wiki_import",
            payload={
                "source_root": "configured/wiki-source",
                "max_files": max_files,
                "scanned": scan.scanned,
                "accepted": scan.accepted,
                "rejected": len(scan.rejected),
                "truncated": scan.truncated,
            },
        )
        self._conn.commit()
        if not jobs.claim(job_id, operator):
            result.update(status="blocked", reason="Wiki 导入任务无法获取执行锁", job_id=job_id)
            self._conn.commit()
            return result
        try:
            pipeline_result = WikiIngestionPipeline(self._conn, self._lock_path).run(raw)
            result["processed"] = scan.accepted - len(
                [error for error in pipeline_result.errors if error.get("scope") == "content"]
            )
            result["batch_id"] = pipeline_result.batch_id
            result["records_written"] = pipeline_result.records_written
            result["records_failed"] = pipeline_result.records_failed
            result["errors"] = pipeline_result.errors
            if pipeline_result.records_failed:
                jobs.complete(job_id, status="failed")
                result["status"] = "degraded"
            else:
                CheckpointStore(self._conn).upsert(
                    adapter_key="wiki",
                    checkpoint_key=checkpoint_key_for("configured/wiki-source", "manifest"),
                    cursor_value=str(scan.accepted),
                    source_hash=_scan_hash(scan),
                    batch_id=pipeline_result.batch_id,
                    payload={
                        "scanned": scan.scanned,
                        "accepted": scan.accepted,
                        "rejected": len(scan.rejected),
                        "truncated": scan.truncated,
                    },
                )
                jobs.complete(job_id, status="succeeded")
                result["status"] = "degraded" if scan.rejected else "succeeded"
            AuditService(self._conn).record(
                action="wiki.import",
                subject_type="ingestion_batch",
                subject_id=pipeline_result.batch_id,
                actor_id=operator,
                outcome="succeeded" if result["status"] == "succeeded" else "failed",
                details={
                    "job_id": job_id,
                    "source_root": "configured/wiki-source",
                    "scanned": scan.scanned,
                    "accepted": scan.accepted,
                    "rejected": len(scan.rejected),
                    "processed": result["processed"],
                    "truncated": scan.truncated,
                },
            )
            result["job_id"] = job_id
            self._conn.commit()
            return result
        except Exception as exc:
            jobs.complete(job_id, status="failed")
            AuditService(self._conn).record(
                action="wiki.import",
                subject_type="job",
                subject_id=job_id,
                actor_id=operator,
                outcome="failed",
                details={
                    "source_root": "configured/wiki-source",
                    "error_type": type(exc).__name__,
                },
            )
            self._conn.commit()
            result.update(status="degraded", reason="导入失败，请查看任务与审计记录", job_id=job_id)
            return result

    def tree(self) -> list[dict[str, Any]]:
        entries = self._entries()
        if not entries:
            return []
        root = {
            "bucket": "legacy_wiki",
            "name": "母文章库",
            "path": "",
            "source_ref": "",
            "relative_path": "",
            "files": [],
            "sub_dirs": [],
        }
        nodes: dict[str, dict[str, Any]] = {"": root}
        for entry in entries:
            source_ref = entry["source_ref"]
            parent = ""
            for segment in Path(source_ref).parts[:-1]:
                node_ref = f"{parent}/{segment}".strip("/")
                if node_ref not in nodes:
                    child = {
                        "bucket": "legacy_wiki",
                        "name": segment,
                        "path": node_ref,
                        "source_ref": node_ref,
                        "relative_path": node_ref,
                        "files": [],
                        "sub_dirs": [],
                    }
                    nodes[node_ref] = child
                    nodes[parent]["sub_dirs"].append(child)
                parent = node_ref
            nodes[parent]["files"].append(self._public_entry(entry))
        self._sort_tree(root)
        return [root]

    def _entries(self) -> list[dict[str, Any]]:
        """以导入同一安全扫描规则构造 UI 条目；私有绝对 Path 不序列化。"""
        root = self.import_root()
        if root is None:
            return []
        scan = scan_directory(root, max_files=1_000_000)
        entries: list[dict[str, Any]] = []
        for path, title, category, content_hash, text in scan.files:
            source_ref = path.relative_to(scan.root).as_posix()
            content_id = content_id_for_source(self._conn, source_ref, content_hash)
            try:
                updated = (
                    datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except OSError:
                continue
            content_type = (
                "knowledge_article"
                if source_ref == "wiki" or source_ref.startswith("wiki/")
                else "mother_article"
            )
            entry = {
                "content_id": content_id,
                "title": title,
                "excerpt": _peek_title_and_excerpt(text, fallback=path.stem)[1],
                "path": source_ref,
                "source_ref": source_ref,
                "relative_path": source_ref,
                "bucket": "legacy_wiki",
                "category": category,
                "content_type": content_type,
                "word_count": len(text),
                "has_image": bool(re.search(r"!\[[^\]]*\]\(", text)) or "<img" in text,
                "updated_at": updated,
                "_path": path,
                "_root": scan.root,
            }
            entries.append(entry)
        return entries

    @staticmethod
    def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in entry.items() if not key.startswith("_")}

    def _sort_tree(self, node: dict[str, Any]) -> None:
        node["files"].sort(key=lambda item: (str(item["title"]).lower(), item["source_ref"]))
        node["sub_dirs"].sort(key=lambda item: str(item["name"]).lower())
        for child in node["sub_dirs"]:
            self._sort_tree(child)

    def _entry_for_content(self, content_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self._entries() if entry["content_id"] == content_id), None)

    @staticmethod
    def _safe_entry_path(entry: dict[str, Any]) -> Path | None:
        path, _reason = safe_markdown_path(entry["_path"], entry["_root"])
        return path

    def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        all_entries = self.collect()
        needle = query.strip().lower()
        if not needle:
            return all_entries[:limit]
        results: list[dict[str, Any]] = []
        for entry in all_entries:
            haystack = (
                entry["title"] + "\n" + entry["excerpt"] + "\n" + entry["relative_path"]
            ).lower()
            if needle in haystack:
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def collect(self, nodes: list[dict[str, Any]] | None = None, _depth: int = 0) -> list[dict[str, Any]]:
        if _depth > 8:
            return []
        output: list[dict[str, Any]] = []
        queue = list(nodes or self.tree())
        while queue:
            node = queue.pop()
            output.extend(node.get("files", []))
            for sub in node.get("sub_dirs", []):
                queue.append(sub)
        return output

    def read(self, content_id: str) -> dict[str, Any] | None:
        entry = self._entry_for_content(content_id)
        if not entry:
            return None
        path = self._safe_entry_path(entry)
        if not path:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return {
            "content_id": content_id,
            "title": entry["title"],
            "body": text,
            "entry": self._public_entry(entry),
        }

    def save(self, content_id: str, *, body: str, operator: str = "user") -> dict[str, Any]:
        entry = self._entry_for_content(content_id)
        if not entry:
            raise FileNotFoundError(f"未找到 content_id={content_id} 的母文章")
        safe_path = self._safe_entry_path(entry)
        if not safe_path:
            raise FileNotFoundError("母文章路径不安全或不可读取")
        text = body if body.endswith("\n") else body + "\n"
        if not text.startswith("---"):
            frontmatter = (
                "---\n"
                "schema_version: content-md/1.1\n"
                f"content_id: {content_id}\n"
                f"content_type: {entry['content_type']}\n"
                f"updated_at: {utc_now_iso()}\n"
                "---\n\n"
            )
            text = frontmatter + text
        bytes_payload = text.encode("utf-8")
        snapshot_path = self._create_snapshot(safe_path, content_id)
        if self._safe_entry_path(entry) != safe_path:
            raise FileNotFoundError("母文章路径在保存前发生变化")
        self._atomic_write(safe_path, bytes_payload)
        new_hash = hashlib.sha256(bytes_payload).hexdigest()
        content_only = text.split("---", 2)[-1].strip()
        content_hash = hashlib.sha256(content_only.encode("utf-8")).hexdigest()
        self._conn.execute(
            "UPDATE contents SET file_hash=?, updated_at=?, content_hash=? WHERE content_id=?",
            (new_hash, utc_now_iso(), content_hash, content_id),
        )
        self._record_audit(operator, "wiki.save", content_id, {"source_ref": entry["source_ref"]})
        # Router 为每个请求新建连接；没有显式提交会导致正文已写入而索引和审计丢失。
        self._conn.commit()
        return {
            "content_id": content_id,
            "source_ref": entry["source_ref"],
            "relative_path": entry["relative_path"],
            "file_hash": new_hash,
            "snapshot_ref": snapshot_path.relative_to(self._asset_root).as_posix(),
        }

    def list_buckets(self) -> list[str]:
        return sorted({node["bucket"] for node in self.tree()})

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), prefix=".tmp_", suffix=".md") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)

    def _create_snapshot(self, source_path: Path, content_id: str) -> Path:
        """在写入前创建唯一快照；快照目录只允许追加，不覆盖既有文件。"""
        snapshot_root = resolve_within(
            self._asset_root / "wiki" / ".snapshots",
            [self._asset_root],
        )
        snapshot_root.mkdir(parents=True, exist_ok=True)
        original = source_path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = snapshot_root / f"{content_id}_{stamp}_{digest[:16]}.md"
        safe_snapshot = resolve_within(candidate, [snapshot_root])
        with safe_snapshot.open("xb") as snapshot:
            snapshot.write(original)
            snapshot.flush()
            os.fsync(snapshot.fileno())
        return safe_snapshot

    def _record_audit(self, operator: str, action: str, subject_id: str, details: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_log(
                audit_id, occurred_at, actor_type, actor_id, action,
                subject_type, subject_id, outcome, details_json
            ) VALUES (?, ?, 'user', ?, ?, 'content', ?, 'succeeded', ?)
            """,
            (
                generate_ulid_like("cev"),
                utc_now_iso(),
                operator,
                action,
                subject_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )


def _peek_title_and_excerpt(text: str, fallback: str) -> tuple[str, str]:
    title = fallback
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()[:80]
            break
    body = "\n".join(text.splitlines()[:80])
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"[#>*`]", "", plain)
    excerpt = " ".join(plain.split())[:140]
    return title, excerpt


def _content_id_from_path(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"cnt_{digest}"


def _scan_hash(scan: Any) -> str:
    manifest = "\n".join(
        f"{item[0].relative_to(scan.root).as_posix()}:{item[3]}"
        for item in scan.files
    )
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()
