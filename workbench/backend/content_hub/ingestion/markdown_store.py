"""统一内容工作台 · 摄取层 / Markdown 资产库。

对外行为依据 v3.2 §五（文件系统与 Markdown 规范）。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..validation.paths import resolve_within
from ..validation.timestamps import utc_now_iso

SAFE_TITLE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\-_]+")


@dataclass(slots=True)
class MarkdownWriteResult:
    md_path: str
    file_hash: str
    content_hash: str
    written: bool
    short_id: str


class MarkdownStore:
    """统一管理 asset_store 目录的原子写入与读取。"""

    def __init__(self, asset_root: Path):
        self._root = Path(asset_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(
        self,
        *,
        bucket: str,
        content_id: str,
        title: str,
        published_at: str | None = None,
    ) -> Path:
        bucket_root = self._root / bucket
        bucket_root.mkdir(parents=True, exist_ok=True)
        date_part = self._date_part(published_at)
        year, month = date_part[0:4], date_part[4:6]
        sub = bucket_root / year / month
        sub.mkdir(parents=True, exist_ok=True)
        safe_title = self._safe_title(title)
        short = self._short_id_from_content_id(content_id)
        filename = f"{date_part}_{safe_title}_{short}.md"
        candidate = sub / filename
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = sub / f"{date_part}_{safe_title}_{short}_{suffix}.md"
        return candidate

    def write(
        self,
        *,
        bucket: str,
        content_id: str,
        content_type: str,
        title: str,
        author: str = "",
        canonical_url: str = "",
        published_at: str = "",
        body: str = "",
        extra_frontmatter: Mapping[str, Any] | None = None,
    ) -> MarkdownWriteResult:
        path = self.path_for(
            bucket=bucket,
            content_id=content_id,
            title=title,
            published_at=published_at or utc_now_iso(),
        )
        safe_path = resolve_within(path, [self._root])
        body_md = body.rstrip() + "\n" if body else "\n"
        frontmatter_lines = [
            "---",
            "schema_version: content-md/1.1",
            f"content_id: {content_id}",
            f"content_type: {content_type}",
            f"title: {self._escape_yaml(title)}",
            f"canonical_url: {self._escape_yaml(canonical_url)}",
            f"published_at: {self._escape_yaml(published_at)}",
            f"author: {self._escape_yaml(author)}",
        ]
        if extra_frontmatter:
            for key, value in extra_frontmatter.items():
                frontmatter_lines.append(f"{key}: {self._escape_yaml(str(value))}")
        frontmatter_lines.append("---")
        frontmatter_lines.append("")
        text = "\n".join(frontmatter_lines) + body_md
        bytes_payload = text.encode("utf-8")
        file_hash = hashlib.sha256(bytes_payload).hexdigest()
        content_hash = hashlib.sha256(body_md.strip().encode("utf-8")).hexdigest()
        written = self._atomic_write(safe_path, bytes_payload)
        return MarkdownWriteResult(
            md_path=str(safe_path),
            file_hash=file_hash,
            content_hash=content_hash,
            written=written,
            short_id=self._short_id_from_content_id(content_id),
        )

    def read(self, md_path: str) -> str:
        safe_path = resolve_within(Path(md_path), [self._root])
        return safe_path.read_text(encoding="utf-8")

    def exists(self, md_path: str) -> bool:
        try:
            return resolve_within(Path(md_path), [self._root]).exists()
        except ValueError:
            return False

    def list_by_bucket(self, bucket: str) -> list[Path]:
        bucket_root = self._root / bucket
        if not bucket_root.exists():
            return []
        return sorted([p for p in bucket_root.rglob("*.md") if p.is_file()])

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent), prefix=".tmp_", suffix=".md") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
        return not existed

    @staticmethod
    def _date_part(iso: str | None) -> str:
        if not iso:
            return datetime.now(timezone.utc).strftime("%Y%m%d")
        try:
            cleaned = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
            dt = datetime.fromisoformat(cleaned)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc).strftime("%Y%m%d")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y%m%d")

    @staticmethod
    def _safe_title(title: str) -> str:
        cleaned = SAFE_TITLE_RE.sub("_", (title or "").strip())
        cleaned = cleaned.strip("_")
        if not cleaned:
            cleaned = "untitled"
        if len(cleaned) > 60:
            cleaned = cleaned[:60].rstrip("_")
        return cleaned

    @staticmethod
    def _short_id_from_content_id(content_id: str) -> str:
        if not content_id:
            return "c0000"
        token = content_id.split("_", 1)[-1] if "_" in content_id else content_id
        return token[:4].upper() or "c0000"

    @staticmethod
    def _escape_yaml(value: str) -> str:
        if not value:
            return ""
        escaped = value.replace('"', '\\"')
        if any(ch in escaped for ch in (":", "#", "\n", "\r")) or escaped != escaped.strip():
            return f'"{escaped}"'
        return escaped
