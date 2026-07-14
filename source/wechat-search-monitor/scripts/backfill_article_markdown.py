#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingest.common import normalize_url, normalize_title_key, project_display_path

ARTICLES_JSON = PROJECT_ROOT / "normalized" / "articles.json"
ACCOUNTS_JSON = PROJECT_ROOT / "normalized" / "accounts.json"
OUTPUT_DIR = PROJECT_ROOT / "补全文Markdown"
VISION_SCRIPT = Path("/Users/works14/.claude/skills/zk-vision-workflow/scripts/markdown_image_vision.py")
WERSS_URL = "http://192.168.31.89:8001"
WERSS_USER = "admin"
WERSS_PASS = "admin@123"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_account_name_map() -> dict[str, str]:
    accounts = load_json(ACCOUNTS_JSON)
    return {item["account_id"]: item["canonical_name"] for item in accounts}


def build_title_to_article_map() -> dict[str, dict]:
    articles = load_json(ARTICLES_JSON)
    out: dict[str, dict] = {}
    for item in articles:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        key = normalize_title_key(title)
        out.setdefault(key, item)
    return out


def iter_existing_mps() -> list[dict]:
    token = _login()
    articles = []
    offset = 0
    limit = 100
    while True:
        url = f"{WERSS_URL}/api/v1/wx/mps?limit={limit}&offset={offset}"
        res = _get(url, token)
        items = res.get("data", {}).get("list", [])
        if not items:
            break
        articles.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return articles


def iter_mp_articles(mp_id: str) -> list[dict]:
    token = _login()
    articles = []
    offset = 0
    limit = 100
    while True:
        url = f"{WERSS_URL}/api/v1/wx/articles?mp_id={mp_id}&limit={limit}&offset={offset}"
        res = _get(url, token)
        items = res.get("data", {}).get("list", [])
        if not items:
            break
        articles.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return articles


def find_mp_id_by_name(name: str) -> str | None:
    for mp in iter_existing_mps():
        if mp.get("mp_name") == name:
            return mp["id"]
    return None


# ----- HTTP helpers -----

def _login() -> str:
    import urllib.request
    data = f"username={WERSS_USER}&password={WERSS_PASS}".encode()
    req = urllib.request.Request(f"{WERSS_URL}/api/v1/wx/auth/login", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    return body["data"]["access_token"]


def _get(url: str, token: str) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ----- Markdown helpers -----

def ensure_link_header(md_path: Path, url: str) -> None:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = f"链接：{normalize_url(url)}"
    if any(line.strip() == header for line in lines[:8]):
        return

    if lines and lines[0].startswith("# "):
        new_lines = [lines[0], "", header]
        if len(lines) > 1:
            new_lines.extend([""] + lines[1:])
    else:
        new_lines = [header, ""] + lines

    md_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def parse_saved_path(output: str) -> Path | None:
    for line in output.splitlines():
        if line.startswith("保存路径:"):
            return Path(line.split(":", 1)[1].strip())
    return None


def convert_one(url: str) -> Path:
    cmd = [
        "python3",
        str(VISION_SCRIPT),
        "wechat",
        url,
        "--output-dir",
        str(OUTPUT_DIR),
        "--no-annotate-images",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(output.strip() or f"convert failed: {url}")
    if "错误：" in output:
        raise RuntimeError(output.strip())
    saved = parse_saved_path(output)
    if not saved:
        raise RuntimeError(f"missing saved path in output: {output.strip()}")
    return saved


def convert_articles(articles: Iterable[dict], limit: int | None = None) -> list[dict]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done = []
    for idx, article in enumerate(articles, start=1):
        if limit is not None and idx > limit:
            break
        url = article.get("_real_url") or article["raw_url"]
        md_path = convert_one(url)
        ensure_link_header(md_path, url)
        done.append({
            "article_id": article["article_id"],
            "title": article["title"],
            "url": url,
            "path": project_display_path(md_path),
        })
        print(f"[ok] {article['title']} -> {md_path}")
    return done


# ----- main pipeline -----

def match_missing_from_mp(mp_id: str, account_name: str) -> list[dict]:
    mp_articles = iter_mp_articles(mp_id)
    existing_by_title = build_title_to_article_map()
    matched = []
    for mp_a in mp_articles:
        key = normalize_title_key(mp_a.get("title") or "")
        if key not in existing_by_title:
            continue
        item = existing_by_title[key]
        if item.get("content_status") == "available" and item.get("content_file_path"):
            continue
        raw_url = str(item.get("raw_url") or "").strip()
        if not raw_url or raw_url.startswith("placeholder://"):
            item["_real_url"] = mp_a.get("url") or raw_url
        else:
            item["_real_url"] = raw_url
        matched.append(item)
    return matched


def main() -> int:
    parser = argparse.ArgumentParser(description="批量补齐缺失正文 Markdown")
    parser.add_argument("--account", required=True, help="公众号名称，例如：维港保典")
    parser.add_argument("--limit", type=int, default=None, help="最多补齐多少篇")
    args = parser.parse_args()

    mp_id = find_mp_id_by_name(args.account)
    if not mp_id:
        print(f"[error] 在 we-mp-rss 中找不到公众号: {args.account}")
        return 1

    targets = match_missing_from_mp(mp_id, args.account)
    print(f"[info] account={args.account} mp_id={mp_id} matched_missing={len(targets)}")
    if not targets:
        return 0
    done = convert_articles(targets, limit=args.limit)
    print(f"[ok] converted {len(done)} article(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
