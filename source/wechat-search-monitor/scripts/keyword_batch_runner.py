#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐词串行运行微信搜索，适合长时间无人值守批跑。

设计目标：
1. 每个关键词单独调用一次上游客户端，避免单词失败拖垮整批。
2. 每个关键词单独落地到子目录，避免正文文件同名覆盖。
3. 状态持续写入 data/runs/<batch_id>/，支持巡检与断点续跑。
4. 跑完后自动触发重建，让网页数据可见。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_KEYWORDS_FILE = PROJECT_ROOT / "data" / "keyword_lists" / "2026-06-07_过夜100词.txt"
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "data" / "runs"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "微信搜索结果" / "批量抓取"
DEFAULT_CLIENT_SCRIPT = Path("/Users/works14/.skills-manager/skills/zk-wechat-search/scripts/wechat_search_client.py")
DEFAULT_REBUILD_SCRIPT = PROJECT_ROOT / "scripts" / "rebuild_data.py"
ARTICLE_METRIC_SCRIPT = PROJECT_ROOT / "scripts" / "build_article_metric_observations.py"
DEFAULT_SERVER = os.environ.get("WX_SEARCH_SERVER", "http://192.168.31.238:8000")

FINAL_STATUSES = {"completed", "completed_with_failures", "failed", "cancelled"}


def now_dt() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now_dt().isoformat(timespec="seconds")


def sanitize_filename(text: str, max_length: int = 48) -> str:
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "keyword"
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_keyword_items(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    items: list[dict[str, str]] = []
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        groups = payload.get("groups", []) if isinstance(payload, dict) else []
        for group in groups:
            group_label = str(group.get("label") or "未分类")
            for kw in group.get("keywords", []):
                text = str(kw.get("keyword_text") or "").strip()
                if not text:
                    continue
                item_id = f"{len(items) + 1:03d}"
                items.append({
                    "item_id": item_id,
                    "keyword": text,
                    "safe_name": sanitize_filename(text),
                    "group_label": group_label,
                    "keyword_id": str(kw.get("keyword_id") or ""),
                })
                if limit and len(items) >= limit:
                    return items
        return items

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw in raw_lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        item_id = f"{len(items) + 1:03d}"
        items.append({
            "item_id": item_id,
            "keyword": text,
            "safe_name": sanitize_filename(text),
        })
        if limit and len(items) >= limit:
            break
    return items


def parse_jsonl_item_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        item_id = payload.get("item_id")
        if item_id:
            ids.add(str(item_id))
    return ids


def tail_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def extract_result_json(stdout_text: str) -> dict[str, Any] | None:
    for line in reversed(stdout_text.splitlines()):
        if not line.startswith("RESULT_JSON:"):
            continue
        payload = line.split("RESULT_JSON:", 1)[1].strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


def make_batch_id() -> str:
    return f"overnight_{now_dt().strftime('%Y%m%d_%H%M%S')}"


def build_batch_paths(batch_id: str, runs_root: Path, output_root: Path) -> dict[str, Path]:
    batch_dir = runs_root / batch_id
    return {
        "batch_dir": batch_dir,
        "state_file": batch_dir / "state.json",
        "events_file": batch_dir / "events.jsonl",
        "completed_file": batch_dir / "completed.jsonl",
        "failed_file": batch_dir / "failed.jsonl",
        "rebuilds_file": batch_dir / "rebuilds.jsonl",
        "heartbeat_file": batch_dir / "heartbeat.json",
        "launch_file": batch_dir / "launch.json",
        "cancel_flag": batch_dir / "cancel.flag",
        "keywords_dir": batch_dir,
        "logs_dir": batch_dir / "logs",
        "diagnostics_dir": batch_dir / "diagnostics",
        "output_dir": output_root / batch_id,
    }


def resolve_batch_keywords_file(batch_dir: Path) -> Path | None:
    for candidate in ("keywords.json", "keywords.txt"):
        path = batch_dir / candidate
        if path.exists():
            return path
    return None


def ensure_batch_layout(paths: dict[str, Path]) -> None:
    paths["batch_dir"].mkdir(parents=True, exist_ok=True)
    paths["logs_dir"].mkdir(parents=True, exist_ok=True)
    paths["diagnostics_dir"].mkdir(parents=True, exist_ok=True)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)


