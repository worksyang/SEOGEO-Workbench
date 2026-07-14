#!/usr/bin/env python3
"""将统一关键词注册表的全部 keyword 合并进 kg_data.json。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_registry_repo import KeywordRegistryRepository

kg_path = ROOT / "kg_data.json"

with kg_path.open(encoding="utf-8") as f:
    kg = json.load(f)

kws = KeywordRegistryRepository(ROOT / "data/state/app.db").list_keywords()

existing_ids = {n["id"] for n in kg["nodes"]}
existing_names = {n["name"] for n in kg["nodes"]}

added = 0
for item in kws:
    text = item["keyword_text"].strip()
    if not text:
        continue
    # 用 keyword_id 作为 id，保证唯一
    kid = item["keyword_id"]
    if kid in existing_ids or text in existing_names:
        continue
    kg["nodes"].append({
        "id": kid,
        "name": text,
        "kind": "keyword",
        "sub": ""
    })
    existing_ids.add(kid)
    existing_names.add(text)
    added += 1

with kg_path.open("w", encoding="utf-8") as f:
    json.dump(kg, f, ensure_ascii=False, indent=2)

print(f"新增 {added} 个 keyword 节点，当前共 {len(kg['nodes'])} 个节点")
