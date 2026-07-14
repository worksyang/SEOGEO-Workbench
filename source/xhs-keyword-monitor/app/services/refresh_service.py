"""refresh_service — 单词/批量刷新走 TikHub 小红书。

字段语义对应（与源 wechat-ybxhyyh-top3 一致）：
- 单刷 → 队列/状态/失败原因
- 批量 → batch_id / progress / cancel
- 历史 → 滚动记录
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.ingest.tikhub import TikHubError, extract_notes_from_search_response, search_xhs_notes

LOG = logging.getLogger(__name__)


# ── 路径常量 ──
def _project_root() -> Path:
    from app.config import Config
    return Config.PROJECT_ROOT


PROJECT_ROOT = _project_root()
TIKHUB_BATCH_RUNNER = PROJECT_ROOT / "scripts" / "tikhub_batch_runner.py"
TIKHUB_BATCH_GUARD = PROJECT_ROOT / "scripts" / "tikhub_batch_guard.py"
STATE_DIR = PROJECT_ROOT / "data" / "refresh_jobs"
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "tikhub" / "xhs"
BATCH_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
KEYWORDS_CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "keywords.json"
LEDGER_PATH = PROJECT_ROOT / "data" / "state" / "keyword_refresh_ledger.json"
BATCH_FINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled"}
BATCH_ACTIVE_STATUSES = {"starting", "running"}

STATE_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
BATCH_RUNS_ROOT.mkdir(parents=True, exist_ok=True)

# 单刷队列
_single_refresh_lock = threading.Lock()
_single_refresh_current: dict[str, Any] | None = None
_single_refresh_queue: deque[dict[str, str]] = deque()
_batch_recovery_lock = threading.Lock()


class BatchAlreadyRunningError(RuntimeError):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__("batch refresh already running")
        self.state = state


class SingleRefreshBusyError(RuntimeError):
    def __init__(self, current_keyword: str, queued_ahead: int = 0) -> None:
        super().__init__(f"single refresh busy: {current_keyword}")
        self.current_keyword = current_keyword
        self.queued_ahead = queued_ahead


def _state_path(job_id: str) -> Path:
    return STATE_DIR / f"{job_id}.json"


def _batch_dir(batch_id: str) -> Path:
    return BATCH_RUNS_ROOT / batch_id


def _batch_state_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "state.json"


def _batch_launch_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "launch.json"


def _batch_cancel_flag_path(batch_id: str) -> Path:
    return _batch_dir(batch_id) / "cancel.flag"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_state(job_id: str, payload: dict[str, Any]) -> None:
    _state_path(job_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _launch_failure_autofix(batch_id: str) -> None:
    """失败后异步交给 Claude -p 做有证据、幂等的诊断；手动取消不调用。"""
    if os.environ.get("XHS_REFRESH_AUTOFIX_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    script = PROJECT_ROOT / "scripts" / "refresh_failure_autofix.py"
    if not script.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(script), "--batch-id", batch_id, "--runs-root", str(BATCH_RUNS_ROOT)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        LOG.exception("[refresh] unable to launch Claude autofix for %s", batch_id)


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_completed_keywords(batch_id: str) -> list[str]:
    if not batch_id:
        return []
    completed_path = _batch_dir(batch_id) / "completed.jsonl"
    if not completed_path.exists():
        return []
    keywords: list[str] = []
    for line in completed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        kw = str(payload.get("keyword") or "").strip()
        if kw:
            keywords.append(kw)
    return keywords


def _read_failed_keywords(batch_id: str) -> list[dict[str, str]]:
    if not batch_id:
        return []
    failed_path = _batch_dir(batch_id) / "failed.jsonl"
    if not failed_path.exists():
        return []
    succeeded = set(_read_completed_keywords(batch_id))
    latest: dict[str, dict[str, str]] = {}
    for line in failed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        kw = str(payload.get("keyword") or "").strip()
        if not kw:
            continue
        keyword_id = str(payload.get("keyword_id") or kw).strip()
        if keyword_id in succeeded:
            continue
        reason = str(payload.get("reason") or "未知错误")
        latest[keyword_id] = {"keyword": kw, "reason": reason}
    return list(latest.values())


def _load_keyword_catalog() -> list[dict[str, Any]]:
    payload = _read_json(KEYWORDS_CONFIG_FILE, default={}) or {}
    items: list[dict[str, Any]] = []
    for group in payload.get("groups", []):
        for keyword in group.get("keywords", []):
            kid = str(keyword.get("keyword_id") or "").strip()
            text = str(keyword.get("keyword_text") or "").strip()
            if not kid or not text:
                continue
            items.append({
                "group_id": group.get("group_id"),
                "group_label": group.get("label"),
                "group_order": group.get("order", 0),
                "keyword_id": kid,
                "keyword_text": text,
                "batch_default_selected": True,
            })
    return items


def _default_ledger() -> dict[str, Any]:
    return {
        "current_round": 1,
        "round_started_at": _now(),
        "updated_at": _now(),
        "keywords": {},
    }


def _read_ledger() -> dict[str, Any]:
    payload = _read_json(LEDGER_PATH, default=None)
    if not isinstance(payload, dict):
        return _default_ledger()
    payload["current_round"] = max(1, int(payload.get("current_round") or 1))
    payload.setdefault("round_started_at", _now())
    payload.setdefault("updated_at", _now())
    if not isinstance(payload.get("keywords"), dict):
        payload["keywords"] = {}
    return payload


def _write_ledger(payload: dict[str, Any]) -> None:
    payload["updated_at"] = _now()
    _write_json(LEDGER_PATH, payload)


def _advance_ledger_round(ledger: dict[str, Any]) -> dict[str, Any]:
    ledger["current_round"] = int(ledger.get("current_round") or 1) + 1
    ledger["round_started_at"] = _now()
    _write_ledger(ledger)
    return ledger


def get_incremental_keywords() -> dict[str, Any]:
    catalog = _load_keyword_catalog()
    if not catalog:
        return {"keywords": [], "total_keywords": 0, "current_round": 1, "round_started_at": None, "new_round_started": False}
    ledger = _read_ledger()
    current_round = int(ledger.get("current_round") or 1)
    records = ledger.get("keywords") or {}

    def is_due(item: dict[str, Any]) -> bool:
        rec = records.get(str(item.get("keyword_id") or ""))
        if not isinstance(rec, dict):
            return True
        return int(rec.get("last_round") or 0) < current_round

    due = [item for item in catalog if is_due(item)]
    new_round_started = False
    if not due:
        ledger = _advance_ledger_round(ledger)
        current_round = int(ledger.get("current_round") or 1)
        due = catalog
        new_round_started = True

    return {
        "keywords": due,
        "total_keywords": len(catalog),
        "current_round": current_round,
        "round_started_at": ledger.get("round_started_at"),
        "new_round_started": new_round_started,
    }


def _iter_batch_dirs() -> list[Path]:
    if not BATCH_RUNS_ROOT.exists():
        return []
    return sorted(
        [p for p in BATCH_RUNS_ROOT.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _make_batch_id() -> str:
    return f"web_{time.strftime('%Y%m%d_%H%M%S')}"


# ── 单刷抓取：走 TikHub，保存 raw + 调 rebuild ─────────
def _run_keyword_refresh(keyword: str, job_id: str, offsets: list[int]) -> None:
    """实际抓取 + 写 raw + 调 rebuild；offset 参数仅为旧调用兼容。"""
    from app.ingest.rebuild import rebuild_all
    from app.ingest.builders.entity_builder import build_entities
    from scripts import import_tikhub

    started_at = _now()
    _write_state(job_id, {"job_id": job_id, "keyword": keyword, "status": "running", "started_at": started_at})
    try:
        keyword_catalog = {
            item["keyword_text"]: item for item in _load_keyword_catalog()
        }
        item = keyword_catalog.get(keyword)
        if not item:
            raise ValueError("关键词不在当前配置中")
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        raw = search_xhs_notes(keyword=keyword, page=1, sort_type="general")
        raw_path = import_tikhub._save_raw(item["keyword_id"], raw, page=1)
        envelope = extract_notes_from_search_response(raw, keyword=keyword, captured_at=captured_at)
        envelope.raw_file_path = import_tikhub._project_relative(raw_path)
        import_tikhub._upsert_normalized(build_entities([envelope]))
        rebuild_all(verbose=False)
    except TikHubError as exc:
        _write_state(job_id, {
            "job_id": job_id,
            "keyword": keyword,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _now(),
            "success": False,
            "error": str(exc),
        })
        _drain_single_refresh_queue()
        return
    except Exception as exc:
        _write_state(job_id, {
            "job_id": job_id,
            "keyword": keyword,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _now(),
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        _drain_single_refresh_queue()
        return

    _write_state(job_id, {
        "job_id": job_id,
        "keyword": keyword,
        "status": "done",
        "started_at": started_at,
        "finished_at": _now(),
        "success": True,
        "raw_files": [str(envelope.raw_file_path or "")],
        "result_count": envelope.result_count,
        "envelope_status": envelope.status,
        "provider": "tikhub_xhs",
    })
    _drain_single_refresh_queue()


def _merge_envelope_into_normalized(envelopes: list[SnapshotEnvelope]) -> None:
    """把 envelope 合并到 normalized/{keywords,snapshots,accounts,articles,ranking_hits,...}.json。

    按 content_id 去重，按 snapshot 时间合并。
    """
    from app.ingest.builders.entity_builder import build_entities

    entities = build_entities(envelopes)
    normalized_dir = PROJECT_ROOT / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    # keywords：upsert by id
    _upsert_jsonl(normalized_dir / "keywords.json", entities["keywords"], key="keyword_id")
    _upsert_jsonl(normalized_dir / "accounts.json", entities["accounts"], key="account_id")
    _upsert_jsonl(normalized_dir / "articles.json", entities["articles"], key="article_id")

    # snapshots 是按 (keyword, captured_at) 唯一，append only
    _append_jsonl(normalized_dir / "snapshots.json", entities["snapshots"])
    _append_jsonl(normalized_dir / "snapshot_terms.json", entities["snapshot_terms"])
    _append_jsonl(normalized_dir / "ranking_hits.json", entities["ranking_hits"])
    _append_jsonl(normalized_dir / "note_metric_observations.json", entities["note_metric_observations"])


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
            # shallow merge: 新字段覆盖；但 platform_payload 等 dict 走深层更新
            merged = {**cur, **item}
            cur_pp = cur.get("platform_payload") or {}
            new_pp = item.get("platform_payload") or {}
            if cur_pp or new_pp:
                merged["platform_payload"] = {**cur_pp, **new_pp}
            by_key[k] = merged
    _write_json(path, list(by_key.values()))


def _append_jsonl(path: Path, new_items: list[dict]) -> None:
    if not new_items:
        return
    existing = _read_json(path, default=[])
    if not isinstance(existing, list):
        existing = []
    _write_json(path, existing + new_items)


def _drain_single_refresh_queue() -> None:
    global _single_refresh_current
    with _single_refresh_lock:
        _single_refresh_current = None
        if _single_refresh_queue:
            next_item = _single_refresh_queue.popleft()
            _single_refresh_current = {"keyword": next_item["keyword"], "job_id": next_item["job_id"]}
        else:
            return
    _write_state(next_item["job_id"], {
        "job_id": next_item["job_id"],
        "keyword": next_item["keyword"],
        "status": "queued_to_running",
        "started_at": _now(),
    })
    t = threading.Thread(target=_run_single_wrapper, args=(next_item["keyword"], next_item["job_id"]), daemon=True)
    t.start()


def _run_single_wrapper(keyword: str, job_id: str) -> None:
    from app.config import Config
    offsets = list(getattr(Config, "DEFAULT_FETCH_OFFSETS", (0,)))
    _run_keyword_refresh(keyword, job_id, offsets)


def start_single_refresh(keyword: str) -> dict[str, Any]:
    global _single_refresh_current
    job_id = uuid.uuid4().hex[:12]

    with _single_refresh_lock:
        active_batch = get_active_batch_status()
        if active_batch:
            return {
                "job_id": job_id, "status": "rejected", "keyword": keyword,
                "reason": "batch_running",
                "current": active_batch.get("current_keyword") or "批量刷新",
            }
        if _single_refresh_current is not None:
            _single_refresh_queue.append({"keyword": keyword, "job_id": job_id})
            queued_ahead = len(_single_refresh_queue) - 1
            _write_state(job_id, {
                "job_id": job_id, "keyword": keyword, "status": "queued",
                "started_at": _now(),
                "queued_behind": _single_refresh_current["keyword"],
                "queued_ahead": queued_ahead,
            })
            return {
                "job_id": job_id, "status": "queued", "keyword": keyword,
                "current": _single_refresh_current["keyword"],
                "queued_ahead": queued_ahead,
            }
        else:
            _single_refresh_current = {"keyword": keyword, "job_id": job_id}

    t = threading.Thread(target=_run_single_wrapper, args=(keyword, job_id), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "running", "keyword": keyword, "current": keyword, "queued_ahead": 0}


def get_single_refresh_status() -> dict[str, Any] | None:
    with _single_refresh_lock:
        if _single_refresh_current is None and not _single_refresh_queue:
            return None
        return {
            "current": _single_refresh_current["keyword"] if _single_refresh_current else None,
            "queue_length": len(_single_refresh_queue),
        }


def get_job_status(job_id: str) -> dict[str, Any] | None:
    p = _state_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _normalize_batch_state(state: dict[str, Any], launch: dict[str, Any] | None = None) -> dict[str, Any]:
    launch = launch or {}
    batch_id = str(state.get("batch_id") or launch.get("batch_id") or "").strip()
    status = str(state.get("status") or "unknown").strip() or "unknown"
    total = int(state.get("total_keywords") or state.get("total") or 0)
    success_count = int(state.get("success_count") or state.get("done") or 0)
    failed_count = int(state.get("failed_count") or state.get("failed") or 0)
    raw_pending_count = state.get("pending_count")
    if raw_pending_count is None:
        pending_count = max(total - success_count - failed_count, 0)
        processed = min(total, success_count + failed_count) if total else success_count + failed_count
    else:
        pending_count = max(0, int(raw_pending_count or 0))
        processed = max(total - pending_count, 0) if total else success_count + failed_count
    # runner 将 pending_count 定义为“本轮还有多少词待处理”。这使恢复失败
    # 关键词时进度可以从真实成功数继续推进，而不会一打开就显示 100%。
    runner_pid = state.get("runner_pid") or launch.get("runner_pid")
    runner_alive = _is_pid_alive(int(runner_pid)) if runner_pid else False
    cancel_requested = bool(state.get("cancel_requested"))
    # “running 但没有活着的 runner”绝不是继续运行中。旧实现把没有记录 PID
    # 的卡死批次永远当成 active，前端因而只会无响应地等待。
    guard_pid = state.get("guard_pid") or launch.get("guard_pid")
    guard_alive = _is_pid_alive(int(guard_pid)) if guard_pid else False
    # runner 重启的极短窗口、以及“本轮有失败、guard 正在准备补跑”的窗口，
    # 都由 guard 持有；此时不能把刷新入口提前释放，否则会并发两批全量请求。
    recovery_pending = status in {"failed", "completed_with_failures"} and guard_alive
    is_active = (
        status in BATCH_ACTIVE_STATUSES and (runner_alive or guard_alive)
    ) or recovery_pending
    finished = status in BATCH_FINAL_STATUSES
    return {
        "batch_id": batch_id,
        "status": status,
        "total": total,
        "success_count": success_count,
        "failed_count": failed_count,
        "processed_count": processed,
        "pending_count": pending_count,
        "current_keyword": state.get("current_keyword"),
        "current_item_id": state.get("current_item_id"),
        "current_attempt": state.get("current_attempt"),
        "completed_keywords": _read_completed_keywords(batch_id),
        "failed_keywords": _read_failed_keywords(batch_id),
        "started_at": state.get("started_at") or launch.get("launched_at"),
        "finished_at": state.get("finished_at"),
        "updated_at": state.get("updated_at") or launch.get("launched_at"),
        "heartbeat_at": state.get("heartbeat_at"),
        "runner_pid": runner_pid,
        "runner_alive": runner_alive,
        "guard_pid": guard_pid,
        "guard_alive": guard_alive,
        "recovery_pending": recovery_pending,
        "cancel_requested": cancel_requested,
        "cancel_requested_at": state.get("cancel_requested_at"),
        "cancelled_at": state.get("cancelled_at"),
        "cancel_reason": str(state.get("cancel_reason") or ""),
        "is_active": is_active,
        "is_finished": finished,
        "batch_dir": str(_batch_dir(batch_id)) if batch_id else None,
    }


def _reconcile_stale_batch(batch_id: str, state: dict[str, Any], launch: dict[str, Any]) -> dict[str, Any]:
    """将已退出且未收尾的 runner 变成可见终态，避免永久占用刷新入口。"""
    normalized = _normalize_batch_state(state, launch)
    status = normalized.get("status")
    if (
        status not in BATCH_ACTIVE_STATUSES
        or normalized.get("runner_alive")
        or normalized.get("guard_alive")
    ):
        return normalized

    now = _now()
    if normalized.get("cancel_requested"):
        state.update({
            "status": "cancelled",
            "finished_at": now,
            "cancelled_at": now,
            "cancel_reason": state.get("cancel_reason") or "用户主动停止",
            "current_keyword": None,
            "current_attempt": None,
        })
    else:
        state.update({
            "status": "failed",
            "finished_at": now,
            "current_keyword": None,
            "current_attempt": None,
            "error": state.get("error") or "刷新 runner 已退出，未写入完成状态。",
        })
    _write_json(_batch_state_path(batch_id), state)
    if state.get("status") == "failed":
        _launch_failure_autofix(batch_id)
    return _normalize_batch_state(state, launch)


def get_batch_status(batch_id: str) -> dict[str, Any] | None:
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        return None
    state = _read_json(_batch_state_path(batch_id), default=None)
    if not isinstance(state, dict):
        return None
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    return _reconcile_stale_batch(batch_id, state, launch)


def get_active_batch_status() -> dict[str, Any] | None:
    for batch_dir in _iter_batch_dirs():
        state = _read_json(batch_dir / "state.json", default=None)
        if not isinstance(state, dict):
            continue
        launch = _read_json(batch_dir / "launch.json", default={}) or {}
        normalized = _reconcile_stale_batch(batch_dir.name, state, launch)
        if normalized.get("is_active"):
            return normalized
    return None


def cancel_batch(batch_id: str) -> dict[str, Any]:
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        raise ValueError("batch_id is required")
    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default=None)
    if not isinstance(state, dict):
        raise FileNotFoundError(f"batch not found: {batch_id}")
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    status = str(state.get("status") or "unknown")
    guard_pid = state.get("guard_pid") or launch.get("guard_pid")
    recovery_pending = (
        status in {"failed", "completed_with_failures"}
        and bool(guard_pid)
        and _is_pid_alive(int(guard_pid))
    )
    if status in BATCH_FINAL_STATUSES and not recovery_pending:
        return _normalize_batch_state(state, launch)
    flag = _batch_cancel_flag_path(batch_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("", encoding="utf-8")
    if not state.get("cancel_requested"):
        requested_at = _now()
        state["cancel_requested"] = True
        state["cancel_requested_at"] = requested_at
        state["updated_at"] = requested_at
        _write_json(state_path, state)
    return _normalize_batch_state(state, launch)


def list_batch_history(limit: int = 20) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for batch_dir in _iter_batch_dirs()[:limit]:
        state = _read_json(batch_dir / "state.json", default=None)
        if not isinstance(state, dict):
            continue
        batch_id = str(state.get("batch_id") or batch_dir.name)
        status = str(state.get("status") or "unknown")
        failed_count = int(state.get("failed_count") or 0)
        failed_keywords = _read_failed_keywords(batch_id)
        reason_seen: set[str] = set()
        reasons: list[str] = []
        for item in failed_keywords:
            reason = str(item.get("reason") or "").strip()
            if reason and reason not in reason_seen:
                reason_seen.add(reason)
                reasons.append(reason)
        autofix = _read_json(batch_dir / "autofix" / "run.json", default={}) or {}
        provider = str(state.get("provider") or "")
        keywords_file_exists = (batch_dir / "keywords.json").exists()
        launch_exists = (batch_dir / "launch.json").exists()
        # 旧 RedFox 批次不会伪装成 TikHub，更不能被“恢复”按钮误启动。
        if not provider and not launch_exists:
            provider = "legacy"
        guard_pid = state.get("guard_pid") or (_read_json(batch_dir / "launch.json", default={}) or {}).get("guard_pid")
        history.append({
            "batch_id": batch_id,
            "status": status,
            "total": int(state.get("total_keywords") or 0),
            "success_count": int(state.get("success_count") or 0),
            "failed_count": failed_count,
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "failure_reasons": reasons[:5],
            "failed_keywords": failed_keywords,
            "cancel_reason": str(state.get("cancel_reason") or ""),
            "source": str(state.get("source") or "web_refresh_all"),
            "refresh_round": state.get("refresh_round"),
            "provider": provider,
            "runner_pid": state.get("runner_pid"),
            "heartbeat_at": state.get("heartbeat_at"),
            "resumable": (
                provider == "tikhub_xhs"
                and status in BATCH_FINAL_STATUSES
                and status != "completed"
                and keywords_file_exists
                and not _is_pid_alive(int(guard_pid))
            ) if guard_pid else (
                provider == "tikhub_xhs"
                and status in BATCH_FINAL_STATUSES
                and status != "completed"
                and keywords_file_exists
            ),
            "autofix": {
                "status": autofix.get("status"),
                "reason": autofix.get("reason"),
                "finished_at": autofix.get("finished_at"),
            } if autofix else None,
        })
    return history


def _spawn_batch_processes(
    batch_id: str,
    *,
    resume: bool = False,
    include_failed: bool = False,
    start_guard: bool = True,
) -> dict[str, Any]:
    """后台启动 runner，并为每个新批次挂一个独立护航器。"""
    batch_dir = _batch_dir(batch_id)
    keywords_path = batch_dir / "keywords.json"
    if not keywords_path.exists():
        raise FileNotFoundError(f"batch keywords file not found: {keywords_path}")
    if not TIKHUB_BATCH_RUNNER.exists():
        raise FileNotFoundError(f"tikhub batch runner not found: {TIKHUB_BATCH_RUNNER}")
    if start_guard and not TIKHUB_BATCH_GUARD.exists():
        raise FileNotFoundError(f"tikhub batch guard not found: {TIKHUB_BATCH_GUARD}")

    cmd = [
        sys.executable, str(TIKHUB_BATCH_RUNNER),
        "--batch-id", batch_id,
        "--keywords-file", str(keywords_path),
        "--runs-root", str(BATCH_RUNS_ROOT),
    ]
    if resume:
        cmd.append("--resume")
    if include_failed:
        cmd.append("--include-failed")
    log_path = batch_dir / "runner.log"
    with log_path.open("a", encoding="utf-8") as log_fp:
        runner = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    launched_at = _now()
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    launch.update({
        "batch_id": batch_id,
        "runner_pid": runner.pid,
        "launched_at": launched_at,
        "runner_command": cmd,
        "log_path": str(log_path),
    })
    _write_json(_batch_launch_path(batch_id), launch)

    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default={}) or {}
    guard_pid: int | None = None
    state.update({
        "status": "starting",
        "runner_pid": runner.pid,
        "guard_pid": guard_pid or state.get("guard_pid"),
        "heartbeat_at": launched_at,
        "updated_at": launched_at,
        "current_keyword": None,
        "current_attempt": None,
    })
    _write_json(state_path, state)
    if start_guard:
        guard_pid = _spawn_batch_guard(batch_id, preserve_status=True)
    return {"runner_pid": runner.pid, "guard_pid": guard_pid, "command": cmd}


def _spawn_batch_guard(batch_id: str, *, preserve_status: bool = False) -> int:
    """为已启动的 TikHub runner 补挂护航器，不重复启动 runner。

    这用于服务自身重启后的恢复窗口：runner 仍活着但 guard 因父进程退出而
    消失时，不能粗暴调用 `_spawn_batch_processes`，否则会并发重复抓取。
    """
    batch_dir = _batch_dir(batch_id)
    if not TIKHUB_BATCH_GUARD.exists():
        raise FileNotFoundError(f"tikhub batch guard not found: {TIKHUB_BATCH_GUARD}")
    guard_cmd = [
        sys.executable, str(TIKHUB_BATCH_GUARD),
        "--batch-id", batch_id,
        "--runs-root", str(BATCH_RUNS_ROOT),
    ]
    guard_log_path = batch_dir / "guard.launch.log"
    with guard_log_path.open("a", encoding="utf-8") as guard_log:
        guard = subprocess.Popen(
            guard_cmd,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=guard_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    launched_at = _now()
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    launch.update({
        "batch_id": batch_id,
        "guard_pid": guard.pid,
        "guard_command": guard_cmd,
        "guard_log_path": str(guard_log_path),
        "guard_launched_at": launched_at,
    })
    _write_json(_batch_launch_path(batch_id), launch)
    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default={}) or {}
    state.update({
        "guard_pid": guard.pid,
        "heartbeat_at": launched_at,
        "updated_at": launched_at,
    })
    if not preserve_status:
        state["status"] = "starting"
    _write_json(state_path, state)
    return guard.pid


def recover_orphaned_batches() -> list[dict[str, Any]]:
    """恢复服务重启时遗留的 TikHub 活跃批次。

    runner/guard 使用独立 session，正常的 Codex 关闭不会影响它们；但机器重启
    或 launchd 重启时二者都会退出。这里仅恢复 `starting/running` 的 TikHub
    批次，主动取消和历史 RedFox 批次绝不重新启动。
    """
    recovered: list[dict[str, Any]] = []
    with _batch_recovery_lock:
        for batch_dir in _iter_batch_dirs():
            batch_id = batch_dir.name
            state = _read_json(batch_dir / "state.json", default=None)
            if not isinstance(state, dict):
                continue
            if str(state.get("provider") or "") != "tikhub_xhs":
                continue
            if str(state.get("status") or "") not in BATCH_ACTIVE_STATUSES:
                continue
            if state.get("cancel_requested") or _batch_cancel_flag_path(batch_id).exists():
                continue

            launch = _read_json(batch_dir / "launch.json", default={}) or {}
            runner_pid = state.get("runner_pid") or launch.get("runner_pid")
            guard_pid = state.get("guard_pid") or launch.get("guard_pid")
            runner_alive = _is_pid_alive(int(runner_pid)) if runner_pid else False
            guard_alive = _is_pid_alive(int(guard_pid)) if guard_pid else False
            try:
                if runner_alive and not guard_alive:
                    new_guard_pid = _spawn_batch_guard(batch_id, preserve_status=True)
                    recovered.append({
                        "batch_id": batch_id,
                        "action": "guard_restarted",
                        "guard_pid": new_guard_pid,
                    })
                elif not runner_alive and not guard_alive:
                    # runner 从 completed.jsonl 之后接续；先前失败项仍由 guard
                    # 在本轮结束后按既有上限处理，避免遗漏或无限重试。
                    spawned = _spawn_batch_processes(batch_id, resume=True, start_guard=True)
                    recovered.append({
                        "batch_id": batch_id,
                        "action": "runner_resumed",
                        "runner_pid": spawned.get("runner_pid"),
                        "guard_pid": spawned.get("guard_pid"),
                    })
            except Exception:
                LOG.exception("[refresh] unable to recover orphaned TikHub batch %s", batch_id)
    return recovered


def resume_batch(batch_id: str) -> dict[str, Any]:
    """显式恢复已停止/失败的 TikHub 批次；不新建关键词清单，也不碰 RedFox。"""
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        raise ValueError("batch_id is required")
    active = get_active_batch_status()
    if active:
        raise BatchAlreadyRunningError(active)
    state_path = _batch_state_path(batch_id)
    state = _read_json(state_path, default=None)
    if not isinstance(state, dict):
        raise FileNotFoundError(f"batch not found: {batch_id}")
    status = str(state.get("status") or "")
    if status == "completed":
        raise ValueError("completed batch does not need resume")
    if status not in BATCH_FINAL_STATUSES:
        raise ValueError(f"batch status {status or 'unknown'} cannot be resumed")
    if str(state.get("provider") or "") != "tikhub_xhs":
        raise ValueError("legacy batch cannot be resumed; please start a new TikHub refresh")
    launch = _read_json(_batch_launch_path(batch_id), default={}) or {}
    guard_pid = launch.get("guard_pid") or state.get("guard_pid")
    if guard_pid and _is_pid_alive(int(guard_pid)):
        raise ValueError("batch guard is still stopping; retry in a few seconds")

    try:
        _batch_cancel_flag_path(batch_id).unlink()
    except FileNotFoundError:
        pass
    failed_ids = _read_failed_keywords(batch_id)
    state.update({
        "status": "starting",
        "finished_at": None,
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancelled_at": None,
        "cancel_reason": "",
        "resume_requested_at": _now(),
        "resume_include_failed": bool(failed_ids),
        "error": None,
    })
    _write_json(state_path, state)
    _spawn_batch_processes(
        batch_id,
        resume=True,
        include_failed=bool(failed_ids),
        start_guard=True,
    )
    return get_batch_status(batch_id) or _normalize_batch_state(state)


def start_batch_refresh(
    selected_keywords: list[dict[str, Any]],
    source: str = "web_refresh_all",
    refresh_round: int | None = None,
) -> dict[str, Any]:
    """启动 TikHub 批量刷新 runner。

    selected_keywords: list of {keyword_id, keyword_text, group_id, ...}
    """
    active = get_active_batch_status()
    if active:
        raise BatchAlreadyRunningError(active)
    single_status = get_single_refresh_status()
    if single_status:
        raise BatchAlreadyRunningError({
            "status": "single_refresh_running",
            "current_keyword": single_status.get("current"),
            "queue_length": single_status.get("queue_length", 0),
        })

    cleaned = []
    seen_ids: set[str] = set()
    for item in selected_keywords:
        kid = str(item.get("keyword_id") or "").strip()
        text = str(item.get("keyword_text") or "").strip()
        if not kid or not text or kid in seen_ids:
            continue
        seen_ids.add(kid)
        cleaned.append({
            "group_id": item.get("group_id"),
            "group_label": item.get("group_label"),
            "group_order": item.get("group_order", 0),
            "keyword_id": kid,
            "keyword_text": text,
        })
    if not cleaned:
        raise ValueError("no selected keywords")
    if not TIKHUB_BATCH_RUNNER.exists():
        raise FileNotFoundError(f"tikhub batch runner not found: {TIKHUB_BATCH_RUNNER}")

    batch_id = _make_batch_id()
    batch_dir = _batch_dir(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)

    bootstrap_state = {
        "batch_id": batch_id,
        "status": "starting",
        "started_at": _now(),
        "finished_at": None,
        "total_keywords": len(cleaned),
        "success_count": 0,
        "failed_count": 0,
        "pending_count": len(cleaned),
        "source": source,
        "refresh_round": refresh_round,
        "provider": "tikhub_xhs",
        "runner_pid": None,
        "current_keyword": None,
        "current_attempt": None,
        "heartbeat_at": _now(),
    }
    _write_json(_batch_state_path(batch_id), bootstrap_state)

    keywords_path = batch_dir / "keywords.json"
    _write_json(keywords_path, {"updated_at": _now(), "keywords": cleaned})

    try:
        # Popen 后立即返回 HTTP 202；不能再像旧版一样 run(timeout=30) 把
        # 151 词长任务在第 30 秒杀掉。guard 会处理 runner 异常退出与有限次续跑。
        _spawn_batch_processes(batch_id)
    except Exception as exc:
        failed_state = {**bootstrap_state, "status": "failed", "finished_at": _now(), "error": str(exc)}
        _write_json(_batch_state_path(batch_id), failed_state)
        raise RuntimeError(str(exc))

    return get_batch_status(batch_id) or _normalize_batch_state(bootstrap_state)


def _classify_failure(stderr_tail: str, diagnostic_summary: str = "") -> str:
    text = (stderr_tail or "").strip()
    if diagnostic_summary:
        return diagnostic_summary
    if not text:
        return "未知错误"
    if "TIKHUB_API_TOKEN" in text:
        return "缺少 TIKHUB_API_TOKEN"
    if "Connection" in text or "网络错误" in text or "ConnectError" in text:
        return "TikHub 网络错误"
    if "超时" in text or "timeout" in text or "Timeout" in text:
        return "请求超时"
    if "2000" in text:
        return "TikHub 返回异常状态"
    return text[:80]
