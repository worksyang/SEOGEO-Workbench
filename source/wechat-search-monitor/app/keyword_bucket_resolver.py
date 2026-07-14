from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path


DEFAULT_BUCKET = "未分类"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config" / "keyword_buckets.txt"


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[·•|｜:：,，。！？!?\-—_（）()\[\]【】<>《》“”\"'‘’`×x]+", "", value)
    return value


@lru_cache(maxsize=1)
def _load_bucket_seed() -> tuple[list[str], dict[str, str]]:
    options: list[str] = []
    mapping: dict[str, str] = {}
    if not CONFIG_PATH.exists():
        return options, mapping

    current_bucket: str | None = None
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_bucket = line.lstrip("#").strip()
            if current_bucket and current_bucket not in options:
                options.append(current_bucket)
            continue
        if current_bucket:
            mapping[_normalize(line)] = current_bucket
    return options, mapping


def bucket_options() -> list[str]:
    options, _ = _load_bucket_seed()
    return [*options, DEFAULT_BUCKET]


def infer_keyword_bucket(keyword_text: str) -> str | None:
    _, mapping = _load_bucket_seed()
    return mapping.get(_normalize(keyword_text))
