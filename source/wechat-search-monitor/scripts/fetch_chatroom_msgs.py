"""工作手机3.0开放接口 - 拉群消息最小可用脚本

用法示例:
    python3 fetch_chatroom_msgs.py --chatroom-id "<db_id>" --days 7

依赖: requests
    pip install requests
"""
import argparse
import datetime as dt
import json
import sys
import time

import requests

BASE_URL = "https://user.ifangzhou.com"
ADMIN_TOKEN = "534d14ad2cba4b4886cbcf6889e764e1"  # 从浏览器 localStorage Admin-Token 取
ACCOUNT_ID = "5d753a3cfdf6b1652a5b24845c54a3a69a88284c"  # 工作微信号 db_id

# 关键陷阱:
# 1) wechatChatroomId 必须是 db_id,不是微信原始 @chatroom ID
# 2) 时间区间硬限 7 天,7 天外的请求会报 -1
# 3) 分页用 lastMessageId,每页默认上限
HEADERS = {
    "admin-token": ADMIN_TOKEN,
    "Content-Type": "application/json",
}


def list_chatrooms(keyword: str | None = None, page_size: int = 500) -> list[dict]:
    """列出所有群组,可按关键词过滤"""
    url = f"{BASE_URL}/wp/api/v1/wechatChatrooms"
    resp = requests.get(
        url,
        headers=HEADERS,
        params={"maxWechatChatroomId": "", "pageSize": page_size},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if keyword:
        data = [r for r in data if keyword in r.get("nickname", "")]
    return data


def fetch_chatroom_messages(
    wechat_chatroom_db_id: str,
    wechat_account_id: str,
    begin: dt.datetime,
    end: dt.datetime,
) -> list[dict]:
    """拉一个 7 天窗口内的群消息(接口硬限)"""
    url = f"{BASE_URL}/wp/api/v1/wechat/chatroomMessages"
    resp = requests.get(
        url,
        headers=HEADERS,
        params={
            "beginTime": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
            "wechatChatroomId": wechat_chatroom_db_id,
            "wechatAccountId": wechat_account_id,
            "lastMessageId": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_window(
    wechat_chatroom_db_id: str,
    wechat_account_id: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict]:
    """跨任意时间长度的分页拉取,内部按 6 天滑窗切片(留 1 天安全余量)"""
    step = dt.timedelta(days=6)
    all_msgs: list[dict] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + step, end)
        msgs = fetch_chatroom_messages(
            wechat_chatroom_db_id, wechat_account_id, cursor, window_end
        )
        all_msgs.extend(msgs)
        print(
            f"  [{cursor:%Y-%m-%d} ~ {window_end:%Y-%m-%d}] +{len(msgs)}",
            flush=True,
        )
        cursor = window_end + dt.timedelta(seconds=1)
    # 按时间升序
    all_msgs.sort(key=lambda m: m.get("wechatTime", 0))
    return all_msgs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="列出所有群组")
    parser.add_argument("--keyword", default=None, help="按群名关键词过滤")
    parser.add_argument("--chatroom-id", help="目标群 db_id (注意是 id 不是 @chatroom 字符串)")
    parser.add_argument("--account-id", default=ACCOUNT_ID, help="工作微信号 db_id")
    parser.add_argument("--days", type=int, default=7, help="拉最近 N 天")
    parser.add_argument("--output", help="结果输出 JSON 路径")
    args = parser.parse_args()

    if args.list:
        rooms = list_chatrooms(keyword=args.keyword)
        for r in rooms:
            print(f"  {r['id']}  {r['chatroomId']:30s}  {r['nickname']}  isDel={r.get('isDeleted')}")
        return 0

    if not args.chatroom_id:
        parser.error("需要 --chatroom-id (db_id) 或 --list")

    end = dt.datetime.now()
    start = end - dt.timedelta(days=args.days)
    print(f"拉取群 {args.chatroom_id} 最近 {args.days} 天消息 ({start} ~ {end})")
    msgs = fetch_window(args.chatroom_id, args.account_id, start, end)
    print(f"\n总计: {len(msgs)} 条")
    if msgs:
        print(f"  首条: {msgs[0].get('wechatTime')} sender={msgs[0].get('senderWechatId')}")
        print(f"  末条: {msgs[-1].get('wechatTime')} sender={msgs[-1].get('senderWechatId')}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=2)
        print(f"\n已写入: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())