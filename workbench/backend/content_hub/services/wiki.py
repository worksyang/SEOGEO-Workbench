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
from ..validation.paths import resolve_within
from ..validation.timestamps import utc_now_iso


class WikiService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        asset_root: Path,
        source_roots: Iterable[Path],
    ):
        self._conn = connection
        self._asset_root = Path(asset_root).resolve()
        self._source_roots = [Path(root).resolve() for root in source_roots if root is not None]

    def tree(self) -> list[dict[str, Any]]:
        bucket_roots: list[tuple[Path, str]] = []
        if (self._asset_root / "wiki").exists():
            bucket_roots.append((self._asset_root / "wiki", "wiki"))
        for root in self._source_roots:
            candidate = root / "wiki"
            if candidate.exists():
                bucket_roots.append((candidate, "legacy_wiki"))
        return [self._scan_dir(root, bucket, Path()) for root, bucket in bucket_roots]

    def _scan_dir(self, root: Path, bucket: str, relative: Path) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        sub_dirs: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                sub_dirs.append(self._scan_dir(entry, bucket, relative / entry.name))
            elif entry.suffix.lower() == ".md":
                files.append(self._describe_file(entry, bucket, relative / entry.name))
        return {
            "bucket": bucket,
            "name": root.name,
            "path": str(root),
            "relative_path": str(relative) if str(relative) != "." else "",
            "files": files,
            "sub_dirs": sub_dirs,
        }

    def _describe_file(self, path: Path, bucket: str, relative: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = ""
        title, excerpt = _peek_title_and_excerpt(text, fallback=path.stem)
        word_count = len(text)
        has_image = bool(re.search(r"!\[[^\]]*\]\(", text)) or "<img" in text
        try:
            stat = path.stat()
            updated = (
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except OSError:
            updated = utc_now_iso()
        content_id = _content_id_from_path(path)
        return {
            "content_id": content_id,
            "title": title,
            "excerpt": excerpt,
            "path": str(path),
            "relative_path": str(relative),
            "bucket": bucket,
            "category": relative.parts[0] if relative.parts else "",
            "word_count": word_count,
            "has_image": has_image,
            "updated_at": updated,
        }

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
        for entry in self.collect():
            if entry["content_id"] == content_id:
                text = Path(entry["path"]).read_text(encoding="utf-8")
                return {"content_id": content_id, "title": entry["title"], "body": text, "entry": entry}
        return None

    def save(self, content_id: str, *, body: str, operator: str = "user") -> dict[str, Any]:
        entry = next((item for item in self.collect() if item["content_id"] == content_id), None)
        if not entry:
            raise FileNotFoundError(f"未找到 content_id={content_id} 的母文章")
        safe_path = resolve_within(
            Path(entry["path"]),
            self._source_roots + [self._asset_root],
        )
        text = body if body.endswith("\n") else body + "\n"
        if not text.startswith("---"):
            frontmatter = (
                "---\n"
                "schema_version: content-md/1.1\n"
                f"content_id: {content_id}\n"
                "content_type: mother_article\n"
                f"updated_at: {utc_now_iso()}\n"
                "---\n\n"
            )
            text = frontmatter + text
        bytes_payload = text.encode("utf-8")
        self._atomic_write(safe_path, bytes_payload)
        new_hash = hashlib.sha256(bytes_payload).hexdigest()
        content_only = text.split("---", 2)[-1].strip()
        content_hash = hashlib.sha256(content_only.encode("utf-8")).hexdigest()
        self._conn.execute(
            "UPDATE contents SET file_hash=?, updated_at=?, content_hash=? WHERE content_id=?",
            (new_hash, utc_now_iso(), content_hash, content_id),
        )
        self._record_audit(operator, "wiki.save", content_id, {"path": str(safe_path)})
        return {"content_id": content_id, "md_path": str(safe_path), "file_hash": new_hash}

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
