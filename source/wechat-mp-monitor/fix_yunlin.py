#!/usr/bin/env python3
"""补全云林学社缺少正文的文章：直接通过 URL 抓取微信文章转为 Markdown"""

import json
import os
import re
import sys
import time
import requests

# 把 SomeURL2MD 加入路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "SomeURL2MD"))
from wechat_to_markdown import WechatToMarkdownService

BASE_URL = "http://192.168.31.89:8001"
MP_ID = "MP_WXS_3937941902"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "云林学社")

# 登录
session = requests.Session()
resp = session.post(
    f"{BASE_URL}/api/v1/wx/auth/token",
    data={"username": "admin", "password": "admin@123", "grant_type": "password"},
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    timeout=10,
)
session.headers.update({"Authorization": f"Bearer {resp.json()['access_token']}"})
print("登录成功", flush=True)

# 获取所有文章
all_articles = []
for offset in [0, 100]:
    resp = session.get(
        f"{BASE_URL}/api/v1/wx/articles",
        params={"limit": 100, "offset": offset, "mp_id": MP_ID},
        timeout=30,
    )
    all_articles.extend(resp.json().get("data", {}).get("list", []))
print(f"共 {len(all_articles)} 篇文章", flush=True)

# 筛选本地文件小于 500 字节的（即缺少正文的文章）
service = WechatToMarkdownService()
os.makedirs(OUTPUT_DIR, exist_ok=True)

need_fix = []
for a in all_articles:
    # 找到对应文件
    title = a.get("title", "无标题")
    url = a.get("url", "")
    if not url:
        continue

    from datetime import datetime
    pt = a.get("publish_time", 0)
    date_str = datetime.fromtimestamp(pt).strftime("%Y-%m-%d") if isinstance(pt, (int, float)) else str(pt)[:10]
    safe_title = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip()[:80]
    filename = f"{date_str}_{safe_title}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)

    # 如果文件不存在或小于 800 字节，需要重新抓取
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 800:
        need_fix.append((a, filepath, url, title, date_str))

print(f"需要补全: {len(need_fix)} 篇", flush=True)

# 逐个通过 URL 直接抓取
success = 0
fail = 0
for i, (a, filepath, url, title, date_str) in enumerate(need_fix, 1):
    print(f"[{i}/{len(need_fix)}] {title[:40]}... ", end="", flush=True)
    try:
        md_content, article_title = service.url_to_markdown_content(url)
        if len(md_content) < 100:
            raise Exception("抓取内容过短")

        # 加上元信息头
        header = f"""# {title}

> 来源：云林学社
> 日期：{date_str}
> 链接：{url}

"""
        # 去掉转换结果里自带的标题行（第一行的 # xxx）
        lines = md_content.split("\n")
        if lines and lines[0].startswith("# "):
            md_body = "\n".join(lines[1:])
        else:
            md_body = md_content

        full_content = header + md_body.strip() + "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_content)

        success += 1
        print(f"OK ({len(full_content)} 字节)", flush=True)
    except Exception as e:
        fail += 1
        print(f"FAIL: {e}", flush=True)

    time.sleep(1)  # 避免请求过快

print(f"\n补全完成！成功 {success}，失败 {fail}", flush=True)
