#!/usr/bin/env python3
"""拉取所有微信群列表并搜索含'佳佳'的群。"""
import json, sys, urllib.request, urllib.parse, urllib.error

BASE_URL = "https://user.ifangzhou.com"
TOKEN = "b737429f81cb4f53902cc4a7aff16bf6"  # localStorage Admin-Token value

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

# Step 1: 获取所有微信账号列表
print("=== Step 1: 获取微信账号列表 ===")
data = api_get("/wp/api/v1/wechatAccounts", {"pageIndex": 0, "pageSize": 50})
if isinstance(data, dict) and data.get("_http_error"):
    print(f"HTTP Error: {data['_http_error']}")
    print(data.get("_body", "")[:500])
    sys.exit(1)

accounts = data if isinstance(data, list) else data.get("data", data.get("results", []))
print(f"账号数量: {len(accounts)}")
for acc in accounts:
    print(f"  id={acc.get('id')} wechatId={acc.get('wechatId')} nickname={acc.get('nickname')}")

if not accounts:
    print("没有找到任何账号！")
    sys.exit(1)

# Use first account
account_id = accounts[0].get("id")
account_nickname = accounts[0].get("nickname", "")
print(f"\n使用账号: id={account_id} nickname={account_nickname}")

# Step 2: 拉取该账号下所有群（翻页）
print("\n=== Step 2: 拉取所有群 ===")
all_groups = []
page = 0
page_size = 50
while True:
    params = {"pageIndex": page, "pageSize": page_size, "withMembers": "false"}
    data = api_get(f"/wp/api/v1/wechatAccounts/{account_id}/chatrooms", params)
    if isinstance(data, dict) and data.get("_http_error"):
        print(f"HTTP Error on page {page}: {data['_http_error']}")
        break

    groups = data if isinstance(data, list) else data.get("data", data.get("results", []))
    if not groups:
        break
    all_groups.extend(groups)
    print(f"  页 {page}: 获取到 {len(groups)} 个群 (累计 {len(all_groups)})")
    if len(groups) < page_size:
        break
    page += 1

print(f"\n总共 {len(all_groups)} 个群")

# Step 3: 搜索含"佳佳"的群
print("\n=== Step 3: 搜索含'佳佳'的群 ===")
found_groups = []
for g in all_groups:
    name = g.get("nickname", "")
    owner = g.get("owner", {})
    owner_name = owner.get("nickname", "") if owner else ""
    # Search in group name, owner name
    if "佳佳" in name or "佳佳" in owner_name:
        found_groups.append(g)
    # Also search withMembers if available
    members = g.get("members", [])
    for m in members:
        mn = m.get("nickname", "")
        if "佳佳" in mn:
            found_groups.append(g)
            break

if found_groups:
    print(f"找到 {len(found_groups)} 个含'佳佳'的群:")
    for g in found_groups:
        print(f"  id={g.get('id')} nickname={g.get('nickname')} owner={g.get('owner',{}).get('nickname','')} wechatAccountId={g.get('wechatAccountId')}")
else:
    print("没有找到含'佳佳'的群。显示所有群名:")
    for i, g in enumerate(all_groups):
        owner = g.get("owner", {})
        owner_name = owner.get("nickname", "") if owner else ""
        print(f"  {i+1}. {g.get('nickname','')} [owner={owner_name}]")

# Save all groups to file
with open("/Users/works14/.claude/监控/wechat-ybxhyyh-top3/raw/all_groups.json", "w") as f:
    json.dump(all_groups, f, ensure_ascii=False, indent=2)
print(f"\n所有群已保存到 raw/all_groups.json")
