#!/usr/bin/env python3
"""可恢复的 TikHub 小红书关键词批量刷新 runner。

每个关键词独立重试、独立落 raw、独立写 completed/failed/state，避免单词
异常拖垮整批；取消、进程重启后可从已完成清单继续。该脚本不接触 RedFox。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.ingest.builders.entity_builder import build_entities  # noqa: E402
from app.ingest.tikhub import TikHubError, extract_notes_from_search_response, search_xhs_notes  # noqa: E402
from scripts import import_tikhub  # noqa: E402


FINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl_records(path: Path, key: str = "keyword_id") -> dict[str, dict[str, Any]]:
    """读取 jsonl 的每个关键词最后一条记录。

    同一关键词在“失败后恢复”时会有多条历史记录；统计与 UI 必须以最终
    结果为准，不能把历史失败重复计数。
    """
    values: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = str(row.get(key) or "").strip()
        if value:
            values[value] = row
    return values


def read_effective_outcomes(
    completed_path: Path, failed_path: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """成功记录优先于旧失败记录，得到批次当前的有效结果。"""
    completed = read_jsonl_records(completed_path)
    failed = read_jsonl_records(failed_path)
    for keyword_id in completed:
        failed.pop(keyword_id, None)
    return completed, failed


def load_keywords(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path, {}) or {}
    rows = payload.get("keywords") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        kid = str(row.get("keyword_id") or "").strip()
        text = str(row.get("keyword_text") or "").strip()
        if not kid or not text or kid in seen:
            continue
        seen.add(kid)
        out.append({**row, "keyword_id": kid, "keyword_text": text})
    return out


def patch_state(state_path: Path, **patch: Any) -> dict[str, Any]:
    state = read_json(state_path, {}) or {}
    state.update(patch)
    state["updated_at"] = now_iso()
    write_json(state_path, state)
    return state


def cancelled(batch_dir: Path) -> bool:
    return (batch_dir / "cancel.flag").exists()


def capture_one(item: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """请求一页、保存无鉴权 raw，并转成当前 TikHub entity envelope。"""
    captured_at = now_iso()
    raw = search_xhs_notes(keyword=item["keyword_text"], page=1, sort_type="general")
    raw_path = import_tikhub._save_raw(item["keyword_id"], raw, page=1)
    envelope = extract_notes_from_search_response(
        raw, keyword=item["keyword_text"], captured_at=captured_at
    )
    envelope.raw_file_path = import_tikhub._project_relative(raw_path)
    return envelope, {
        "keyword_id": item["keyword_id"],
        "keyword": item["keyword_text"],
        "captured_at": captured_at,
        "result_count": envelope.result_count,
        "raw_file_path": envelope.raw_file_path,
        "provider": "tikhub_xhs",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikHub keyword batch runner")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--keywords-file", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=PROJECT_ROOT / "data" / "runs")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=3.0)
    parser.add_argument("--rebuild-every", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="恢复时也重新请求此前最终失败的关键词",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Config.TIKHUB_API_TOKEN:
        raise SystemExit("TIKHUB_API_TOKEN 未设置，拒绝启动刷新。")

    batch_dir = Path(args.runs_root) / args.batch_id
    state_path = batch_dir / "state.json"
    completed_path = batch_dir / "completed.jsonl"
    failed_path = batch_dir / "failed.jsonl"
    events_path = batch_dir / "events.jsonl"
    heartbeat_path = batch_dir / "heartbeat.json"
    batch_dir.mkdir(parents=True, exist_ok=True)

    keywords = load_keywords(args.keywords_file)
    completed_records, failed_records = read_effective_outcomes(completed_path, failed_path)
    completed_ids = set(completed_records)
    failed_ids = set(failed_records)
    if args.resume:
        if args.include_failed:
            pending = [item for item in keywords if item["keyword_id"] not in completed_ids]
        else:
            pending = [
                item
                for item in keywords
                if item["keyword_id"] not in completed_ids
                and item["keyword_id"] not in failed_ids
            ]
    else:
        pending = list(keywords)

    state = read_json(state_path, {}) or {}
    success_count = len(completed_ids)
    failed_count = len(failed_ids)
    patch_state(
        state_path,
        batch_id=args.batch_id,
        provider="tikhub_xhs",
        status="running",
        started_at=state.get("started_at") or now_iso(),
        # 若启动 HTTP 响应在 runner/guard 交接窗口失败，旧的完成/错误字段
        # 不能继续留在一个实际运行中的批次里，否则历史和进度 UI 会误判。
        finished_at=None,
        error=None,
        total_keywords=len(keywords),
        success_count=success_count,
        failed_count=failed_count,
        # pending_count 是本轮 runner 尚未处理的词数；恢复失败词时它能让
        # 前端进度从“实际已成功数”继续走，而不是一开始就错误显示 100%。
        pending_count=len(pending),
        runner_pid=os.getpid(),
        current_keyword=None,
        current_attempt=None,
        cancel_requested=bool(state.get("cancel_requested")),
    )
    append_jsonl(events_path, {
        "at": now_iso(),
        "event": "runner_started",
        "resume": bool(args.resume),
        "include_failed": bool(args.include_failed),
        "pid": os.getpid(),
        "pending_count": len(pending),
    })

    completed_since_rebuild = 0
    remaining_count = len(pending)
    for index, item in enumerate(pending, start=1):
        if cancelled(batch_dir):
            patch_state(
                state_path,
                status="cancelled",
                current_keyword=None,
                current_attempt=None,
                pending_count=remaining_count,
                finished_at=now_iso(),
                cancelled_at=now_iso(),
                cancel_reason="用户主动停止",
            )
            append_jsonl(events_path, {"at": now_iso(), "event": "cancelled", "success_count": success_count, "failed_count": failed_count})
            break

        patch_state(
            state_path,
            current_keyword=item["keyword_text"],
            current_item_id=item["keyword_id"],
            pending_count=remaining_count,
            heartbeat_at=now_iso(),
        )
        write_json(heartbeat_path, {"at": now_iso(), "keyword": item["keyword_text"], "index": index, "total": len(pending)})

        envelope = None
        record: dict[str, Any] | None = None
        last_error = ""
        attempts = max(1, args.max_attempts)
        for attempt in range(1, attempts + 1):
            if cancelled(batch_dir):
                break
            patch_state(state_path, current_attempt=attempt, heartbeat_at=now_iso())
            try:
                envelope, record = capture_one(item)
                append_jsonl(events_path, {"at": now_iso(), "event": "keyword_captured", "keyword_id": item["keyword_id"], "keyword": item["keyword_text"], "attempt": attempt, "result_count": record["result_count"]})
                break
            except TikHubError as exc:
                last_error = str(exc)
            except Exception as exc:  # 每个词独立兜底
                last_error = f"{type(exc).__name__}: {exc}"
            append_jsonl(events_path, {"at": now_iso(), "event": "keyword_attempt_failed", "keyword_id": item["keyword_id"], "keyword": item["keyword_text"], "attempt": attempt, "error": last_error[:300]})
            if attempt < attempts and not cancelled(batch_dir):
                time.sleep(max(args.retry_sleep, 0.0) * attempt)

        if cancelled(batch_dir):
            patch_state(
                state_path,
                status="cancelled",
                current_keyword=None,
                current_attempt=None,
                pending_count=remaining_count,
                finished_at=now_iso(),
                cancelled_at=now_iso(),
                cancel_reason="用户主动停止",
            )
            append_jsonl(events_path, {"at": now_iso(), "event": "cancelled", "success_count": success_count, "failed_count": failed_count})
            break

        if envelope is None or record is None:
            append_jsonl(failed_path, {
                "keyword_id": item["keyword_id"],
                "keyword": item["keyword_text"],
                "reason": last_error or "未知错误",
                "attempts": attempts,
                "recorded_at": now_iso(),
            })
        else:
            # 事实层先落盘，再标记关键词完成，进程中断时可以安全恢复。
            import_tikhub._upsert_normalized(build_entities([envelope]))
            completed_since_rebuild += 1
            append_jsonl(completed_path, {**record, "status": "success"})

        completed_records, failed_records = read_effective_outcomes(completed_path, failed_path)
        success_count = len(completed_records)
        failed_count = len(failed_records)
        remaining_count = max(remaining_count - 1, 0)
        patch_state(
            state_path,
            success_count=success_count,
            failed_count=failed_count,
            pending_count=remaining_count,
            heartbeat_at=now_iso(),
        )

        if completed_since_rebuild >= max(1, args.rebuild_every):
            from app.ingest.rebuild import rebuild_all
            rebuild_all(verbose=False)
            completed_since_rebuild = 0

        time.sleep(max(Config.TIKHUB_INTER_REQUEST_DELAY, 0.0))
    else:
        if completed_since_rebuild:
            from app.ingest.rebuild import rebuild_all
            rebuild_all(verbose=False)
        completed_records, failed_records = read_effective_outcomes(completed_path, failed_path)
        success_count = len(completed_records)
        failed_count = len(failed_records)
        final_status = "completed" if failed_count == 0 else "completed_with_failures"
        patch_state(
            state_path,
            status=final_status,
            current_keyword=None,
            current_attempt=None,
            pending_count=0,
            success_count=success_count,
            failed_count=failed_count,
            heartbeat_at=now_iso(),
            finished_at=now_iso(),
            final_rebuild_ok=True,
        )
        append_jsonl(events_path, {"at": now_iso(), "event": "runner_finished", "status": final_status, "success_count": success_count, "failed_count": failed_count})
    # 已取消的批次也重建一次，确保已完成部分可见。
    state = read_json(state_path, {}) or {}
    if state.get("status") == "cancelled":
        from app.ingest.rebuild import rebuild_all
        rebuild_all(verbose=False)
    return 0 if state.get("status") in {"completed", "cancelled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
