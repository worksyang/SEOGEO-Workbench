from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.ingest.common import is_placeholder_url, normalize_title_key, normalize_url
from app.ingest.parsers.search_md_parser import ParsedHit


def find_content_for_hit(hit: ParsedHit, index: dict[str, dict]) -> Optional[Path]:
    if hit.url_raw and not is_placeholder_url(hit.url_raw):
        meta = index["by_url"].get(normalize_url(hit.url_raw))
        if meta:
            return meta.path

    title_key = normalize_title_key(hit.title_raw)
    if not title_key:
        return None

    for bucket_name in ("by_title", "by_filename"):
        bucket = index[bucket_name].get(title_key, [])
        if bucket:
            return bucket[0].path
    return None

