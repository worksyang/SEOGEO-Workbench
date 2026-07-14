"""TikHub 数据源 smoke 探针。

用途：单关键词快速验证 6 个端点是否可用。
输出：纯字段摘要（绝不回显 token）。

用法：
    python3 scripts/tikhub_probe.py --keyword 友邦环宇盈活
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 把项目根加进 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.ingest.tikhub import (  # noqa: E402
    extract_creators_from_search_users_response,
    extract_note_from_detail_response,
    extract_notes_from_search_response,
    get_image_note_detail,
    get_user_info,
    get_user_posted_notes,
    get_video_note_detail,
    search_xhs_notes,
    search_xhs_users,
)


def _probe_note_detail(note_id: str, note_type: str) -> dict:
    func = get_video_note_detail if note_type == "video" else get_image_note_detail
    try:
        raw = func(note_id=note_id)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    item = extract_note_from_detail_response(raw, target_note_id=note_id)
    return {
        "ok": item is not None,
        "has_full_desc": bool(item and (item.desc_full or "")),
        "user_in_detail": bool(item and item.creator_name),
        "video": bool(item and item.images_list),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", default="友邦环宇盈活")
    parser.add_argument("--user-id", default="6800b320000000000e01f87b")  # from earlier probe
    args = parser.parse_args()

    print(f"TikHub smoke probe: keyword={args.keyword!r}, user_id={args.user_id!r}")
    print(f"  base_url: {Config.TIKHUB_BASE_URL}")
    print(f"  token_loaded: {bool(Config.TIKHUB_API_TOKEN)} (prefix={Config.TIKHUB_API_TOKEN[:4] + '***' if Config.TIKHUB_API_TOKEN else '(empty)'})")
    print()
    summary = {}

    # 1. search_notes
    try:
        raw = search_xhs_notes(keyword=args.keyword, page=1, sort_type="general")
        env = extract_notes_from_search_response(raw, keyword=args.keyword, captured_at="2026-07-11T00:00:00")
        summary["search_notes"] = {
            "ok": True,
            "result_count": env.result_count,
            "has_search_id": bool(env.search_id),
            "has_next_page": env.has_more,
            "first_title": env.items[0].title[:40] if env.items else None,
            "first_creator": env.items[0].creator_name if env.items else None,
        }
        if env.items:
            note_id = env.items[0].content_id.replace("xhs_tk_", "")
            note_type = env.items[0].work_type
            # 2 + 3. detail
            summary["image_note_detail"] = _probe_note_detail(note_id, "normal")
            if note_type == "video":
                summary["video_note_detail"] = _probe_note_detail(note_id, "video")
            summary["first_note_id"] = note_id
    except Exception as e:
        summary["search_notes"] = {"ok": False, "error": str(e)[:200]}

    # 4. get_user_info
    try:
        raw = get_user_info(user_id=args.user_id)
        from app.ingest.tikhub.parser import extract_creator_from_user_info_response
        u = extract_creator_from_user_info_response(raw)
        summary["user_info"] = {
            "ok": u is not None,
            "name": u.name if u else None,
            "fans": u.fans if u else None,
            "ip": u.ip_location if u else None,
            "has_description": bool(u and u.description),
            "note_num_stat": u.note_num_stat if u else None,
        }
    except Exception as e:
        summary["user_info"] = {"ok": False, "error": str(e)[:200]}

    # 5. user_posted_notes
    try:
        raw = get_user_posted_notes(user_id=args.user_id)
        notes = extract_notes_from_search_response(
            raw, keyword=f"user_posts:{args.user_id}", captured_at="2026-07-11T00:00:00"
        ).items
        summary["user_posted_notes"] = {"ok": True, "count": len(notes)}
    except Exception as e:
        summary["user_posted_notes"] = {"ok": False, "error": str(e)[:200]}

    # 6. search_users
    try:
        raw = search_xhs_users(keyword=args.keyword, page=1)
        users = extract_creators_from_search_users_response(raw, captured_at="2026-07-11T00:00:00")
        summary["search_users"] = {"ok": True, "count": len(users), "first": users[0].name if users else None}
    except Exception as e:
        summary["search_users"] = {"ok": False, "error": str(e)[:200]}

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(
        isinstance(v, dict) and v.get("ok")
        for k, v in summary.items() if k != "first_note_id"
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
