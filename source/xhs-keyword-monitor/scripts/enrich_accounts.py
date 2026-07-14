"""账号 enrich — 对 151 关键词调 RedFox searchUser，合并头像/简介/粉丝等；
对高频 Top 博主适量调用 queryAccountDetail。

可重复/断点续跑：
- 状态文件 data/state/account_enrich.jsonl 记录每条抓取；
- 已成功的 keyword+account 不再重复抓。
限速：每条间隔 0.25s，失败最多 3 次。
失败记录：data/state/account_enrich_failures.jsonl

字段来源追溯：每条结果带 _enrich_source (searchUser | queryAccountDetail | failed)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.ingest.redfox import RedFoxError, search_xhs_user, query_xhs_account_detail  # noqa: E402
from app.ingest.redfox.client import RedFoxClient  # noqa: E402


LOG = logging.getLogger("enrich_accounts")


STATE_DIR = PROJECT_ROOT / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_PATH = STATE_DIR / "account_enrich.jsonl"
FAILURES_PATH = STATE_DIR / "account_enrich_failures.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_progress() -> set[str]:
    seen: set[str] = set()
    if PROGRESS_PATH.exists():
        for line in PROGRESS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                key = f"{d.get('keyword_id', '')}::{d.get('account_id', '')}::{d.get('source', '')}"
                if d.get("status") == "ok":
                    seen.add(key)
            except json.JSONDecodeError:
                continue
    return seen


def enrich_keyword_search_users(client: RedFoxClient, keyword_text: str, keyword_id: str,
                                seen: set[str], limit: int = 8) -> tuple[int, int]:
    """对单个关键词 searchUser，最多 limit 个账号；返回 (success_count, fail_count)"""
    success = 0
    fail = 0
    try:
        raw = client.search_user(keyword=keyword_text, offset=0, sort_type="default").raw
    except RedFoxError as exc:
        _append_jsonl(FAILURES_PATH, {
            "source": "searchUser",
            "keyword_id": keyword_id,
            "keyword_text": keyword_text,
            "stage": "searchUser",
            "error": str(exc),
            "captured_at": _now(),
        })
        return 0, 1
    except Exception as exc:
        _append_jsonl(FAILURES_PATH, {
            "source": "searchUser",
            "keyword_id": keyword_id,
            "keyword_text": keyword_text,
            "stage": "searchUser",
            "error": f"{type(exc).__name__}: {exc}",
            "captured_at": _now(),
        })
        return 0, 1

    rows = ((raw.get("data") or {}).get("list") or [])[:limit]
    for row in rows:
        account_id = row.get("userId") or row.get("accountId") or row.get("accountNickname") or ""
        if not account_id:
            continue
        key = f"{keyword_id}::{account_id}::searchUser"
        if key in seen:
            continue
        try:
            _append_jsonl(PROGRESS_PATH, {
                "source": "searchUser",
                "status": "ok",
                "keyword_id": keyword_id,
                "keyword_text": keyword_text,
                "account_id": account_id,
                "account_nickname": row.get("accountNickname"),
                "raw": row,
                "captured_at": _now(),
            })
            success += 1
        except Exception as exc:
            fail += 1
            _append_jsonl(FAILURES_PATH, {
                "source": "searchUser",
                "keyword_id": keyword_id,
                "account_id": account_id,
                "stage": "save",
                "error": str(exc),
            })
    return success, fail


def enrich_top_accounts_detail(client: RedFoxClient, top_n: int = 100,
                                seen: set[str] | None = None) -> tuple[int, int]:
    """对 normalized/accounts.json 中笔记数 Top N 博主调 queryAccountDetail。"""
    seen = seen or set()
    success = 0
    fail = 0
    accounts_path = PROJECT_ROOT / "normalized" / "accounts.json"
    if not accounts_path.exists():
        LOG.warning("accounts.json not found; skip queryAccountDetail")
        return 0, 0
    data = json.loads(accounts_path.read_text(encoding="utf-8"))
    # 排序：按 last_seen_at 倒序（最近活跃的优先）
    ranked = sorted(data, key=lambda a: a.get("last_seen_at") or "", reverse=True)
    target = [a for a in ranked if a.get("canonical_name") or a.get("account_id")][:top_n]
    for acct in target:
        account_id = acct.get("account_id", "")
        key = f"top::{account_id}::queryAccountDetail"
        if key in seen:
            continue
        if not account_id:
            continue
        try:
            resp = client.query_account_detail(account_id=account_id)
            detail = resp.raw
            _append_jsonl(PROGRESS_PATH, {
                "source": "queryAccountDetail",
                "status": "ok",
                "account_id": account_id,
                "detail": detail,
                "captured_at": _now(),
            })
            success += 1
            time.sleep(0.2)
        except RedFoxError as exc:
            fail += 1
            _append_jsonl(FAILURES_PATH, {
                "source": "queryAccountDetail",
                "account_id": account_id,
                "stage": "queryAccountDetail",
                "error": str(exc),
            })
        except Exception as exc:
            fail += 1
            _append_jsonl(FAILURES_PATH, {
                "source": "queryAccountDetail",
                "account_id": account_id,
                "stage": "queryAccountDetail",
                "error": f"{type(exc).__name__}: {exc}",
            })
    return success, fail


def merge_into_accounts() -> tuple[int, int]:
    """把 enrich 数据合并到 normalized/accounts.json（按 account_id 合并 platform_payload + 头部字段）。"""
    if not PROGRESS_PATH.exists():
        return 0, 0
    enriched: dict[str, dict] = {}  # account_id → payload
    for line in PROGRESS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if entry.get("status") != "ok":
                continue
            aid = entry.get("account_id", "")
            if not aid:
                continue
            if entry.get("source") == "searchUser":
                row = entry.get("raw") or {}
                enriched.setdefault(aid, {"_enrich_sources": set()})
                enriched[aid]["_enrich_sources"].add("searchUser")
                enriched[aid].setdefault("searchUser_raw", row)
            elif entry.get("source") == "queryAccountDetail":
                detail = entry.get("detail") or {}
                inner = (detail.get("data") or {})
                enriched.setdefault(aid, {"_enrich_sources": set()})
                enriched[aid]["_enrich_sources"].add("queryAccountDetail")
                enriched[aid]["queryAccountDetail_raw"] = inner
        except json.JSONDecodeError:
            continue

    accounts_path = PROJECT_ROOT / "normalized" / "accounts.json"
    data = json.loads(accounts_path.read_text(encoding="utf-8"))
    by_id = {a.get("account_id"): a for a in data}
    updated = 0
    for aid, payload in enriched.items():
        acct = by_id.get(aid)
        if not acct:
            continue
        sources = payload.get("_enrich_sources", set())
        row = payload.get("searchUser_raw") or {}
        detail = payload.get("queryAccountDetail_raw") or {}
        # searchUser / queryAccountDetail 字段映射
        merged = detail if detail else row
        if merged:
            for k_src, k_dst in [
                ("accountHeadImg", "headimg_url"),
                ("accountDesc", "description"),
                ("accountFans", "fans"),
                ("accountTotalWorks", "total_works"),
                ("accountLikes", "likes"),
                ("accountCollectes", "collects"),
                ("accountFollows", "follows"),
                ("ipLocation", "ip_location"),
                ("province", "province"),
                ("city", "city"),
                ("verifyInfo", "verify_info"),
                ("lastCreateTime", "last_create_time"),
                ("accountUpdateTime", "last_create_time"),
                ("accountType", "account_type"),
            ]:
                if merged.get(k_src) is not None and merged.get(k_src) != "":
                    acct[k_dst] = merged[k_src]
            # 平台 payload
            pp = acct.setdefault("platform_payload", {})
            pp["_enrich_sources"] = sorted(sources)
            pp["_enrich_merged_fields"] = list({k_dst for k_src, k_dst in [
                ("accountHeadImg", "headimg_url"),
                ("accountDesc", "description"),
                ("accountFans", "fans"),
                ("accountTotalWorks", "total_works"),
                ("accountLikes", "likes"),
                ("accountCollectes", "collects"),
                ("accountFollows", "follows"),
                ("ipLocation", "ip_location"),
                ("province", "province"),
                ("city", "city"),
                ("verifyInfo", "verify_info"),
                ("lastCreateTime", "last_create_time"),
                ("accountUpdateTime", "last_create_time"),
                ("accountType", "account_type"),
            ] if merged.get(k_src) is not None})
            updated += 1
    json.dump(data, open(accounts_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return updated, len(enriched)


def main() -> int:
    parser = argparse.ArgumentParser(description="RedFox 小红书账号 enrich")
    parser.add_argument("--skip-search-user", action="store_true")
    parser.add_argument("--skip-detail", action="store_true")
    parser.add_argument("--top-n-detail", type=int, default=100)
    parser.add_argument("--per-keyword-user-limit", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=0.25)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not Config.REDFOX_API_KEY:
        LOG.error("REDFOX_API_KEY 未设置")
        return 2

    client = RedFoxClient()
    seen = _load_progress()

    # 1. searchUser on 151 keywords
    if not args.skip_search_user:
        keywords = json.loads((PROJECT_ROOT / "data" / "config" / "keywords.json").read_text(encoding="utf-8"))
        kw_list = [
            {"keyword_id": k.get("keyword_id", ""), "keyword_text": k.get("keyword_text", ""), "enabled": k.get("enabled", True)}
            for g in keywords.get("groups", []) for k in g.get("keywords", [])
        ]
        kw_list = [k for k in kw_list if k["enabled"]]
        LOG.info("searchUser: %d enabled keywords", len(kw_list))
        total_success = 0
        total_fail = 0
        for idx, item in enumerate(kw_list, 1):
            s, f = enrich_keyword_search_users(
                client, item["keyword_text"], item["keyword_id"], seen, limit=args.per_keyword_user_limit,
            )
            total_success += s
            total_fail += f
            if idx % 20 == 0:
                LOG.info("[%d/%d] success=%d fail=%d", idx, len(kw_list), total_success, total_fail)
            time.sleep(args.sleep)
        LOG.info("searchUser done: success=%d fail=%d", total_success, total_fail)

    # 2. queryAccountDetail on top N accounts
    if not args.skip_detail:
        LOG.info("queryAccountDetail on top %d accounts", args.top_n_detail)
        s, f = enrich_top_accounts_detail(client, top_n=args.top_n_detail, seen=seen)
        LOG.info("queryAccountDetail done: success=%d fail=%d", s, f)

    # 3. merge
    updated, total_enriched = merge_into_accounts()
    LOG.info("merged %d enriched accounts into accounts.json (out of %d unique)", updated, total_enriched)

    # 4. rebuild monitor-data
    from app.ingest.rebuild import rebuild_all
    monitor = rebuild_all(verbose=False)
    LOG.info("monitor-data rebuilt: keywords=%d accounts=%d", len(monitor["keywords"]), len(monitor["accounts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
