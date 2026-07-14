#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取公众号「大湾通一峰火燎源」自 2025-10-01 起的所有文章并保存为 Markdown。
"""
import os
import re
import sys
import time
import json
import argparse
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_DIR, "SomeURL2MD"))

from werss_client import WeRSSClient
from wechat_to_markdown import WechatToMarkdownService

BASE_URL = "http://192.168.31.89:8001"
USERNAME = "admin"
PASSWORD = "admin@123"
MP_ID = "MP_WXS_3921283819"
MP_NAME = "大湾通一峰火燎源"
OUTPUT_DIR = os.path.join(PROJECT_DIR, MP_NAME)
LINKS_FILE = os.path.join(PROJECT_DIR, "大湾通一峰火燎源_links.json")
CUTOFF = "2025-10-01"


def safe_filename(title: str) -> str:
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title.encode("utf-8")) > 220:
        title = title[:100] + "..."
    return title


def extract_date(article: dict) -> str:
    publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
    if not publish_time:
        return ""
    if isinstance(publish_time, (int, float)):
        return datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
    return str(publish_time)[:10]


def extract_url(article: dict) -> str:
    return (article.get("url") or article.get("link") or article.get("content_url") or "").strip()


def build_markdown(title: str, date_str: str, url: str, markdown_content: str) -> str:
    lines = markdown_content.splitlines()
    if lines and lines[0].startswith("# "):
        markdown_content = "\n".join(lines[1:]).strip()
    header = f"# {title}\n\n> 来源：{MP_NAME}\n> 日期：{date_str}\n> 链接：{url}\n\n"
    return header + markdown_content + "\n"


def fetch_target_articles() -> list[dict]:
    client = WeRSSClient(base_url=BASE_URL, username=USERNAME, password=PASSWORD)
    if not client.token:
        raise RuntimeError("登录 WeRSS 失败")

    print(f"开始触发 {MP_NAME} 历史抓取...")
    resp = client.session.get(
        f"{BASE_URL}/api/v1/wx/mps/update/{MP_ID}",
        params={"start_page": 0, "end_page": 20},
        timeout=60,
    )
    print(f"更新响应: {resp.status_code}")

    articles = client.get_mp_articles(MP_ID)
    print(f"共获取文章 {len(articles)} 篇")

    target_articles = []
    seen_urls = set()
    for article in articles:
        date_str = extract_date(article)
        url = extract_url(article)
        if not date_str or not url or date_str < CUTOFF or url in seen_urls:
            continue
        seen_urls.add(url)
        target_articles.append(
            {
                "title": article.get("title", "无标题"),
                "date": date_str,
                "url": url,
            }
        )

    print(f"{CUTOFF} 之后文章 {len(target_articles)} 篇")
    with open(LINKS_FILE, "w", encoding="utf-8") as file:
        json.dump(target_articles, file, ensure_ascii=False, indent=2)
    print(f"链接清单已保存：{LINKS_FILE}")
    return target_articles


def load_target_articles(refresh_links: bool) -> list[dict]:
    if refresh_links or not os.path.exists(LINKS_FILE):
        return fetch_target_articles()
    with open(LINKS_FILE, "r", encoding="utf-8") as file:
        target_articles = json.load(file)
    print(f"从本地链接清单读取 {len(target_articles)} 篇文章：{LINKS_FILE}")
    return target_articles


def convert_articles(target_articles: list[dict]) -> tuple[int, int]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    md_service = WechatToMarkdownService()
    success_count = 0
    fail_count = 0

    for index, article in enumerate(target_articles, 1):
        filename = f"{article['date']}_{safe_filename(article['title'])}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            print(f"[{index}/{len(target_articles)}] 已存在，跳过：{article['title'][:40]}")
            success_count += 1
            continue

        print(f"[{index}/{len(target_articles)}] 正在抓取：{article['title'][:40]}")
        try:
            last_error = None
            for attempt in range(3):
                try:
                    markdown_content, _ = md_service.url_to_markdown_content(article["url"])
                    if not markdown_content or not markdown_content.strip():
                        raise ValueError("返回内容为空")
                    final_content = build_markdown(article["title"], article["date"], article["url"], markdown_content)
                    with open(filepath, "w", encoding="utf-8") as file:
                        file.write(final_content)
                    success_count += 1
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        print(f"    第 {attempt + 1} 次失败，1 秒后重试：{exc}")
                        time.sleep(1)
            if last_error is not None:
                raise last_error
        except Exception as exc:
            fail_count += 1
            print(f"    失败: {exc}")

        if index < len(target_articles):
            time.sleep(1)

    return success_count, fail_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-links",
        action="store_true",
        help="从 WeRSS 重新拉取文章链接清单，并写入本地 JSON。",
    )
    args = parser.parse_args()

    target_articles = load_target_articles(refresh_links=args.refresh_links)
    success_count, fail_count = convert_articles(target_articles)

    print("=" * 50)
    print(f"完成：成功 {success_count} 篇，失败 {fail_count} 篇")
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
