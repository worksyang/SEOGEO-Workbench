from __future__ import annotations

import re

import requests


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mp.weixin.qq.com/",
}

_HEADIMG_PATTERNS = [
    re.compile(r'hd_head_img[\'"\s:=]+([^\'\"<>\s,]+)'),
    re.compile(r'headimg\s*=\s*"([^"]+)"'),
    re.compile(r'headimgurl\s*=\s*"([^"]+)"'),
]

_MSG_CDN_PATTERNS = [
    re.compile(r"var\s+msg_cdn_url\s*=\s*\"([^\"]+)\""),
    re.compile(r"var\s+msg_cdn_url\s*=\s*'([^']+)'") ,
]
_OG_IMAGE_PATTERNS = [
    re.compile(r'<meta\s+property=\"og:image\"\s+content=\"([^\"]+)\"'),
    re.compile(r"<meta\s+property='og:image'\s+content='([^']+)'") ,
]


def extract_account_headimg_from_html(html: str) -> str | None:
    for pattern in _HEADIMG_PATTERNS:
        matched = pattern.search(html)
        if matched:
            return matched.group(1).strip().strip("'\"")
    return None


def extract_cover_url_from_html(html: str) -> str | None:
    for pattern in _MSG_CDN_PATTERNS:
        matched = pattern.search(html)
        if matched:
            return matched.group(1).strip()

    for pattern in _OG_IMAGE_PATTERNS:
        matched = pattern.search(html)
        if matched:
            return matched.group(1).strip()

    return None


def fetch_article_html(url: str, timeout: int = 15) -> str:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=(5, timeout),
        allow_redirects=True,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def extract_cover_url(url: str, timeout: int = 15) -> str | None:
    html = fetch_article_html(url=url, timeout=timeout)
    return extract_cover_url_from_html(html)