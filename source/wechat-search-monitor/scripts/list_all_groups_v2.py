#!/usr/bin/env python3
import json, urllib.request, urllib.parse

BASE_URL = "https://user.ifangzhou.com"
TOKEN = "b737429f81cb4f53902cc4a7aff16bf6"
WECHAT_ACCOUNT_ID = "5d753a3cfdf6b1652a5b24845c54a3a69a88284c"

def api_get(path, params=None):
 url = f"{BASE_URL}{path}"
 if params:
 url += "?" + urllib.parse.urlencode(params)
 req = urllib.request.Request(url, headers={
 "admin-token": TOKEN,
 "Content-Type": "application/json;charset=UTF-8",
 })
 with urllib.request.urlopen(req, timeout=15) as resp:
 return json.loads(resp.read().decode())

all_groups = []
max_id = ""
page_size =200
rounds =0
while True:
 rounds +=1
 params = {"pageSize": page_size}
 if max_id:
 params["maxWechatChatroomId"] = max_id
 data = api_get(f"/wp/api/v1/wechatAccounts/{WECHAT_ACCOUNT_ID}/chatrooms", params)
 groups = data if isinstance(data, list) else []
 if not groups:
 print(f"round {rounds}: empty, stop")
 break
 all_groups.extend(groups)
 print(f"round {rounds}: +{len(groups)} total={len(all_groups)}")
 if len(groups) < page_size:
 break
 max_id = groups[-1].get("id", "")
 if not max_id:
 break
 if rounds >100:
 print("safety break")
 break

print(f"\nTOTAL: {len(all_groups)} groups")

hits = []
for g in all_groups:
 name = g.get("nickname", "")
 owner = (g.get("owner") or {}).get("nickname", "")
 if "佳佳" in name or "佳佳" in owner:
 hits.append({
 "id": g.get("id"),
 "nickname": name,
 "owner": owner,
 "isDeleted": g.get("isDeleted"),
 "wechatAccountId": g.get("wechatAccountId"),
 "chatroomId": g.get("chatroomId"),
 })

print(f"\nHits by name/owner: {len(hits)}")
for h in hits[:50]:
 print(f" {h}")

with open("raw/all_groups.json", "w") as f:
 json.dump(all_groups, f, ensure_ascii=False, indent=2)
print(f"saved raw/all_groups.json ({len(all_groups)} groups)")
