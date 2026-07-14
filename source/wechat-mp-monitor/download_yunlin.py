#!/usr/bin/env python3
"""批量下载云林学社所有文章并保存为 Markdown 格式"""

import json
import os
import re
import sys
import time
from datetime import datetime

import markdownify
import requests

# 配置
BASE_URL = "http://192.168.31.89:8001"
MP_ID = "MP_WXS_3937941902"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "云林学社")

# 登录获取 Token
session = requests.Session()
resp = session.post(
    f"{BASE_URL}/api/v1/wx/auth/token",
    data={"username": "admin", "password": "admin@123", "grant_type": "password"},
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    timeout=10,
)
token = resp.json()["access_token"]
session.headers.update({"Authorization": f"Bearer {token}"})
print("登录成功", flush=True)

# 获取所有文章列表
all_articles = []
offset = 0
limit = 100
while True:
    resp = session.get(
        f"{BASE_URL}/api/v1/wx/articles",
        params={"limit": limit, "offset": offset, "mp_id": MP_ID},
        timeout=30,
    )
    data = resp.json().get("data", {})
    articles = data.get("list", [])
    total = data.get("total", 0)
    all_articles.extend(articles)
    print(f"已获取 {len(all_articles)}/{total} 篇文章列表", flush=True)
    if len(all_articles) >= total or len(articles) < limit:
        break
    offset += limit

print(f"共获取 {len(all_articles)} 篇文章列表", flush=True)

# 按 publish_time 排序（最早的在前）
all_articles.sort(key=lambda a: a.get("publish_time", 0))


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80]


def html_to_markdown(html_content):
    """将 HTML 转换为 Markdown"""
    if not html_content:
        return ""
    md = markdownify.markdownify(
        html_content,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


os.makedirs(OUTPUT_DIR, exist_ok=True)

# 逐个下载文章内容（不带 content=true，直接读取数据库中的已有内容）
success_count = 0
fail_count = 0
no_content_count = 0

for i, article in enumerate(all_articles, 1):
    article_id = article.get("id", "")
    title = article.get("title", "无标题")
    url = article.get("url", "")
    description = article.get("description", "")
    publish_time = article.get("publish_time", 0)

    print(f"[{i}/{len(all_articles)}] {title[:50]}... ", end="", flush=True)

    try:
        # 获取文章详情（不加 content 参数，直接读取已有内容）
        resp = session.get(
            f"{BASE_URL}/api/v1/wx/articles/{article_id}",
            timeout=60,
        )
        detail = resp.json().get("data", {})
        content_html = detail.get("content", "")
        content_md = html_to_markdown(content_html)

        if not content_md or len(content_md) < 10:
            # 内容为空时用 description
            content_md = description or "（文章内容为空）"
            no_content_count += 1

        # 格式化日期
        if isinstance(publish_time, (int, float)):
            date_str = datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
        else:
            date_str = str(publish_time)[:10]

        # 构建 Markdown 内容
        md_content = f"""# {title}

> 来源：云林学社
> 日期：{date_str}
> 链接：{url}

{content_md}
"""

        # 保存文件
        safe_title = sanitize_filename(title)
        filename = f"{date_str}_{safe_title}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)

        success_count += 1
        print(f"OK ({len(content_md)} 字)", flush=True)

    except Exception as e:
        fail_count += 1
        print(f"FAIL: {e}", flush=True)

print(flush=True)
print(f"下载完成！成功 {success_count} 篇，失败 {fail_count} 篇，无正文 {no_content_count} 篇", flush=True)
print(f"文件保存位置：{OUTPUT_DIR}", flush=True)
