#!/usr/bin/env python3
"""列出账号下所有群，搜索含'佳佳'的群。"""
import json, urllib.request, urllib.parse, urllib.error

BASE_URL = "https://user.ifangzhou.com"
TOKEN = "b737429f81cb4f53902cc4a7aff16bf6"

def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "admin-token": TOKEN,
        "Content-Type": "application/json;charset=UTF-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()}

# Step 1: 获取微信账号列表
print("=== 获取微信账号列表 ===")
data = api_get("/wp/api/v1/wechatAccounts", {"pageIndex": 0, "pageSize": 50})
accounts = data if isinstance(data, list) else data.get("data", data.get("results", []))
if isinstance(accounts, dict):
    accounts = accounts.get("data", accounts.get("results", []))
print(f"账号数: {len(accounts)}")
for a in accounts:
    print(f"  id={a.get('id')} wechatId={a.get('wechatId')} nickname={a.get('nickname')}")

if not accounts:
    print("ERROR: 没有账号！"); exit(1)

account_id = accounts[0]["id"]
print(f"\n使用账号 id={account_id}")

# Step 2: 拉取所有群（翻页，withMembers=true 搜成员）
print("\n=== 拉取所有群（翻页） ===")
all_groups = []
page = 0
page_size = 50
while True:
    params = {"pageIndex": page, "pageSize": page_size}
    data = api_get(f"/wp/api/v1/wechatAccounts/{account_id}/chatrooms", params)
    if isinstance(data, dict) and data.get("_http_error"):
        print(f"HTTP {data['_http_error']}: {data.get('_body','')[:200]}")
        break
    groups = data if isinstance(data, list) else data.get("data", data.get("results", []))
    if not groups:
        break
    all_groups.extend(groups)
    print(f"  page {page}: +{len(groups)}  (累计 {len(all_groups)})")
    if len(groups) < page_size:
        break
    page += 1

print(f"\n总共 {len(all_groups)} 个群")

# Step 3: 在群名、群主名、成员名中搜"佳佳"
print("\n=== 搜索'佳佳' ===")
hits = []
for g in all_groups:
    name = g.get("nickname", "")
    owner = g.get("owner") or {}
    owner_name = owner.get("nickname", "")
    member_hits = []
    for m in (g.get("members") or []):
        mn = m.get("nickname", "")
        if "佳佳" in mn:
            member_hits.append(mn)
    if "佳佳" in name or "佳佳" in owner_name or member_hits:
        hits.append({
            "db_id": g.get("id"),
            "nickname": name,
            "chatroomId": g.get("chatroomId"),
            "wechatAccountId": g.get("wechatAccountId"),
            "owner": owner_name,
            "member_hits": member_hits,
            "isDeleted": g.get("isDeleted"),
            "raw": g,
        })

if hits:
    print(f"找到 {len(hits)} 个含'佳佳'的群:")
    for h in hits:
        print(f"  db_id={h['db_id']}  name={h['nickname']}  owner={h['owner']}  member_hits={h['member_hits']}")
else:
    print("没有找到含'佳佳'的群。全部群名列表:")
    for i, g in enumerate(all_groups):
        owner = (g.get("owner") or {}).get("nickname", "")
        print(f"  {i+1}. {g.get('nickname','(空)')}  [owner={owner}]  id={g.get('id','')[:16]}")

# 保存
with open("/Users/works14/.claude/监控/wechat-ybxhyyh-top3/raw/all_groups.json", "w") as f:
    json.dump(all_groups, f, ensure_ascii=False, indent=2)
print(f"\n已保存 raw/all_groups.json ({len(all_groups)} 个群)")
