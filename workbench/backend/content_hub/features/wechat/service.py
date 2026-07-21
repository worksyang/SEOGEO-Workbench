from __future__ import annotations

import ast
import base64
import codecs
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import zlib
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import WriterLockTimeout, writer_lock
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.repositories.wechat_legacy import WechatLegacyRepository
from content_hub.services.migration import MigrationResolver
from content_hub.services.wechat_refresh import WechatRefreshService
from content_hub.ingestion.source_manifests import manifest_id_for, manifest_ref, write_manifest
from content_hub.validation.urls import canonicalize_url

SOURCE_TZ = ZoneInfo("Asia/Shanghai")
CANONICAL_METRIC_KEYS = {
    "read_delta_estimated": ("wechat.keyword.read_delta_estimated", "关键词阅读增量估算"),
    "read_delta_raw": ("wechat.keyword.read_delta_raw", "关键词原始阅读增量"),
    "steady_read_median": ("wechat.keyword.steady_read_median", "关键词稳定阅读中位数"),
    "confidence_score": ("wechat.keyword.confidence_score", "关键词阅读增量置信度"),
    "trend_signal": ("wechat.keyword.trend_signal", "关键词趋势信号"),
    "daily_read_delta": ("wechat.keyword.daily_read_delta", "关键词日阅读增量"),
}
ARTICLE_METRIC_KEYS = {
    "read_count": ("wechat.article.read_count", "微信文章阅读数"),
    "like_count": ("wechat.article.like_count", "微信文章点赞数"),
    "friends_follow_count": ("wechat.article.friends_follow_count", "微信文章在看数"),
    "original_article_count": ("wechat.article.original_article_count", "微信文章原创数"),
}
def _id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:20]}"


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_time(value: Any) -> str | None:
    if value is None or str(value).strip() == "": return None
    raw = str(value).strip()
    candidates = [raw, raw.replace("/", "-")]
    parsed = None
    for candidate in candidates:
        try:
            if re.fullmatch(r"\d{2}-\d{2}-\d{2}", candidate): parsed = datetime.strptime(candidate, "%y-%m-%d").replace(tzinfo=SOURCE_TZ); break
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00")); break
        except ValueError: continue
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try: parsed = datetime.strptime(raw, fmt).replace(tzinfo=SOURCE_TZ); break
            except ValueError: pass
    if parsed is None: return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=SOURCE_TZ)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime_value(value: Any) -> datetime | None:
    normalized = _source_time(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool): return None
    try:
        number = float(str(value).replace(",", "").strip())
        if not math.isfinite(number): return None
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError): return None


def _safe_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or _is_placeholder_url(raw): return None
    try: return canonicalize_url(raw)
    except ValidationAppError: return None


def _is_placeholder_url(value: Any) -> bool:
    return str(value or "").strip().lower().startswith("placeholder://")


def _legacy_keyword_status(row: dict[str, Any]) -> str:
    if row.get("status") in {"active", "paused", "archived"}:
        return str(row["status"])
    if row.get("status") in {"inactive", "disabled", "deleted"}:
        return "archived"
    if row.get("is_active") is False or row.get("active") is False or row.get("enabled") is False:
        return "archived"
    if row.get("archived") is True or row.get("deleted") is True:
        return "archived"
    return "active"


def _keyword_manage_visible(row: dict[str, Any]) -> bool:
    """Match the legacy R15 page visibility, not the 457-row history registry."""
    return row.get("status") == "active" and row.get("archived_at") is None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The frozen reference process iterates frozensets while pruning bootstrap.
# Pin the observed insertion order so R02 bytes/ETag do not depend on this
# process' PYTHONHASHSEED.
_BOOTSTRAP_KEYWORD_FIELDS = (
    "today_best", "keyword", "heat_summary", "pin_order", "keyword_id",
    "coverage_days", "is_pinned", "article_count", "tracked_accounts",
    "today_count", "history_best", "keyword_bucket", "history_hits", "topic",
    "kw_score",
)
_BOOTSTRAP_ACCOUNT_FIELDS = (
    "account_id", "name", "headimg_url",
    "score", "score_raw", "score_yesterday", "score_delta", "score_level",
    "timeliness_score", "timeliness_score_raw", "timeliness_score_yesterday",
    "timeliness_score_delta", "timeliness_score_level",
    "today_score", "today_score_raw", "today_score_yesterday",
    "today_score_delta", "today_score_level",
    "article_count", "kw_count", "topic_count", "bucket_count",
    "today_hit_count", "recent_hit_days", "current_streak", "longest_streak",
    "friends_follow_count", "original_article_count", "move_summary",
)
_BOOTSTRAP_DELTA_FIELDS = (
    "slot_coverage_ratio", "read_delta_estimated",
    "provisional_steady_read_median", "snapshot_count", "steady_read_median",
    "confidence_level", "trend_signal", "trend_label", "observed_share",
    "provisional_sample_count", "status", "provisional_read_delta_estimated",
    "recent_vs_baseline_ratio", "provisional_status", "estimated_share",
)
_LATEST_RUN_FIELDS = ("run_at", "date", "trigger_type", "result_count", "id", "time")
_LEGACY_BUCKET_OPTIONS = [
    "热门单品", "保司入口词", "单品对比词", "提领成交词", "风控审查词",
    "保费融资词", "香港杠杆寿", "传承架构词", "保单功能词", "缴费结构词", "未分类",
]

# The legacy adapter intentionally offers a convenient all-records API for
# small imports.  A freeze contains several hundred MB of JSON, however, and
# materialising every array as Python dicts multiplies its size by an order of
# magnitude.  Large full imports therefore use the streaming path below.
_STREAMING_IMPORT_BYTES = 256 * 1024 * 1024
_IMPORT_BATCH_SIZE = 500
# Hard cap on the initial scan for the opening '['.  Without this bound a
# frozen source full of leading whitespace or comment-like text would let
# ``prefix`` grow without limit before the streaming loop takes over.  1 MiB
# is large enough to skip legitimate preamble bytes but small enough that a
# pathological input cannot exhaust the Hub's working memory.
_STREAM_LEADING_SCAN_BYTES = 1 << 20
# Initial scan reads in small 4 KiB chunks so we can fail fast on a missing
# array opener.  The main streaming loop uses a larger 64 KiB read so a
# single malformed object can never grow the row buffer beyond ~128 KiB.
_STREAM_SCAN_CHUNK = 4 * 1024
_STREAM_READ_CHUNK = 64 * 1024
_IMPORT_LOCK_NAME = "wechat-import.lock"
_CONTENT_PROTECTED_FIELDS = (
    "creator_id", "title", "published_at", "updated_at", "md_path",
    "payload_json", "file_hash", "content_hash",
)
# These namespaces are identities owned by the WeChat importer.  Any other
# namespace means the canonical row is shared with another system and its
# content facts must not be overwritten by a WeChat replay.
_WECHAT_IDENTIFIER_NAMESPACES = frozenset({
    "wechat_article",
    "wechat_article_id",
    "wechat_url",
    "wechat.article_id",
    "wechat.article_url",
})
_UTF8_BOM = b"\xef\xbb\xbf"
_WHITESPACE_BYTES = frozenset({0x09, 0x0A, 0x0D, 0x20})


def _stream_json_array(path: Path) -> Iterator[dict[str, Any]]:
    """Yield object rows from a top-level JSON array without loading the file.

    Dependency-free: the workbench runtime only requires the stdlib and
    jsonschema.  Memory is bounded in two places:

    1. The initial leading-whitespace scan never reads past
       ``_STREAM_LEADING_SCAN_BYTES`` (1 MiB) without finding the array
       opener; a frozen source full of garbage preamble therefore fails
       fast instead of forcing unbounded allocation.
    2. The main streaming loop holds at most one row plus a 64 KiB buffer.

    A UTF-8 BOM at the start of the file is recognised and stripped so frozen
    sources produced by Windows tooling still parse without preprocessing.
    """
    decoder = json.JSONDecoder()
    with path.open("rb") as handle:
        # Phase 1: bounded leading-whitespace scan.  Read small chunks and
        # short-circuit on the first non-whitespace byte.  A UTF-8 BOM is
        # recognised once at the very start of the file (it is not valid in
        # any other position and is dropped before the whitespace scan).
        scan = bytearray()
        head = handle.read(3)
        if head == _UTF8_BOM:
            pass  # BOM consumed; do not echo it into the scan buffer.
        else:
            scan.extend(head)
        found_bracket = False

        def _scan_first_non_ws(buffer: bytearray) -> None:
            nonlocal found_bracket
            for index, byte in enumerate(buffer):
                if byte in _WHITESPACE_BYTES:
                    continue
                if byte == ord("["):
                    found_bracket = True
                    del buffer[: index + 1]
                return

        _scan_first_non_ws(scan)
        while not found_bracket and len(scan) < _STREAM_LEADING_SCAN_BYTES:
            chunk = handle.read(_STREAM_SCAN_CHUNK)
            if not chunk:
                break
            scan.extend(chunk)
            _scan_first_non_ws(scan)
        if not found_bracket:
            if not scan:
                raise ValueError(f"empty or whitespace-only file: {path}")
            stripped = bytes(scan).lstrip()
            if stripped and not stripped.startswith(b"["):
                raise ValueError(f"expected JSON array: {path}")
            raise ValueError(
                f"leading whitespace in {path} exceeds "
                f"{_STREAM_LEADING_SCAN_BYTES} bytes; refusing to scan further"
            )
        # Phase 2: stream the array body.  The main loop holds at most one
        # row plus a small buffer; chunks are 64 KiB so a malformed object
        # never grows ``prefix`` unboundedly across iterations.
        utf8_decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            prefix = utf8_decoder.decode(bytes(scan), final=False)
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 in JSON array: {path}") from exc
        eof = False

        def _read_more() -> str:
            nonlocal eof
            if eof:
                return ""
            more = handle.read(_STREAM_READ_CHUNK)
            if not more:
                eof = True
                try:
                    return utf8_decoder.decode(b"", final=True)
                except UnicodeDecodeError as exc:
                    raise ValueError(f"invalid UTF-8 in JSON array: {path}") from exc
            try:
                return utf8_decoder.decode(more, final=False)
            except UnicodeDecodeError as exc:
                raise ValueError(f"invalid UTF-8 in JSON array: {path}") from exc

        while True:
            while True:
                prefix = prefix.lstrip()
                if prefix.startswith(","):
                    prefix = prefix[1:]
                    continue
                if prefix.startswith("]"):
                    return
                if prefix:
                    break
                if eof:
                    raise ValueError(f"unterminated JSON array: {path}")
                prefix += _read_more()
            while True:
                try:
                    value, end = decoder.raw_decode(prefix)
                    prefix = prefix[end:]
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError(f"invalid JSON array row: {path}")
                    prefix += _read_more()
            if not isinstance(value, dict):
                raise ValueError(f"JSON array row is not an object: {path}")
            yield value


def _stream_json_count(path: Path) -> int:
    return sum(1 for _ in _stream_json_array(path))


