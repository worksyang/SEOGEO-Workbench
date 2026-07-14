"""initial_import — 把 data/config/keywords.json 全部 151 词用 RedFox 抓一遍。

两种调用方式：
  A) 启动时一次性全量：
     python3 scripts/initial_import.py
  B) 批量刷新模式（接 scheduler_service）：
     python3 scripts/initial_import.py \
         --batch-id web_20260710_220000 \
         --keywords-file data/runs/<batch>/keywords.json \
         --runs-root data/runs

产物：
  data/raw/redfox/xhs/<keyword>/<timestamp>_offset_<n>.json   # 原始响应
  normalized/keywords.json                                # 事实层合并
  normalized/snapshots.json
  normalized/snapshot_terms.json
  normalized/accounts.json
  normalized/articles.json
  normalized/ranking_hits.json
  normalized/note_metric_observations.json
  normalized/monitor-data.json                            # 派生层

每个关键词至少 offset=0 一页 20 条；默认也会拉 offset=20 做覆盖。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 把项目根加进 sys.path，让 `from app.xxx import yyy` 能找到
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402  必须在 sys.path 调整之后
from app.ingest.redfox import RedFoxError  # noqa: E402
from app.ingest.redfox.client import (  # noqa: E402
    RedFoxClient,
    search_xhs_article,
    search_xhs_user,
    query_xhs_account_detail,
)
from app.ingest.redfox.envelope import (  # noqa: E402
    SnapshotEnvelope,
    build_content_item,
    build_creator_item,
)
from app.ingest.builders.entity_builder import build_entities  # noqa: E402


LOG = logging.getLogger("initial_import")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_jsonl(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_keywords(keywords_file: Path | None) -> list[dict]:
    """返回 [{keyword_id, keyword_text, group_id, group_label, group_order, ...}]"""
    src = keywords_file or (PROJECT_ROOT / "data" / "config" / "keywords.json")
    payload = _read_json(src, default={})
    items: list[dict] = []
    if "keywords" in payload and isinstance(payload["keywords"], list):
        # runs/<batch>/keywords.json 模式
        for item in payload["keywords"]:
            kid = str(item.get("keyword_id") or "").strip()
            text = str(item.get("keyword_text") or "").strip()
            if not kid or not text:
                continue
            items.append({
                "keyword_id": kid,
                "keyword_text": text,
                "group_id": item.get("group_id", ""),
                "group_label": item.get("group_label", "未分组"),
                "group_order": int(item.get("group_order") or 0),
            })
        return items
    # 标准 keywords.json 模式
    for group in payload.get("groups", []):
        for kw in group.get("keywords", []):
            kid = str(kw.get("keyword_id") or "").strip()
            text = str(kw.get("keyword_text") or "").strip()
            if not kid or not text:
                continue
            items.append({
                "keyword_id": kid,
                "keyword_text": text,
                "group_id": group.get("group_id", ""),
                "group_label": group.get("label", "未分组"),
                "group_order": int(group.get("order") or 0),
            })
    return items


def _upsert_jsonl(path: Path, new_items: list[dict], key: str) -> None:
    existing = _read_json(path, default=[])
    if not isinstance(existing, list):
        existing = []
    by_key = {item.get(key): item for item in existing if item.get(key)}
    for item in new_items:
        k = item.get(key)
        if k is None:
            continue
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = item
        else:
            merged = {**cur, **item}
            cur_pp = cur.get("platform_payload") or {}
            new_pp = item.get("platform_payload") or {}
            if cur_pp or new_pp:
                merged["platform_payload"] = {**cur_pp, **new_pp}
            by_key[k] = merged
    _write_jsonl(path, list(by_key.values()))


def _append_jsonl(path: Path, new_items: list[dict]) -> None:
    if not new_items:
        return
    existing = _read_json(path, default=[])
    if not isinstance(existing, list):
        existing = []
    _write_jsonl(path, existing + new_items)


def _capture_keyword(
    client: RedFoxClient,
    keyword_text: str,
    offsets: list[int],
    raw_dir: Path,
) -> SnapshotEnvelope:
    """抓一个关键词的 searchArticle 多个 offset，返回 SnapshotEnvelope。"""
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    all_items: list = []
    seen_work_ids: set = set()
    failed_offsets: list[int] = []
    last_error: str | None = None

    for offset in offsets:
        raw_path = raw_dir / keyword_text / f"{captured_at.replace(':', '-')}_offset_{offset}.json"
        try:
            base_resp = client.search_article(keyword=keyword_text, offset=offset, sort_type="default")
            raw = base_resp.raw
        except RedFoxError as exc:
            failed_offsets.append(offset)
            last_error = str(exc)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        except Exception as exc:
            failed_offsets.append(offset)
            last_error = f"{type(exc).__name__}: {exc}"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
            continue

        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = ((raw.get("data") or {}).get("list") or [])
        for row in rows:
            wid = row.get("workId") or row.get("workIdStr") or row.get("workUrl") or f"unknown_{offset}"
            if wid in seen_work_ids:
                continue
            seen_work_ids.add(wid)
            all_items.append(row)

    # 构造 items + 去重后的 rank
    items: list = []
    rank_counter = 1
    for row in all_items:
        item = build_content_item(row, rank_counter)
        items.append(item)
        rank_counter += 1

    if all_items and not failed_offsets:
        status = "success"
    elif all_items and failed_offsets:
        status = "partial"
    else:
        status = "failed"

    return SnapshotEnvelope(
        platform="小红书",
        keyword=keyword_text,
        captured_at=captured_at,
        status=status,
        has_more=len(items) >= 20,
        result_count=len(items),
        suggestions=[],
        related_terms=[],
        items=items,
        raw={},  # raw 已落盘
        error_message=last_error if failed_offsets else None,
        source_version="xhs_redfox_v1",
    )


def _capture_user_meta(client: RedFoxClient, keyword_text: str, raw_dir: Path) -> list[dict]:
    """拉一次 searchUser（按需）；失败也不影响主流程。"""
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    raw_path = raw_dir / keyword_text / f"{captured_at.replace(':', '-')}_users_offset_0.json"
    try:
        raw = client.search_user(keyword=keyword_text, offset=0, sort_type="default").raw
    except Exception as exc:
        raw_path.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return []
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ((raw.get("data") or {}).get("list") or [])
    return [build_creator_item(r).to_dict() for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="RedFox 小红书首抓 / 批量刷新")
    parser.add_argument("--keywords-file", type=Path, default=None,
                        help="可选：仅对指定 keyword 列表抓取")
    parser.add_argument("--offsets", type=int, nargs="+", default=None,
                        help="RedFox searchArticle offset 列表，默认 [0]")
    parser.add_argument("--with-user-meta", action="store_true",
                        help="每个关键词额外拉一次 searchUser")
    parser.add_argument("--batch-id", type=str, default=None,
                        help="批量刷新模式：写入 data/runs/<batch>/{state,completed,failed}.json")
    parser.add_argument("--runs-root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="最多抓 N 个关键词（调试用）")
    parser.add_argument("--skip-rebuild", action="store_true",
                        help="跳过最后 rebuild_all（脚本链外调用时使用）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not Config.REDFOX_API_KEY:
        LOG.error("REDFOX_API_KEY 未设置；请在 .env 或父目录 .env 配置。")
        return 2

    client = RedFoxClient()
    raw_dir = PROJECT_ROOT / "data" / "raw" / "redfox" / "xhs"
    raw_dir.mkdir(parents=True, exist_ok=True)

    offsets = args.offsets if args.offsets is not None else list(Config.DEFAULT_FETCH_OFFSETS)

    keywords = _load_keywords(args.keywords_file)
    if args.limit:
        keywords = keywords[: args.limit]
    LOG.info("共 %d 个关键词，offsets=%s", len(keywords), offsets)

    # ── 批量刷新模式：维护 batch state.json ──
    batch_state: dict | None = None
    batch_dir: Path | None = None
    completed_path: Path | None = None
    failed_path: Path | None = None
    if args.batch_id:
        runs_root = args.runs_root or (PROJECT_ROOT / "data" / "runs")
        batch_dir = runs_root / args.batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        completed_path = batch_dir / "completed.jsonl"
        failed_path = batch_dir / "failed.jsonl"
        state_path = batch_dir / "state.json"
        existing_state = _read_json(state_path, default={})
        batch_state = {
            "batch_id": args.batch_id,
            "status": "running",
            "started_at": existing_state.get("started_at") or _now_iso(),
            "total_keywords": len(keywords),
            "success_count": 0,
            "failed_count": 0,
            "current_keyword": None,
        }
        _write_jsonl(state_path, batch_state)
        cancel_flag = batch_dir / "cancel.flag"

    # ── 主循环 ──
    envelopes: list[SnapshotEnvelope] = []
    creator_payloads: list[dict] = []
    success_count = 0
    failed_count = 0

    for idx, item in enumerate(keywords, 1):
        keyword_text = item["keyword_text"]
        keyword_id = item["keyword_id"]
        if batch_state is not None:
            batch_state["current_keyword"] = keyword_text
            batch_state["updated_at"] = _now_iso()
            batch_state["processed"] = idx - 1
            _write_jsonl(batch_dir / "state.json", batch_state)

            # 检查取消
            if (batch_dir / "cancel.flag").exists():
                LOG.info("检测到 cancel.flag，停止抓取。已完成 %d/%d", success_count, len(keywords))
                batch_state["status"] = "cancelled"
                batch_state["finished_at"] = _now_iso()
                _write_jsonl(batch_dir / "state.json", batch_state)
                break

        LOG.info("[%d/%d] 抓取 %s ...", idx, len(keywords), keyword_text)
        envelope = _capture_keyword(client, keyword_text, offsets, raw_dir)
        envelopes.append(envelope)

        if envelope.status == "failed":
            failed_count += 1
            if batch_state is not None:
                batch_state["failed_count"] = failed_count
                with failed_path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps({
                        "keyword": keyword_text,
                        "reason": envelope.error_message or "未知错误",
                        "recorded_at": _now_iso(),
                    }, ensure_ascii=False) + "\n")
            continue

        success_count += 1
        if batch_state is not None:
            batch_state["success_count"] = success_count
            with completed_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({
                    "keyword": keyword_text,
                    "captured_at": envelope.captured_at,
                    "result_count": envelope.result_count,
                    "status": envelope.status,
                }, ensure_ascii=False) + "\n")

        if args.with_user_meta:
            try:
                users = _capture_user_meta(client, keyword_text, raw_dir)
                creator_payloads.extend(users)
            except Exception as exc:
                LOG.warning("searchUser 失败 %s: %s", keyword_text, exc)

        # 节流，避免 RedFox 限流
        time.sleep(0.2)

    # ── 合并事实层 ──
    entities = build_entities(envelopes)
    LOG.info("实体合并: keywords=%d snapshots=%d accounts=%d articles=%d hits=%d obs=%d",
             len(entities["keywords"]), len(entities["snapshots"]), len(entities["accounts"]),
             len(entities["articles"]), len(entities["ranking_hits"]), len(entities["note_metric_observations"]))

    normalized_dir = PROJECT_ROOT / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    _upsert_jsonl(normalized_dir / "keywords.json", entities["keywords"], key="keyword_id")
    _upsert_jsonl(normalized_dir / "accounts.json", entities["accounts"] + creator_payloads, key="account_id")
    _upsert_jsonl(normalized_dir / "articles.json", entities["articles"], key="article_id")
    _append_jsonl(normalized_dir / "snapshots.json", entities["snapshots"])
    _append_jsonl(normalized_dir / "snapshot_terms.json", entities["snapshot_terms"])
    _append_jsonl(normalized_dir / "ranking_hits.json", entities["ranking_hits"])
    _append_jsonl(normalized_dir / "note_metric_observations.json", entities["note_metric_observations"])

    # ── 派生层 ──
    if not args.skip_rebuild:
        from app.ingest.rebuild import rebuild_all
        monitor = rebuild_all(verbose=False)
        LOG.info("monitor-data.json: keywords=%d accounts=%d",
                 len(monitor.get("keywords", [])), len(monitor.get("accounts", [])))

    # ── 收尾 ──
    if batch_state is not None:
        if batch_state.get("status") != "cancelled":
            batch_state["status"] = "completed" if failed_count == 0 else "completed_with_failures"
        batch_state["finished_at"] = _now_iso()
        batch_state["current_keyword"] = None
        _write_jsonl(batch_dir / "state.json", batch_state)

    LOG.info("完成。成功=%d 失败=%d", success_count, failed_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