def is_cancel_requested(paths: dict[str, Path]) -> bool:
    return paths["cancel_flag"].exists()


_OFFLINE_MARKERS = (
    "无法连接", "Connection refused", "ConnectError",
    "连接被拒绝", "Errno 61", "ConnectionResetError",
)


def is_offline_failure(stderr_tail: str) -> bool:
    """检测失败是否因对方电脑掉线（连接被拒绝/无法连接）。"""
    text = (stderr_tail or "").strip()
    if not text:
        return False
    return any(m in text for m in _OFFLINE_MARKERS)


def parse_server_url(server: str) -> dict[str, Any]:
    """拆出远端服务的地址，供失败体检使用。"""
    raw = (server or "").strip()
    if raw and "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    base_url = f"{scheme}://{parsed.netloc}" if parsed.netloc else raw.rstrip("/")
    return {
        "raw": server,
        "base_url": base_url.rstrip("/"),
        "scheme": scheme,
        "host": host,
        "port": port,
    }


def run_probe_command(command: list[str], timeout: int = 5) -> dict[str, Any]:
    started_ts = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "duration_sec": round(time.time() - started_ts, 2),
            "stdout_tail": tail_text(result.stdout or "", 1200),
            "stderr_tail": tail_text(result.stderr or "", 1200),
            "command": command,
        }
    except Exception as exc:  # pragma: no cover - 诊断兜底，不能影响主流程
        return {
            "ok": False,
            "duration_sec": round(time.time() - started_ts, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "command": command,
        }


def get_default_gateway() -> str:
    route_bin = shutil.which("route")
    if not route_bin:
        return ""
    probe = run_probe_command([route_bin, "-n", "get", "default"], timeout=3)
    text = probe.get("stdout_tail") or ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("gateway:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def ping_probe(host: str, timeout: int = 4) -> dict[str, Any]:
    ping_bin = shutil.which("ping")
    if not host:
        return {"ok": None, "skipped": True, "reason": "empty_host"}
    if not ping_bin:
        return {"ok": None, "skipped": True, "reason": "ping_not_found"}
    return run_probe_command([ping_bin, "-c", "2", "-W", "1000", host], timeout=timeout)


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    started_ts = time.time()
    if not host or not port:
        return {"ok": False, "error": "empty_host_or_port"}
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "duration_sec": round(time.time() - started_ts, 2)}
    except Exception as exc:
        return {
            "ok": False,
            "duration_sec": round(time.time() - started_ts, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def http_get_probe(base_url: str, path: str, timeout: float = 5.0) -> dict[str, Any]:
    started_ts = time.time()
    url = f"{base_url.rstrip('/')}{path}"
    try:
        req = Request(url, headers={"User-Agent": "wechat-monitor-diagnostic/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(2000).decode("utf-8", errors="replace")
            payload: Any = None
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            status = int(getattr(resp, "status", 0) or 0)
            return {
                "ok": 200 <= status < 400,
                "http_reachable": True,
                "status": status,
                "duration_sec": round(time.time() - started_ts, 2),
                "body_tail": tail_text(body, 1200),
                "json": payload,
                "url": url,
            }
    except HTTPError as exc:
        body = exc.read(2000).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return {
            "ok": False,
            "http_reachable": True,
            "status": exc.code,
            "duration_sec": round(time.time() - started_ts, 2),
            "body_tail": tail_text(body, 1200),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "url": url,
        }
    except URLError as exc:
        return {
            "ok": False,
            "http_reachable": False,
            "duration_sec": round(time.time() - started_ts, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "url": url,
        }
    except Exception as exc:  # pragma: no cover - 诊断兜底，不能影响主流程
        return {
            "ok": False,
            "http_reachable": False,
            "duration_sec": round(time.time() - started_ts, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "url": url,
        }


def summarize_diagnostics(diagnostic: dict[str, Any]) -> str:
    probes = diagnostic.get("probes") or {}
    gateway_ping = probes.get("gateway_ping") or {}
    remote_ping = probes.get("remote_ping") or {}
    tcp = probes.get("tcp_8000") or {}
    health = probes.get("http_health") or {}
    status = probes.get("http_status") or {}
    status_json = status.get("json") if isinstance(status.get("json"), dict) else {}

    if gateway_ping.get("ok") is False and remote_ping.get("ok") is False:
        return "本机或局域网网络异常"
    if remote_ping.get("ok") is False:
        return "对方电脑可能离线或局域网不通"
    if tcp.get("ok") is False:
        return "对方电脑在线，但搜索服务端口不通"
    if health.get("http_reachable") is True and health.get("status") and health.get("status") >= 500:
        return "搜索服务在线，但健康检查报错"
    if health.get("ok") is True:
        if status_json.get("busy") is True:
            return "搜索服务在线，但抓取任务可能卡住"
        return "搜索服务当前在线，像是刚才短暂闪断"
    if health.get("http_reachable") is False:
        return "对方电脑在线，但搜索服务访问异常"
    return "失败原因未能自动确认"


def compact_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    probes = diagnostic.get("probes") or {}
    status_json = (probes.get("http_status") or {}).get("json")
    if not isinstance(status_json, dict):
        status_json = {}
    return {
        "summary": diagnostic.get("summary"),
        "gateway_ping_ok": (probes.get("gateway_ping") or {}).get("ok"),
        "remote_ping_ok": (probes.get("remote_ping") or {}).get("ok"),
        "tcp_ok": (probes.get("tcp_8000") or {}).get("ok"),
        "health_ok": (probes.get("http_health") or {}).get("ok"),
        "health_status": (probes.get("http_health") or {}).get("status"),
        "remote_busy": status_json.get("busy"),
        "remote_current_keywords": status_json.get("current_keywords"),
        "remote_running_since": status_json.get("running_since"),
    }


def collect_failure_diagnostics(
    paths: dict[str, Path],
    server: str,
    item_id: str,
    keyword: str,
    safe_name: str,
    attempt: int,
    attempt_record: dict[str, Any],
) -> dict[str, Any]:
    """失败瞬间做一次轻量体检，并把完整结果落盘。"""
    server_info = parse_server_url(server)
    gateway = get_default_gateway()
    tcp_result = tcp_probe(server_info["host"], int(server_info["port"]))
    if tcp_result.get("ok"):
        health_result = http_get_probe(server_info["base_url"], "/health")
        status_result = http_get_probe(server_info["base_url"], "/status")
    else:
        health_result = {"ok": None, "skipped": True, "reason": "tcp_failed"}
        status_result = {"ok": None, "skipped": True, "reason": "tcp_failed"}
    probes = {
        "gateway_ping": ping_probe(gateway) if gateway else {"ok": None, "skipped": True, "reason": "gateway_not_found"},
        "remote_ping": ping_probe(server_info["host"]),
        "tcp_8000": tcp_result,
        "http_health": health_result,
        "http_status": status_result,
    }
    diagnostic = {
        "at": now_iso(),
        "probe_version": 1,
        "batch_id": paths["batch_dir"].name,
        "item_id": item_id,
        "keyword": keyword,
        "attempt": attempt,
        "server": server_info,
        "default_gateway": gateway,
        "attempt_failure": {
            "returncode": attempt_record.get("returncode"),
            "timeout_hit": attempt_record.get("timeout_hit"),
            "duration_sec": attempt_record.get("duration_sec"),
            "stderr_tail": attempt_record.get("stderr_tail"),
            "log_path": attempt_record.get("log_path"),
        },
        "probes": probes,
    }
    diagnostic["summary"] = summarize_diagnostics(diagnostic)
    filename = f"{item_id}_{safe_name}.attempt{attempt}_{now_dt().strftime('%H%M%S')}.json"
    path = paths["diagnostics_dir"] / filename
    write_json(path, diagnostic)
    diagnostic["path"] = str(path.relative_to(PROJECT_ROOT))
    return diagnostic


def mark_batch_cancelled(
    paths: dict[str, Path],
    state: dict[str, Any],
    success_count: int,
    failed_count: int,
    total_keywords: int,
    cancel_reason: str = "",
) -> None:
    cancelled_at = now_iso()
    pending_count = max(total_keywords - success_count - failed_count, 0)
    update_state(
        paths,
        state,
        status="cancelled",
        finished_at=cancelled_at,
        current_item_id=None,
        current_keyword=None,
        current_attempt=None,
        success_count=success_count,
        failed_count=failed_count,
        pending_count=pending_count,
        cancel_requested=True,
        cancelled_at=cancelled_at,
        cancel_reason=cancel_reason,
    )
    write_event(
        paths,
        "batch_cancelled",
        {
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "cancel_reason": cancel_reason,
        },
    )


def update_state(paths: dict[str, Path], state: dict[str, Any], **changes: Any) -> dict[str, Any]:
    state.update(changes)
    state["updated_at"] = now_iso()
    state["heartbeat_at"] = state["updated_at"]
    write_json(paths["state_file"], state)
    write_json(
        paths["heartbeat_file"],
        {
            "batch_id": state["batch_id"],
            "status": state["status"],
            "heartbeat_at": state["heartbeat_at"],
            "current_item_id": state.get("current_item_id"),
            "current_keyword": state.get("current_keyword"),
        },
    )
    return state


def write_event(paths: dict[str, Path], event_type: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        paths["events_file"],
        {
            "at": now_iso(),
            "event": event_type,
            **payload,
        },
    )


def build_command(
    client_script: Path,
    server: str,
    keyword: str,
    output_dir: Path,
    fetch_depth: int,
    fetch_max_count: int,
    timeout: int,
) -> list[str]:
    return [
        sys.executable,
        str(client_script),
        keyword,
        "--server",
        server,
        "--fetch-depth",
        str(fetch_depth),
        "--fetch-max-count",
        str(fetch_max_count),
        "--timeout",
        str(timeout),
        "--output-dir",
        str(output_dir),
        "--no-ai-summary",
    ]


def run_client_once(
    command: list[str],
    log_path: Path,
    overall_timeout: int,
) -> dict[str, Any]:
    started_at = now_iso()
    started_ts = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=overall_timeout,
        )
        timeout_hit = False
        stdout_text = result.stdout or ""
        stderr_text = result.stderr or ""
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        timeout_hit = True
        stdout_text = exc.stdout or ""
        stderr_text = (exc.stderr or "") + f"\n[runner] overall timeout after {overall_timeout}s"
        returncode = 124

    finished_at = now_iso()
    duration_sec = round(time.time() - started_ts, 2)
    log_path.write_text(
        "\n".join(
            [
                f"$ {' '.join(command)}",
                "",
                "===== STDOUT =====",
                stdout_text,
                "",
                "===== STDERR =====",
                stderr_text,
            ]
        ),
        encoding="utf-8",
    )
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_sec": duration_sec,
        "returncode": returncode,
        "timeout_hit": timeout_hit,
        "stdout_tail": tail_text(stdout_text),
        "stderr_tail": tail_text(stderr_text),
        "result_json": extract_result_json(stdout_text),
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
    }


def run_rebuild(paths: dict[str, Path], rebuild_script: Path, reason: str) -> dict[str, Any]:
    command = [sys.executable, str(rebuild_script)]
    result = run_client_once(
        command=command,
        log_path=paths["logs_dir"] / f"rebuild_{reason}_{now_dt().strftime('%H%M%S')}.log",
        overall_timeout=300,
    )
    record = {
        "reason": reason,
        "ok": result["returncode"] == 0,
        **result,
    }
    append_jsonl(paths["rebuilds_file"], record)
    return record


def run_article_metric_rebuild(paths: dict[str, Path]) -> dict[str, Any]:
    result = run_client_once(
        command=[sys.executable, str(ARTICLE_METRIC_SCRIPT)],
        log_path=paths["logs_dir"] / f"article_metrics_final_{now_dt().strftime('%H%M%S')}.log",
        overall_timeout=600,
    )
    record = {
        "reason": "article_metrics_final",
        "ok": result["returncode"] == 0,
        **result,
    }
    append_jsonl(paths["rebuilds_file"], record)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="逐词串行批跑微信搜索")
    parser.add_argument("--keywords-file", required=True, help="关键词清单文件（由启动器从统一注册表导出）")
    parser.add_argument("--batch-id", default="", help="批次 ID；为空时自动生成")
    parser.add_argument("--resume", action="store_true", help="恢复既有批次")
    parser.add_argument("--include-failed", action="store_true", help="恢复时重跑已失败关键词")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个关键词，用于压测前小样本验证")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="微信搜索服务地址")
    parser.add_argument("--client-script", default=str(DEFAULT_CLIENT_SCRIPT), help="上游客户端脚本路径")
    parser.add_argument("--rebuild-script", default=str(DEFAULT_REBUILD_SCRIPT), help="重建脚本路径")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT), help="批次状态根目录")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="搜索结果落地根目录")
    parser.add_argument("--fetch-depth", type=int, choices=[0, 1], default=1, help="抓取深度")
    parser.add_argument("--fetch-max-count", type=int, default=5, help="正文抓取上限")
    parser.add_argument("--timeout", type=int, default=480, help="单词请求超时，秒")
    parser.add_argument("--overall-timeout-buffer", type=int, default=120, help="在客户端 timeout 基础上额外预留秒数")
    parser.add_argument("--max-attempts", type=int, default=2, help="每个关键词最多尝试次数")
    parser.add_argument("--retry-sleep", type=int, default=20, help="重试前等待秒数")
    parser.add_argument("--rebuild-every", type=int, default=10, help="每成功 N 个关键词触发一次中途重建，0 表示关闭")
    parser.add_argument("--skip-final-rebuild", action="store_true", help="结束后不做最终重建")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    keywords_file = Path(args.keywords_file).expanduser().resolve()
    client_script = Path(args.client_script).expanduser().resolve()
    rebuild_script = Path(args.rebuild_script).expanduser().resolve()
    runs_root = Path(args.runs_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    if not keywords_file.exists():
        print(f"[error] 关键词文件不存在：{keywords_file}", file=sys.stderr)
        return 1
    if not client_script.exists():
        print(f"[error] 上游客户端不存在：{client_script}", file=sys.stderr)
        return 1
    if not rebuild_script.exists():
        print(f"[error] 重建脚本不存在：{rebuild_script}", file=sys.stderr)
        return 1

    batch_id = args.batch_id or make_batch_id()
    paths = build_batch_paths(batch_id=batch_id, runs_root=runs_root, output_root=output_root)

    if args.resume:
        if not paths["batch_dir"].exists():
            print(f"[error] 恢复失败，批次目录不存在：{paths['batch_dir']}", file=sys.stderr)
            return 1
    else:
        if paths["state_file"].exists():
            print(f"[error] 批次状态已存在，请更换 batch-id 或使用 --resume：{paths['state_file']}", file=sys.stderr)
            return 1

    ensure_batch_layout(paths)

    if args.resume:
        batch_keywords_file = resolve_batch_keywords_file(paths["keywords_dir"])
        if not batch_keywords_file:
            print(f"[error] 恢复失败，批次内缺少关键词快照（keywords.json/txt）", file=sys.stderr)
            return 1
    else:
        kw_dest = paths["keywords_dir"] / keywords_file.name
        shutil.copyfile(keywords_file, kw_dest)
        batch_keywords_file = kw_dest

    keyword_items = load_keyword_items(batch_keywords_file, limit=args.limit or None)
    if not keyword_items:
        print("[error] 关键词清单为空", file=sys.stderr)
        return 1

    completed_ids = parse_jsonl_item_ids(paths["completed_file"])
    failed_ids_all = parse_jsonl_item_ids(paths["failed_file"])
    failed_ids_outstanding = failed_ids_all - completed_ids
    skipped_failed_ids = set() if args.include_failed else failed_ids_outstanding
    pending_items = [
        item for item in keyword_items
        if item["item_id"] not in completed_ids and item["item_id"] not in skipped_failed_ids
    ]

    state = read_json(paths["state_file"], default={}) or {}
    if not state:
        state = {
            "batch_id": batch_id,
            "status": "running",
            "started_at": now_iso(),
            "finished_at": None,
            "runner_pid": os.getpid(),
            "keywords_file": str(batch_keywords_file),
            "source_keywords_file": str(keywords_file),
            "runs_root": str(runs_root),
            "output_root": str(output_root),
            "fetch_depth": args.fetch_depth,
            "fetch_max_count": args.fetch_max_count,
            "timeout": args.timeout,
            "max_attempts": args.max_attempts,
            "retry_sleep": args.retry_sleep,
            "rebuild_every": args.rebuild_every,
            "server": args.server,
            "total_keywords": len(keyword_items),
            "success_count": len(completed_ids),
            "failed_count": 0 if args.include_failed else len(failed_ids_outstanding),
            "pending_count": len(pending_items),
            "current_item_id": None,
            "current_keyword": None,
            "current_attempt": None,
            "last_success_item_id": None,
            "last_success_keyword": None,
            "last_failure_item_id": None,
            "last_failure_keyword": None,
            "final_rebuild_ok": None,
            "final_rebuild_reason": None,
        }
    else:
        state["runner_pid"] = os.getpid()
        state["status"] = "running"
        state["total_keywords"] = len(keyword_items)
        state["success_count"] = len(completed_ids)
        state["failed_count"] = 0 if args.include_failed else len(failed_ids_outstanding)
        state["pending_count"] = len(pending_items)

    update_state(paths, state)

    write_event(
        paths,
        "batch_started" if not args.resume else "batch_resumed",
        {
            "batch_id": batch_id,
            "total_keywords": len(keyword_items),
            "pending_count": len(pending_items),
            "resume": args.resume,
            "include_failed": args.include_failed,
        },
    )

    overall_timeout = args.timeout + max(args.overall_timeout_buffer, 0)
    success_count = len(completed_ids)
    failed_count = 0 if args.include_failed else len(failed_ids_outstanding)
    total_keywords = len(keyword_items)
    cancelled = False

    for item in pending_items:
        if is_cancel_requested(paths):
            cancelled = True
            mark_batch_cancelled(paths, state, success_count, failed_count, total_keywords)
            break

        item_id = item["item_id"]
        keyword = item["keyword"]
        safe_name = item["safe_name"]
        keyword_output_dir = paths["output_dir"] / f"{item_id}_{safe_name}"
        keyword_output_dir.mkdir(parents=True, exist_ok=True)

        last_attempt_record: dict[str, Any] | None = None
        success = False

        for attempt in range(1, args.max_attempts + 1):
            update_state(
                paths,
                state,
                current_item_id=item_id,
                current_keyword=keyword,
                current_attempt=attempt,
                success_count=success_count,
                failed_count=failed_count,
                pending_count=len(keyword_items) - success_count - failed_count,
            )
            write_event(
                paths,
                "keyword_started",
                {
                    "item_id": item_id,
                    "keyword": keyword,
                    "attempt": attempt,
                    "output_dir": str(keyword_output_dir),
                },
            )

            command = build_command(
                client_script=client_script,
                server=args.server,
                keyword=keyword,
                output_dir=keyword_output_dir,
                fetch_depth=args.fetch_depth,
                fetch_max_count=args.fetch_max_count,
                timeout=args.timeout,
            )
            log_path = paths["logs_dir"] / f"{item_id}_{safe_name}.attempt{attempt}.log"
            attempt_record = run_client_once(command=command, log_path=log_path, overall_timeout=overall_timeout)
            last_attempt_record = attempt_record

            if attempt_record["returncode"] == 0:
                record = {
                    "item_id": item_id,
                    "keyword": keyword,
                    "attempt": attempt,
                    "output_dir": str(keyword_output_dir),
                    **attempt_record,
                }
                append_jsonl(paths["completed_file"], record)
                success_count += 1
                success = True
                update_state(
                    paths,
                    state,
                    success_count=success_count,
                    failed_count=failed_count,
                    pending_count=len(keyword_items) - success_count - failed_count,
                    last_success_item_id=item_id,
                    last_success_keyword=keyword,
                )
                write_event(
                    paths,
                    "keyword_succeeded",
                    {
                        "item_id": item_id,
                        "keyword": keyword,
                        "attempt": attempt,
                        "duration_sec": attempt_record["duration_sec"],
                    },
                )
                break

            diagnostic: dict[str, Any] | None = None
            try:
                diagnostic = collect_failure_diagnostics(
                    paths=paths,
                    server=args.server,
                    item_id=item_id,
                    keyword=keyword,
                    safe_name=safe_name,
                    attempt=attempt,
                    attempt_record=attempt_record,
                )
                compact = compact_diagnostic(diagnostic)
                attempt_record["diagnostic_summary"] = diagnostic.get("summary")
                attempt_record["diagnostic_path"] = diagnostic.get("path")
                attempt_record["diagnostic"] = compact
                write_event(
                    paths,
                    "failure_diagnostic",
                    {
                        "item_id": item_id,
                        "keyword": keyword,
                        "attempt": attempt,
                        "diagnostic_path": diagnostic.get("path"),
                        **compact,
                    },
                )
            except Exception as exc:  # pragma: no cover - 诊断失败不能拖垮批跑
                attempt_record["diagnostic_summary"] = "失败诊断本身出错"
                attempt_record["diagnostic_error"] = f"{type(exc).__name__}: {exc}"

            write_event(
                paths,
                "keyword_attempt_failed",
                {
                    "item_id": item_id,
                    "keyword": keyword,
                    "attempt": attempt,
                    "returncode": attempt_record["returncode"],
                    "timeout_hit": attempt_record["timeout_hit"],
                    "diagnostic_summary": attempt_record.get("diagnostic_summary"),
                },
            )
            if is_offline_failure(attempt_record.get("stderr_tail") or ""):
                if attempt < args.max_attempts:
                    write_event(
                        paths,
                        "keyword_retry_scheduled",
                        {
                            "item_id": item_id,
                            "keyword": keyword,
                            "attempt": attempt,
                            "reason": "连接失败，先等一会儿再试一次",
                            "sleep_sec": max(args.retry_sleep, 0),
                            "diagnostic_summary": attempt_record.get("diagnostic_summary"),
                        },
                    )
                    time.sleep(max(args.retry_sleep, 0))
                    continue
                break
            if attempt < args.max_attempts:
                time.sleep(max(args.retry_sleep, 0))

        if not success:
            failed_count += 1
            record = {
                "item_id": item_id,
                "keyword": keyword,
                "output_dir": str(keyword_output_dir),
                **(last_attempt_record or {}),
            }
            append_jsonl(paths["failed_file"], record)
            update_state(
                paths,
                state,
                failed_count=failed_count,
                pending_count=len(keyword_items) - success_count - failed_count,
                last_failure_item_id=item_id,
                last_failure_keyword=keyword,
                last_failure_diagnostic_summary=record.get("diagnostic_summary"),
                last_failure_diagnostic_path=record.get("diagnostic_path"),
            )
            write_event(
                paths,
                "keyword_failed",
                {
                    "item_id": item_id,
                    "keyword": keyword,
                    "diagnostic_summary": record.get("diagnostic_summary"),
                },
            )
            if is_offline_failure(last_attempt_record.get("stderr_tail") if last_attempt_record else ""):
                cancelled = True
                cancel_reason = (
                    (last_attempt_record or {}).get("diagnostic_summary")
                    or "对方电脑掉线"
                )
                mark_batch_cancelled(
                    paths, state, success_count, failed_count, total_keywords,
                    cancel_reason=cancel_reason,
                )
                break

        if is_cancel_requested(paths):
            cancelled = True
            mark_batch_cancelled(paths, state, success_count, failed_count, total_keywords)
            break

        if args.rebuild_every > 0 and success_count > 0 and success_count % args.rebuild_every == 0:
            update_state(paths, state, current_item_id=None, current_keyword=None, current_attempt=None)
            rebuild_record = run_rebuild(paths, rebuild_script, reason=f"midway_{success_count:03d}")
            update_state(
                paths,
                state,
                last_rebuild_at=rebuild_record["finished_at"],
                last_rebuild_ok=rebuild_record["ok"],
                last_rebuild_reason=rebuild_record["reason"],
            )
            write_event(
                paths,
                "rebuild_finished",
                {
                    "reason": rebuild_record["reason"],
                    "ok": rebuild_record["ok"],
                    "returncode": rebuild_record["returncode"],
                },
            )

    if cancelled:
        print(json.dumps(
            {
                "batch_id": batch_id,
                "status": "cancelled",
                "success_count": success_count,
                "failed_count": failed_count,
                "batch_dir": str(paths["batch_dir"]),
                "output_dir": str(paths["output_dir"]),
            },
            ensure_ascii=False,
        ))
        return 0

    final_rebuild_ok = None
    if not args.skip_final_rebuild:
        update_state(paths, state, current_item_id=None, current_keyword=None, current_attempt=None)
        rebuild_record = run_rebuild(paths, rebuild_script, reason="final")
        final_rebuild_ok = rebuild_record["ok"]
        update_state(
            paths,
            state,
            last_rebuild_at=rebuild_record["finished_at"],
            last_rebuild_ok=rebuild_record["ok"],
            last_rebuild_reason=rebuild_record["reason"],
            final_rebuild_ok=rebuild_record["ok"],
            final_rebuild_reason="final",
        )
        write_event(
            paths,
            "rebuild_finished",
            {
                "reason": "final",
                "ok": rebuild_record["ok"],
                "returncode": rebuild_record["returncode"],
            },
        )
        if rebuild_record["ok"]:
            metric_record = run_article_metric_rebuild(paths)
            final_rebuild_ok = metric_record["ok"]
            update_state(
                paths,
                state,
                article_metrics_rebuild_at=metric_record["finished_at"],
                article_metrics_rebuild_ok=metric_record["ok"],
            )
            write_event(
                paths,
                "article_metrics_rebuild_finished",
                {
                    "ok": metric_record["ok"],
                    "returncode": metric_record["returncode"],
                },
            )

    final_status = "completed"
    if failed_count > 0 or final_rebuild_ok is False:
        final_status = "completed_with_failures"

    update_state(
        paths,
        state,
        status=final_status,
        finished_at=now_iso(),
        current_item_id=None,
        current_keyword=None,
        current_attempt=None,
        success_count=success_count,
        failed_count=failed_count,
        pending_count=len(keyword_items) - success_count - failed_count,
    )
    write_event(
        paths,
        "batch_finished",
        {
            "status": final_status,
            "success_count": success_count,
            "failed_count": failed_count,
            "final_rebuild_ok": final_rebuild_ok,
        },
    )

    print(json.dumps(
        {
            "batch_id": batch_id,
            "status": final_status,
            "success_count": success_count,
            "failed_count": failed_count,
            "batch_dir": str(paths["batch_dir"]),
            "output_dir": str(paths["output_dir"]),
        },
        ensure_ascii=False,
    ))
    return 0 if final_status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
