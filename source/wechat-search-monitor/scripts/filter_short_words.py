#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_registry_repo import KeywordRegistryRepository

all_keywords = [
    {"id": kw["keyword_id"], "text": kw["keyword_text"]}
    for kw in KeywordRegistryRepository(
        ROOT / "data/state/app.db"
    ).list_keywords(include_archived=False)
]

print(f"总监控词数: {len(all_keywords)}")

# 读取已有 WSO 数据
try:
    with open("normalized/aidso_wso_heat.json") as f:
        wso_data = json.load(f)
    fetched_keywords = set()
    for item in wso_data.get("items", []):
        if item.get("fetched_at") and not item.get("error") == "no_data":
            fetched_keywords.add(item["keyword_text"])
    print(f"已抓取词数: {len(fetched_keywords)}")
except FileNotFoundError:
    fetched_keywords = set()
    print("WSO 数据文件不存在")

# 已搜的 26 个主词（从上下文推断）
searched_main = [
    "友邦环宇盈活", "友邦财富盈活", "安盛盛利2", "保诚信守明天", "富卫盈聚天下2",
    "永明星河尊享2", "匠心传承2", "匠心飞越", "国寿傲珑盛世", "友邦 财富盈活",
    "周大福匠心传承 2", "周大福匠心飞越", "安盛盛利 2", "财富盈活 保险", "鑫安逸",
    "保诚骏誉财富", "太保世代悦享3", "万通富饶千秋", "太保鑫安逸", "国寿智裕世代",
    "香港友邦", "香港安盛", "香港保诚", "香港宏利", "香港富卫", "香港永明"
]

# 筛选 2~6 字短词（放宽限制）
short_words = []
for kw in all_keywords:
    text = kw["text"].replace(" ", "")
    # 纯中文数字计算
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
    digit_chars = re.findall(r'\d', text)
    total_len = len(chinese_chars) + len(digit_chars)
    
    if total_len < 2 or total_len > 6:
        continue
    if kw["text"] in searched_main:
        continue
    if kw["text"] in fetched_keywords:
        continue
    short_words.append(kw)

print(f"2~6字短词数（排除已搜）: {len(short_words)}")
for i, kw in enumerate(short_words, 1):
    print(f"{i}. {kw['text']}")
