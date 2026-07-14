from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.ingest.common import ISO_FMT, NORMALIZED_DIR, iter_snapshot_scan_paths, parse_captured_at

SNAPSHOT_REGISTRY_PATH = NORMALIZED_DIR / "snapshot_registry.json"


@dataclasses.dataclass
class ParsedHit:
    rank: int
    title_raw: str
    summary_raw: Optional[str]
    account_name_raw: str
    published_at_raw: Optional[str]
    url_raw: str


@dataclasses.dataclass
class ParsedSnapshot:
    keyword_text: str
    captured_at: datetime
    source_file_path: Path
    suggestions: list[str]
    related: list[str]
    hits: list[ParsedHit]


ARTICLE_HEAD_RE = re.compile(r"^(\d{1,3})\.\s+(.+?)\s*$")


def parse_search_markdown(path: Path) -> Optional[ParsedSnapshot]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[skip] read failed: {path}: {e}", file=sys.stderr)
        return None

    lines = text.splitlines()
    if not lines:
        return None

    first = lines[0].strip()
    if not first.startswith("# "):
        return None
    keyword_text = first[2:].strip()
    if not keyword_text or keyword_text.startswith("微信搜索监控报告") or keyword_text.startswith("友邦微信榜单"):
        return None

    captured_at: Optional[datetime] = None
    for ln in lines[1:8]:
        m = re.match(r"^-\s*时间[:：]\s*(.+?)\s*$", ln.strip())
        if m:
            captured_at = parse_captured_at(m.group(1))
            if captured_at:
                break
    if not captured_at:
        fn = path.stem
        m = re.match(r"^(\d{6})_(\d{6})_", fn)
        if m:
            ymd, hms = m.group(1), m.group(2)
            try:
                captured_at = datetime.strptime(f"20{ymd}{hms}", "%Y%m%d%H%M%S")
            except ValueError:
                captured_at = None
        if not captured_at:
            m = re.match(r"^(\d{6})_", fn)
            if m:
                ymd = m.group(1)
                try:
                    captured_at = datetime.strptime(f"20{ymd}080000", "%Y%m%d%H%M%S")
                except ValueError:
                    pass
    if not captured_at:
        return None

    if "#### 文章列表" not in text:
        return None

    suggestions = _extract_term_list(text, "#### 搜索下拉词")
    related = _extract_term_list(text, "#### 相关搜索")
    hits = _extract_hits(text)
    if not hits:
        return None

    return ParsedSnapshot(
        keyword_text=keyword_text,
        captured_at=captured_at,
        source_file_path=path,
        suggestions=suggestions,
        related=related,
        hits=hits,
    )


def _extract_term_list(text: str, header: str) -> list[str]:
    out: list[str] = []
    lines = text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].strip() == header:
            i += 1
            while i < n:
                s = lines[i].rstrip()
                if s.startswith("####") or s.startswith("##") or s.startswith("# "):
                    return out
                m = re.match(r"^-\s+(.+?)\s*$", s)
                if m:
                    out.append(m.group(1).strip())
                i += 1
            return out
        i += 1
    return out


def _extract_hits(text: str) -> list[ParsedHit]:
    hits: list[ParsedHit] = []
    lines = text.splitlines()
    n = len(lines)

    start = -1
    for i, ln in enumerate(lines):
        if ln.strip() == "#### 文章列表":
            start = i + 1
            break
    if start < 0:
        return hits

    end = n
    for i in range(start, n):
        s = lines[i].rstrip()
        if s.startswith("####") or s.startswith("## "):
            end = i
            break

    cur: Optional[dict] = None
    blocks: list[dict] = []
    for i in range(start, end):
        s = lines[i]
        m = ARTICLE_HEAD_RE.match(s.strip())
        if m and not s.startswith(" "):
            if cur:
                blocks.append(cur)
            cur = {
                "rank": int(m.group(1)),
                "title": m.group(2).strip(),
                "lines": [],
            }
        else:
            if cur is not None:
                cur["lines"].append(s)
    if cur:
        blocks.append(cur)

    for blk in blocks:
        hit = _block_to_hit(blk)
        if hit:
            hits.append(hit)

    return hits


def _block_to_hit(blk: dict) -> Optional[ParsedHit]:
    rank = blk["rank"]
    title = blk["title"]
    summary = None
    account = None
    pub = None
    url = None

    for raw in blk["lines"]:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("文章简介："):
            summary = s[len("文章简介："):].strip()
        elif s.startswith("公众号："):
            account = s[len("公众号："):].strip()
        elif s.startswith("时间："):
            pub = s[len("时间："):].strip()
        elif s.startswith("http"):
            url = s.strip()

    if not (title and account):
        return None

    if not url:
        url = f"placeholder://{account}/{title}"

    return ParsedHit(
        rank=rank,
        title_raw=title,
        summary_raw=summary,
        account_name_raw=account,
        published_at_raw=pub,
        url_raw=url,
    )


def _load_registry() -> dict:
    try:
        if SNAPSHOT_REGISTRY_PATH.exists():
            return json.loads(SNAPSHOT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_registry(registry: dict) -> None:
    try:
        SNAPSHOT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_REGISTRY_PATH.write_text(
            json.dumps(registry, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass


def _snap_to_entry(snap: ParsedSnapshot) -> dict:
    return {
        "keyword_text": snap.keyword_text,
        "captured_at": snap.captured_at.strftime(ISO_FMT),
        "source_file_path": str(snap.source_file_path),
        "suggestions": snap.suggestions,
        "related": snap.related,
        "hits": [
            {
                "rank": h.rank,
                "title_raw": h.title_raw,
                "summary_raw": h.summary_raw,
                "account_name_raw": h.account_name_raw,
                "published_at_raw": h.published_at_raw,
                "url_raw": h.url_raw,
            }
            for h in snap.hits
        ],
    }


def _entry_to_snap(entry: dict) -> ParsedSnapshot:
    return ParsedSnapshot(
        keyword_text=entry["keyword_text"],
        captured_at=datetime.strptime(entry["captured_at"], ISO_FMT),
        source_file_path=Path(entry["source_file_path"]),
        suggestions=entry.get("suggestions", []),
        related=entry.get("related", []),
        hits=[
            ParsedHit(
                rank=h["rank"],
                title_raw=h["title_raw"],
                summary_raw=h.get("summary_raw"),
                account_name_raw=h["account_name_raw"],
                published_at_raw=h.get("published_at_raw"),
                url_raw=h["url_raw"],
            )
            for h in entry.get("hits", [])
        ],
    )


def discover_snapshots(incremental: bool = True) -> list[ParsedSnapshot]:
    registry = _load_registry() if incremental else {}
    updated = False
    seen: dict[tuple[str, str], ParsedSnapshot] = {}

    for path in iter_snapshot_scan_paths():
        path_key = str(path)
        mtime = path.stat().st_mtime
        cached = registry.get(path_key)
        if incremental and cached and cached.get("mtime") == mtime:
            snap = _entry_to_snap(cached)
        else:
            snap = parse_search_markdown(path)
            if snap:
                registry[path_key] = {"mtime": mtime, **_snap_to_entry(snap)}
            else:
                registry.pop(path_key, None)
            updated = True

        if not snap:
            continue
        key = (snap.keyword_text, snap.captured_at.strftime(ISO_FMT))
        if key not in seen:
            seen[key] = snap

    if updated:
        _save_registry(registry)

    return list(seen.values())
