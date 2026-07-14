from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.ingest.common import (
    CONTENT_BLACKLIST_KEYWORDS,
    iter_article_content_paths,
    normalize_title_key,
    normalize_url,
    strip_date_prefix,
)


@dataclasses.dataclass
class ArticleContentMeta:
    path: Path
    normalized_url: Optional[str]
    title_key: str
    filename_key: str
    mtime: float


def _read_head(path: Path, n: int = 40) -> list[str]:
    lines: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                lines.append(line.rstrip("\n"))
                if len(lines) >= n:
                    break
    except Exception:
        pass
    return lines


def _is_likely_article_content_head(path: Path, lines: list[str]) -> bool:
    name = path.name
    if path.suffix.lower() != ".md":
        return False
    for bl in CONTENT_BLACKLIST_KEYWORDS:
        if bl in name:
            return False
    if name.startswith("260606_push_content") or name.startswith("260606_report"):
        return False
    if not lines:
        return False
    first = lines[0].strip()
    if first.startswith("# 微信搜索监控报告") or first.startswith("## 友邦微信榜单"):
        return False
    head = "\n".join(lines)
    if "#### 文章列表" in head and re.search(r"^-\s*时间[:：]\s*\d{4}-\d{2}-\d{2}\s+\d{2}[-:]\d{2}", head, re.M):
        return False
    return True


def extract_content_meta(path: Path, head_lines: Optional[list[str]] = None) -> Optional[ArticleContentMeta]:
    lines = head_lines if head_lines is not None else _read_head(path, 40)
    if not lines:
        return None
    first = lines[0].strip()
    title = first[2:].strip() if first.startswith("# ") else strip_date_prefix(path.stem)
    title_key = normalize_title_key(title)
    filename_key = normalize_title_key(strip_date_prefix(path.stem))
    normalized_link = None
    for ln in lines[:12]:
        m = re.match(r"^链接[:：]\s*(https?://\S+)\s*$", ln.strip())
        if m:
            normalized_link = normalize_url(m.group(1))
            break
    return ArticleContentMeta(
        path=path.resolve(),
        normalized_url=normalized_link,
        title_key=title_key,
        filename_key=filename_key,
        mtime=path.stat().st_mtime,
    )


def index_article_files() -> dict[str, dict]:
    seen: set[Path] = set()
    candidates: list[tuple[Path, list[str]]] = []
    for p in iter_article_content_paths():
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        head = _read_head(rp, 40)
        if _is_likely_article_content_head(rp, head):
            candidates.append((rp, head))

    metas = []
    for p, head in sorted(candidates, key=lambda x: x[0]):
        meta = extract_content_meta(p, head_lines=head)
        if meta:
            metas.append(meta)

    by_url: dict[str, ArticleContentMeta] = {}
    by_title: dict[str, list[ArticleContentMeta]] = defaultdict(list)
    by_filename: dict[str, list[ArticleContentMeta]] = defaultdict(list)
    for meta in metas:
        if meta.normalized_url:
            cur = by_url.get(meta.normalized_url)
            if cur is None or meta.mtime > cur.mtime:
                by_url[meta.normalized_url] = meta
        if meta.title_key:
            by_title[meta.title_key].append(meta)
        if meta.filename_key:
            by_filename[meta.filename_key].append(meta)

    for bucket in list(by_title.values()) + list(by_filename.values()):
        bucket.sort(key=lambda m: m.mtime, reverse=True)

    return {
        "by_url": by_url,
        "by_title": by_title,
        "by_filename": by_filename,
    }
