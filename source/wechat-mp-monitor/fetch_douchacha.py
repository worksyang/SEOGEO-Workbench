#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取指定公众号 2025 年之后的所有文章，保存为 Markdown。
"""
import os
import re
import sys
import time
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_DIR, "SomeURL2MD"))

from werss_client import WeRSSClient
from wechat_to_markdown import WechatToMarkdownService

BASE_URL = "http://192.168.31.89:8001"
USERNAME = "admin"
PASSWORD = "admin@123"
OUTPUT_DIR = os.path.join(PROJECT_DIR, "抖查查")

# 目标公众号（系统 ID 和 fakeid 映射）
TARGET_MPS = {
    "MP_WXS_3930595446": "抖查查CEO波波",
    "MP_WXS_3255263756": "抖查查",
}

CUTOFF = "2025-01-01"


def safe_filename(title: str) -> str:
    """清理标题中的非法文件名字符。"""
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = re.sub(r"\s+", " ", title).strip()
    # 限制长度
    if len(title.encode("utf-8")) > 200:
        title = title[:100] + "..."
    return title


def extract_date(article: dict) -> str:
    """提取文章发布时间，返回 YYYY-MM-DD。"""
    publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
    if not publish_time:
        return ""
    try:
        if isinstance(publish_time, (int, float)):
            return datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
        return str(publish_time)[:10]
    except Exception:
        return ""


def extract_url(article: dict) -> str:
    """提取文章 URL。"""
    return (article.get("url") or article.get("link") or article.get("content_url") or "").strip()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client = WeRSSClient(base_url=BASE_URL, username=USERNAME, password=PASSWORD)
    if not client.token:
        print("登录 WeRSS 失败")
        return

    md_service = WechatToMarkdownService()

    all_articles = []

    # 1. 触发每个公众号的文章更新
    for mp_id, mp_name in TARGET_MPS.items():
        print(f"\n🔄 正在触发 {mp_name} 的文章更新...")
        # 尝试带 start_page/end_page 参数
        try:
            resp = client.session.get(
                f"{BASE_URL}/api/v1/wx/mps/update/{mp_id}",
                params={"start_page": 0, "end_page": 20},
            )
            print(f"   更新响应: {resp.status_code} - {resp.text[:200]}")
        except Exception as e:
            print(f"   更新出错: {e}")

    print("\n⏳ 等待 60 秒让爬虫完成抓取...")
    time.sleep(60)

    # 2. 获取所有文章
    for mp_id, mp_name in TARGET_MPS.items():
        print(f"\n📚 正在获取 {mp_name} 的文章列表...")
        articles = client.get_mp_articles(mp_id)
        print(f"   共获取到 {len(articles)} 篇文章")

        # 过滤 2025 年之后的文章
        for article in articles:
            pub_date = extract_date(article)
            if pub_date and pub_date >= CUTOFF:
                url = extract_url(article)
                if url:
                    all_articles.append({
                        "title": article.get("title", "无标题"),
                        "url": url,
                        "date": pub_date,
                        "mp_name": mp_name,
                    })

    print(f"\n📋 2025 年之后的文章共 {len(all_articles)} 篇")

    # 3. 去重（按 URL）
    seen_urls = set()
    unique_articles = []
    for art in all_articles:
        if art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            unique_articles.append(art)

    print(f"📋 去重后 {len(unique_articles)} 篇")

    # 4. 逐篇下载 Markdown
    success_count = 0
    fail_count = 0
    for i, art in enumerate(unique_articles, 1):
        safe_title = safe_filename(art["title"])
        filename = f"{art['date']}_{safe_title}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 800:
            print(f"  [{i}/{len(unique_articles)}] 已存在，跳过: {art['title'][:40]}")
            success_count += 1
            continue

        print(f"  [{i}/{len(unique_articles)}] 下载: {art['title'][:50]}")
        try:
            md_content, title = md_service.url_to_markdown_content(art["url"])
            if md_content and len(md_content) > 100:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(md_content)
                success_count += 1
                print(f"    ✅ 成功 ({len(md_content)} 字节)")
            else:
                print(f"    ⚠️ 内容为空或太短")
                fail_count += 1
        except Exception as e:
            print(f"    ❌ 下载失败: {e}")
            fail_count += 1

        if i < len(unique_articles):
            time.sleep(1)

    print(f"\n{'='*50}")
    print(f"下载完成！成功 {success_count} 篇，失败 {fail_count} 篇")
    print(f"输出目录: {OUTPUT_DIR}")

    # 5. 验证
    local_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")]
    print(f"本地文件数: {len(local_files)}")


if __name__ == "__main__":
    main()