def _projection_json(value: Any) -> str:
    # 旧 monitor_fast_service._json_bytes() 保留 dict 插入顺序；projection
    # payload 会直接作为 Hub 的兼容响应，因此这里不能为了 hash 稳定性重排
    # R01–R04 的 key。source_hash 另走 canonical 序列化。
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _projection_hash(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sorted_json_value(value: Any) -> Any:
    """Match Flask's recursively sorted ``jsonify`` payload order."""
    if isinstance(value, dict):
        return {
            key: _sorted_json_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [_sorted_json_value(item) for item in value]
    return value


def _legacy_refresh_failure_reason(
    stderr_tail: Any, diagnostic_summary: Any = "",
) -> str:
    """Copy frozen refresh_service._classify_failure semantics."""
    diagnostic = str(diagnostic_summary or "").strip()
    if diagnostic:
        return diagnostic
    text = str(stderr_tail or "").strip()
    if not text:
        return "未知错误"
    if "无法连接" in text or "Connection refused" in text or "ConnectError" in text:
        return "对方电脑掉线"
    if "超时" in text or "timeout" in text or "Timeout" in text:
        return "请求超时"
    if "500" in text or "502" in text or "503" in text:
        return "搜索服务异常"
    return text[:80]


def _legacy_failed_rows(batch_dir: Path) -> list[dict[str, Any]]:
    failed_path = batch_dir / "failed.jsonl"
    if not failed_path.is_file():
        return []
    rows = []
    for line in failed_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _legacy_refresh_history(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Reproduce frozen refresh_service.list_batch_history.

    The legacy endpoint sorts directories by directory mtime, truncates to
    ``limit`` *before* skipping directories without state.json, and therefore
    may return fewer than 20 rows.  The isolated 8774 reference returns 15.
    """
    runs_root = root / "data/runs"
    if not runs_root.is_dir():
        return []
    batch_dirs = sorted(
        [path for path in runs_root.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    history = []
    for batch_dir in batch_dirs:
        state_path = batch_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        batch_id = str(state.get("batch_id") or batch_dir.name)
        failed_count = int(state.get("failed_count") or 0)
        failed_rows = _legacy_failed_rows(batch_dir) if failed_count > 0 else []
        failure_reasons = []
        seen_reasons = set()
        failed_keywords = []
        for row in failed_rows:
            reason = _legacy_refresh_failure_reason(
                row.get("stderr_tail"), row.get("diagnostic_summary")
            )
            if reason not in seen_reasons and len(failure_reasons) < 3:
                seen_reasons.add(reason)
                failure_reasons.append(reason)
            keyword = str(row.get("keyword") or "").strip()
            if keyword:
                failed_keywords.append({"keyword": keyword, "reason": reason})
        history.append(_sorted_json_value({
            "batch_id": batch_id,
            "status": str(state.get("status") or "unknown"),
            "total": int(state.get("total_keywords") or 0),
            "success_count": int(state.get("success_count") or 0),
            "failed_count": failed_count,
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "failure_reasons": failure_reasons,
            "failed_keywords": failed_keywords,
            "cancel_reason": str(state.get("cancel_reason") or ""),
            "source": str(state.get("source") or "web_refresh_all"),
            "refresh_round": state.get("refresh_round"),
        }))
    return history


def _scheduler_source_state(root: Path) -> dict[str, Any]:
    """Read the frozen/reference scheduler module without importing it."""
    candidates = (
        root / "app/services/scheduler_service.py",
        root / "code-snapshot/app/services/scheduler_service.py",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in module.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            if not isinstance(node.target, ast.Name) or node.target.id != "_state":
                continue
            try:
                value = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return dict(value)
    return {}


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_scheduler_status(
    root: Path, *, base_url: str, snapshot_date: str,
    use_persisted_config: bool,
) -> dict[str, Any]:
    """Reproduce frozen scheduler_service.get_status without starting it."""
    state = {
        "enabled": False,
        "interval_hours": 3.0,
        "base_url": base_url,
        "next_run_at": None,
        "last_triggered_at": None,
        "last_result": None,
        "daily_keyword_budget": 1550,
        "max_keywords_per_batch": 250,
        "last_plan": None,
        "last_discovery": None,
    }
    state.update(_scheduler_source_state(root))
    if use_persisted_config:
        persisted = _read_json_object(root / "data/state/scheduler.json")
        for key in (
            "enabled", "interval_hours", "base_url",
            "daily_keyword_budget", "max_keywords_per_batch",
        ):
            if key in persisted:
                state[key] = persisted[key]

    policy = {
        "daily_keyword_budget": 1600,
        "scheduled_keyword_budget": 1550,
        "discovery_daily_search_budget": 30,
        "manual_reserve_budget": 20,
    }
    policy.update(_read_json_object(root / "data/config/keyword_refresh_policy.json"))
    scheduled_limit = int(state.get("daily_keyword_budget") or policy["scheduled_keyword_budget"])
    ledger = _read_json_object(root / "data/state/keyword_refresh_ledger.json")
    day_bucket = (ledger.get("daily_budget") or {}).get(snapshot_date) or {}
    scheduler_batches = day_bucket.get("scheduler_batches") or {}
    scheduled_used = sum(
        max(0, int(item.get("reserved_count") or 0))
        for item in scheduler_batches.values()
        if isinstance(item, dict)
    )
    discovery_used = 0
    for state_path in (root / "data/runs").glob("*/state.json"):
        run_state = _read_json_object(state_path)
        if not str(run_state.get("source") or "").startswith("discovery"):
            continue
        if str(run_state.get("started_at") or "")[:10] != snapshot_date:
            continue
        discovery_used += int(run_state.get("total_keywords") or 0)

    total_budget = int(policy["daily_keyword_budget"])
    discovery_limit = int(policy["discovery_daily_search_budget"])
    manual_reserve = int(policy["manual_reserve_budget"])
    payload = dict(state)
    payload["budget"] = {
        "date": snapshot_date,
        "total_daily_budget": total_budget,
        "budget_type": "scheduled",
        "daily_keyword_budget": scheduled_limit,
        "reserved_count": scheduled_used,
        "remaining_count": max(0, scheduled_limit - scheduled_used),
        "discovery_reserved_count": discovery_limit,
        "manual_reserved_count": manual_reserve,
    }
    payload["budget_breakdown"] = {
        "total_daily_budget": total_budget,
        "scheduled": {
            "limit": scheduled_limit,
            "used": scheduled_used,
            "remaining": max(0, scheduled_limit - scheduled_used),
        },
        "discovery": {
            "limit": discovery_limit,
            "used": discovery_used,
            "remaining": max(0, discovery_limit - discovery_used),
        },
        "manual_reserve": manual_reserve,
        "accounted_used": scheduled_used + discovery_used,
        "unallocated_or_manual_remaining": max(
            0, total_budget - scheduled_used - discovery_used
        ),
    }
    return _sorted_json_value(payload)


def _projection_latest_run(run: Any) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    return {key: run.get(key) for key in _LATEST_RUN_FIELDS if key in run}


def _projection_turnover_runs(keyword: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for run in keyword.get("runs", []) or []:
        articles = []
        for article in run.get("articles", []) or []:
            item = {"article_id": article.get("article_id")}
            if not item["article_id"]:
                item["title"] = article.get("title")
                item["url"] = article.get("url")
            articles.append(item)
        result.append({"id": run.get("id"), "date": run.get("date"), "time": run.get("time"), "articles": articles})
    return result


def _projection_keyword_prune(keyword: dict[str, Any]) -> dict[str, Any]:
    result = {key: keyword.get(key) for key in _BOOTSTRAP_KEYWORD_FIELDS if key in keyword}
    result["history_best"] = keyword.get("history_best") if isinstance(keyword.get("history_best"), list) else []
    result["history_hits"] = keyword.get("history_hits") if isinstance(keyword.get("history_hits"), list) else []
    result["latest_run"] = _projection_latest_run(keyword.get("latest_run"))
    delta = keyword.get("keyword_read_delta")
    if isinstance(delta, dict):
        # 保留旧列表渲染需要的少量指标，删除每日点阵及重复窗口元数据。
        delta_fields = (
            "read_delta_estimated", "read_delta_raw", "steady_read_median",
            "provisional_read_delta_estimated", "confidence_score",
            "confidence_level", "trend_signal", "trend_label", "status",
        )
        result["keyword_read_delta"] = {
            key: delta.get(key) for key in delta_fields if key in delta
        }
    result["turnover_runs"] = _projection_turnover_runs(keyword)
    return result


def _projection_account_prune(account: dict[str, Any]) -> dict[str, Any]:
    result = {key: account.get(key) for key in _BOOTSTRAP_ACCOUNT_FIELDS if key in account}
    result["history"] = account.get("history") if isinstance(account.get("history"), list) else []
    result["topic_names"] = [
        str(info.get("label") or "").strip()
        for info in (account.get("topics") or {}).values()
        if isinstance(info, dict) and str(info.get("label") or "").strip()
    ][:12]
    result["keyword_names"] = list((account.get("keywords") or {}).keys())
    return result


def _projection_bootstrap_payload(
    legacy_full: dict[str, Any],
    legacy_keywords: dict[str, dict[str, Any]],
    legacy_accounts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at": legacy_full.get("generated_at"),
        "window_days": legacy_full.get("window_days"),
        "window_start": legacy_full.get("window_start"),
        "window_end": legacy_full.get("window_end"),
        "account_score_method": legacy_full.get("account_score_method"),
        "wso_fit_meta": legacy_full.get("wso_fit_meta"),
        "keyword_read_delta_meta": legacy_full.get("keyword_read_delta_meta"),
        "keyword_bucket_options": legacy_full.get("keyword_bucket_options"),
        "keyword_scope": legacy_full.get("keyword_scope"),
        "keyword_source_total": legacy_full.get("keyword_source_total"),
        "pinned_keyword_count": legacy_full.get("pinned_keyword_count"),
        "keywords": [
            _projection_keyword_prune(x) for x in legacy_keywords.values()
        ],
        "accounts": [
            _projection_account_prune(x) for x in legacy_accounts.values()
        ],
    }


def _empty_projection_keyword(
    item: dict[str, Any], group: dict[str, Any], window_days: int,
) -> dict[str, Any]:
    keyword_text = str(item.get("keyword_text") or "").strip()
    return {
        "keyword": keyword_text, "keyword_id": str(item.get("keyword_id") or "").strip(),
        "topic": keyword_text, "keyword_bucket": group.get("label") or "未分类",
        "today_best": None, "today_count": 0, "coverage_days": 0,
        "tracked_accounts": 0, "article_count": 0, "latest_run": None,
        "runs": [], "history_best": [0] * window_days,
        "history_hits": [0] * window_days, "accounts": [], "heat_summary": {},
        "kw_score": {"total": 0, "heat": 0, "breadth": 0, "richness": 0, "has_heat": False},
    }


def _registry_projection_payload(
    runtime: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    groups = sorted(
        [row for row in runtime.get("keyword_groups", []) if row.get("archived_at") is None],
        key=lambda row: (int(row.get("display_order") or 0), str(row.get("label") or "")),
    )
    active = sorted(
        [row for row in runtime.get("keyword_registry", []) if row.get("status") == "active"],
        key=lambda row: (
            int(row.get("keyword_order")) if row.get("keyword_order") is not None else 999999,
            str(row.get("keyword_text") or ""),
        ),
    )
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in active:
        if row.get("group_id"):
            by_group.setdefault(str(row["group_id"]), []).append(row)
    payload = [
        {
            "group_id": group.get("group_id"), "label": group.get("label"),
            "order": group.get("display_order"), "created_at": group.get("created_at"),
            "updated_at": group.get("updated_at"),
            "keywords": by_group.get(str(group.get("group_id")), []),
        }
        for group in groups
    ]
    settings = {
        str(row.get("keyword_id")): row
        for row in runtime.get("keyword_registry", []) if row.get("keyword_id")
    }
    return payload, settings


def _metric_projection_number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _legacy_projection_payload(
    records: dict[str, Any], *, read_delta_source: str | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Reproduce frozen ``MonitorFastStore._reload`` in statement order."""
    raw = dict(records.get("monitor") or {})
    runtime = records.get("runtime") or {}
    payload_groups, settings = _registry_projection_payload(runtime)
    monitor_by_id = {
        item.get("keyword_id"): item
        for item in raw.get("keywords", []) if item.get("keyword_id")
    }
    window_days = int(raw.get("window_days") or 15)
    if payload_groups:
        scoped = []
        for group in payload_groups:
            for item in group.get("keywords", []):
                keyword_id = item.get("keyword_id")
                merged = dict(
                    monitor_by_id.get(keyword_id)
                    or _empty_projection_keyword(item, group, window_days)
                )
                merged["keyword_id"] = keyword_id
                merged["keyword"] = item.get("keyword_text", merged.get("keyword", ""))
                merged["keyword_bucket"] = (
                    merged.get("keyword_bucket") or group.get("label") or "未分类"
                )
                scoped.append(merged)
    else:
        scoped = [dict(item) for item in raw.get("keywords", [])]
    raw = {**raw, "keywords": scoped}

    keywords = []
    for item in raw.get("keywords", []):
        merged = dict(item)
        state = settings.get(str(item.get("keyword_id") or ""), {})
        merged["is_pinned"] = bool(state.get("is_pinned", False))
        merged["pin_order"] = state.get("pin_order")
        merged["topic"] = state.get("topic") or merged.get("topic") or merged.get("keyword")
        merged["keyword_bucket"] = (
            state.get("keyword_bucket") or merged.get("keyword_bucket") or "未分类"
        )
        keywords.append(merged)
    raw = {**raw, "keywords": keywords}

    delta_rows = records.get("keyword_read_deltas") or []
    delta_available = bool(read_delta_source and Path(read_delta_source).is_file())
    by_id: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    first_row = delta_rows[0] if delta_rows else {}
    for row in delta_rows:
        if not isinstance(row, dict):
            continue
        keyword_id = str(row.get("keyword_id") or "").strip()
        if keyword_id:
            by_id[keyword_id] = dict(row)
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    if delta_available:
        meta = {
            "available": True, "row_count": len(by_id), "source": read_delta_source,
            "generated_at": (records.get("article_metric_meta") or {}).get("generated_at"),
            "window_start": first_row.get("window_start"),
            "window_end": first_row.get("window_end"),
            "window_days": first_row.get("window_days"), "method": first_row.get("method"),
            "status_counts": status_counts,
        }
    else:
        meta = {"available": False, "row_count": 0, "source": read_delta_source or ""}
    keywords = []
    for item in raw.get("keywords", []):
        merged = dict(item)
        if meta.get("available"):
            row = by_id.get(str(item.get("keyword_id") or ""))
            merged["keyword_read_delta"] = row or {
                "keyword_id": merged.get("keyword_id") or "",
                "keyword": merged.get("keyword") or "",
                "window_start": meta.get("window_start"), "window_end": meta.get("window_end"),
                "window_days": meta.get("window_days"),
                "method": meta.get("method") or "schedule_adjusted_read_rate_v3",
                "status": "insufficient_data", "read_delta_estimated": None,
                "read_delta_raw": None, "steady_read_median": None,
                "legacy_steady_read_median": None, "confidence_score": 0,
                "confidence_level": "insufficient", "trend_signal": 0,
                "trend_label": "观察中",
                "insufficient_reason": "not_in_keyword_read_deltas",
            }
        keywords.append(merged)
    raw = {**raw, "keywords": keywords, "keyword_read_delta_meta": meta}

    def sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        score = item.get("kw_score") or {}
        segment = 0 if item.get("is_pinned") else 1 if score.get("has_heat") else 2
        metric = score.get("heat") if segment == 1 else score.get("richness")
        return segment, -_metric_projection_number(metric), str(item.get("keyword") or "")

    keywords = list(raw.get("keywords", []))
    keywords.sort(key=sort_key)
    raw["keywords"] = keywords
    raw["pinned_keyword_count"] = sum(1 for item in keywords if item.get("is_pinned"))
    raw["keyword_bucket_options"] = list(_LEGACY_BUCKET_OPTIONS)
    raw["keyword_scope"] = "configured"
    raw["keyword_source_total"] = len(keywords)

    accounts = {}
    today_date = str(raw.get("generated_at") or "")[:10]
    today_ids: dict[str, set[str]] = {}
    today_titles: dict[str, set[str]] = {}
    for keyword in keywords:
        for run in keyword.get("runs", []) or []:
            if run.get("date") != today_date:
                continue
            for article in run.get("articles", []) or []:
                name = str(article.get("account") or "").strip()
                if not name:
                    continue
                if article.get("article_id"):
                    today_ids.setdefault(name, set()).add(str(article["article_id"]))
                if article.get("title"):
                    today_titles.setdefault(name, set()).add(str(article["title"]))
    for account in raw.get("accounts") or []:
        item = dict(account)
        name = str(item.get("name") or "")
        item["_today_article_ids"] = sorted(today_ids.get(name, set()))
        item["_today_article_titles"] = sorted(today_titles.get(name, set()))
        accounts[str(item.get("account_id"))] = item
    return raw, {str(x.get("keyword_id")): x for x in keywords if x.get("keyword_id")}, accounts


def _legacy_article_detail(article: dict[str, Any], records: dict[str, Any], keywords: dict[str, dict[str, Any]]) -> dict[str, Any]:
    aid = str(article.get("article_id") or "")
    indexes = records.get("_article_detail_indexes")
    if indexes is None:
        by_url: dict[str, list[dict[str, Any]]] = {}
        for item in records.get("articles") or []:
            url = _safe_url(item.get("normalized_url") or item.get("raw_url"))
            by_url.setdefault(url, []).append(item)
        hits_by_article: dict[str, list[dict[str, Any]]] = {}
        for hit in records.get("hits") or []:
            hits_by_article.setdefault(str(hit.get("article_id") or ""), []).append(hit)
        indexes = records["_article_detail_indexes"] = {
            "accounts": {str(x.get("account_id")): x for x in records.get("accounts") or []},
            "snapshots": {str(x.get("snapshot_id")): x for x in records.get("snapshots") or []},
            "keyword_by_id": {str(x.get("keyword_id")): x for x in records.get("keywords") or []},
            "articles_by_url": by_url,
            "hits_by_article": hits_by_article,
        }
    accounts = indexes["accounts"]
    snapshots = indexes["snapshots"]
    keyword_by_id = indexes["keyword_by_id"]
    target_url = _safe_url(article.get("normalized_url") or article.get("raw_url"))
    related = indexes["articles_by_url"].get(target_url) or [article]
    related_ids = {str(x.get("article_id")) for x in related}
    hits = []
    for related_id in related_ids:
        for hit in indexes["hits_by_article"].get(related_id, []):
            snap = snapshots.get(str(hit.get("snapshot_id")), {})
            kid = str(snap.get("keyword_id") or "")
            meta = keywords.get(kid) or keyword_by_id.get(kid) or {}
            hits.append({
                "hit_id": hit.get("hit_id") or "", "article_id": hit.get("article_id") or "",
                "snapshot_id": hit.get("snapshot_id") or "", "rank": hit.get("rank"),
                "keyword_id": kid, "keyword": meta.get("keyword") or meta.get("keyword_text") or "",
                "topic": meta.get("topic") or meta.get("keyword") or "",
                "keyword_bucket": meta.get("keyword_bucket") or "未分类",
                "captured_at": snap.get("captured_at") or "",
                "captured_at_label": f"{snap.get('snapshot_date') or ''} {snap.get('snapshot_time') or ''}".strip(),
                "snapshot_date": snap.get("snapshot_date") or "", "snapshot_time": snap.get("snapshot_time") or "",
                "batch_id": "", "title": article.get("title") or hit.get("title_raw") or "",
                "account_id": article.get("account_id") or "",
            })
    hits.sort(key=lambda x: (x.get("captured_at") or "", x.get("rank") or 99), reverse=True)
    grouped = {}
    for hit in hits:
        key = hit["keyword_id"] or hit["keyword"] or "unknown"
        group = grouped.setdefault(key, {"keyword_id": hit["keyword_id"], "keyword": hit["keyword"] or "未知关键词", "topic": hit["topic"], "keyword_bucket": hit["keyword_bucket"], "hit_count": 0, "best_rank": None, "latest_rank": None, "latest_seen_at": "", "hits": []})
        group["hit_count"] += 1
        if hit["rank"] and (group["best_rank"] is None or hit["rank"] < group["best_rank"]):
            group["best_rank"] = hit["rank"]
        if hit["captured_at"] > group["latest_seen_at"]:
            group["latest_seen_at"] = hit["captured_at"]; group["latest_rank"] = hit["rank"]
        group["hits"].append(hit)
    groups = sorted(grouped.values(), key=lambda x: (-(x["hit_count"] or 0), x["best_rank"] or 99, x["keyword"] or ""))
    content_files = [{"article_id": x.get("article_id") or "", "title": x.get("title") or "", "path": x.get("content_file_path") or "", "is_primary": x.get("article_id") == aid} for x in related if x.get("content_file_path")]
    metric_points = []
    for item in related:
        if any(item.get(k) is not None for k in ("read_count", "like_count", "friends_follow_count")):
            metric_points.append({"article_id": item.get("article_id") or "", "captured_at": item.get("last_seen_at") or item.get("first_seen_at") or "", "read_count": _number(item.get("read_count")), "like_count": _number(item.get("like_count")), "friends_follow_count": _number(item.get("friends_follow_count")), "content_file_path": item.get("content_file_path") or ""})
    timeline = []
    if hits:
        first = sorted(hits, key=lambda x: (x.get("captured_at") or "", x.get("rank") or 99))[0]
        timeline.append({"type": "first", "label": "首次命中", "title": f"{first.get('keyword') or '未知关键词'} · 第 {first.get('rank') or '—'} 名", "description": f"{first.get('captured_at_label') or ''}，首次进入搜索结果。"})
        first_topic = first.get("topic")
        topic_hit = next((x for x in hits if x.get("keyword_id") != first.get("keyword_id") and x.get("topic") == first_topic), None)
        if topic_hit:
            timeline.append({"type": "topic", "label": "同主题扩散", "title": f"{topic_hit.get('keyword') or '未知关键词'} · 第 {topic_hit.get('rank') or '—'} 名", "description": f"{topic_hit.get('captured_at_label') or ''}，同一主题的另一个关键词也命中。"})
        bucket_hit = next((x for x in hits if x.get("keyword_bucket") and x.get("keyword_bucket") != first.get("keyword_bucket")), None)
        if bucket_hit:
            timeline.append({"type": "bucket", "label": "跨类目命中", "title": f"{bucket_hit.get('keyword') or '未知关键词'} · 第 {bucket_hit.get('rank') or '—'} 名", "description": f"{bucket_hit.get('captured_at_label') or ''}，扩散到「{bucket_hit.get('keyword_bucket')}」。"})
    return {
        "article": {"article_id": aid, "title": article.get("title") or "", "url": article.get("normalized_url") or article.get("raw_url") or "", "normalized_url": _safe_url(article.get("normalized_url") or article.get("raw_url")) or "", "published_at": article.get("published_at") or "", "summary": article.get("summary") or "", "content_path": article.get("content_file_path") or "", "read_count": _number(article.get("read_count")), "like_count": _number(article.get("like_count")), "friends_follow_count": _number(article.get("friends_follow_count")), "original_article_count": _number(article.get("original_article_count")), "first_seen_at": article.get("first_seen_at") or "", "last_seen_at": article.get("last_seen_at") or ""},
        "account": {"account_id": accounts.get(str(article.get("account_id")), {}).get("account_id") or article.get("account_id") or "", "name": accounts.get(str(article.get("account_id")), {}).get("canonical_name") or "", "headimg_url": accounts.get(str(article.get("account_id")), {}).get("headimg_url") or "", "wechat_biz": accounts.get(str(article.get("account_id")), {}).get("wechat_biz") or ""},
        "url_profile": {"url": _safe_url(article.get("normalized_url") or article.get("raw_url")) or "", "article_ids": sorted(related_ids), "article_record_count": len(related), "title_variants": sorted({x.get("title") for x in related if x.get("title")})},
        "keyword_groups": groups,
        "keyword_cloud": [{"keyword": x["keyword"], "hit_count": x["hit_count"], "best_rank": x["best_rank"], "topic": x["topic"], "keyword_bucket": x["keyword_bucket"]} for x in groups],
        "hit_count": len(hits), "keyword_count": len(groups), "content_files": content_files,
        "metric_points": sorted(metric_points, key=lambda x: x.get("captured_at") or ""),
        "timeline_events": timeline,
    }


class WechatService:
    _bootstrap_http_cache_lock = Lock()
    _bootstrap_http_cache: dict[tuple[Any, ...], tuple[float, bytes]] = {}

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.adapter = WechatAdapter(settings)

    def _explicit_migration_mode(self, contract_key: str) -> str:
        """v1 读取只接受明确启用的迁移开关，缺行/停用时禁止静默回退旧 HTTP。"""
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT data_mode, enabled
                FROM migration_switches
                WHERE module_key='wechat-search' AND contract_key=?
                """,
                (contract_key,),
            ).fetchone()
        if row is None or int(row["enabled"] or 0) != 1:
            raise ConflictError(
                f"wechat-search/{contract_key} 迁移开关未明确启用，已拒绝回退旧服务。"
            )
        mode = str(row["data_mode"] or "")
        if mode not in {"legacy", "compare", "hub"}:
            raise ConflictError(
                f"wechat-search/{contract_key} 迁移模式无效，已拒绝回退旧服务。"
            )
        return mode

    def _read_contract(
        self,
        contract_key: str,
        *,
        request_fingerprint: str,
        legacy: Callable[[], dict[str, Any]],
        hub: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mode = self._explicit_migration_mode(contract_key)
        if mode == "legacy":
            return legacy(), {"mode": "legacy"}
        if mode == "hub":
            try:
                return hub(), {"mode": "hub"}
            except NotImplementedError as exc:
                raise ConflictError(
                    f"wechat-search/{contract_key} 的 hub 模式尚未实现：{exc}"
                ) from exc
        return MigrationResolver(
            self.settings,
            module_key="wechat-search",
            contract_key=contract_key,
        ).compare(
            request_fingerprint=request_fingerprint,
            legacy=legacy,
            hub=hub,
        )

    def _connection_status(self, status: str, *, error: str | None = None, success_at: str | None = None) -> None:
        checked_at = _utc_now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT details_json FROM system_connections WHERE system_key='wechat-search'").fetchone()
                    details = json.loads(row[0] or "{}") if row else {}
                    if error:
                        details["last_error"] = error
                        details["last_error_at"] = checked_at
                    else:
                        details.pop("last_error", None)
                    if success_at: details["last_success_at"] = success_at
                    con.execute(
                        """
                        INSERT INTO system_connections(system_key,display_name,base_url,status,last_checked_at,capabilities_json,details_json)
                        VALUES('wechat-search','微信搜一搜',?,?,?,?,?)
                        ON CONFLICT(system_key) DO UPDATE SET
                            status=excluded.status,last_checked_at=excluded.last_checked_at,details_json=excluded.details_json
                        """,
                        (self.adapter.base_url, status, checked_at, '["read","keyword_refresh","history_import"]', _json(details)),
                    )

    def _audit(self, action: str, outcome: str, *, details: dict[str, Any], subject_id: str | None = None) -> None:
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    occurred = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    con.execute("INSERT INTO audit_log(audit_id,occurred_at,actor_type,action,subject_type,subject_id,outcome,details_json) VALUES(?,?,?,?,?,?,?,?)", (_id("audit", f"{action}:{subject_id}:{occurred}"), occurred, "system", action, "wechat", subject_id, outcome, _json(details)))

    def bootstrap(self) -> dict[str, Any]:
        result, migration = self._read_contract(
            "bootstrap",
            request_fingerprint="wechat:bootstrap",
            legacy=self._bootstrap_legacy,
            hub=self._bootstrap_hub,
        )
        result["migration"] = migration
        return result

    def _bootstrap_legacy(self) -> dict[str, Any]:
        try: result = self.adapter.bootstrap()
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc
        self._connection_status(result.status, error=result.error, success_at=_utc_now())
        payload = result.payload
        keywords = payload.get("keywords") or []
        return {"source_status": {"status": result.status, "source": result.source, "error": result.error}, "summary": {"keyword_count": len(keywords), "account_count": len(payload.get("accounts") or []), "generated_at": payload.get("generated_at"), "window_days": payload.get("window_days")}, "keywords": [self._keyword_summary(x) for x in keywords if isinstance(x, dict)], "updated_at": payload.get("generated_at")}

    def _bootstrap_hub(self, projection: dict[str, Any] | None = None) -> dict[str, Any]:
        projection = projection if projection is not None else WechatLegacyRepository(self.settings).bootstrap()
        keywords = [
            self._keyword_summary(item)
            for item in (projection.get("keywords") or [])
            if isinstance(item, dict)
        ]
        accounts = [
            item for item in (projection.get("accounts") or [])
            if isinstance(item, dict)
        ]
        generated_at = projection.get("generated_at")
        return {
            "source_status": {"status": "healthy", "source": "hub_db"},
            "summary": {
                "keyword_count": len(keywords),
                "account_count": len(accounts),
                "generated_at": generated_at,
                "window_days": projection.get("window_days"),
            },
            "keywords": keywords,
            "updated_at": generated_at,
        }

    def bootstrap_http_response(self) -> bytes | None:
        """Return the Hub v1 bootstrap envelope already serialized.

        ``None`` keeps legacy/compare mode on the existing resolver path.  The
        Hub path uses the repository's versioned short-TTL cache and therefore
        avoids both projection decoding and JSON serialization per request.
        """
        mode = MigrationResolver(
            self.settings,
            module_key="wechat-search",
            contract_key="bootstrap",
        ).mode()
        if mode != "hub":
            return None
        now = time.monotonic()
        with self._bootstrap_http_cache_lock:
            entry = WechatLegacyRepository(self.settings).bootstrap_cache_entry()
            key = entry.version
            cached = self._bootstrap_http_cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            # Single-flight: the lock intentionally covers envelope
            # construction and serialization so concurrent misses for one
            # projection version cannot duplicate CPU work.
            result = self._bootstrap_hub(entry.payload)
            result["migration"] = {"mode": "hub"}
            raw = json.dumps(
                {"ok": True, "data": result},
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode()
            for cached_key, cached_value in list(self._bootstrap_http_cache.items()):
                if cached_value[0] <= now:
                    self._bootstrap_http_cache.pop(cached_key, None)
            self._bootstrap_http_cache[key] = (entry.expires_at, raw)
            return raw

    def _safe_status(self, status: str, error: str | None = None) -> None:
        try: self._connection_status(status, error=error, success_at=None)
        except Exception: pass

    @staticmethod
    def _keyword_summary(item: dict[str, Any]) -> dict[str, Any]:
        bucket = item.get("keyword_bucket") or item.get("bucket") or "未分组"
        return {
            "keyword_id": item.get("keyword_id"),
            "keyword": item.get("keyword") or item.get("keyword_text"),
            "group": item.get("group") or bucket,
            "status": _legacy_keyword_status(item),
            "topic": item.get("topic") or item.get("keyword"),
            "bucket": bucket,
            "keyword_bucket": bucket,
            "today_best": item.get("today_best"),
            "today_count": item.get("today_count", 0),
            "article_count": item.get("article_count", 0),
            "latest_run": item.get("latest_run"),
        }

    @classmethod
    def _keyword_detail(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            **cls._keyword_summary(item),
            "history_best": item.get("history_best"),
            "history_hits": item.get("history_hits"),
            "turnover_runs": item.get("turnover_runs"),
            "kw_score": item.get("kw_score"),
        }

    def keyword(self, keyword_id: str) -> dict[str, Any]:
        result, migration = self._read_contract(
            "keyword",
            request_fingerprint=f"wechat:keyword:{keyword_id}",
            legacy=lambda: self._keyword_legacy(keyword_id),
            hub=lambda: self._keyword_hub(keyword_id),
        )
        result["migration"] = migration
        return result

    def _keyword_hub(self, keyword_id: str) -> dict[str, Any]:
        records = self._hub_keyword_records(keyword_id)
        if not records["keyword"]:
            raise NotFoundError("微信关键词", keyword_id)
        return self._keyword_response(
            records["keyword"],
            records=records,
            source_status={"status": "healthy", "source": "hub_db"},
        )

    def _keyword_legacy(self, keyword_id: str) -> dict[str, Any]:
        try:
            remote = self.adapter.remote_keyword(keyword_id)
            self._connection_status("healthy", success_at=_utc_now())
            hub_records = self._hub_keyword_records(keyword_id)
            if hub_records["snapshots"]:
                return self._keyword_response(
                    remote,
                    records=hub_records,
                    source_status={"status": "healthy", "source": "legacy_http", "data_source": "hub_db"},
                )
            return self._keyword_response(remote, source_status={"status": "healthy", "source": "legacy_http"})
        except WechatSourceError as remote_error:
            hub_records = self._hub_keyword_records(keyword_id)
            if hub_records["snapshots"]:
                self._connection_status("degraded", error=str(remote_error), success_at=_utc_now())
                keyword = hub_records["keyword"]
                return self._keyword_response(
                    keyword,
                    records=hub_records,
                    source_status={"status": "degraded", "source": "hub_db", "error": str(remote_error)},
                )
            try: records = self.adapter.all_records()
            except WechatSourceError as exc:
                self._safe_status("offline", str(exc))
                raise ConflictError(str(exc)) from exc
            item = next((x for x in records["keywords"] if x.get("keyword_id") == keyword_id), None)
            if item is None: raise NotFoundError("微信关键词", keyword_id)
            self._connection_status("degraded", error="旧服务不可用，使用 normalized 降级", success_at=_utc_now())
            return self._keyword_response(item, records=records, source_status={"status": "degraded", "source": "legacy_normalized"})

    def _hub_keyword_records(self, keyword_id: str) -> dict[str, Any]:
        """读取已导入的关键词闭包，避免详情页重新解析旧源大 JSON。"""
        with connect(self.settings, readonly=True) as con:
            keyword_row = con.execute("SELECT * FROM keywords WHERE keyword_id=?", (keyword_id,)).fetchone()
            if keyword_row is None:
                return {"keyword": {}, "snapshots": [], "hits": [], "articles": [], "terms": [], "observations": []}
            snapshots = []
            for row in con.execute(
                "SELECT * FROM search_snapshots WHERE keyword_id=? ORDER BY captured_at",
                (keyword_id,),
            ).fetchall():
                snapshot = dict(row)
                try:
                    snapshot["features"] = json.loads(snapshot.get("features_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    snapshot["features"] = {}
                snapshots.append(snapshot)
            if not snapshots:
                return {"keyword": dict(keyword_row), "snapshots": [], "hits": [], "articles": [], "terms": [], "observations": []}
            hits = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT h.*, s.keyword, s.captured_at
                    FROM search_hits h
                    JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                    WHERE s.keyword_id=?
                    ORDER BY s.captured_at, h.rank, h.hit_id
                    """,
                    (keyword_id,),
                ).fetchall()
            ]
            for hit in hits:
                if hit.get("content_id") and not hit.get("article_id"):
                    hit["article_id"] = hit["content_id"]
            content_ids = sorted({str(row["content_id"]) for row in hits if row.get("content_id")})
            articles = []
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                for row in con.execute(
                    f"SELECT * FROM contents WHERE content_id IN ({placeholders})",
                    content_ids,
                ).fetchall():
                    article = dict(row)
                    article["article_id"] = article["content_id"]
                    articles.append(article)
            observations = []
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                observations = [
                    dict(row)
                    for row in con.execute(
                        f"""
                        SELECT *
                        FROM metric_observations
                        WHERE subject_type='content' AND subject_id IN ({placeholders})
                        ORDER BY observed_at
                        """,
                        content_ids,
                    ).fetchall()
                ]
                for row in observations:
                    row["article_id"] = row["subject_id"]
                    row["source_snapshot_id"] = row.get("snapshot_id")
            return {
                "keyword": dict(keyword_row),
                "snapshots": snapshots,
                "hits": hits,
                "articles": articles,
                "terms": [],
                "observations": observations,
            }

    def _keyword_response(self, item: dict[str, Any], *, records: dict[str, Any] | None = None, source_status: dict[str, Any]) -> dict[str, Any]:
        kid = item.get("keyword_id")
        if records is None:
            try: records = self.adapter.detail_records()
            except WechatSourceError: records = {"snapshots": [], "hits": [], "articles": [], "observations": []}
        snapshots = [x for x in records["snapshots"] if x.get("keyword_id") == kid]
        snapshot_views = self._snapshot_views(snapshots, records)
        return {
            "source_status": source_status,
            "keyword": self._keyword_detail(item),
            "snapshots": snapshot_views,
            "hits": [hit for view in snapshot_views for hit in view["hits"]],
            "articles": [article for view in snapshot_views for article in view["articles"]],
            "features": {"today_best": item.get("today_best"), "today_count": item.get("today_count"), "coverage_days": item.get("coverage_days"), "heat_summary": item.get("heat_summary") or {}},
            "observations": [obs for view in snapshot_views for obs in view["observations"]],
        }

    @staticmethod
    def _snapshot_views(snapshots: list[dict[str, Any]], records: dict[str, Any]) -> list[dict[str, Any]]:
        articles_by_id = {str(row.get("article_id")): row for row in records.get("articles", [])}
        hits_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        for row in records.get("hits", []):
            hits_by_snapshot.setdefault(str(row.get("snapshot_id")), []).append(row)
        for rows in hits_by_snapshot.values():
            rows.sort(key=lambda row: (int(row.get("rank")) if str(row.get("rank", "")).isdigit() else 10**9, str(row.get("hit_id") or "")))
        observations_by_snapshot: dict[str, list[dict[str, Any]]] = {}
        observations_by_article_time: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in records.get("observations", []):
            if row.get("source_snapshot_id"):
                observations_by_snapshot.setdefault(str(row["source_snapshot_id"]), []).append(row)
            else:
                key = (str(row.get("article_id")), str(row.get("observed_at")))
                observations_by_article_time.setdefault(key, []).append(row)
        terms_by_snapshot: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for snapshot in snapshots:
            snapshot_features = snapshot.get("features")
            if isinstance(snapshot_features, dict):
                bucket = terms_by_snapshot.setdefault(str(snapshot.get("snapshot_id")), {"suggestions": [], "related": []})
                for feature_key in ("suggestions", "related"):
                    for term in snapshot_features.get(feature_key) or []:
                        if isinstance(term, dict):
                            bucket[feature_key].append({"term": term.get("term"), "position": term.get("position")})
        for term in records.get("terms", []):
            bucket = terms_by_snapshot.setdefault(str(term.get("snapshot_id")), {"suggestions": [], "related": []})
            term_type = str(term.get("term_type") or "related").lower()
            target = "suggestions" if term_type in {"suggestion", "suggestions"} else "related"
            bucket[target].append({"term": term.get("term_text"), "position": term.get("position")})
        views = []
        for snapshot in snapshots:
            sid = str(snapshot.get("snapshot_id"))
            hits = hits_by_snapshot.get(sid, [])
            article_ids = {str(row.get("article_id")) for row in hits if row.get("article_id")}
            observations = observations_by_snapshot.get(sid, [])
            if not observations:
                observations = [
                    row
                    for article_id in article_ids
                    for row in observations_by_article_time.get((article_id, str(snapshot.get("captured_at"))), [])
                ]
            terms = terms_by_snapshot.get(sid, {"suggestions": [], "related": []})
            views.append({
                "snapshot_id": snapshot.get("snapshot_id"),
                "captured_at": snapshot.get("captured_at"),
                "trigger_type": snapshot.get("trigger_type"),
                "result_count": snapshot.get("result_count"),
                "hits": hits,
                "articles": [articles_by_id[str(hit.get("article_id"))] for hit in hits if hit.get("article_id") and str(hit.get("article_id")) in articles_by_id],
                "features": {"suggestions": sorted(terms["suggestions"], key=lambda x: (x["position"] is None, x["position"])), "related": sorted(terms["related"], key=lambda x: (x["position"] is None, x["position"]))},
                "observations": observations,
            })
        return views

    def account_activity(self, account_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            account = con.execute(
                "SELECT * FROM creators WHERE platform='wechat-search' AND creator_id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise NotFoundError("微信账号", account_id)
            rows = con.execute(
                """SELECT s.captured_at,h.content_id
                   FROM search_hits h
                   JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                   JOIN contents c ON c.content_id=h.content_id
                   WHERE s.platform='wechat-search' AND c.creator_id=?
                   ORDER BY s.captured_at""",
                (account_id,),
            ).fetchall()
            latest_global = con.execute(
                "SELECT MAX(captured_at) FROM search_snapshots WHERE platform='wechat-search'"
            ).fetchone()[0]
        window_end_dt = _parse_datetime_value(latest_global)
        activity_dates = sorted(
            {
                parsed.astimezone(SOURCE_TZ).date()
                for row in rows
                if (parsed := _parse_datetime_value(row["captured_at"])) is not None
            }
        )
        window_end = window_end_dt.astimezone(SOURCE_TZ).date() if window_end_dt else None
        recent_dates = (
            {day for day in activity_dates if window_end - timedelta(days=6) <= day <= window_end}
            if window_end
            else set()
        )
        streak = 0
        if window_end:
            cursor = window_end
            while cursor in recent_dates:
                streak += 1
                cursor -= timedelta(days=1)
        longest = 0
        running = 0
        previous = None
        for day in sorted(recent_dates):
            running = running + 1 if previous and day == previous + timedelta(days=1) else 1
            longest = max(longest, running)
            previous = day
        first_ranked = min(
            (_parse_datetime_value(row["captured_at"]) for row in rows),
            default=None,
            key=lambda value: value or datetime.max.replace(tzinfo=UTC),
        )
        first_ranked = first_ranked if isinstance(first_ranked, datetime) else None
        new_window_days = 30
        is_new = bool(
            first_ranked
            and window_end
            and first_ranked.astimezone(SOURCE_TZ).date()
            >= window_end - timedelta(days=new_window_days - 1)
        )
        return {
            "account": {
                "account_id": account_id,
                "name": account["canonical_name"],
                "first_seen_at": account["first_seen_at"],
                "updated_at": account["updated_at"],
            },
            "activity": {
                "window_end": window_end.isoformat() if window_end else None,
                "window_days": 7,
                "active_days_7d": len(recent_dates),
                "streak": streak,
                "longest_streak_7d": longest,
                "activity_basis": "distinct_snapshot_dates_with_rank_hit",
            },
            "new_account": {
                "is_new_account": is_new,
                "first_ranked_at": _source_time(first_ranked) if first_ranked else None,
                "new_account_window_days": new_window_days,
                "definition": "first observed ranked appearance within 30 calendar days ending window_end",
            },
        }

    def article(self, article_id: str) -> dict[str, Any]:
        result, migration = self._read_contract(
            "article-hit-detail",
            request_fingerprint=f"wechat:article:{article_id}",
            legacy=lambda: self._article_legacy(article_id),
            hub=lambda: self._article_hub(article_id),
        )
        result["migration"] = migration
        return result

    def _article_hub(self, article_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            article = con.execute(
                """
                SELECT c.*
                FROM contents c
                WHERE (c.content_id=? OR EXISTS (
                    SELECT 1 FROM content_identifiers ix
                    WHERE ix.namespace='wechat_article'
                      AND ix.content_id=c.content_id
                      AND ix.external_id=?
                ))
                  AND (
                    EXISTS (
                        SELECT 1 FROM content_identifiers i
                        WHERE i.namespace='wechat_article' AND i.content_id=c.content_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM content_discoveries d
                        WHERE d.discovery_system='wechat-search' AND d.content_id=c.content_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM search_hits h
                        JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                        WHERE h.content_id=c.content_id AND s.platform='wechat-search'
                    )
                  )
                """,
                (article_id, article_id),
            ).fetchone()
            if article:
                content_id = str(article["content_id"])
                hits = [dict(x) for x in con.execute("SELECT h.*,s.keyword_id,s.keyword,s.captured_at,s.features_json FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id WHERE h.content_id=? ORDER BY s.captured_at DESC,h.rank", (content_id,)).fetchall()]
                obs = [dict(x) for x in con.execute("SELECT * FROM metric_observations WHERE subject_type='content' AND subject_id=? ORDER BY observed_at DESC", (content_id,)).fetchall()]
                rank_points: dict[tuple[str, str], dict[str, Any]] = {}
                for hit in hits:
                    captured = _parse_datetime_value(hit.get("captured_at"))
                    day = captured.astimezone(SOURCE_TZ).date().isoformat() if captured else str(hit.get("captured_at") or "")[:10]
                    keyword_id = str(hit.get("keyword_id") or "")
                    key = (day, keyword_id)
                    point = rank_points.setdefault(
                        key,
                        {
                            "date": day,
                            "keyword_id": keyword_id,
                            "keyword": hit.get("keyword") or "",
                            "best_rank": int(hit.get("rank") or 0),
                            "snapshot_count": 0,
                            "latest_captured_at": hit.get("captured_at") or "",
                        },
                    )
                    rank = int(hit.get("rank") or 0)
                    if rank > 0 and (not point["best_rank"] or rank < point["best_rank"]):
                        point["best_rank"] = rank
                    point["snapshot_count"] += 1
                    if str(hit.get("captured_at") or "") > str(point["latest_captured_at"]):
                        point["latest_captured_at"] = hit.get("captured_at") or ""
                rank_history = {
                    "aggregation": "best_rank_per_keyword_per_calendar_day",
                    "timezone": "Asia/Shanghai",
                    "points": sorted(rank_points.values(), key=lambda item: (item["date"], item["keyword_id"])),
                }
                payload = json.loads(article["payload_json"] or "{}")
                snapshot_rows: dict[str, dict[str, Any]] = {}
                for hit in hits:
                    sid = str(hit["snapshot_id"])
                    snapshot_rows.setdefault(sid, {"snapshot_id": sid, "captured_at": hit["captured_at"], "keyword": hit["keyword"], "features": json.loads(hit.get("features_json") or "{}"), "hits": []})["hits"].append(hit)
                return {"source_status": {"status": "healthy", "source": "hub_db"}, "article": {**dict(article), "source": payload}, "rank_history": rank_history, "snapshots": list(snapshot_rows.values()), "hits": hits, "articles": [{**dict(article), "source": payload}], "features": {"canonical_url": article["canonical_url"], "published_at": article["published_at"]}, "observations": obs}
        raise NotFoundError("微信文章", article_id)

    def _article_legacy(self, article_id: str) -> dict[str, Any]:
        try:
            return self._article_hub(article_id)
        except NotFoundError:
            pass
        try: records = self.adapter.detail_records()
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc
        article = next((x for x in records["articles"] if x.get("article_id") == article_id), None)
        if article is None: raise NotFoundError("微信文章", article_id)
        self._connection_status("degraded", error="旧服务不可用，使用 normalized 降级", success_at=_utc_now())
        hits = [x for x in records["hits"] if x.get("article_id") == article_id]
        obs = [x for x in records["observations"] if x.get("article_id") == article_id]
        content = None
        try:
            if article.get("content_file_path"): content = self.adapter.remote_article_content(article["content_file_path"])
        except WechatSourceError: pass
        snapshot_rows = [x for x in records["snapshots"] if x.get("snapshot_id") in {h.get("snapshot_id") for h in hits}]
        views = self._snapshot_views(snapshot_rows, records)
        return {"source_status": {"status": "degraded", "source": "legacy_normalized"}, "article": article, "snapshots": views, "hits": hits, "articles": [article], "features": {"content": content, "canonical_url": _safe_url(article.get("normalized_url") or article.get("raw_url"))}, "observations": obs}

    def article_content(self, article_id: str) -> dict[str, Any]:
        result, migration = self._read_contract(
            "article-content",
            request_fingerprint=f"wechat:article-content:{article_id}",
            legacy=lambda: self._article_content_legacy(article_id),
            hub=lambda: self._article_content_hub(article_id),
        )
        result["migration"] = migration
        return result

    def _article_content_hub(self, article_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT c.title,p.relative_path,p.asset_path
                FROM contents c
                JOIN content_identifiers i
                  ON i.content_id=c.content_id AND i.namespace='wechat_article'
                JOIN wechat_article_paths p
                  ON p.article_id=c.content_id
                WHERE c.content_id=? OR i.external_id=?
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                (article_id, article_id),
            ).fetchone()
        if row is None or not row["asset_path"]:
            raise NotFoundError("微信正文", article_id)
        root = self.settings.asset_store_path.resolve()
        candidate = self.settings.asset_store_path / str(row["asset_path"])
        try:
            relative_parts = candidate.relative_to(self.settings.asset_store_path).parts
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if any((self.settings.asset_store_path.joinpath(*relative_parts[:index])).is_symlink()
               for index in range(1, len(relative_parts) + 1)):
            raise ValidationAppError("正文资产路径无效。")
        asset = candidate.resolve()
        try:
            asset.relative_to(root)
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if not asset.is_file():
            raise NotFoundError("微信正文资产", str(row["asset_path"]))
        try:
            content = asset.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConflictError(f"读取微信正文资产失败: {exc}") from exc
        return {
            "article_id": article_id,
            "title": row["title"],
            "path": row["relative_path"] or "",
            "content": content,
        }

    def _article_content_legacy(self, article_id: str) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT c.title,p.relative_path,p.asset_path
                FROM contents c
                JOIN content_identifiers i
                  ON i.content_id=c.content_id AND i.namespace='wechat_article'
                JOIN wechat_article_paths p ON p.article_id=c.content_id
                WHERE c.content_id=? OR i.external_id=?
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                (article_id, article_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("微信文章", article_id)
        if not row["asset_path"]:
            raise NotFoundError("微信正文", article_id)
        root = self.settings.asset_store_path.resolve()
        candidate = self.settings.asset_store_path / str(row["asset_path"])
        try:
            relative_parts = candidate.relative_to(self.settings.asset_store_path).parts
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if any((self.settings.asset_store_path.joinpath(*relative_parts[:index])).is_symlink()
               for index in range(1, len(relative_parts) + 1)):
            raise ValidationAppError("正文资产路径无效。")
        asset = candidate.resolve()
        try:
            asset.relative_to(root)
        except ValueError as exc:
            raise ValidationAppError("正文资产路径无效。") from exc
        if not asset.is_file():
            raise NotFoundError("微信正文资产", str(row["asset_path"]))
        try:
            content = asset.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConflictError(f"读取微信正文资产失败: {exc}") from exc
        return {"article_id": article_id, "title": row["title"], "path": row["relative_path"] or "", "content": content}

    def refresh(
        self,
        keyword_id: str,
        confirm: bool,
        *,
        idempotency_key: str = "",
        request_keyword: str = "",
        provider: Any | None = None,
        actor_id: str = "user",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """兼容内部调用的 Hub-only 包装；不得通过此入口访问旧 adapter。"""
        if confirm is not True:
            raise ValidationAppError("刷新必须明确传入 confirm=true。")
        return WechatRefreshService(
            self.settings,
            provider=provider,
            actor_id=actor_id,
        ).refresh_one(
            keyword_id=keyword_id,
            request_keyword=request_keyword,
            key=idempotency_key,
            request_id=request_id,
            confirm=True,
            semantic=True,
        )

    def refresh_status(self, job_id: str) -> dict[str, Any]:
        result, migration = self._read_contract(
            "refresh-status",
            request_fingerprint=f"wechat:refresh-status:{job_id}",
            legacy=lambda: self._refresh_status_legacy(job_id),
            hub=lambda: self._refresh_status_hub(job_id),
        )
        result["migration"] = migration
        return result

    def _refresh_status_hub(self, job_id: str) -> dict[str, Any]:
        runtime = WechatRefreshService(self.settings).runtime(
            job_id,
            batch=False,
            semantic=True,
        )
        return {
            "source_status": {"status": "healthy", "source": "hub_runtime"},
            "result": runtime,
        }

    def _refresh_status_legacy(self, job_id: str) -> dict[str, Any]:
        try:
            result = self.adapter.remote_refresh_status(job_id)
            self._connection_status("healthy", success_at=_utc_now())
            return {"source_status": {"status": "healthy", "source": "legacy_http"}, "result": result}
        except WechatSourceError as exc:
            self._safe_status("offline", str(exc))
            raise ConflictError(str(exc)) from exc

    def _import_lock_path(self) -> Path:
        return self.settings.lock_path.with_name(_IMPORT_LOCK_NAME)

    @staticmethod
    def _import_command_input(*, dry_run: bool, limit: int | None) -> dict[str, Any]:
        """Canonical idempotency payload for history imports."""
        return {"dry_run": bool(dry_run), "limit": limit}

    @classmethod
    def _assert_import_input_matches(
        cls,
        stored_input_json: Any,
        *,
        dry_run: bool,
        limit: int | None,
    ) -> None:
        try:
            stored = json.loads(stored_input_json or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError("该幂等键已有无法解析请求指纹的导入记录。") from exc
        expected = cls._import_command_input(dry_run=dry_run, limit=limit)
        if stored != expected:
            raise ConflictError(
                "同一幂等键不能用于不同的微信导入请求参数。"
            )

    def _command_result(
        self, *, key: str, dry_run: bool, limit: int | None,
    ) -> dict[str, Any] | None:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT status,input_json,output_json,error_json
                FROM command_runs
                WHERE module_key='wechat-search' AND command_type='history-import'
                  AND idempotency_key=?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        self._assert_import_input_matches(
            row["input_json"], dry_run=dry_run, limit=limit
        )
        try:
            value = json.loads(row["output_json"] or "{}")
        except json.JSONDecodeError:
            value = {}
        if isinstance(value, dict) and value.get("semantic_status") in {
            "succeeded", "partial_failed", "dry_run",
        }:
            return value
        if row["status"] == "running":
            raise ConflictError("微信正式导入正在运行，请等待当前任务完成。")
        if row["status"] == "failed":
            # A failed attempt is retained as evidence, but the same request
            # key is replayable after rollback; a new key is not required.
            return None
        return None

    def _begin_import_command(
        self, *, key: str, dry_run: bool, limit: int | None, now: str,
    ) -> str:
        command_id = _id("command", f"wechat-search:history-import:{key}")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    existing = con.execute(
                        """
                        SELECT command_id,status,input_json
                        FROM command_runs
                        WHERE module_key='wechat-search' AND command_type='history-import'
                          AND idempotency_key=?
                        """,
                        (key,),
                    ).fetchone()
                    if existing is not None:
                        self._assert_import_input_matches(
                            existing["input_json"],
                            dry_run=dry_run,
                            limit=limit,
                        )
                        if existing["status"] == "running":
                            raise ConflictError("微信正式导入正在运行，请等待当前任务完成。")
                        if existing["status"] == "succeeded":
                            return str(existing["command_id"])
                        # Keep the original failed audit rows, but let the
                        # command row be retried with the same idempotency key.
                        con.execute(
                            """
                            UPDATE command_runs
                            SET status='running',output_json='{}',error_json='{}',updated_at=?
                            WHERE command_id=?
                            """,
                            (now, existing["command_id"]),
                        )
                        return str(existing["command_id"])
                    con.execute(
                        """
                        INSERT INTO command_runs(
                            command_id,module_key,command_type,idempotency_key,
                            actor_id,request_id,status,confirmation_json,input_json,
                            output_json,error_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            command_id, "wechat-search", "history-import", key,
                            "system", None, "running",
                            _json({"confirm": True, "dry_run": dry_run}),
                            _json(self._import_command_input(
                                dry_run=dry_run, limit=limit
                            )),
                            "{}", "{}", now, now,
                        ),
                    )
        return command_id

    def _finish_import_command(
        self, command_id: str, *, status: str, output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    con.execute(
                        """
                        UPDATE command_runs
                        SET status=?,output_json=?,error_json=?,updated_at=?
                        WHERE command_id=?
                        """,
                        (status, _json(output or {}), _json(error or {}), now, command_id),
                    )

    def _cleanup_import_assets(self) -> None:
        created = getattr(self, "_import_new_assets", set())
        if not created:
            return
        with connect(self.settings, readonly=True) as con:
            referenced = {
                str(row["asset_path"])
                for row in con.execute(
                    "SELECT asset_path FROM wechat_article_paths WHERE asset_path IS NOT NULL"
                )
            }
        for path in list(created):
            try:
                relative = str(path.relative_to(self.settings.asset_store_path)).replace("\\", "/")
            except ValueError:
                continue
            if relative in referenced:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        created.clear()

    def import_history(
        self, *, dry_run: bool, limit: int | None, confirm: bool = True,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if not dry_run and limit is None and confirm is not True:
            raise ValidationAppError("正式全量导入必须明确传入 confirm=true。")
        key = str(idempotency_key or "").strip()
        if not dry_run and limit is None and not key:
            raise ValidationAppError("正式全量导入必须提供非空幂等键。")
        if not key:
            key = _id("dry-import", f"{self.adapter.root.resolve()}:{limit}")
        migration = {
            "module_key": "wechat-search",
            "contract_key": "history-import",
            "data_mode": "hub",
            "hub_only": True,
            "resolver": False,
        }
        self._import_new_assets = set()
        command_id = ""
        try:
            with writer_lock(self._import_lock_path(), timeout_seconds=0.05):
                prior = self._command_result(key=key, dry_run=dry_run, limit=limit)
                if prior is not None:
                    return prior
                command_id = self._begin_import_command(
                    key=key, dry_run=dry_run, limit=limit, now=_utc_now()
                )
                result = self._import_history(
                    dry_run=dry_run, limit=limit, command_id=command_id
                )
                result["migration"] = migration
                result["command_id"] = command_id
                semantic_status = str(result.get("semantic_status") or "")
                # command_runs has a deliberately narrower CHECK constraint:
                # semantic partial failure is retained in output_json, while
                # the storage status remains the legal terminal value failed.
                command_status = (
                    "succeeded"
                    if semantic_status in {"succeeded", "dry_run"}
                    else "failed"
                )
                self._finish_import_command(
                    command_id, status=command_status, output=result,
                    error=None if command_status != "failed" else {
                        "type": "ImportVerificationError",
                        "message": "微信导入未通过完整性或对账校验。",
                    },
                )
                return result
        except WriterLockTimeout as exc:
            raise ConflictError("已有微信导入正在运行，当前请求未开始解析。") from exc
        except Exception as exc:
            if command_id:
                self._finish_import_command(
                    command_id, status="failed",
                    error={"type": type(exc).__name__, "message": str(exc)[:500]},
                )
            raise
        finally:
            self._cleanup_import_assets()
            self.adapter.clear_cache_for_root(self.adapter.root)

    def _import_history(
        self, *, dry_run: bool, limit: int | None, command_id: str,
    ) -> dict[str, Any]:
        if limit is None and self._large_source_requires_streaming():
            return self._import_history_streaming(
                dry_run=dry_run, command_id=command_id
            )
        try: records, manifest, reconcile = self.adapter.import_records(limit=limit)
        except WechatSourceError as exc:
            message = f"{exc.kind}: {exc}"
            self._safe_status("offline", message); self._audit("wechat.import", "failed", details={"command_id": command_id, "kind": exc.kind, "error": str(exc)}); raise ConflictError(message) from exc
        counts = self._counts(records)
        scope = "full" if limit is None else _id("selection", json.dumps(reconcile.get("selection_snapshot_ids", []), ensure_ascii=False))
        batch_id = _id("batch", f"wechat:{self.adapter.manifest_id(manifest)}:{scope}")
        report = {
            "manifest_id": self.adapter.manifest_id(manifest),
            "manifest": manifest,
            "freeze_seal": self.adapter.verify_freeze_seal(),
            "reconcile": reconcile,
            "rejected": [],
            "collision_count": 0,
            "content_collisions": [],
            "placeholder_count": 0,
            "placeholder_samples": [],
            "metric_fact_count": 0,
            "metric_unique_count": 0,
            "metric_collision_extra_count": 0,
            "metric_collision_group_count": 0,
            "metric_collision_same_value_count": 0,
            "metric_collision_value_diff_count": 0,
            "metric_collisions": [],
            "full_sync": limit is None,
            "metric_compatibility": {
                "canonical_prefix": "wechat.article./wechat.keyword.",
                "legacy_keys_preserved": True,
                "policy": "只新增规范 key，不静默改写已有历史观测",
            },
        }
        if dry_run:
            with connect(self.settings, readonly=True) as con:
                ids, snapshot_map = self._metric_context(con, records, report)
                self._prepare_metric_facts(con, records, ids, snapshot_map, report, planned_snapshot_ids=set(snapshot_map.values()))
                self._prepare_keyword_delta_facts(records, report)
            report["reconcile"] = self._reconcile_summary(records, limit=limit, dry_run=True)
            self._connection_status("healthy", success_at=_utc_now())
            self._audit("wechat.import", "succeeded", details={"command_id": command_id, "dry_run": True, "batch_id": batch_id, "counts": counts, "audit": report, "semantic_status": "dry_run"})
            return {
                "dry_run": True,
                "source": "legacy_normalized",
                "counts": counts,
                "batch_id": batch_id,
                "semantic_status": "dry_run",
                "verified": False,
                "audit": report,
            }
        now = _utc_now()
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con): self._write(con, records, batch_id, report, now)
        report["reconcile"] = self._reconcile_summary(records)
        report["asset_integrity"] = self._asset_integrity(records)
        reconciliation = report["reconcile"]
        verified = (
            not report["rejected"]
            and reconciliation.get("status") == "matched"
            and reconciliation.get("verified") is True
            and report["asset_integrity"].get("verified") is True
        )
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    final_status = "succeeded" if verified else (
                        "partial_failed" if report["rejected"] or report["reconcile"]["status"] == "mismatch"
                        else "failed"
                    )
                    records_seen = sum(counts.values())
                    con.execute(
                        """
                        UPDATE ingestion_batches
                        SET status=?,finished_at=?,records_seen=?,
                            records_written=?,records_failed=?,
                            error_json=?,payload_json=?
                        WHERE batch_id=?
                        """,
                        (
                            final_status,
                            _utc_now(),
                            records_seen,
                            max(0, records_seen - len(report["rejected"])),
                            len(report["rejected"]),
                            _json(report["rejected"]),
                            _json(report),
                            batch_id,
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO ingestion_checkpoints(
                            adapter_key,checkpoint_key,cursor_value,source_hash,
                            last_success_at,batch_id,payload_json
                        ) VALUES(
                            ?,?,
                            CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                            CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                            CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                            CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                            ?
                        )
                        ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET
                            cursor_value=CASE WHEN ?='succeeded'
                                THEN excluded.cursor_value
                                ELSE ingestion_checkpoints.cursor_value END,
                            source_hash=CASE WHEN ?='succeeded'
                                THEN excluded.source_hash
                                ELSE ingestion_checkpoints.source_hash END,
                            batch_id=CASE WHEN ?='succeeded'
                                THEN excluded.batch_id
                                ELSE ingestion_checkpoints.batch_id END,
                            payload_json=excluded.payload_json,
                            last_success_at=CASE WHEN ?='succeeded'
                                THEN excluded.last_success_at
                                ELSE ingestion_checkpoints.last_success_at END
                        """,
                        (
                            "wechat-search", "normalized",
                            final_status, now,
                            final_status, report["manifest_id"],
                            final_status, now,
                            final_status, batch_id,
                            _json(report),
                            final_status, final_status,
                            final_status, final_status,
                        ),
                    )
        if not verified:
            reason = (
                f"{len(report['rejected'])} rows rejected"
                if report["rejected"] else (
                    "reconciliation_mismatch"
                    if report["reconcile"].get("status") == "mismatch"
                    else "asset_integrity_failed"
                )
            )
            self._connection_status("degraded", error=reason, success_at=None)
        else:
            self._connection_status("healthy", success_at=now)
        outcome = "succeeded" if verified else ("partial_failed" if report["rejected"] or report["reconcile"]["status"] == "mismatch" else "failed")
        self._audit(
            "wechat.import",
            "succeeded" if outcome == "succeeded" else "failed",
            details={"command_id": command_id, "batch_id": batch_id, "counts": counts, "audit": report,
                     "semantic_status": outcome},
        )
        return {
            "dry_run": False,
            "source": "legacy_normalized",
            "counts": counts,
            "batch_id": batch_id,
            "job_id": batch_id,
            "semantic_status": outcome,
            "verified": verified,
            "checkpoint": {
                "adapter_key": "wechat-search",
                "checkpoint_key": "normalized",
                "source_hash": report["manifest_id"],
                "advanced": verified,
            },
            "audit": report,
        }

    def _large_source_requires_streaming(self) -> bool:
        total = 0
        for relative in self.adapter.FILES.values():
            path = self.adapter.root / relative
            try:
                total += path.stat().st_size
            except OSError:
                return False
        return total >= _STREAMING_IMPORT_BYTES

    def _compat_read_delta_source(self) -> str:
        local = self.adapter.root / self.adapter.OPTIONAL_FILES["keyword_read_deltas"]
        migration_root = self.settings.project_root / "data/migration/wechat"
        reference = migration_root / "reference/instance/normalized/keyword_read_deltas.json"
        try:
            self.adapter.root.resolve().relative_to(migration_root.resolve())
            if reference.is_file():
                return str(reference.resolve())
        except ValueError:
            pass
        return str(local.resolve())

    def _compat_runtime_projection_root(self) -> tuple[Path, bool]:
        """Use the isolated 8774 reference tree for runtime-only semantics.

        The immutable freeze remains the imported fact source.  R19 depends on
        directory mtimes and R20 depends on the disabled reference process'
        in-memory defaults, so those two compatibility projections must read
        the isolated reference clone when importing this migration baseline.
        """
        migration_root = self.settings.project_root / "data/migration/wechat"
        reference = migration_root / "reference/instance"
        try:
            self.adapter.root.resolve().relative_to(migration_root.resolve())
            if (
                (reference / "data/runs").is_dir()
                and (reference / "app/services/scheduler_service.py").is_file()
            ):
                return reference.resolve(), True
        except ValueError:
            pass
        return self.adapter.root.resolve(), False

    def _runtime_projection_date(self) -> str:
        for path in (self.adapter.root, *self.adapter.root.parents):
            match = re.match(r"freeze_(\d{4})(\d{2})(\d{2})T", path.name)
            if match:
                return "-".join(match.groups())
        return datetime.now(SOURCE_TZ).date().isoformat()

    def _stream_runtime_projection_payloads(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        root, is_reference = self._compat_runtime_projection_root()
        history = _legacy_refresh_history(root)
        scheduler = _legacy_scheduler_status(
            root,
            base_url=self.adapter.base_url,
            snapshot_date=self._runtime_projection_date(),
            # The 8774 process is explicitly scheduler-disabled, so its
            # persisted scheduler.json is not loaded by scheduler_service.start.
            use_persisted_config=not is_reference,
        )
        return history, scheduler

    def _stream_rows(self, key: str) -> Iterator[dict[str, Any]]:
        relative = self.adapter.FILES[key]
        yield from _stream_json_array(self.adapter.root / relative)

    def _stream_optional_rows(self, key: str) -> Iterator[dict[str, Any]]:
        relative = self.adapter.OPTIONAL_FILES[key]
        path = self.adapter.root / relative
        if not path.is_file():
            return
        yield from _stream_json_array(path)

    def _flush_stream_batch(
        self,
        settings: Any,
        lock_path: Path,
        writer: Callable[[sqlite3.Connection], None],
    ) -> None:
        active = getattr(self, "_stream_atomic_connection", None)
        if active is not None:
            writer(active)
            return
        with writer_lock(lock_path):
            with connect(settings) as con:
                with transaction(con):
                    writer(con)

    @staticmethod
    def _wechat_content_owned(con: sqlite3.Connection, content_id: str) -> tuple[bool, list[str]]:
        namespaces = [
            str(row["namespace"])
            for row in con.execute(
                "SELECT namespace FROM content_identifiers WHERE content_id=? ORDER BY namespace",
                (content_id,),
            )
        ]
        if namespaces:
            # Presence of even one non-WeChat identifier makes this a shared
            # canonical row.  A WeChat identifier alongside it is additive
            # identity, not ownership of the canonical content facts.
            return (
                bool(set(namespaces) <= _WECHAT_IDENTIFIER_NAMESPACES),
                namespaces,
            )
        # Existing canonical rows without any identifier have unknown/shared
        # ownership. Discovery and hit relations are observations, not proof
        # that WeChat owns the canonical content facts.
        return False, namespaces

    @staticmethod
    def _apply_article_identity_report(
        report: dict[str, Any], resolution: dict[str, Any],
    ) -> None:
        rejection = resolution.get("rejection")
        if rejection:
            report.setdefault("rejected", []).append(rejection)
        collisions = resolution.get("collisions") or []
        if collisions:
            report.setdefault("content_collisions", []).extend(collisions)
            report["collision_count"] = (
                int(report.get("collision_count", 0))
                + sum(int(item.get("collision_count", 1)) for item in collisions)
            )

    def _resolve_wechat_article_identity(
        self,
        con: sqlite3.Connection,
        *,
        row: dict[str, Any],
        source_id: str,
        planned: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Resolve the canonical target without mutating Hub state."""
        planned = planned or {}
        raw_url = row.get("normalized_url") or row.get("raw_url")
        url = _safe_url(raw_url)
        article_external_id = str(row.get("article_id") or source_id)
        candidates: dict[str, str] = {}

        identifier = con.execute(
            """
            SELECT content_id
            FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id=?
            """,
            (article_external_id,),
        ).fetchone()
        if identifier is not None:
            candidates["wechat_article_identifier"] = str(identifier["content_id"])
        elif article_external_id in planned.get("wechat_article", {}):
            candidates["wechat_article_identifier"] = planned["wechat_article"][
                article_external_id
            ]

        if url:
            url_content = con.execute(
                "SELECT content_id FROM contents WHERE canonical_url=?", (url,)
            ).fetchone()
            if url_content is not None:
                candidates["canonical_url"] = str(url_content["content_id"])
            elif url in planned.get("canonical_url", {}):
                candidates["canonical_url"] = planned["canonical_url"][url]
            url_identifier = con.execute(
                """
                SELECT content_id
                FROM content_identifiers
                WHERE namespace='wechat_url' AND external_id=?
                """,
                (url,),
            ).fetchone()
            if url_identifier is not None:
                candidates["wechat_url_identifier"] = str(url_identifier["content_id"])
            elif url in planned.get("wechat_url", {}):
                candidates["wechat_url_identifier"] = planned["wechat_url"][url]

        source_content = con.execute(
            "SELECT content_id FROM contents WHERE content_id=?", (source_id,)
        ).fetchone()
        if source_content is not None:
            candidates["source_content_id"] = str(source_content["content_id"])
        elif source_id in planned.get("content_id", {}):
            candidates["source_content_id"] = planned["content_id"][source_id]

        evidence = {
            key: value for key, value in candidates.items() if value
        }

        def conflict() -> dict[str, Any]:
            return {
                "accepted": False,
                "content_id": None,
                "is_owned": False,
                "existing": None,
                "url": url,
                "article_external_id": article_external_id,
                "identity_preserved": False,
                "rejection": {
                    "kind": "article_identity_conflict",
                    "source_id": source_id,
                    "reason": "identity_candidates_disagree",
                    "candidates": evidence,
                },
                "collisions": [{
                    "collision_count": 1,
                    "collision_type": "identity_conflict",
                    "source_id": source_id,
                    "candidates": evidence,
                    "preserved_fields": list(_CONTENT_PROTECTED_FIELDS),
                }],
            }

        identity_target = candidates.get("wechat_article_identifier")
        url_target = candidates.get("canonical_url")
        url_identifier_target = candidates.get("wechat_url_identifier")
        source_target = candidates.get("source_content_id")
        identity_preserved = False
        collisions: list[dict[str, Any]] = []

        if identity_target:
            hard_conflicts = {
                key: value
                for key, value in {
                    "source_content_id": source_target,
                    "wechat_url_identifier": url_identifier_target,
                }.items()
                if value and value != identity_target
            }
            if hard_conflicts:
                return conflict()
            if url_target and url_target != identity_target:
                identity_owned, identity_namespaces = self._wechat_content_owned(
                    con, identity_target
                )
                url_owned, url_namespaces = self._wechat_content_owned(
                    con, url_target
                )
                if not identity_owned or url_owned:
                    return conflict()
                identity_preserved = True
                collisions.append({
                    "collision_count": 1,
                    "collision_type": "identity_preserved",
                    "source_id": source_id,
                    "identity_content_id": identity_target,
                    "canonical_url_content_id": url_target,
                    "identity_namespaces": identity_namespaces,
                    "canonical_url_namespaces": url_namespaces,
                    "resolution": "identity_preserved",
                    "preserved_fields": ["canonical_url"],
                })
            cid = identity_target
        else:
            targets = {
                value for value in (
                    url_target, url_identifier_target, source_target
                ) if value
            }
            if len(targets) > 1:
                return conflict()
            cid = next(iter(targets), source_id)

        existing = con.execute(
            "SELECT * FROM contents WHERE content_id=?", (cid,)
        ).fetchone()
        is_owned = False
        namespaces: list[str] = []
        if existing is not None:
            is_owned, namespaces = self._wechat_content_owned(con, cid)
            if (
                not identity_preserved
                and url
                and str(existing["canonical_url"] or "") == url
            ):
                collisions.append({
                    "collision_count": 1,
                    "content_id": cid,
                    "original_namespaces": namespaces,
                    "preserved_fields": (
                        list(_CONTENT_PROTECTED_FIELDS) if not is_owned else []
                    ),
                })
        return {
            "accepted": True,
            "content_id": cid,
            "is_owned": is_owned if existing is not None else True,
            "existing": existing,
            "url": url,
            "article_external_id": article_external_id,
            "identity_preserved": identity_preserved,
            "rejection": None,
            "collisions": collisions,
        }

    def _upsert_wechat_article(
        self, con: sqlite3.Connection, *, row: dict[str, Any], source_id: str,
        now: str, report: dict[str, Any],
    ) -> tuple[str | None, bool]:
        resolution = self._resolve_wechat_article_identity(
            con, row=row, source_id=source_id
        )
        self._apply_article_identity_report(report, resolution)
        if not resolution["accepted"]:
            return None, False
        cid = str(resolution["content_id"])
        existing = resolution["existing"]
        is_owned = bool(resolution["is_owned"])
        url = resolution["url"]
        article_external_id = str(resolution["article_external_id"])
        identity_preserved = bool(resolution["identity_preserved"])
        creator = row.get("account_id")
        if creator:
            con.execute(
                "INSERT OR IGNORE INTO creators(creator_id,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (creator, "wechat-search", creator, now, now, _json({"source": "article_reference"})),
            )
        canonical_url = (
            existing["canonical_url"]
            if identity_preserved and existing is not None
            else url
        )
        values = (
            cid, "external_article", row.get("title"), canonical_url, creator,
            row.get("author_name"), _source_time(row.get("published_at")),
            _source_time(row.get("first_seen_at")) or now,
            _source_time(row.get("last_seen_at")) or now, row.get("content_file_path"),
            "mp.weixin.qq.com" if url and "mp.weixin.qq.com" in url else None,
            _json({**row, "source_timezone": "Asia/Shanghai"}),
        )
        if existing is None:
            con.execute(
                """
                INSERT INTO contents(
                    content_id,content_type,title,canonical_url,creator_id,author_name,
                    published_at,first_seen_at,updated_at,md_path,domain,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            is_owned = True
        elif is_owned:
            con.execute(
                """
                UPDATE contents
                SET content_type=?,title=?,canonical_url=?,creator_id=?,author_name=?,
                    published_at=?,updated_at=?,md_path=?,domain=?,payload_json=?
                WHERE content_id=?
                """,
                (
                    values[1], values[2], values[3], values[4], values[5],
                    values[6], values[8], values[9], values[10], values[11], cid,
                ),
            )
        # 对非微信共享内容只追加微信身份，不触碰其内容事实列。
        for namespace, external in (
            ("wechat_article", article_external_id),
            ("wechat_url", url),
        ):
            if not external:
                continue
            con.execute(
                """
                INSERT INTO content_identifiers(
                    namespace,external_id,content_id,first_seen_at,payload_json
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(namespace,external_id) DO UPDATE SET
                    payload_json=excluded.payload_json
                """,
                (namespace, str(external), cid, now, _json(row)),
            )
        return cid, is_owned

    def _import_article_markdown(
        self,
        con: sqlite3.Connection,
        *,
        article_id: str,
        old_article_id: str,
        original_relative: str,
        manifest_id: str,
        created_at: str,
        update_content_fields: bool = True,
    ) -> None:
        """落盘正文资产，同时保留原路径和实际冻结来源两个维度。"""
        relative_path = str(original_relative).replace("\\", "/")
        asset_path = None
        actual_source_relative = relative_path
        try:
            markdown, actual_source_relative = self.adapter.read_markdown_with_source(
                relative_path
            )
            content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            asset_file = self.settings.asset_store_path / "wechat" / f"{content_hash}.md"
            asset_file.parent.mkdir(parents=True, exist_ok=True)
            if not asset_file.exists():
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{content_hash}.", suffix=".tmp", dir=str(asset_file.parent)
                )
                temporary_path = Path(temporary)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(markdown)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, asset_file)
                    getattr(self, "_import_new_assets", set()).add(asset_file)
                finally:
                    temporary_path.unlink(missing_ok=True)
            asset_path = str(
                asset_file.relative_to(self.settings.asset_store_path)
            ).replace("\\", "/")
            if update_content_fields:
                con.execute(
                    "UPDATE contents SET file_hash=?,content_hash=? WHERE content_id=?",
                    (content_hash, content_hash, article_id),
                )
        except WechatSourceError:
            pass
        source_ref = manifest_ref(
            "wechat-search",
            manifest_id,
            actual_source_relative,
        )
        # 旧版本可能已用原 relative 生成过空资产 source_ref；重放时收敛到
        # 实际命中的 code-snapshot/...，不保留双记录。
        con.execute(
            """
            DELETE FROM wechat_article_paths
            WHERE article_id=? AND old_article_id=? AND source_ref<>?
            """,
            (article_id, old_article_id, source_ref),
        )
        con.execute(
            """
            INSERT INTO wechat_article_paths(
                article_id,old_article_id,relative_path,asset_path,source_ref,created_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(article_id,old_article_id,source_ref) DO UPDATE SET
                relative_path=excluded.relative_path,
                asset_path=excluded.asset_path
            """,
            (
                article_id,
                old_article_id,
                relative_path,
                asset_path,
                source_ref,
                created_at,
            ),
        )

    def _import_history_streaming(
        self, *, dry_run: bool, command_id: str,
    ) -> dict[str, Any]:
        """Run the streaming importer under one writer lock and transaction."""
        if dry_run:
            return self._import_history_streaming_impl(
                dry_run=dry_run, command_id=command_id
            )
        result: dict[str, Any] | None = None
        try:
            with writer_lock(self.settings.lock_path):
                with connect(self.settings) as con:
                    with transaction(con):
                        self._stream_atomic_connection = con
                        try:
                            result = self._import_history_streaming_impl(
                                dry_run=dry_run, command_id=command_id
                            )
                        finally:
                            self._stream_atomic_connection = None
            assert result is not None
            final_status = str(result.get("semantic_status") or "failed")
            if final_status == "succeeded":
                self._connection_status("healthy", success_at=_utc_now())
            else:
                self._connection_status("degraded", error="wechat_import_verification_failed")
            self._audit(
                "wechat.import",
                "succeeded" if final_status == "succeeded" else "failed",
                details={
                    "command_id": command_id,
                    "batch_id": result.get("batch_id"),
                    "counts": result.get("counts"),
                    "audit": result.get("audit"),
                    "semantic_status": final_status,
                    "transaction_committed": True,
                },
            )
            return result
        except Exception as exc:
            # The transaction has already rolled back.  Keep command/audit
            # evidence in independent transactions; never leave a running
            # ingestion batch behind after a failed streaming write.
            self._safe_status("degraded", f"wechat_import_failed:{type(exc).__name__}")
            self._audit(
                "wechat.import",
                "failed",
                details={
                    "command_id": command_id,
                    "kind": type(exc).__name__,
                    "error": str(exc)[:500],
                    "transaction_rolled_back": True,
                },
            )
            raise

    def _import_history_streaming_impl(
        self, *, dry_run: bool, command_id: str,
    ) -> dict[str, Any]:
        """Import a large freeze in bounded phases.

        The source remains read-only and every phase is idempotent.  All
        business writes share one writer lock and one SQLite transaction:
        a crash or injected failure rolls back the entire business import,
        while command/audit failure evidence is persisted separately.
        A replay executes the same natural-key upserts only after the prior
        transaction has rolled back.
        """
        try:
            seal = self.adapter.verify_freeze_seal()
            manifest = self.adapter.file_manifest()
            runtime = self.adapter.runtime_records()
            monitor = self.adapter.local_json(self.adapter.FILES["monitor"])
            deltas = self.adapter.local_json(
                self.adapter.OPTIONAL_FILES["keyword_read_deltas"], required=False
            ) or []
            delta_meta = self.adapter.local_json(
                self.adapter.OPTIONAL_FILES["article_metric_meta"], required=False
            ) or {}
        except WechatSourceError as exc:
            message = f"{exc.kind}: {exc}"
            if getattr(self, "_stream_atomic_connection", None) is None:
                self._safe_status("offline", message)
                self._audit("wechat.import", "failed", details={"command_id": command_id, "kind": exc.kind, "error": str(exc)})
            raise ConflictError(message) from exc

        adapter_manifest_digest = self.adapter.manifest_id(manifest)
        batch_id = _id("batch", f"wechat:{adapter_manifest_digest}:full")
        counts = {
            "keywords": len((monitor or {}).get("keywords") or []),
            "creators": _stream_json_count(self.adapter.root / self.adapter.FILES["accounts"]),
            "contents": _stream_json_count(self.adapter.root / self.adapter.FILES["articles"]),
            "search_snapshots": _stream_json_count(self.adapter.root / self.adapter.FILES["snapshots"]),
            "search_hits": _stream_json_count(self.adapter.root / self.adapter.FILES["hits"]),
            "snapshot_terms": _stream_json_count(self.adapter.root / self.adapter.FILES["terms"]),
            "metric_observations": _stream_json_count(self.adapter.root / self.adapter.FILES["observations"]),
            "keyword_read_deltas": len(deltas),
        }
        entries = [
            {"relative_path": item["path"], "content_hash": item.get("sha256"), "size_bytes": item.get("size")}
            for item in manifest.values() if isinstance(item, dict) and item.get("path")
        ]
        manifest_db_id = manifest_id_for(
            "wechat-search", {"source_kind": "normalized+markdown"}, entries
        )
        report: dict[str, Any] = {
            "manifest_id": manifest_db_id,
            "adapter_manifest_digest": adapter_manifest_digest,
            "manifest": manifest, "rejected": [],
            "freeze_seal": seal,
            "collision_count": 0, "content_collisions": [],
            "placeholder_count": 0, "placeholder_samples": [], "full_sync": True,
            "streaming": True, "batch_size": _IMPORT_BATCH_SIZE,
            "metric_fact_count": 0, "metric_unique_count": 0,
            "metric_collision_extra_count": 0, "metric_collision_group_count": 0,
            "metric_collision_same_value_count": 0, "metric_collision_value_diff_count": 0,
            "metric_collisions": [],
            "metric_compatibility": {
                "canonical_prefix": "wechat.article./wechat.keyword.",
                "legacy_keys_preserved": True,
                "policy": "只新增规范 key，不静默改写已有历史观测",
            },
        }
        if dry_run:
            # Do not turn a dry-run into a memory-heavy operation.  Counts and
            # the manifest are sufficient evidence that no write occurred.
            report["reconcile"] = self._reconcile_summary(
                {"keywords": monitor.get("keywords", []), "articles": [], "snapshots": [], "hits": [], "observations": [], "keyword_read_deltas": deltas},
                dry_run=True,
            )
            self._audit(
                "wechat.import",
                "succeeded",
                details={
                    "command_id": command_id,
                    "dry_run": True,
                    "batch_id": batch_id,
                    "counts": counts,
                    "audit": report,
                    "semantic_status": "dry_run",
                },
            )
            return {
                "dry_run": True,
                "source": "legacy_normalized",
                "counts": counts,
                "batch_id": batch_id,
                "semantic_status": "dry_run",
                "verified": False,
                "audit": report,
            }

        now = _utc_now()
        # The adapter digest describes the raw file manifest; the checkpoint
        # and readiness contract use the persisted canonical source-manifest ID.

        def prepare(con: sqlite3.Connection) -> None:
            write_manifest(
                con, manifest_id=manifest_db_id, system_key="wechat-search",
                source_kind="normalized+markdown",
                root_fingerprint=hashlib.sha256(f"wechat-search:{self.adapter.root.resolve()}".encode()).hexdigest(),
                entries=entries, captured_at=now,
                payload={"source_root": "configured/wechat-search", "batch_id": batch_id},
            )
            con.execute(
                "INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET status='running',started_at=excluded.started_at,payload_json=excluded.payload_json",
                (batch_id, "wechat-search", "history", "running", now,
                 manifest_ref("wechat-search", manifest_db_id), _json(report)),
            )
            legacy_full, legacy_keywords, legacy_accounts = _legacy_projection_payload(
                {
                    "monitor": monitor, "keyword_read_deltas": deltas,
                    "article_metric_meta": delta_meta, "runtime": runtime,
                },
                read_delta_source=self._compat_read_delta_source(),
            )
            projection_rows = [
                ("full", "", legacy_full),
                (
                    "bootstrap", "",
                    _projection_bootstrap_payload(
                        legacy_full, legacy_keywords, legacy_accounts
                    ),
                ),
                ("keyword_manage", "", self._stream_keyword_manage_payload(runtime, legacy_keywords)),
            ]
            projection_rows.extend(("keyword", key, value) for key, value in legacy_keywords.items())
            projection_rows.extend(("account", key, value) for key, value in legacy_accounts.items())
            for kind, subject_id, payload in projection_rows:
                self._upsert_projection(con, kind, subject_id, payload, manifest_db_id, now)

        self._flush_stream_batch(self.settings, self.settings.lock_path, prepare)

        ids: dict[str, str] = {}
        rejected_article_ids: set[str] = set()
        source_url_ids: dict[str, str] = {}
        def write_accounts(con: sqlite3.Connection) -> None:
            for row in self._stream_rows("accounts"):
                aid = str(row.get("account_id") or _id("creator", row.get("canonical_name")))
                con.execute(
                    "INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json",
                    (aid, row.get("canonical_name"), "wechat-search", aid,
                     _source_time(row.get("first_seen_at")) or now,
                     _source_time(row.get("last_seen_at")) or now, _json(row)),
                )
        self._flush_stream_batch(self.settings, self.settings.lock_path, write_accounts)

        article_rows: list[dict[str, Any]] = []
        def write_articles(con: sqlite3.Connection) -> None:
            for row in self._stream_rows("articles"):
                article_rows.append(row)
                source_id = str(row.get("article_id") or _id("content", row.get("title")))
                raw_url = row.get("normalized_url") or row.get("raw_url")
                url = _safe_url(raw_url)
                if url:
                    source_url_ids.setdefault(url, source_id)
                if raw_url and _is_placeholder_url(raw_url):
                    report["placeholder_count"] += 1
                elif raw_url and url is None:
                    report["rejected"].append({"kind": "url", "source_id": source_id, "value": raw_url, "reason": "invalid_url"})
                cid, is_owned = self._upsert_wechat_article(
                    con, row=row, source_id=source_id, now=now, report=report
                )
                if cid is None:
                    rejected_article_ids.add(source_id)
                    continue
                ids[source_id] = cid
                old_path = row.get("content_file_path") or row.get("source_file_path")
                if old_path:
                    self._import_article_markdown(
                        con,
                        article_id=cid,
                        old_article_id=source_id,
                        original_relative=str(old_path),
                        manifest_id=manifest_db_id,
                        created_at=now,
                        update_content_fields=is_owned,
                    )
        self._flush_stream_batch(self.settings, self.settings.lock_path, write_articles)

        keyword_ids = {str(row.get("keyword_id")) for row in monitor.get("keywords", []) if row.get("keyword_id")}
        def write_keywords(con: sqlite3.Connection) -> None:
            for row in monitor.get("keywords", []):
                kid, keyword = str(row.get("keyword_id") or _id("kw", row.get("keyword"))), str(row.get("keyword") or "").strip()
                if not keyword:
                    report["rejected"].append({"kind": "keyword", "row": row, "reason": "missing keyword"})
                    continue
                con.execute(
                    "INSERT INTO keywords(keyword_id,platform,keyword,status,topic,keyword_bucket,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(keyword_id) DO UPDATE SET keyword=excluded.keyword,status=excluded.status,topic=excluded.topic,keyword_bucket=excluded.keyword_bucket,updated_at=excluded.updated_at,payload_json=excluded.payload_json",
                    (kid, "wechat-search", keyword, _legacy_keyword_status(row), row.get("topic") or keyword,
                     row.get("keyword_bucket") or row.get("bucket"), _source_time(row.get("first_seen_at")) or now,
                     _source_time(row.get("updated_at")) or now, _json(row)),
                )
            if keyword_ids:
                con.execute(
                    f"UPDATE keywords SET status='archived',updated_at=? WHERE platform='wechat-search' AND keyword_id NOT IN ({','.join('?' for _ in keyword_ids)})",
                    (now, *sorted(keyword_ids)),
                )
        self._flush_stream_batch(self.settings, self.settings.lock_path, write_keywords)

        snapshot_features: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for term in self._stream_rows("terms"):
            bucket = snapshot_features.setdefault(str(term.get("snapshot_id")), {"suggestions": [], "related": []})
            target = "suggestions" if str(term.get("term_type") or "").lower() in {"suggestion", "suggestions"} else "related"
            bucket[target].append({"term": term.get("term_text"), "position": term.get("position")})
        snapshot_map: dict[str, str] = {}
        valid_snapshots: set[str] = set()
        def write_snapshots(con: sqlite3.Connection) -> None:
            keyword_by_id = {str(x.get("keyword_id")): x for x in monitor.get("keywords", [])}
            for row in self._stream_rows("snapshots"):
                sid, kid = str(row.get("snapshot_id") or ""), row.get("keyword_id")
                keyword = (keyword_by_id.get(str(kid)) or {}).get("keyword") or str(kid or "")
                captured = _source_time(row.get("captured_at"))
                if not sid or not captured:
                    report["rejected"].append({"kind": "snapshot", "row": row, "reason": "invalid snapshot_id/captured_at"})
                    continue
                if kid:
                    con.execute(
                        "INSERT OR IGNORE INTO keywords(keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                        (str(kid), "wechat-search", keyword or str(kid), "active", captured, captured,
                         _json({"source": "snapshot_reference"})),
                    )
                existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("wechat-search", keyword, captured)).fetchone()
                actual_sid = str(existing[0]) if existing else sid
                snapshot_map[sid] = actual_sid
                valid_snapshots.add(actual_sid)
                con.execute(
                    "INSERT INTO search_snapshots(snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(snapshot_id) DO UPDATE SET keyword=excluded.keyword,keyword_id=excluded.keyword_id,captured_at=excluded.captured_at,result_count=excluded.result_count,features_json=excluded.features_json,source_ref=excluded.source_ref,payload_json=excluded.payload_json",
                    (actual_sid, "wechat-search", keyword, kid, captured, row.get("trigger_type"),
                     _number(row.get("result_count")), _json(snapshot_features.get(sid, {"suggestions": [], "related": []})),
                     manifest_ref("wechat-search", manifest_db_id, str(row.get("source_file_path") or "")),
                     _json({**row, "source_timezone": row.get("timezone") or "Asia/Shanghai"})),
                )
        self._flush_stream_batch(self.settings, self.settings.lock_path, write_snapshots)

        def write_hits(con: sqlite3.Connection) -> None:
            for row in self._stream_rows("hits"):
                sid = snapshot_map.get(str(row.get("snapshot_id")))
                rank = _number(row.get("rank"))
                hid = str(row.get("hit_id") or _id("hit", f"{row.get('snapshot_id')}:{row.get('rank')}"))
                if sid not in valid_snapshots or not isinstance(rank, (int, float)) or int(rank) <= 0 or int(rank) != rank:
                    report["rejected"].append({"kind": "hit", "row": row, "reason": "invalid snapshot/rank"})
                    continue
                if str(row.get("article_id") or "") in rejected_article_ids:
                    continue
                mapped = ids.get(str(row.get("article_id")))
                con.execute("DELETE FROM search_hits WHERE snapshot_id=? AND rank=? AND hit_id<>?", (sid, int(rank), hid))
                con.execute(
                    "INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(hit_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,rank=excluded.rank,content_id=excluded.content_id,title_raw=excluded.title_raw,url_raw=excluded.url_raw,creator_name_raw=excluded.creator_name_raw,payload_json=excluded.payload_json",
                    (hid, sid, int(rank), mapped, row.get("title_raw"), row.get("url_raw"), row.get("account_name_raw"), _json(row)),
                )
                if mapped:
                    captured = con.execute("SELECT captured_at FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0]
                    con.execute(
                        "INSERT OR IGNORE INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                        (_id("discovery", f"{mapped}:{sid}"), mapped, "wechat-search", "keyword-rank", captured, sid, row.get("url_raw"), _json(row)),
                    )
        self._flush_stream_batch(self.settings, self.settings.lock_path, write_hits)

        self._stream_metric_phase(manifest_db_id, report, ids, snapshot_map, now)
        keyword_delta_facts = self._prepare_keyword_delta_facts(
            {"keyword_read_deltas": deltas}, report
        )
        self._flush_stream_batch(
            self.settings, self.settings.lock_path,
            lambda con: [self._write_metric_fact(con, fact) for fact in keyword_delta_facts],
        )
        runtime_history, scheduler_runtime = self._stream_runtime_projection_payloads()
        def write_runtime(con: sqlite3.Connection) -> None:
            self._write_runtime_records(con, runtime, manifest_db_id, now)
            history_count = len(runtime_history)
            for index, payload in enumerate(runtime_history):
                subject_id = str(payload.get("batch_id") or index)
                self._upsert_runtime_projection(
                    con, subject_id, "batch", payload, manifest_db_id, now,
                    sort_rank=history_count - index,
                )
            self._upsert_runtime_projection(
                con, "scheduler", "scheduler", scheduler_runtime,
                manifest_db_id, now,
            )
        self._flush_stream_batch(
            self.settings, self.settings.lock_path,
            write_runtime,
        )
        self._stream_article_projections(article_rows, monitor, manifest_db_id, now)
        report["asset_integrity"] = self._asset_integrity(
            {"articles": article_rows},
            connection=getattr(self, "_stream_atomic_connection", None),
        )

        def finish(con: sqlite3.Connection) -> None:
            monitor_keyword_ids = {
                str(row.get("keyword_id"))
                for row in monitor.get("keywords", []) if row.get("keyword_id")
            }
            registry_keyword_ids = {
                str(row.get("keyword_id"))
                for row in runtime.get("keyword_registry", []) if row.get("keyword_id")
            }
            source_article_ids = {
                str(row.get("article_id")) for row in article_rows if row.get("article_id")
            }
            hub_monitor_keywords = con.execute(
                "SELECT COUNT(*) FROM keywords WHERE platform='wechat-search' AND keyword_id IN ({})".format(
                    ",".join("?" for _ in monitor_keyword_ids) or "''"
                ), tuple(sorted(monitor_keyword_ids))
            ).fetchone()[0]
            hub_registry_keywords = con.execute(
                "SELECT COUNT(*) FROM keywords WHERE platform='wechat-search' AND keyword_id IN ({})".format(
                    ",".join("?" for _ in registry_keyword_ids) or "''"
                ), tuple(sorted(registry_keyword_ids))
            ).fetchone()[0]
            hub_article_identifiers = con.execute(
                "SELECT COUNT(*) FROM content_identifiers WHERE namespace='wechat_article'"
            ).fetchone()[0]
            hub = {
                "keywords": con.execute("SELECT COUNT(*) FROM keywords WHERE platform='wechat-search'").fetchone()[0],
                "contents": con.execute("SELECT COUNT(DISTINCT content_id) FROM content_identifiers WHERE namespace='wechat_article'").fetchone()[0],
                "snapshots": con.execute("SELECT COUNT(*) FROM search_snapshots WHERE platform='wechat-search'").fetchone()[0],
                "hits": con.execute("SELECT COUNT(*) FROM search_hits WHERE snapshot_id IN (SELECT snapshot_id FROM search_snapshots WHERE platform='wechat-search')").fetchone()[0],
                "metric_observations": con.execute(
                    """
                    SELECT COUNT(*) FROM metric_observations
                    WHERE metric_key LIKE 'wechat.article.%'
                       OR metric_key LIKE 'wechat.keyword.%'
                    """
                ).fetchone()[0],
            }
            source = {
                "keywords": len({
                    str(row.get("keyword_id"))
                    for row in monitor.get("keywords", []) if row.get("keyword_id")
                } | {
                    str(row.get("keyword_id"))
                    for row in runtime.get("keyword_registry", []) if row.get("keyword_id")
                }),
                "contents": len({
                    _safe_url(row.get("normalized_url") or row.get("raw_url"))
                    or str(row.get("article_id") or _id("content", row.get("title")))
                    for row in article_rows
                }),
                "snapshots": counts["search_snapshots"], "hits": counts["search_hits"],
                "metric_observations": report["metric_unique_count"] + len(keyword_delta_facts),
            }
            match = {key: source[key] == hub[key] for key in source}
            scope_match = {
                "keywords_monitor_active_scope": len(monitor_keyword_ids) == hub_monitor_keywords,
                "keywords_runtime_registry": len(registry_keyword_ids) == hub_registry_keywords,
                "contents_wechat_article_identifiers": len(source_article_ids) == hub_article_identifiers,
                "contents_canonical_unique": source["contents"] == hub["contents"],
            }
            report["reconcile"] = {
                "source": source, "hub": hub,
                "difference": {key: hub[key] - source[key] for key in source},
                "match": match,
                "dimensions": {key: {"source": source[key], "hub": hub[key], "difference": hub[key] - source[key], "match": match[key]} for key in source},
                "scope": {
                    "keywords_monitor_active_scope": {"source": len(monitor_keyword_ids), "hub": hub_monitor_keywords},
                    "keywords_runtime_registry": {"source": len(registry_keyword_ids), "hub": hub_registry_keywords},
                    "contents_wechat_article_identifiers": {"source": len(source_article_ids), "hub": hub_article_identifiers},
                    "contents_canonical_unique": {"source": source["contents"], "hub": hub["contents"]},
                },
                "scope_match": scope_match,
                "status": "mismatch" if not all(scope_match.values()) or not all(match.values()) else "matched",
                "verified": all(scope_match.values()) and all(match.values()),
                "note": "关键词按 monitor active scope/runtime registry 分层；文章按 wechat_article 身份与 canonical contents 去重分层核对",
            }
            verified = (
                not report["rejected"]
                and report["reconcile"]["status"] == "matched"
                and report["reconcile"].get("verified") is True
                and report["asset_integrity"].get("verified") is True
            )
            status = "succeeded" if verified else (
                "partial_failed"
                if report["rejected"] or report["reconcile"]["status"] == "mismatch"
                else "failed"
            )
            records_written = max(0, sum(counts.values()) - len(report["rejected"]))
            con.execute(
                "UPDATE ingestion_batches SET status=?,finished_at=?,records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=? WHERE batch_id=?",
                (status, now, sum(counts.values()), records_written, len(report["rejected"]), _json(report["rejected"]), _json(report), batch_id),
            )
            con.execute(
                """
                INSERT INTO ingestion_checkpoints(
                    adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json
                ) VALUES(
                    ?,?,
                    CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                    CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                    CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                    CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
                    ?
                )
                ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET
                    cursor_value=CASE WHEN ?='succeeded' THEN excluded.cursor_value ELSE ingestion_checkpoints.cursor_value END,
                    source_hash=CASE WHEN ?='succeeded' THEN excluded.source_hash ELSE ingestion_checkpoints.source_hash END,
                    batch_id=CASE WHEN ?='succeeded' THEN excluded.batch_id ELSE ingestion_checkpoints.batch_id END,
                    payload_json=excluded.payload_json,
                    last_success_at=CASE WHEN ?='succeeded'
                        THEN excluded.last_success_at
                        ELSE ingestion_checkpoints.last_success_at END
                """,
                (
                    "wechat-search", "normalized",
                    status, now,
                    status, manifest_db_id,
                    status, now,
                    status, batch_id,
                    _json(report),
                    status, status, status, status,
                ),
            )
        self._flush_stream_batch(self.settings, self.settings.lock_path, finish)
        final_status = "succeeded" if (
            not report["rejected"]
            and report["reconcile"]["status"] == "matched"
            and report["reconcile"].get("verified") is True
            and report["asset_integrity"].get("verified") is True
        ) else (
            "partial_failed"
            if report["rejected"] or report["reconcile"]["status"] == "mismatch"
            else "failed"
        )
        return {
            "dry_run": False,
            "source": "legacy_normalized",
            "counts": counts,
            "batch_id": batch_id,
            "job_id": batch_id,
            "semantic_status": final_status,
            "verified": final_status == "succeeded",
            "checkpoint": {
                "adapter_key": "wechat-search",
                "checkpoint_key": "normalized",
                "source_hash": manifest_db_id,
                "advanced": final_status == "succeeded",
            },
            "audit": report,
        }

    def _upsert_projection(
        self, con: sqlite3.Connection, kind: str, subject_id: str,
        payload: dict[str, Any], manifest_id: str, now: str,
    ) -> None:
        projection_hash = _projection_hash(payload)
        stored = _projection_json(payload)
        if kind == "article_detail":
            stored = _json({
                "__compressed_json__": "zlib+base64",
                "data": base64.b64encode(zlib.compress(stored.encode("utf-8"), 6)).decode("ascii"),
            })
        con.execute(
            """
            INSERT INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,
                source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(projection_kind,subject_id,source_hash) DO UPDATE SET
                payload_json=excluded.payload_json,source_manifest_id=excluded.source_manifest_id,
                source_ref=excluded.source_ref,updated_at=excluded.updated_at
            """,
            (
                _id("wechat-projection", f"{kind}:{subject_id}:{projection_hash}"),
                kind, subject_id, stored, projection_hash, manifest_id,
                manifest_ref("wechat-search", manifest_id, "normalized/monitor-data.json"), now,
            ),
        )

    def cleanup_derived_projections(
        self,
        *,
        projection_kinds: tuple[str, ...] = (
            "top_level", "full", "keyword", "account", "bootstrap",
            "keyword_manage", "article_detail",
        ),
    ) -> int:
        """Explicitly remove superseded derived rows without decoding payloads.

        The cleanup is deliberately opt-in and never runs ``VACUUM``.  Runtime
        projections, core snapshots/articles, and audit facts are excluded.
        """
        if not projection_kinds:
            return 0
        placeholders = ",".join("?" for _ in projection_kinds)
        rank_order = """
            CASE WHEN projection_kind='article_detail' THEN 1
                 WHEN json_extract(payload_json,'$.generated_at') IS NULL THEN 1
                 ELSE 0 END,
            CASE WHEN projection_kind='article_detail' THEN NULL
                 ELSE julianday(json_extract(payload_json,'$.generated_at')) END DESC,
            updated_at DESC,
            projection_id DESC
        """
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    result = con.execute(
                        f"""
                        DELETE FROM wechat_legacy_projections
                        WHERE projection_id IN (
                            SELECT projection_id
                            FROM (
                                SELECT projection_id,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY projection_kind,subject_id
                                           ORDER BY {rank_order}
                                       ) AS row_number
                                FROM wechat_legacy_projections
                                WHERE projection_kind IN ({placeholders})
                            )
                            WHERE row_number > 1
                        )
                        """,
                        projection_kinds,
                    )
                    return int(result.rowcount or 0)

    def _upsert_runtime_projection(
        self, con: sqlite3.Connection, subject_id: str, subtype: str,
        payload: dict[str, Any], manifest_id: str, now: str,
        *, sort_rank: int | None = None,
    ) -> None:
        """Upsert an imported runtime projection without leaking Hub metadata.

        The old importer used to prepend a duplicate ``started_at`` key to
        preserve directory-mtime order.  That made SQLite JSON1 and Python
        disagree about the timestamp.  Keep the business payload canonical:
        history ordering is determined by the decoded ``started_at`` value.
        """
        ordered = _sorted_json_value(payload)
        projection_hash = _projection_hash(ordered)
        parts = [json.dumps("runtime_subtype") + ":" + json.dumps(subtype)]
        for key, value in ordered.items():
            encoded_key = json.dumps(key, ensure_ascii=False)
            parts.append(
                encoded_key + ":" + json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), default=str
                )
            )
        stored = "{" + ",".join(parts) + "}"
        con.execute(
            """
            INSERT INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,
                source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(projection_kind,subject_id,source_hash) DO UPDATE SET
                payload_json=excluded.payload_json,
                source_manifest_id=excluded.source_manifest_id,
                source_ref=excluded.source_ref,
                updated_at=excluded.updated_at
            """,
            (
                _id(
                    "wechat-projection",
                    f"runtime:{subject_id}:{projection_hash}",
                ),
                "runtime", subject_id, stored, projection_hash, manifest_id,
                manifest_ref("wechat-search", manifest_id, "data/runs"), now,
            ),
        )
        if subtype == "scheduler":
            con.execute(
                """
                INSERT INTO search_scheduler_state(
                    system_key,platform,enabled,next_run_at,last_run_at,
                    updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(system_key,platform) DO UPDATE SET
                    enabled=excluded.enabled,
                    next_run_at=excluded.next_run_at,
                    last_run_at=excluded.last_run_at,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    "wechat-search",
                    "wechat-search",
                    int(bool(ordered.get("enabled", False))),
                    ordered.get("next_run_at"),
                    ordered.get("last_run_at"),
                    ordered.get("updated_at") or now,
                    _json(ordered),
                ),
            )

    @staticmethod
    def _stream_keyword_manage_payload(
        runtime: dict[str, list[dict[str, Any]]],
        legacy_keywords: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        groups = []
        total = ranked = 0
        registry = runtime.get("keyword_registry") or []
        for group in sorted(
            [
                row for row in (runtime.get("keyword_groups") or [])
                if row.get("archived_at") is None
            ],
            key=lambda x: (int(x.get("display_order") or x.get("order") or 0), str(x.get("group_id") or "")),
        ):
            items = []
            for item in registry:
                if not _keyword_manage_visible(item):
                    continue
                if str(item.get("group_id") or "") != str(group.get("group_id") or ""):
                    continue
                stat = legacy_keywords.get(str(item.get("keyword_id")), {})
                today_best = stat.get("today_best")
                items.append({
                    "keyword_id": item.get("keyword_id"),
                    "keyword_text": item.get("keyword_text", ""),
                    "note": item.get("note") or "",
                    "batch_default_selected": bool(item.get("batch_default_selected", True)),
                    "refresh_frequency_days": int(item.get("refresh_frequency_days") or 1),
                    "effective_refresh_interval_hours": int(item.get("refresh_frequency_days") or 1) * 24,
                    "refresh_frequency_source": item.get("refresh_frequency_source") or "auto",
                    "refresh_policy_reason": item.get("refresh_policy_reason") or "",
                    "last_refresh_at": item.get("last_refresh_at"),
                    "last_refresh_attempt_at": item.get("last_refresh_attempt_at"),
                    "last_refresh_status": item.get("last_refresh_status"),
                    "next_refresh_at": item.get("next_refresh_at"),
                    "refresh_age_days": item.get("refresh_age_days"),
                    "is_refresh_due": bool(item.get("is_refresh_due", True)),
                    "commercial_value_score": int(item.get("commercial_value_score") or 5),
                    "commercial_value_source": item.get("commercial_value_source") or "auto",
                    "commercial_value_reason": item.get("commercial_value_reason") or "",
                    "lifecycle_stage": item.get("lifecycle_stage") or "established",
                    "observation_started_at": item.get("observation_started_at"),
                    "observation_deadline_at": item.get("observation_deadline_at"),
                    "discovery_candidate_id": item.get("discovery_candidate_id"),
                    "auto_archive_locked": bool(item.get("auto_archive_locked")),
                    "archive_reason_code": item.get("archive_reason_code"),
                    "archive_reason_detail": item.get("archive_reason_detail"),
                    "today_best": today_best,
                    "coverage_days": stat.get("coverage_days", 0),
                    "tracked_accounts": stat.get("tracked_accounts", 0),
                    "article_count": stat.get("article_count", 0),
                    "seo_status": "ranked" if today_best else "not_ranked",
                })
            ranked_here = sum(1 for x in items if x.get("today_best"))
            total += len(items)
            ranked += ranked_here
            groups.append({
                "group_id": group.get("group_id"), "label": group.get("label"),
                "order": group.get("display_order", group.get("order", 0)),
                "keywords": items, "total": len(items), "ranked_count": ranked_here,
                "not_ranked_count": len(items) - ranked_here,
            })
        return {"groups": groups, "total": total, "ranked_total": ranked,
                "not_ranked_total": total - ranked,
                "updated_at": (registry or [{}])[0].get("updated_at")}

    def _stream_metric_phase(
        self, manifest_id: str, report: dict[str, Any], ids: dict[str, str],
        snapshot_map: dict[str, str], now: str,
    ) -> None:
        winners: dict[tuple[str, str, str, str, str | None], dict[str, Any]] = {}
        collision_candidates: dict[tuple[str, str, str, str, str | None], list[dict[str, Any]]] = {}
        labels = tuple(ARTICLE_METRIC_KEYS.items())
        for row in self._stream_rows("observations"):
            cid = ids.get(str(row.get("article_id")))
            observed = _source_time(row.get("observed_at"))
            source_oid = str(row.get("observation_id") or "")
            snapshot_id = snapshot_map.get(str(row.get("source_snapshot_id"))) if row.get("source_snapshot_id") else None
            if not cid or observed is None:
                continue
            for field, (metric_key, label) in labels:
                value = _number(row.get(field))
                if value is None:
                    continue
                source_path = str(row.get("source_file_path") or row.get("source_ref") or "").replace("\\", "/").lstrip("./")
                fact = {
                    # The legacy source format used ``<source_oid>:wechat.<field>``.
                    # Keep that historical observation untouched and put canonical
                    # facts in a distinct, deterministic namespace.
                    "observation_id": f"{source_oid}:{metric_key}" if source_oid else _id("observation", f"{cid}:{metric_key}:{observed}:{snapshot_id}:{_json(row)}"),
                    "source_observation_id": source_oid, "subject_id": cid, "metric_key": metric_key,
                    "metric_label": label, "observed_at": observed, "numeric_value": value,
                    "snapshot_id": snapshot_id,
                    "source_ref": manifest_ref("wechat-search", manifest_id, source_path) if source_path else manifest_ref("wechat-search", manifest_id),
                    "source_file_path": source_path,
                    "canonical_row_json": _json(row),
                    "row": {**row, "source_snapshot_id": snapshot_id},
                }
                natural = ("content", cid, metric_key, observed, snapshot_id)
                collision_candidates.setdefault(natural, []).append(fact)
                old = winners.get(natural)
                if old is None or self._candidate_sort_key(fact) < self._candidate_sort_key(old):
                    winners[natural] = fact
        for natural, candidates in collision_candidates.items():
            if len(candidates) > 1:
                winner = winners[natural]
                same = len({str(x["numeric_value"]) for x in candidates}) == 1
                report["metric_collisions"].append({
                    "natural_key": {"subject_type": natural[0], "subject_id": natural[1], "metric_key": natural[2], "observed_at": natural[3], "snapshot_id": natural[4]},
                    "same_value": same,
                    "candidates": [{"observation_id": x["observation_id"], "numeric_value": x["numeric_value"],
                                    "winner": x is winner} for x in candidates],
                })
        report["metric_fact_count"] = sum(len(x) for x in collision_candidates.values())
        report["metric_unique_count"] = len(winners)
        report["metric_collision_group_count"] = len(report["metric_collisions"])
        report["metric_collision_extra_count"] = sum(max(0, len(x) - 1) for x in collision_candidates.values())
        report["metric_collision_same_value_count"] = sum(1 for x in report["metric_collisions"] if x["same_value"])
        report["metric_collision_value_diff_count"] = sum(1 for x in report["metric_collisions"] if not x["same_value"])

        def write(con: sqlite3.Connection) -> None:
            for fact in winners.values():
                self._write_metric_fact(con, fact)
        self._flush_stream_batch(self.settings, self.settings.lock_path, write)

    def _stream_article_projections(
        self, article_rows: list[dict[str, Any]], monitor: dict[str, Any],
        manifest_id: str, now: str,
    ) -> None:
        # Only the compact source rows needed by the compatibility projection
        # are retained.  The 39k observation rows and 174k term rows are not.
        snapshots = list(self._stream_rows("snapshots"))
        hits = list(self._stream_rows("hits"))
        records = {
            "monitor": monitor, "accounts": monitor.get("accounts") or [],
            "keywords": monitor.get("keywords") or [], "articles": article_rows,
            "snapshots": snapshots, "hits": hits, "keyword_read_deltas": [],
        }
        _, legacy_keywords, _ = _legacy_projection_payload(records)
        existing_ids: set[str] = set()
        with connect(self.settings, readonly=True) as con:
            existing_ids = {
                str(row[0]) for row in con.execute(
                    "SELECT subject_id FROM wechat_legacy_projections WHERE projection_kind='article_detail' AND source_manifest_id=?",
                    (manifest_id,),
                )
            }
        for offset in range(0, len(article_rows), _IMPORT_BATCH_SIZE):
            chunk = article_rows[offset:offset + _IMPORT_BATCH_SIZE]
            def write(con: sqlite3.Connection, chunk=chunk) -> None:
                for article in chunk:
                    aid = str(article.get("article_id") or "")
                    if aid and aid not in existing_ids:
                        self._upsert_projection(
                            con, "article_detail", aid,
                            _legacy_article_detail(article, records, legacy_keywords),
                            manifest_id, now,
                        )
            self._flush_stream_batch(self.settings, self.settings.lock_path, write)

    def _reconcile_summary(self, records: dict[str, Any], *, limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            hub = {
                "keywords": con.execute("SELECT COUNT(*) FROM keywords WHERE platform='wechat-search'").fetchone()[0],
                "contents": con.execute("SELECT COUNT(*) FROM contents WHERE content_id IN (SELECT content_id FROM content_identifiers WHERE namespace='wechat_article')").fetchone()[0],
                "snapshots": con.execute("SELECT COUNT(*) FROM search_snapshots WHERE platform='wechat-search'").fetchone()[0],
                "hits": con.execute("SELECT COUNT(*) FROM search_hits WHERE snapshot_id IN (SELECT snapshot_id FROM search_snapshots WHERE platform='wechat-search')").fetchone()[0],
                "metric_observations": con.execute(
                    """
                    SELECT COUNT(*) FROM metric_observations
                    WHERE metric_key LIKE 'wechat.article.%'
                       OR metric_key LIKE 'wechat.keyword.%'
                    """
                ).fetchone()[0],
            }
        metric_keys: set[tuple[str, str, str, str | None]] = set()
        for row in records.get("observations") or []:
            observed = _source_time(row.get("observed_at"))
            article_id = str(row.get("article_id") or "")
            snapshot_id = str(row.get("source_snapshot_id")) if row.get("source_snapshot_id") else None
            if not article_id or observed is None:
                continue
            for field, (metric_key, _) in ARTICLE_METRIC_KEYS.items():
                if _number(row.get(field)) is not None:
                    metric_keys.add(("content", article_id, metric_key, f"{observed}:{snapshot_id}"))
        for row in records.get("keyword_read_deltas") or []:
            keyword_id = str(row.get("keyword_id") or "")
            observed = _source_time(row.get("window_end"))
            if not keyword_id or observed is None:
                continue
            for field, (metric_key, _) in CANONICAL_METRIC_KEYS.items():
                if _number(row.get(field)) is not None:
                    metric_keys.add(("keyword", keyword_id, metric_key, observed))
        source = {
            "keywords": len(records.get("keywords") or []),
            "contents": len({
                _safe_url(row.get("normalized_url") or row.get("raw_url"))
                or str(row.get("article_id") or _id("content", row.get("title")))
                for row in records.get("articles") or []
            }),
            "snapshots": len(records.get("snapshots") or []),
            "hits": len(records.get("hits") or []),
            "metric_observations": len(metric_keys),
        }
        difference = {key: hub[key] - source[key] for key in source}
        match = {key: source[key] == hub[key] for key in source}
        dimensions = {
            key: {"source": source[key], "hub": hub[key], "difference": difference[key], "match": match[key]}
            for key in source
        }
        if dry_run:
            status = "not_comparable"
            note = "dry-run 未写入 Hub，不能进行双向精确对账"
        elif limit is not None:
            status = "partial"
            note = "limit 导入只覆盖源选择集，不能与 Hub 全量闭包做双向精确对账"
        elif not all(match.values()):
            status = "mismatch"
            note = "完整同步后的 source 与 Hub 计数不一致，已成功写入的事实保留"
        else:
            status = "matched"
            note = "完整同步后的 source 与 Hub 各维度计数完全一致"
        return {
            "source": source,
            "hub": hub,
            "difference": difference,
            "match": match,
            "dimensions": dimensions,
            "status": status,
            "verified": status == "matched",
            "note": note,
        }

    def _asset_integrity(
        self,
        records: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        expected = sum(
            1 for row in records.get("articles") or []
            if row.get("content_file_path") or row.get("source_file_path")
        )
        connection_context = (
            nullcontext(connection)
            if connection is not None
            else connect(self.settings, readonly=True)
        )
        with connection_context as con:
            rows = con.execute(
                """
                SELECT p.old_article_id,p.relative_path,p.asset_path
                FROM wechat_article_paths p
                WHERE EXISTS (
                    SELECT 1
                    FROM content_identifiers i
                    WHERE i.namespace='wechat_article'
                      AND i.content_id=p.article_id
                )
                """
            ).fetchall()
            path_records = len(rows)
            readable = 0
            verified_paths = 0
            mismatches: list[dict[str, Any]] = []
            fallback_count = 0
            source_blobs: set[str] = set()
            source_by_article = {
                str(row.get("article_id")): row
                for row in records.get("articles") or []
                if row.get("article_id")
            }
            for row in rows:
                if not row["asset_path"]:
                    mismatches.append({
                        "old_article_id": row["old_article_id"],
                        "relative_path": row["relative_path"],
                        "reason": "asset_path_missing",
                    })
                    continue
                path = (self.settings.asset_store_path / str(row["asset_path"])).resolve()
                try:
                    path.relative_to(self.settings.asset_store_path.resolve())
                except ValueError:
                    mismatches.append({
                        "old_article_id": row["old_article_id"],
                        "relative_path": row["relative_path"],
                        "reason": "asset_path_escape",
                    })
                    continue
                if path.is_file() and not path.is_symlink() and path.stat().st_size >= 0:
                    try:
                        asset_bytes = path.read_bytes()
                        readable += 1
                        asset_sha256 = hashlib.sha256(asset_bytes).hexdigest()
                        source_row = source_by_article.get(str(row["old_article_id"]))
                        source_path = str(
                            (source_row or {}).get("content_file_path")
                            or (source_row or {}).get("source_file_path")
                            or row["relative_path"]
                        )
                        try:
                            _, actual_source = self.adapter.read_markdown_with_source(source_path)
                            if actual_source.startswith("code-snapshot/"):
                                fallback_count += 1
                            source_bytes = self.adapter.read_markdown(source_path).encode("utf-8")
                            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
                            source_blobs.add(source_sha256)
                            if asset_sha256 == source_sha256:
                                verified_paths += 1
                            else:
                                mismatches.append({
                                    "old_article_id": row["old_article_id"],
                                    "relative_path": source_path,
                                    "asset_path": row["asset_path"],
                                    "reason": "sha256_mismatch",
                                    "source_sha256": source_sha256,
                                    "asset_sha256": asset_sha256,
                                })
                        except WechatSourceError as exc:
                            mismatches.append({
                                "old_article_id": row["old_article_id"],
                                "relative_path": source_path,
                                "asset_path": row["asset_path"],
                                "reason": exc.kind,
                            })
                    except OSError:
                        mismatches.append({
                            "old_article_id": row["old_article_id"],
                            "relative_path": row["relative_path"],
                            "reason": "asset_unreadable",
                        })
                else:
                    mismatches.append({
                        "old_article_id": row["old_article_id"],
                        "relative_path": row["relative_path"],
                        "reason": "asset_missing_or_symlink",
                    })
            asset_blob_count = sum(
                1 for path in (self.settings.asset_store_path / "wechat").glob("*.md")
                if path.is_file() and not path.is_symlink()
            )
        missing = max(0, expected - readable)
        return {
            "expected_path_records": expected,
            "path_records": path_records,
            "readable_assets": readable,
            "sha256_verified_paths": verified_paths,
            "sha256_mismatch_count": len(mismatches),
            "sha256_mismatches": mismatches[:100],
            "fallback_code_snapshot_count": fallback_count,
            "source_unique_blob_count": len(source_blobs),
            "missing_path_asset_count": missing,
            "source_no_content_path": sum(
                1 for row in records.get("articles") or []
                if not (row.get("content_file_path") or row.get("source_file_path"))
            ),
            "asset_blob_count": asset_blob_count,
            "verified": (
                expected == path_records == readable
                and missing == 0
                and verified_paths == path_records
                and not mismatches
            ),
        }

    @staticmethod
    def _counts(records: dict[str, Any]) -> dict[str, int]:
        return {"keywords": len(records["keywords"]), "creators": len(records["accounts"]), "contents": len(records["articles"]), "search_snapshots": len(records["snapshots"]), "search_hits": len(records["hits"]), "snapshot_terms": len(records["terms"]), "metric_observations": len(records["observations"]), "keyword_read_deltas": len(records.get("keyword_read_deltas") or [])}

    def _write(self, con: sqlite3.Connection, records: dict[str, Any], batch_id: str, report: dict[str, Any], now: str) -> None:
        records_seen = sum(len(v) for v in records.values() if isinstance(v, list))
        entries = [
            {"relative_path": item["path"], "content_hash": item.get("sha256"), "size_bytes": item.get("size")}
            for item in report.get("manifest", {}).values()
            if isinstance(item, dict) and item.get("path")
        ]
        manifest_id = manifest_id_for("wechat-search", {"source_kind": "normalized+markdown"}, entries)
        write_manifest(
            con,
            manifest_id=manifest_id,
            system_key="wechat-search",
            source_kind="normalized+markdown",
            root_fingerprint=hashlib.sha256(
                f"wechat-search:{self.adapter.root.resolve()}".encode()
            ).hexdigest(),
            entries=entries,
            captured_at=now,
            payload={"source_root": "configured/wechat-search", "batch_id": batch_id},
        )
        report["manifest_id"] = manifest_id
        legacy_full, legacy_keywords, legacy_accounts = _legacy_projection_payload(
            records, read_delta_source=self._compat_read_delta_source()
        )
        projection_rows = [
            ("full", "", legacy_full),
            (
                "bootstrap", "",
                _projection_bootstrap_payload(
                    legacy_full, legacy_keywords, legacy_accounts
                ),
            ),
        ]
        manage_groups = []
        manage_total = manage_ranked = 0
        for group in sorted(
            [
                row for row in records.get("runtime", {}).get("keyword_groups", [])
                if row.get("archived_at") is None
            ],
            key=lambda x: (int(x.get("display_order") or x.get("order") or 0), str(x.get("group_id") or "")),
        ):
            group_keywords = []
            for item in records.get("runtime", {}).get("keyword_registry", []):
                if not _keyword_manage_visible(item):
                    continue
                if str(item.get("group_id") or "") != str(group.get("group_id") or ""):
                    continue
                stat = legacy_keywords.get(str(item.get("keyword_id")), {})
                today_best = stat.get("today_best")
                group_keywords.append({
                    "keyword_id": item.get("keyword_id"),
                    "keyword_text": item.get("keyword_text", ""),
                    "note": item.get("note") or "",
                    "batch_default_selected": bool(item.get("batch_default_selected", True)),
                    "refresh_frequency_days": int(item.get("refresh_frequency_days") or 1),
                    "effective_refresh_interval_hours": int(item.get("refresh_frequency_days") or 1) * 24,
                    "refresh_frequency_source": item.get("refresh_frequency_source") or "auto",
                    "refresh_policy_reason": item.get("refresh_policy_reason") or "",
                    "last_refresh_at": item.get("last_refresh_at"),
                    "last_refresh_attempt_at": item.get("last_refresh_attempt_at"),
                    "last_refresh_status": item.get("last_refresh_status"),
                    "next_refresh_at": item.get("next_refresh_at"),
                    "refresh_age_days": item.get("refresh_age_days"),
                    "is_refresh_due": bool(item.get("is_refresh_due", True)),
                    "commercial_value_score": int(item.get("commercial_value_score") or 5),
                    "commercial_value_source": item.get("commercial_value_source") or "auto",
                    "commercial_value_reason": item.get("commercial_value_reason") or "",
                    "lifecycle_stage": item.get("lifecycle_stage") or "established",
                    "observation_started_at": item.get("observation_started_at"),
                    "observation_deadline_at": item.get("observation_deadline_at"),
                    "discovery_candidate_id": item.get("discovery_candidate_id"),
                    "auto_archive_locked": bool(item.get("auto_archive_locked")),
                    "archive_reason_code": item.get("archive_reason_code"),
                    "archive_reason_detail": item.get("archive_reason_detail"),
                    "today_best": today_best,
                    "coverage_days": stat.get("coverage_days", 0),
                    "tracked_accounts": stat.get("tracked_accounts", 0),
                    "article_count": stat.get("article_count", 0),
                    "seo_status": "ranked" if today_best else "not_ranked",
                })
            ranked = sum(1 for x in group_keywords if x.get("today_best"))
            manage_total += len(group_keywords)
            manage_ranked += ranked
            manage_groups.append({
                "group_id": group.get("group_id"), "label": group.get("label"),
                "order": group.get("display_order", group.get("order", 0)),
                "keywords": group_keywords, "total": len(group_keywords),
                "ranked_count": ranked, "not_ranked_count": len(group_keywords) - ranked,
            })
        projection_rows.append(("keyword_manage", "", {
            "groups": manage_groups, "total": manage_total, "ranked_total": manage_ranked,
            "not_ranked_total": manage_total - manage_ranked,
            "updated_at": (records.get("runtime", {}).get("keyword_registry") or [{}])[0].get("updated_at"),
        }))
        projection_rows.extend(("keyword", key, value) for key, value in legacy_keywords.items())
        projection_rows.extend(("account", key, value) for key, value in legacy_accounts.items())
        projection_rows.extend(
            ("article_detail", str(article.get("article_id")), _legacy_article_detail(article, records, legacy_keywords))
            for article in records.get("articles") or [] if article.get("article_id")
        )
        projection_rows.extend(
            (
                "runtime",
                str(row.get("job_id") or row.get("id") or row.get("batch_id") or row.get("_source_file") or index),
                {"runtime_subtype": "single_job", **row},
            )
            for index, row in enumerate(records.get("runtime", {}).get("refresh_jobs") or [])
        )
        projection_rows.extend(
            (
                "runtime",
                str(row.get("batch_id") or row.get("id") or row.get("_source_file") or index),
                {"runtime_subtype": "batch", **row},
            )
            for index, row in enumerate(records.get("runtime", {}).get("batch_runs") or [])
        )
        if records.get("runtime", {}).get("scheduler"):
            projection_rows.append(("runtime", "scheduler", {"runtime_subtype": "scheduler", **records["runtime"]["scheduler"][0]}))
        if records.get("runtime", {}).get("keyword_refresh_ledger"):
            projection_rows.append(("runtime", "ledger", {"items": records["runtime"]["keyword_refresh_ledger"]}))
        for kind, subject_id, payload in projection_rows:
            projection_hash = _projection_hash(payload)
            con.execute(
                """
                INSERT INTO wechat_legacy_projections(
                    projection_id,projection_kind,subject_id,payload_json,
                    source_hash,source_manifest_id,source_ref,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(projection_kind,subject_id,source_hash) DO UPDATE SET
                    payload_json=excluded.payload_json,source_manifest_id=excluded.source_manifest_id,
                    source_ref=excluded.source_ref,updated_at=excluded.updated_at
                """,
                (
                    _id("wechat-projection", f"{kind}:{subject_id}:{projection_hash}"),
                    kind, subject_id, _projection_json(payload), projection_hash,
                    manifest_id, manifest_ref("wechat-search", manifest_id, "normalized/monitor-data.json"), now,
                ),
            )
        con.execute("INSERT INTO ingestion_batches(batch_id,adapter_key,source_scope,status,started_at,source_ref,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status='running',started_at=excluded.started_at,payload_json=excluded.payload_json", (batch_id, "wechat-search", "history", "running", now, manifest_ref("wechat-search", manifest_id), _json(report)))
        ids: dict[str, str] = {}
        rejected_article_ids: set[str] = set()
        accepted_rows: dict[str, set[str]] = {}

        def accept(kind: str, key: Any) -> None:
            accepted_rows.setdefault(kind, set()).add(str(key))
        for row in records["keywords"]:
            kid, keyword = str(row.get("keyword_id") or _id("kw", row.get("keyword"))), str(row.get("keyword") or "").strip()
            if not keyword: report["rejected"].append({"kind": "keyword", "row": row, "reason": "missing keyword"}); continue
            con.execute("INSERT INTO keywords(keyword_id,platform,keyword,status,topic,keyword_bucket,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id) DO UPDATE SET keyword=excluded.keyword,status=excluded.status,topic=excluded.topic,keyword_bucket=excluded.keyword_bucket,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (kid,"wechat-search",keyword,_legacy_keyword_status(row),row.get("topic") or keyword,row.get("keyword_bucket") or row.get("bucket"),_source_time(row.get("first_seen_at")) or now,_source_time(row.get("updated_at")) or now,_json(row)))
            accept("keywords", kid)
        if report["full_sync"]:
            active_source_ids = {str(row.get("keyword_id")) for row in records["keywords"] if row.get("keyword_id")}
            con.execute(
                "UPDATE keywords SET status='archived',updated_at=? WHERE platform='wechat-search' AND keyword_id NOT IN ({})".format(
                    ",".join("?" for _ in active_source_ids) or "''"
                ),
                (now, *sorted(active_source_ids)),
            )
        for row in records["accounts"]:
            aid = str(row.get("account_id") or _id("creator", row.get("canonical_name")))
            con.execute("INSERT INTO creators(creator_id,canonical_name,platform,external_id,profile_url,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(creator_id) DO UPDATE SET canonical_name=excluded.canonical_name,updated_at=excluded.updated_at,payload_json=excluded.payload_json", (aid,row.get("canonical_name"),"wechat-search",aid,None,_source_time(row.get("first_seen_at")) or now,_source_time(row.get("last_seen_at")) or now,_json(row)))
            accept("accounts", aid)
        for row in records["articles"]:
            source_id = str(row.get("article_id") or _id("content", row.get("title")))
            raw_url = row.get("normalized_url") or row.get("raw_url")
            url = _safe_url(raw_url)
            if raw_url and _is_placeholder_url(raw_url):
                report["placeholder_count"] += 1
                if len(report["placeholder_samples"]) < 10:
                    report["placeholder_samples"].append({"source_id": source_id, "value": raw_url})
            elif raw_url and url is None:
                report["rejected"].append({"kind": "url", "source_id": source_id, "value": raw_url, "reason": "invalid_url"})
            cid, is_owned = self._upsert_wechat_article(
                con, row=row, source_id=source_id, now=now, report=report
            )
            if cid is None:
                rejected_article_ids.add(source_id)
                continue
            ids[source_id] = cid
            accept("articles", source_id)
            old_path = row.get("content_file_path") or row.get("source_file_path")
            if old_path:
                self._import_article_markdown(
                    con,
                    article_id=cid,
                    old_article_id=source_id,
                    original_relative=str(old_path),
                    manifest_id=manifest_id,
                    created_at=now,
                    update_content_fields=is_owned,
                )
        snapshot_features: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for term in records["terms"]:
            bucket = snapshot_features.setdefault(str(term.get("snapshot_id")), {"suggestions": [], "related": []})
            target = "suggestions" if str(term.get("term_type") or "").lower() in {"suggestion", "suggestions"} else "related"
            bucket[target].append({"term": term.get("term_text"), "position": term.get("position")})
        snapshot_map: dict[str, str] = {}
        valid_snapshots: set[str] = set()
        for row in records["snapshots"]:
            sid = str(row.get("snapshot_id")); kid = row.get("keyword_id"); keyword = next((x.get("keyword") for x in records["keywords"] if x.get("keyword_id") == kid), str(kid or "")); captured = _source_time(row.get("captured_at"))
            result_count = _number(row.get("result_count"))
            if not sid or not captured: report["rejected"].append({"kind": "snapshot", "row": row, "reason": "invalid snapshot_id/captured_at"}); continue
            if kid: con.execute("INSERT OR IGNORE INTO keywords(keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)", (kid,"wechat-search",keyword or str(kid),"active",captured,captured,_json({"source":"snapshot_reference"})))
            if result_count is None and row.get("result_count") is not None:
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "invalid_result_count"})
            elif isinstance(result_count, float) and not result_count.is_integer():
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "fractional_result_count"})
                result_count = None
            elif isinstance(result_count, (int, float)) and result_count < 0:
                report["rejected"].append({"kind": "snapshot", "row": row, "reason": "negative_result_count"})
                result_count = None
            existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("wechat-search", keyword, captured)).fetchone()
            actual_sid = str(existing[0]) if existing else sid
            snapshot_map[sid] = actual_sid
            valid_snapshots.add(actual_sid)
            features = snapshot_features.get(sid, {"suggestions": [], "related": []})
            raw_source = str(row.get("source_file_path") or "").replace("\\", "/").lstrip("./")
            snapshot_ref = manifest_ref("wechat-search", manifest_id, raw_source) if raw_source else manifest_ref("wechat-search", manifest_id)
            con.execute("INSERT INTO search_snapshots(snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET keyword=excluded.keyword,keyword_id=excluded.keyword_id,captured_at=excluded.captured_at,result_count=excluded.result_count,features_json=excluded.features_json,source_ref=excluded.source_ref,payload_json=excluded.payload_json", (actual_sid,"wechat-search",keyword,kid,captured,row.get("trigger_type"),result_count,_json(features),snapshot_ref,_json({**row,"source_timezone":row.get("timezone") or "Asia/Shanghai"})))
            accept("snapshots", sid)
        for row in records["terms"]:
            if row.get("snapshot_id") in snapshot_map:
                accept("terms", row.get("term_id") or f"{row.get('snapshot_id')}:{row.get('position')}")
        for row in records["hits"]:
            source_sid, rank, hid = row.get("snapshot_id"), _number(row.get("rank")), str(row.get("hit_id") or _id("hit", f"{row.get('snapshot_id')}:{row.get('rank')}"))
            sid = snapshot_map.get(str(source_sid))
            if sid not in valid_snapshots or not isinstance(rank, (int, float)) or int(rank) <= 0 or int(rank) != rank:
                report["rejected"].append({"kind":"hit","row":row,"reason":"invalid snapshot/rank"}); continue
            if str(row.get("article_id") or "") in rejected_article_ids:
                continue
            mapped = ids.get(str(row.get("article_id")))
            con.execute("DELETE FROM search_hits WHERE snapshot_id=? AND rank=? AND hit_id<>?", (sid,int(rank),hid))
            con.execute("INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(hit_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,rank=excluded.rank,content_id=excluded.content_id,title_raw=excluded.title_raw,url_raw=excluded.url_raw,creator_name_raw=excluded.creator_name_raw,payload_json=excluded.payload_json", (hid,sid,int(rank),mapped,row.get("title_raw"),row.get("url_raw"),row.get("account_name_raw"),_json(row)))
            accept("hits", hid)
            if mapped:
                snapshot_time = con.execute("SELECT captured_at FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO content_discoveries(discovery_id,content_id,discovery_system,discovery_channel,discovered_at,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?)", (_id("discovery", f"{mapped}:{sid}"),mapped,"wechat-search","keyword-rank",snapshot_time,sid,row.get("url_raw"),_json(row)))
        facts = self._prepare_metric_facts(con, records, ids, snapshot_map, report)
        for fact in facts:
            self._write_metric_fact(con, fact)
            accept("observations", fact["source_observation_id"])
        for fact in self._prepare_keyword_delta_facts(records, report):
            self._write_metric_fact(con, fact)
            accept("keyword_read_deltas", fact["source_observation_id"])
        self._write_runtime_records(con, records.get("runtime") or {}, manifest_id, now)
        records_failed = len(report["rejected"])
        records_written = sum(len(values) for values in accepted_rows.values())
        con.execute(
            "UPDATE ingestion_batches SET status='running',records_seen=?,records_written=?,records_failed=?,error_json=?,payload_json=? WHERE batch_id=?",
            (records_seen, records_written, records_failed, _json(report["rejected"]),
             _json({**report, "accepted_rows": {key: len(value) for key, value in accepted_rows.items()},
                    "count_semantics": "records_seen=source rows; records_failed=rejected facts; records_written=accepted source rows by entity"}), batch_id),
        )
        con.execute(
            """
            INSERT INTO ingestion_checkpoints(
                adapter_key,checkpoint_key,cursor_value,source_hash,last_success_at,batch_id,payload_json
            ) VALUES(?,?,NULL,NULL,NULL,NULL,?)
            ON CONFLICT(adapter_key,checkpoint_key) DO UPDATE SET
                payload_json=excluded.payload_json
            """,
            ("wechat-search", "normalized", _json(report)),
        )


    @staticmethod
    def _write_runtime_records(
        con: sqlite3.Connection, runtime: dict[str, list[dict[str, Any]]],
        manifest_id: str, now: str,
    ) -> None:
        for row in runtime.get("keyword_groups", []):
            gid = str(row.get("group_id") or "")
            label = str(row.get("label") or "").strip()
            if not gid or not label:
                continue
            con.execute(
                """
                INSERT INTO search_keyword_groups(
                    group_id,system_key,platform,group_name,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                    group_name=excluded.group_name,sort_order=excluded.sort_order,
                    updated_at=excluded.updated_at
                """,
                (
                    gid, "wechat-search", "wechat-search", label,
                    int(row.get("display_order") or 0),
                    row.get("created_at") or now, row.get("updated_at") or now,
                ),
            )
        for row in runtime.get("keyword_registry", []):
            kid = str(row.get("keyword_id") or "")
            if not kid:
                continue
            if not con.execute("SELECT 1 FROM keywords WHERE keyword_id=?", (kid,)).fetchone():
                text = str(row.get("keyword_text") or kid).strip()
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        kid, "wechat-search", text,
                        "archived" if row.get("status") == "archived" else "active",
                        row.get("first_seen_at") or now, row.get("updated_at") or now,
                        _json({"source": "keyword_registry", **row}),
                    ),
                )
            con.execute(
                """
                INSERT INTO search_keyword_settings(
                    setting_id,system_key,platform,keyword_id,group_id,pinned,
                    refresh_strategy,refresh_interval_minutes,commercial_value,note,
                    archived_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(setting_id) DO UPDATE SET
                    group_id=excluded.group_id,pinned=excluded.pinned,
                    refresh_interval_minutes=excluded.refresh_interval_minutes,
                    commercial_value=excluded.commercial_value,note=excluded.note,
                    archived_at=excluded.archived_at,updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    f"wechat-search:{kid}", "wechat-search", "wechat-search", kid,
                    row.get("group_id"), int(row.get("is_pinned") or 0),
                    "disabled" if row.get("status") == "archived" else "scheduled" if row.get("refresh_frequency_days") else "manual",
                    row.get("refresh_frequency_days"),
                    row.get("commercial_value_score"), row.get("note") or "",
                    row.get("archived_at"), row.get("updated_at") or now, _json(row),
                ),
            )
        for table, target, columns in (
            ("keyword_discovery_probes", "wechat_discovery_probes", ("probe_id", "probe_text", "status")),
            ("keyword_discovery_candidates", "wechat_discovery_candidates", ("candidate_id", "candidate_text", "status")),
        ):
            for row in runtime.get(table, []):
                key = str(row.get(columns[0]) or "")
                text = str(row.get(columns[1]) or "").strip()
                if not key or not text:
                    continue
                con.execute(
                    f"INSERT INTO {target}({columns[0]},{columns[1]},status,payload_json,source_ref,updated_at) VALUES(?,?,?,?,?,?) "
                    f"ON CONFLICT({columns[0]}) DO UPDATE SET {columns[1]}=excluded.{columns[1]},status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                    (key, text, row.get(columns[2]) or "", _json(row), manifest_ref("wechat-search", manifest_id), row.get("updated_at") or now),
                )
        for row in runtime.get("keyword_discovery_evidence", []):
            eid = str(row.get("evidence_id") or "")
            cid = str(row.get("candidate_id") or "")
            snapshot_id = row.get("snapshot_id")
            if snapshot_id and not con.execute("SELECT 1 FROM search_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone():
                snapshot_id = None
            if not eid or not cid or not con.execute("SELECT 1 FROM wechat_discovery_candidates WHERE candidate_id=?", (cid,)).fetchone():
                continue
            con.execute(
                """
                INSERT INTO wechat_discovery_evidence(
                    evidence_id,candidate_id,probe_id,snapshot_id,source_article_id,
                    evidence_date,payload_json,source_ref,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    candidate_id=excluded.candidate_id,probe_id=excluded.probe_id,
                    snapshot_id=excluded.snapshot_id,source_article_id=excluded.source_article_id,
                    evidence_date=excluded.evidence_date,payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    eid, cid, row.get("probe_id"), snapshot_id,
                    row.get("source_article_id"), row.get("evidence_date"), _json(row),
                    manifest_ref("wechat-search", manifest_id), row.get("created_at") or now,
                ),
            )
        scheduler = (runtime.get("scheduler") or [{}])[0]
        if isinstance(scheduler, dict):
            con.execute(
                """
                INSERT INTO search_scheduler_state(
                    system_key,platform,enabled,next_run_at,last_run_at,
                    updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(system_key,platform) DO UPDATE SET
                    enabled=excluded.enabled,next_run_at=excluded.next_run_at,
                    last_run_at=excluded.last_run_at,updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    "wechat-search", "wechat-search",
                    int(bool(scheduler.get("enabled", False))),
                    scheduler.get("next_run_at"), scheduler.get("last_run_at"),
                    scheduler.get("updated_at") or now, _json(scheduler),
                ),
            )
        for row in runtime.get("refresh_jobs", []):
            job_id = str(row.get("job_id") or row.get("id") or "")
            if not job_id:
                continue
            status = str(row.get("status") or "failed")
            if status not in {"queued", "running", "succeeded", "partial_failed", "failed", "cancelled", "blocked"}:
                status = "failed"
            con.execute(
                """
                INSERT INTO search_refresh_jobs(
                    refresh_job_id,system_key,platform,trigger_type,status,
                    requested_count,succeeded_count,failed_count,checkpoint_json,
                    started_at,finished_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(refresh_job_id) DO UPDATE SET
                    status=excluded.status,requested_count=excluded.requested_count,
                    succeeded_count=excluded.succeeded_count,failed_count=excluded.failed_count,
                    checkpoint_json=excluded.checkpoint_json,started_at=excluded.started_at,
                    finished_at=excluded.finished_at,updated_at=excluded.updated_at
                """,
                (
                    job_id, "wechat-search", "wechat-search",
                    str(row.get("trigger_type") or "import") if str(row.get("trigger_type") or "import") in {"manual", "scheduled", "replay", "import"} else "import",
                    status, int(row.get("requested_count") or row.get("total") or 0),
                    int(row.get("succeeded_count") or row.get("completed") or 0),
                    int(row.get("failed_count") or row.get("failed") or 0),
                    _json(row.get("checkpoint") or row), row.get("started_at"),
                    row.get("finished_at"), row.get("created_at") or now, row.get("updated_at") or now,
                ),
            )

    @staticmethod
    def _time_precision(value: Any) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if re.fullmatch(r"\d{2}[-/]\d{2}[-/]\d{2}", raw):
            return "date_2digit_year"
        if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", raw):
            return "date"
        if "T" in raw or re.search(r"\d{2}:\d{2}", raw):
            return "datetime"
        return None

    @staticmethod
    def _candidate_sort_key(fact: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            fact["observation_id"],
            str(fact.get("source_file_path") or ""),
            str(fact["numeric_value"]),
            fact["canonical_row_json"],
        )

    def _metric_context(
        self,
        con: sqlite3.Connection,
        records: dict[str, Any],
        report: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        ids: dict[str, str] = {}
        planned: dict[str, dict[str, str]] = {
            "wechat_article": {},
            "wechat_url": {},
            "canonical_url": {},
            "content_id": {},
        }
        for row in records["articles"]:
            source_id = str(row.get("article_id") or _id("content", row.get("title")))
            resolution = self._resolve_wechat_article_identity(
                con, row=row, source_id=source_id, planned=planned
            )
            self._apply_article_identity_report(report, resolution)
            if not resolution["accepted"]:
                continue
            cid = str(resolution["content_id"])
            ids[source_id] = cid
            url = resolution.get("url")
            article_external_id = str(resolution["article_external_id"])
            planned["wechat_article"][article_external_id] = cid
            planned["content_id"][cid] = cid
            if url:
                planned["wechat_url"][str(url)] = cid
                if (
                    resolution["existing"] is None
                    or (
                        resolution["is_owned"]
                        and not resolution["identity_preserved"]
                    )
                ):
                    planned["canonical_url"][str(url)] = cid
        snapshot_map: dict[str, str] = {}
        source_snapshot_keys: dict[tuple[str, str, str], str] = {}
        for row in records["snapshots"]:
            sid = str(row.get("snapshot_id") or "")
            captured = _source_time(row.get("captured_at"))
            kid = row.get("keyword_id")
            keyword = next((x.get("keyword") for x in records["keywords"] if x.get("keyword_id") == kid), str(kid or ""))
            existing = con.execute("SELECT snapshot_id FROM search_snapshots WHERE platform=? AND keyword=? AND captured_at=?", ("wechat-search", keyword, captured)).fetchone() if captured else None
            mapped = con.execute("SELECT snapshot_id FROM search_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
            if existing:
                snapshot_map[sid] = str(existing[0])
            elif mapped:
                snapshot_map[sid] = str(mapped[0])
            elif sid and captured:
                snapshot_map[sid] = source_snapshot_keys.setdefault(("wechat-search", keyword, captured), sid)
            if sid and captured and (("wechat-search", keyword, captured) not in source_snapshot_keys):
                source_snapshot_keys[("wechat-search", keyword, captured)] = snapshot_map[sid]
        return ids, snapshot_map

    def _prepare_metric_facts(self, con: sqlite3.Connection, records: dict[str, Any], ids: dict[str, str], snapshot_map: dict[str, str], report: dict[str, Any], *, planned_snapshot_ids: set[str] | None = None) -> list[dict[str, Any]]:
        planned_snapshot_ids = planned_snapshot_ids or set()
        groups: dict[tuple[str, str, str, str, str | None], list[dict[str, Any]]] = {}
        labels = tuple((key, label) for key, (_, label) in ARTICLE_METRIC_KEYS.items())
        for row in records["observations"]:
            cid = ids.get(str(row.get("article_id")))
            observed = _source_time(row.get("observed_at"))
            source_oid = str(row.get("observation_id") or "")
            snapshot_id = snapshot_map.get(str(row.get("source_snapshot_id"))) if row.get("source_snapshot_id") else None
            if snapshot_id and snapshot_id not in planned_snapshot_ids and con.execute("SELECT 1 FROM search_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone() is None:
                snapshot_id = None
            for key, label in labels:
                if not cid or row.get(key) is None:
                    continue
                if observed is None:
                    report["rejected"].append({"kind": "metric", "source_observation_id": source_oid, "metric": key, "value": row.get(key), "reason": "invalid_observed_at"})
                    continue
                value = _number(row.get(key))
                if value is None:
                    report["rejected"].append({"kind": "metric", "source_observation_id": source_oid, "metric": key, "value": row.get(key), "reason": "invalid_numeric"})
                    continue
                metric_key, _ = ARTICLE_METRIC_KEYS[key]
                raw_observed_at = row.get("raw_observed_at") or row.get("observed_at")
                source_precision = row.get("observed_at_precision") or self._time_precision(raw_observed_at)
                source_origin = row.get("observed_at_source")
                source_file_path = row.get("source_file_path") or row.get("source_ref")
                report_source_file_path = str(source_file_path or "").replace("\\", "/").lstrip("./")
                manifest_id = str(report.get("manifest_id") or "")
                source_file_path = manifest_ref("wechat-search", manifest_id, report_source_file_path) if source_file_path else manifest_ref("wechat-search", manifest_id)
                canonical_row_json = _json(row)
                # 旧事实的 ID 形如 ``<source_oid>:wechat.<field>``。规范事实
                # 必须包含完整 canonical metric key，避免与旧行发生 PRIMARY
                # KEY 冲突并触发 _write_metric_fact 的 upsert 改写。
                oid = f"{source_oid}:{metric_key}" if source_oid else _id("observation", f"{cid}:{metric_key}:{observed}:{snapshot_id}:{canonical_row_json}")
                fact = {
                    "observation_id": oid, "source_observation_id": source_oid, "subject_id": cid,
                    "metric_key": metric_key, "metric_label": label, "observed_at": observed,
                    "numeric_value": value, "snapshot_id": snapshot_id, "source_ref": source_file_path,
                    "source_file_path": source_file_path, "raw_observed_at": raw_observed_at,
                    "observed_at_precision": source_precision, "observed_at_source": source_origin,
                    "canonical_row_json": canonical_row_json,
                    "row": {**row, "source_snapshot_id": snapshot_id},
                }
                groups.setdefault(("content", cid, metric_key, observed, snapshot_id), []).append(fact)
        # A reused source observation_id must not make two different natural keys
        # fight over one PRIMARY KEY. Keep the base id where possible, and add a
        # deterministic variant only for cross-natural-key reuse.
        by_base_id: dict[str, list[tuple[tuple[str, str, str, str, str | None], dict[str, Any]]]] = {}
        for natural_key, candidates in groups.items():
            for candidate in candidates:
                by_base_id.setdefault(candidate["observation_id"], []).append((natural_key, candidate))
        for base_id, entries in by_base_id.items():
            natural_keys = {entry[0] for entry in entries}
            if len(natural_keys) > 1:
                for natural_key, candidate in entries:
                    candidate["observation_id"] = f"{base_id}:{_id('variant', _json({'natural_key': natural_key, 'candidate': candidate['canonical_row_json']}))[-20:]}"
        winners: list[dict[str, Any]] = []
        for natural_key in sorted(groups, key=lambda x: tuple("" if v is None else str(v) for v in x)):
            candidates = sorted(groups[natural_key], key=self._candidate_sort_key)
            winner = candidates[0]
            winners.append(winner)
            if len(candidates) > 1:
                same_value = len({str(x["numeric_value"]) for x in candidates}) == 1
                report["metric_collisions"].append({
                    "natural_key": {"subject_type": natural_key[0], "subject_id": natural_key[1], "metric_key": natural_key[2], "observed_at": natural_key[3], "snapshot_id": natural_key[4]},
                    "same_value": same_value,
                    "candidates": [{
                        "observation_id": x["observation_id"],
                        "numeric_value": x["numeric_value"],
                        "source_file_path": str(x["source_file_path"]).split("/", 4)[-1],
                        "source_ref": str(x["source_ref"]).split("/", 4)[-1],
                        "raw_observed_at": x["raw_observed_at"],
                        "observed_at_precision": x["observed_at_precision"],
                        "observed_at_source": x["observed_at_source"],
                        "winner": x is winner,
                    } for x in candidates],
                })
        report["metric_fact_count"] = sum(len(v) for v in groups.values())
        report["metric_unique_count"] = len(groups)
        report["metric_collision_group_count"] = len(report["metric_collisions"])
        report["metric_collision_extra_count"] = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
        report["metric_collision_same_value_count"] = sum(1 for x in report["metric_collisions"] if x["same_value"])
        report["metric_collision_value_diff_count"] = sum(1 for x in report["metric_collisions"] if not x["same_value"])
        return winners

    def _prepare_keyword_delta_facts(self, records: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for row in records.get("keyword_read_deltas") or []:
            kid = str(row.get("keyword_id") or "")
            observed = _source_time(row.get("window_end"))
            if not kid or observed is None:
                report["rejected"].append({"kind": "keyword_read_delta", "row": row, "reason": "invalid_keyword_id/window_end"})
                continue
            common = {"keyword_id": kid, "keyword": row.get("keyword"), "status": row.get("status"), "window_start": row.get("window_start"), "window_end": row.get("window_end")}
            for source_field in ("read_delta_estimated", "read_delta_raw", "steady_read_median", "confidence_score", "trend_signal"):
                value = _number(row.get(source_field))
                if value is None:
                    continue
                metric_key, label = CANONICAL_METRIC_KEYS[source_field]
                facts.append({
                    "observation_id": f"{kid}:{observed}:{metric_key}",
                    "source_observation_id": f"{kid}:{observed}:{source_field}",
                    "subject_id": kid, "metric_key": metric_key, "metric_label": label,
                    "observed_at": observed, "numeric_value": value, "snapshot_id": None,
                    "source_ref": manifest_ref("wechat-search", str(report.get("manifest_id") or ""), "normalized/keyword_read_deltas.json"),
                    "source_file_path": "normalized/keyword_read_deltas.json",
                    "row": {**common, source_field: row.get(source_field)},
                })
            for point in row.get("daily_read_delta_points") or []:
                date = _source_time(point.get("date"))
                value = _number(point.get("read_delta"))
                if date is None or value is None:
                    report["rejected"].append({"kind": "keyword_read_delta", "row": point, "reason": "invalid_daily_point"})
                    continue
                metric_key, label = CANONICAL_METRIC_KEYS["daily_read_delta"]
                facts.append({
                    "observation_id": f"{kid}:{date}:{metric_key}",
                    "source_observation_id": f"{kid}:{date}:daily_read_delta",
                    "subject_id": kid, "metric_key": metric_key, "metric_label": label,
                    "observed_at": date, "numeric_value": value, "snapshot_id": None,
                    "source_ref": manifest_ref("wechat-search", str(report.get("manifest_id") or ""), "normalized/keyword_read_deltas.json"),
                    "source_file_path": "normalized/keyword_read_deltas.json",
                    "row": {**common, "daily_point": point},
                })
        report["keyword_delta_fact_count"] = len(facts)
        return facts

    @staticmethod
    def _write_metric_fact(con: sqlite3.Connection, fact: dict[str, Any]) -> None:
        subject_type = "keyword" if fact["metric_key"].startswith("wechat.keyword.") else "content"
        accumulation_mode = "delta" if fact["metric_key"].endswith(("read_delta", "read_delta_estimated", "read_delta_raw")) else "gauge"
        con.execute("INSERT OR IGNORE INTO metric_definitions(metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description) VALUES(?,?,?,?,?,?,?,?)", (fact["metric_key"], "wechat-search", subject_type, fact["metric_label"], "number", "count", accumulation_mode, "旧微信 normalized 事实；规范 key"))
        key = (fact["subject_id"], fact["metric_key"], fact["observed_at"], fact["snapshot_id"])
        con.execute("DELETE FROM metric_observations WHERE subject_type=? AND subject_id=? AND metric_key=? AND observed_at=? AND COALESCE(snapshot_id,'no-snapshot')=COALESCE(?,'no-snapshot') AND observation_id<>?", (subject_type, *key, fact["observation_id"]))
        existing = con.execute("SELECT subject_type,subject_id,metric_key,observed_at,snapshot_id FROM metric_observations WHERE observation_id=?", (fact["observation_id"],)).fetchone()
        if existing and tuple(existing) != (subject_type, *key):
            con.execute("DELETE FROM metric_observations WHERE observation_id=?", (fact["observation_id"],))
        con.execute("INSERT INTO metric_observations(observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,snapshot_id,source_ref,payload_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET subject_type=excluded.subject_type,subject_id=excluded.subject_id,metric_key=excluded.metric_key,observed_at=excluded.observed_at,numeric_value=excluded.numeric_value,snapshot_id=excluded.snapshot_id,source_ref=excluded.source_ref,payload_json=excluded.payload_json", (fact["observation_id"], subject_type, fact["subject_id"], fact["metric_key"], fact["observed_at"], fact["numeric_value"], fact["snapshot_id"], fact["source_ref"], _json({**fact["row"], "source_observation_id": fact["source_observation_id"]})))
