from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
NORMALIZED_DIR = PROJECT_ROOT / "normalized"

SCAN_TARGETS = [
    (PROJECT_ROOT / "微信搜索结果", True),
    (PROJECT_ROOT, False),
]

ARTICLE_CONTENT_TARGETS = [
    (PROJECT_ROOT, False),
    (PROJECT_ROOT / "补全文Markdown", True),
    (PROJECT_ROOT / "微信搜索结果", True),
]

SKIP_FILENAME_PREFIXES_AS_CONTENT = {
    "260606_push_content",
    "260606_report",
}

CONTENT_BLACKLIST_KEYWORDS = (
    "搜索结果_",
    "_wechat-ybxhyyh-top3",
    "_wechat-cfyh-top10",
    "_财富盈活-top10对比",
    "_push",
    "_report",
)

ISO_FMT = "%Y-%m-%dT%H:%M:%S"
TZ = "Asia/Shanghai"


def iter_markdown_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(p for p in root.rglob("*.md") if p.is_file())
    return sorted(p for p in root.glob("*.md") if p.is_file())


def iter_snapshot_scan_paths() -> list[Path]:
    paths: list[Path] = []
    for root, recursive in SCAN_TARGETS:
        if not root.exists():
            continue
        paths.extend(iter_markdown_files(root, recursive))
    return paths


def iter_article_content_paths() -> list[Path]:
    paths: list[Path] = []
    for root, recursive in ARTICLE_CONTENT_TARGETS:
        if not root.exists():
            continue
        paths.extend(iter_markdown_files(root, recursive))
    return paths


def md5_short(s: str, n: int = 8) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:n]


def kw_id(text: str) -> str:
    return f"kw_{md5_short(text)}"


def acct_id(name: str) -> str:
    return f"acct_{md5_short(name)}"


def art_id(stable_key: str) -> str:
    return f"art_{md5_short(stable_key, 12)}"


def snap_id(keyword_id: str, captured_at: datetime) -> str:
    return f"snap_{keyword_id}_{captured_at.strftime('%Y%m%d%H%M%S')}"


def now_iso() -> str:
    return datetime.now().strftime(ISO_FMT)


def to_iso(dt: datetime) -> str:
    return dt.strftime(ISO_FMT)


def project_display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    if raw.startswith("placeholder://"):
        return raw
    try:
        u = urlparse(raw)
    except Exception:
        return raw
    if u.netloc != "mp.weixin.qq.com":
        return f"{u.scheme}://{u.netloc}{u.path}"
    path = u.path
    if path.startswith("/s/"):
        seg = path.split("/")[2] if len(path.split("/")) > 2 else ""
        if seg:
            return f"https://mp.weixin.qq.com/s/{seg}"
    return f"https://mp.weixin.qq.com{path}"


def is_placeholder_url(raw: str) -> bool:
    return raw.strip().startswith("placeholder://")


def normalize_title_key(raw: str) -> str:
    raw = unicodedata.normalize("NFKC", (raw or "").strip()).lower()
    raw = re.sub(r"\s+", "", raw)
    raw = re.sub(r"[·•|｜:：,，。！？!?\-—_（）()\[\]【】<>《》“”\"'‘’`]+", "", raw)
    return raw


def article_identity_key(account_name: str, title: str) -> str:
    return f"{normalize_title_key(account_name)}::{normalize_title_key(title)}"


def strip_date_prefix(name: str) -> str:
    return re.sub(r"^\d{6}(?:_\d{6})?_", "", name)


def parse_captured_at(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H-%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_published_at(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_captured_at_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO_FMT)


def infer_trigger_type(captured_at: datetime) -> str:
    minute_of_day = captured_at.hour * 60 + captured_at.minute
    target = 8 * 60
    return "scheduled" if abs(minute_of_day - target) <= 90 else "manual"
