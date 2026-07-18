from __future__ import annotations

import asyncio
import json
import hashlib
import os
import posixpath
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    ValidationAppError,
)
from content_hub.features.xhs.legacy_projection import project_hub_payload
from content_hub.features.xhs.policy import (
    XHS_FREEZE_CODE,
    XHS_FREEZE_MESSAGE,
)
from content_hub.features.xhs.runtime import (
    XhsBatchAlreadyRunningError,
    XhsBatchRefreshService,
)
from content_hub.features.xhs.service import XhsService, _trusted_url
from content_hub.features.xhs.state import XhsStateService
from content_hub.services.wiki import WikiService
from content_hub.validation.timestamps import utc_now_iso


# 只代理两个原版业务岛屿已经使用的接口；禁止把工作台变成任意 URL 代理。
_ALLOWED_EXACT = frozenset({
    "monitor-data",
    "monitor-data/bootstrap",
    "creator-detail",
    "keyword-manage",
    "article-content",
    "article-covers",
    "article-hit-detail",
    "keyword-turnover",
    "article-cover-image",
    "note-detail",
    "articles",
    "articles/accounts",
    "account-aliases",
    "penalty-signals",
    "refresh-all",
    "refresh-all/status",
    "refresh-all/history",
    "refresh-all/cancel",
    "refresh-all/resume",
    "keyword-discovery",
    "settings",
    "accounts",
    "categories",
    "jobs",
    "data",
    "import-json",
})
_ALLOWED_PATTERNS = (
    re.compile(r"^monitor-data/(?:keyword|account)/[^/]+$"),
    re.compile(r"^keywords/[^/]+/refresh$"),
    re.compile(r"^keywords/[^/]+/(?:pin|unpin|topic|note|bucket)$"),
    re.compile(r"^keyword-manage/(?:groups|keywords)(?:/[^/]+)?(?:/(?:refresh-policy|commercial-value|auto-archive-lock))?$"),
    re.compile(r"^refresh-status/[^/]+$"),
    re.compile(r"^accounts/[^/]+$"),
    re.compile(r"^categories/[^/]+$"),
    re.compile(r"^jobs/[^/]+$"),
    re.compile(r"^runtime/[^/]+$"),
    re.compile(r"^auth/[^/]+$"),
    re.compile(r"^ai/[^/]+$"),
    re.compile(r"^agent/(?:manifest|daily-brief|metric-dictionary|evidence/[^/]+)$"),
    re.compile(r"^aidso/keyword-heat$"),
)


def _allowed(path: str) -> bool:
    normalized = path.lstrip("/")
    return normalized in _ALLOWED_EXACT or any(
        pattern.fullmatch(normalized) for pattern in _ALLOWED_PATTERNS
    )


def legacy_referer_kind(referer: str) -> str | None:
    """只接受工作台同源 Referer，避免外域伪造路径触发业务岛屿分流。"""
    raw = str(referer or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.port != 8799:
        return None
    path = parsed.path or ""
    if not path.startswith("/legacy/"):
        return None
    for kind in ("xhs", "mp", "geo", "wechat"):
        if path.startswith(f"/legacy/{kind}/"):
            return kind
    return None


def _upstream_url(base_url: str, path: str, query: str) -> str:
    encoded_path = urllib.parse.quote(
        path.lstrip("/"),
        safe="/:%@-._~!$&'()*+,;=",
    )
    url = f"{base_url.rstrip('/')}/api/{encoded_path}"
    return f"{url}?{query}" if query else url


_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "secret",
    "api_key",
    "apikey",
    "authorization",
}


def _redact_json_payload(payload: bytes, content_type: str) -> bytes:
    """旧控制台 settings/AI 响应不得把凭据回显到 iframe。

    仅对可解析 JSON 做递归字段脱敏；HTML、图片和二进制响应保持原样。
    """
    if "json" not in (content_type or "").lower():
        return payload
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload

    def redact(item: Any):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _audit_xhs_legacy_write(settings: Any, *, method: str, path: str) -> None:
    """记录被白名单代理拦截的旧小红书副作用请求。"""
    with writer_lock(settings.lock_path):
        with connect(settings) as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, actor_type, actor_id, action,
                    subject_type, subject_id, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit_{uuid.uuid4().hex[:16]}",
                    utc_now_iso(),
                    "legacy_proxy",
                    "legacy-xhs",
                    "xhs.legacy_write_blocked",
                    "legacy_endpoint",
                    path,
                    "blocked",
                    json.dumps(
                        {
                            "method": method,
                            "path": path,
                            "upstream_called": False,
                            "reason_code": "xhs.legacy_write_blocked",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )


def _audit_legacy_write_blocked(
    settings: Any,
    *,
    legacy_system: str,
    method: str,
    path: str,
) -> None:
    """所有原版 iframe 写请求必须止于工作台，不能绕过新运行层。"""
    with writer_lock(settings.lock_path):
        with connect(settings) as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, actor_type, actor_id, action,
                    subject_type, subject_id, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit_{uuid.uuid4().hex[:16]}",
                    utc_now_iso(),
                    "legacy_proxy",
                    f"legacy-{legacy_system}",
                    f"{legacy_system}.legacy_write_blocked",
                    "legacy_endpoint",
                    path,
                    "blocked",
                    json.dumps(
                        {
                            "method": method,
                            "path": path,
                            "upstream_called": False,
                            "reason_code": "legacy_write_requires_hub_command",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )


def _xhs_frozen_keyword_manage(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the small read shape consumed by the frozen XHS monitor page."""
    groups: dict[str, dict[str, Any]] = {}
    for item in payload.get("keywords", []):
        if not isinstance(item, dict):
            continue
        row_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        label = str(
            item.get("keyword_bucket")
            or item.get("topic")
            or row_payload.get("keyword_bucket")
            or row_payload.get("topic")
            or "未分组"
        ).strip() or "未分组"
        group = groups.setdefault(
            label,
            {
                "group_id": f"frozen-{hashlib.sha256(label.encode('utf-8')).hexdigest()[:16]}",
                "label": label,
                "keywords": [],
            },
        )
        group["keywords"].append(
            {
                "keyword_id": str(row_payload.get("source_keyword_id") or item.get("keyword_id") or ""),
                "keyword_text": str(item.get("keyword") or row_payload.get("keyword_text") or ""),
                "is_active": item.get("status") == "active",
                "topic": item.get("topic") or row_payload.get("topic"),
                "keyword_bucket": item.get("keyword_bucket") or row_payload.get("keyword_bucket"),
            }
        )
    result = list(groups.values())
    return {"groups": result, "total": sum(len(group["keywords"]) for group in result)}


def _xhs_frozen_bootstrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add legacy field aliases without changing the Hub fact payload."""
    result = dict(payload)
    counts = result.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    for key in ("keywords", "accounts", "snapshots", "ranking_hits", "articles", "snapshot_terms"):
        value = result.get(key)
        counts.setdefault(key, len(value) if isinstance(value, list) else 0)
    result["counts"] = counts
    accounts = []
    for item in payload.get("accounts", []):
        row = dict(item)
        row_payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        row.setdefault("account_id", row.get("external_id") or row.get("creator_id"))
        row.setdefault("name", row.get("canonical_name") or row_payload.get("canonical_name") or "")
        accounts.append(row)
    result["accounts"] = accounts
    return result


def _xhs_frozen_monitor_data_path(settings: Any) -> Path:
    migration_root = Path(settings.project_root) / "data" / "migration" / "xhs"
    candidates = sorted(
        path / "payload" / "normalized" / "monitor-data.json"
        for path in migration_root.glob("freeze_*")
        if (path / "payload" / "normalized" / "monitor-data.json").is_file()
    )
    if candidates:
        return candidates[-1]
    return Path(settings.xhs_normalized_root) / "monitor-data.json"


@lru_cache(maxsize=2)
def _read_xhs_frozen_monitor_data(path: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("小红书冻结 monitor-data 顶层必须是对象")
    return payload


def _xhs_frozen_monitor_data(settings: Any) -> dict[str, Any] | None:
    path = _xhs_frozen_monitor_data_path(settings)
    try:
        stat = path.stat()
        return _read_xhs_frozen_monitor_data(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _xhs_frozen_keyword_payload(settings: Any, keyword_id: str) -> dict[str, Any] | None:
    payload = _xhs_frozen_monitor_data(settings)
    if not payload:
        return None
    keywords = payload.get("keywords")
    if not isinstance(keywords, list):
        return None
    for item in keywords:
        if not isinstance(item, dict):
            continue
        if str(item.get("keyword_id") or "") == keyword_id:
            return item
    return None


def _xhs_frozen_account_payload(settings: Any, account_id: str) -> dict[str, Any] | None:
    payload = _xhs_frozen_monitor_data(settings)
    if not payload:
        return None
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return None
    for item in accounts:
        if isinstance(item, dict) and str(item.get("account_id") or "") == account_id:
            return item
    return None


def _xhs_frozen_refresh_history(settings: Any) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    with connect(settings, readonly=True) as connection:
        jobs = connection.execute(
            """SELECT refresh_job_id, status, requested_count, succeeded_count,
                      failed_count, started_at, finished_at, created_at
               FROM search_refresh_jobs
               WHERE system_key='xhs-search'
               ORDER BY COALESCE(finished_at, created_at) DESC"""
        ).fetchall()
        for job in jobs:
            items = connection.execute(
                """SELECT i.keyword_id, i.status, i.error_json, k.keyword
                   FROM search_refresh_items i
                   LEFT JOIN keywords k ON k.keyword_id=i.keyword_id
                   WHERE i.refresh_job_id=?
                   ORDER BY i.ordinal""",
                (job["refresh_job_id"],),
            ).fetchall()
            failed_keywords = []
            failure_reasons = []
            for item in items:
                if item["status"] in {"succeeded", "done"}:
                    continue
                error = json.loads(item["error_json"] or "{}") if item["error_json"] else {}
                reason = str(error.get("reason_code") or error.get("message") or item["status"] or "失败")
                failed_keywords.append(
                    {
                        "keyword": item["keyword"] or item["keyword_id"],
                        "reason": reason,
                    }
                )
                if reason not in failure_reasons:
                    failure_reasons.append(reason)
            status = str(job["status"] or "")
            status = {
                "succeeded": "completed",
                "blocked": "failed",
            }.get(status, status)
            history.append(
                {
                    "batch_id": job["refresh_job_id"],
                    "status": status,
                    "total": int(job["requested_count"] or len(items)),
                    "success_count": int(job["succeeded_count"] or 0),
                    "failed_count": int(job["failed_count"] or len(failed_keywords)),
                    "started_at": job["started_at"] or job["created_at"],
                    "finished_at": job["finished_at"],
                    "source": "shadow",
                    "provider": "tikhub_xhs" if status == "completed" else "hub_policy",
                    "resumable": False,
                    "failed_keywords": failed_keywords,
                    "failure_reasons": failure_reasons,
                }
            )
    return history


def _xhs_refresh_service(request: Request) -> XhsBatchRefreshService:
    return XhsBatchRefreshService(
        request.app.state.settings,
        provider=getattr(request.app.state, "xhs_shadow_provider", None),
        actor_id=request.headers.get("X-Actor-ID", "user"),
    )


def _xhs_state_service(request: Request) -> XhsStateService:
    return XhsStateService(
        request.app.state.settings,
        actor_id=request.headers.get("X-Actor-ID", "user"),
    )


def _xhs_write_key(request: Request, payload: dict[str, Any], operation: str) -> str:
    explicit = (
        payload.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
    )
    if explicit:
        return str(explicit).strip()
    return f"xhs-{operation}-{uuid.uuid4().hex}"


def _xhs_schedule_batch(request: Request, batch_id: str, *, confirm: bool = False) -> None:
    tasks = getattr(request.app.state, "xhs_refresh_tasks", None)
    if tasks is None:
        tasks = {}
        request.app.state.xhs_refresh_tasks = tasks
    existing = tasks.get(batch_id)
    if existing is not None and not existing.done():
        return
    service = _xhs_refresh_service(request)
    task = asyncio.create_task(asyncio.to_thread(service.run_batch, batch_id, confirm=confirm))
    tasks[batch_id] = task

    def cleanup(done_task: asyncio.Task) -> None:
        del done_task
        tasks.pop(batch_id, None)

    task.add_done_callback(cleanup)


def _xhs_refresh_status_legacy(request: Request, job_id: str) -> Response:
    from content_hub.services.search_runtime import SearchRefreshRuntime

    payload = _xhs_refresh_service(request).status(job_id)
    if payload is None:
        runtime = SearchRefreshRuntime(
            request.app.state.settings,
            system_key="xhs-search",
            platform="xiaohongshu",
        ).status(job_id)
        if runtime is None:
            return JSONResponse(status_code=404, content={"error": "job not found"})
        payload = {
            "batch_id": job_id,
            "job_id": job_id,
            "status": runtime["status"],
            "is_active": runtime["status"] in {"queued", "running"},
            "is_finished": runtime["status"] not in {"queued", "running"},
            "total": runtime["requested_count"],
            "success_count": runtime["succeeded_count"],
            "failed_count": runtime["failed_count"],
            "processed_count": runtime["succeeded_count"] + runtime["failed_count"],
            "finished_at": runtime["finished_at"],
        }
    status = str(payload.get("status") or "")
    legacy_status = {
        "completed": "done",
        "succeeded": "done",
        "cancelled": "failed",
        "completed_with_failures": "failed",
    }.get(status, status)
    keyword = payload.get("current_keyword")
    if not keyword:
        keyword = next(iter(payload.get("completed_keywords") or []), None)
    return JSONResponse(
        content={
            "job_id": job_id,
            "refresh_job_id": job_id,
            "status": legacy_status,
            "keyword": keyword,
            "is_active": bool(payload.get("is_active")),
            "is_finished": bool(payload.get("is_finished")),
            "result": payload,
        }
    )


def _xhs_decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _xhs_article_row(settings: Any, note_id: str) -> Any:
    value = str(note_id or "").strip()
    if not value:
        return None
    candidates = [value]
    if value.startswith("xhs_tk_"):
        candidates.append(value.removeprefix("xhs_tk_"))
    else:
        candidates.append(f"xhs_tk_{value}")
    placeholders = ",".join("?" for _ in candidates)
    with connect(settings, readonly=True) as connection:
        return connection.execute(
            f"""SELECT c.*
                FROM contents c
                WHERE c.content_type='social_note'
                  AND (
                    c.content_id IN ({placeholders})
                    OR EXISTS (
                        SELECT 1 FROM content_identifiers i
                        WHERE i.content_id=c.content_id
                          AND i.namespace IN ('xiaohongshu_note','xiaohongshu_article')
                          AND i.external_id IN ({placeholders})
                    )
                  )
                ORDER BY c.updated_at DESC
                LIMIT 1""",
            (*candidates, *candidates),
        ).fetchone()


def _xhs_article_facts(row: Any) -> dict[str, Any]:
    payload = _xhs_decode_payload(row["payload_json"] if row is not None else "{}")
    nested = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    facts = dict(payload)
    facts.update(nested)
    return facts


def _xhs_account_facts(row: Any) -> dict[str, Any]:
    payload = _xhs_decode_payload(row["payload_json"] if row is not None else "{}")
    nested = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    facts = dict(payload)
    facts.update(nested)
    return facts


def _xhs_account_name(row: Any) -> str:
    facts = _xhs_account_facts(row)
    return str(
        (row["canonical_name"] if row is not None else None)
        or facts.get("canonical_name")
        or facts.get("name")
        or facts.get("nickname")
        or (row["external_id"] if row is not None else "")
        or ""
    ).strip()


def _xhs_account_avatar(row: Any) -> str:
    facts = _xhs_account_facts(row)
    candidates = [
        facts.get("headimg_url"),
        facts.get("avatar_url"),
        facts.get("creator_avatar"),
    ]
    images = facts.get("images")
    if isinstance(images, dict):
        candidates.append(images.get("large"))
    return str(next((value for value in candidates if value), "")).strip()


def _xhs_metric_values(
    settings: Any,
    content_id: str,
    facts: dict[str, Any],
    *,
    fallback_to_observations: bool = True,
) -> dict[str, Any]:
    values = {
        "liked_count": facts.get("liked_count"),
        "collected_count": facts.get("collected_count"),
        "comment_count": facts.get("comment_count"),
        "shared_count": facts.get("shared_count"),
        "read_count": facts.get("read_count"),
    }
    aliases = {
        "liked_count": ("like_count", "likes"),
        "collected_count": ("collect_count", "collects"),
        "comment_count": ("comments_count", "comments"),
        "shared_count": ("share_count", "shares"),
    }
    for target, names in aliases.items():
        if values[target] is not None:
            continue
        for name in names:
            if facts.get(name) is not None:
                values[target] = facts[name]
                break
    rows = []
    if fallback_to_observations and any(value is None for value in values.values()):
        with connect(settings, readonly=True) as connection:
            rows = connection.execute(
                """SELECT metric_key,numeric_value
                   FROM metric_observations
                   WHERE subject_type='content' AND subject_id=?
                     AND metric_key LIKE 'xhs.note.%'
                   ORDER BY observed_at DESC""",
                (content_id,),
            ).fetchall()
    metric_map = {
        "xhs.note.like": "liked_count",
        "xhs.note.collect": "collected_count",
        "xhs.note.comment": "comment_count",
        "xhs.note.share": "shared_count",
        "xhs.note.read": "read_count",
    }
    for row in rows:
        target = metric_map.get(str(row["metric_key"]))
        if target and values[target] is None:
            values[target] = row["numeric_value"]
    values["like_count"] = values["liked_count"]
    return values


def _xhs_legacy_article(
    settings: Any,
    row: Any,
    *,
    account: Any = None,
    hit_keywords: list[str] | None = None,
    hit_count: int = 0,
    on_rank_days: int = 0,
    account_score: Any = 0,
    metric_fallback: bool = True,
) -> dict[str, Any]:
    facts = _xhs_article_facts(row)
    metrics = _xhs_metric_values(
        settings,
        str(row["content_id"]),
        facts,
        fallback_to_observations=metric_fallback,
    )
    account_name = _xhs_account_name(account)
    account_avatar = _xhs_account_avatar(account)
    return {
        "article_id": str(row["content_id"]),
        "title": row["title"] or facts.get("title") or "",
        "url": row["canonical_url"] or facts.get("normalized_url") or facts.get("raw_url") or "",
        "account_id": str(
            (account["external_id"] if account is not None else None)
            or facts.get("account_id")
            or row["creator_id"]
            or ""
        ),
        "account_name": str(row["author_name"] or account_name or ""),
        "account_headimg": account_avatar,
        "liked_count": metrics["liked_count"],
        "collected_count": metrics["collected_count"],
        "comment_count": metrics["comment_count"],
        "shared_count": metrics["shared_count"],
        "read_count": metrics["read_count"],
        "like_count": metrics["like_count"],
        "work_type": facts.get("work_type") or "normal",
        "cover_url": facts.get("cover_url") or facts.get("cover"),
        "is_relevant": facts.get("is_relevant"),
        "relevance_score": facts.get("relevance_score"),
        "content_status": facts.get("content_status") or ("available" if facts.get("summary") else "missing"),
        "hit_count": int(hit_count),
        "hit_keywords": sorted(set(hit_keywords or [])),
        "on_rank_days": int(on_rank_days),
        "account_score": account_score or 0,
        "published_at": row["published_at"] or facts.get("published_at"),
        "content_file_path": None,
    }


def _xhs_article_list(request: Request) -> Response:
    settings = request.app.state.settings
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", "50"))
    except (TypeError, ValueError):
        page_size = 50
    if page_size < 1 or page_size > 200:
        page_size = 50
    sort = str(request.query_params.get("sort") or "reads").strip()
    try:
        time_range = int(request.query_params.get("time_range", "15"))
    except (TypeError, ValueError):
        time_range = 15
    try:
        min_hits = max(0, int(request.query_params.get("min_hits", "0")))
    except (TypeError, ValueError):
        min_hits = 0
    account_filter = str(request.query_params.get("account") or "").strip()
    search_filter = str(request.query_params.get("search") or "").strip().lower()

    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            """SELECT c.*,cr.external_id AS account_external_id,
                      cr.canonical_name AS account_canonical_name,
                      cr.payload_json AS account_payload_json
               FROM contents c
               LEFT JOIN creators cr ON cr.creator_id=c.creator_id
               WHERE c.content_type='social_note'
                 AND EXISTS (
                   SELECT 1
                   FROM search_hits h
                   JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                   WHERE h.content_id=c.content_id AND s.platform='xiaohongshu'
                 )
               ORDER BY c.published_at DESC,c.content_id"""
        ).fetchall()
        article_rows: list[dict[str, Any]] = []
        for row in rows:
            hit_rows = connection.execute(
                """SELECT s.keyword,s.captured_at
                   FROM search_hits h
                   JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                   WHERE h.content_id=? AND s.platform='xiaohongshu'
                   ORDER BY s.captured_at DESC,h.rank""",
                (row["content_id"],),
            ).fetchall()
            if not hit_rows:
                continue
            hit_keywords = sorted({str(item["keyword"] or "") for item in hit_rows if item["keyword"]})
            rank_days = {
                str(item["captured_at"] or "")[:10]
                for item in hit_rows
                if item["captured_at"]
            }
            account_row = None
            if row["account_external_id"]:
                account_row = {
                    "external_id": row["account_external_id"],
                    "canonical_name": row["account_canonical_name"],
                    "payload_json": row["account_payload_json"],
                }
            article = _xhs_legacy_article(
                settings,
                row,
                account=account_row,
                hit_keywords=hit_keywords,
                hit_count=len(hit_keywords),
                on_rank_days=len(rank_days),
                account_score=_xhs_account_facts(account_row).get("score", 0) if account_row else 0,
                metric_fallback=False,
            )
            if time_range > 0:
                published = _parse_xhs_time(article.get("published_at"))
                if published is None or (datetime.now(UTC) - published).total_seconds() > time_range * 86400:
                    continue
            if article["hit_count"] < min_hits:
                continue
            if account_filter and article["account_id"] != account_filter:
                continue
            if search_filter and search_filter not in str(article["title"] or "").lower():
                continue
            article_rows.append(article)

    if sort not in {
        "reads", "hitCount", "publishTime", "likes", "collects",
        "comments", "shared", "accountScore", "todayTop3", "onRankDays",
    }:
        sort = "reads"

    def numeric(value: Any) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def missing_first(value: Any) -> tuple[bool, float]:
        return value is None, -numeric(value)

    if sort == "reads":
        article_rows.sort(key=lambda item: missing_first(item["read_count"]))
    elif sort == "hitCount":
        article_rows.sort(key=lambda item: -item["hit_count"])
    elif sort == "publishTime":
        article_rows.sort(key=lambda item: _parse_xhs_time(item.get("published_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
    elif sort == "likes":
        article_rows.sort(key=lambda item: missing_first(item["liked_count"]))
    elif sort == "collects":
        article_rows.sort(key=lambda item: missing_first(item["collected_count"]))
    elif sort == "comments":
        article_rows.sort(key=lambda item: missing_first(item["comment_count"]))
    elif sort == "shared":
        article_rows.sort(key=lambda item: missing_first(item["shared_count"]))
    elif sort == "onRankDays":
        article_rows.sort(key=lambda item: (-item["on_rank_days"],) + missing_first(item["collected_count"]))
    elif sort == "accountScore":
        by_account: dict[str, list[dict[str, Any]]] = {}
        for item in article_rows:
            by_account.setdefault(item["account_id"], []).append(item)
        for values in by_account.values():
            values.sort(key=lambda item: missing_first(item["collected_count"]))
        article_rows = []
        for account_id in sorted(by_account, key=lambda key: -numeric(by_account[key][0]["account_score"])):
            article_rows.extend(by_account[account_id][:3])
    else:
        article_rows.sort(key=lambda item: _parse_xhs_time(item.get("published_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)

    total = len(article_rows)
    start = (page - 1) * page_size
    return JSONResponse(
        content={
            "articles": article_rows[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "hub_db",
        }
    )


def _parse_xhs_time(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _xhs_article_accounts(request: Request) -> Response:
    settings = request.app.state.settings
    with connect(settings, readonly=True) as connection:
        rows = connection.execute(
            """SELECT cr.creator_id,cr.external_id,cr.canonical_name,cr.payload_json,
                      COUNT(DISTINCT h.content_id) AS article_count
               FROM creators cr
               JOIN contents c ON c.creator_id=cr.creator_id AND c.content_type='social_note'
               JOIN search_hits h ON h.content_id=c.content_id
               JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id AND s.platform='xiaohongshu'
               WHERE cr.platform='xiaohongshu'
               GROUP BY cr.creator_id,cr.external_id,cr.canonical_name,cr.payload_json
               ORDER BY article_count DESC,cr.canonical_name,cr.external_id"""
        ).fetchall()
    accounts = []
    for row in rows:
        accounts.append(
            {
                "account_id": str(row["external_id"] or row["creator_id"] or ""),
                "name": _xhs_account_name(row),
                "headimg_url": _xhs_account_avatar(row),
                "article_count": int(row["article_count"] or 0),
            }
        )
    return JSONResponse(content={"accounts": accounts, "source": "hub_db"})


def _xhs_hit_detail(request: Request) -> Response:
    settings = request.app.state.settings
    article_id = str(request.query_params.get("article_id") or "").strip()
    raw_url = str(request.query_params.get("url") or "").strip()
    if not article_id and not raw_url:
        return JSONResponse(status_code=400, content={"error": "article_id or url is required"})
    with connect(settings, readonly=True) as connection:
        row = None
        if article_id:
            row = _xhs_article_row(settings, article_id)
        if row is None and raw_url:
            url = _trusted_url(raw_url)
            if url:
                row = connection.execute(
                    """SELECT c.*
                       FROM contents c
                       WHERE c.content_type='social_note' AND c.canonical_url=?
                       LIMIT 1""",
                    (url,),
                ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "小红书笔记不存在"})
        account = connection.execute(
            "SELECT * FROM creators WHERE creator_id=? AND platform='xiaohongshu'",
            (row["creator_id"],),
        ).fetchone()
        hits = connection.execute(
            """SELECT h.*,s.keyword,s.keyword_id,s.captured_at,s.trigger_type,
                      k.topic,k.keyword_bucket
               FROM search_hits h
               JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
               LEFT JOIN keywords k ON k.keyword_id=s.keyword_id
               WHERE h.content_id=? AND s.platform='xiaohongshu'
               ORDER BY s.captured_at,h.rank""",
            (row["content_id"],),
        ).fetchall()
        observations = connection.execute(
            """SELECT metric_key,observed_at,numeric_value
               FROM metric_observations
               WHERE subject_type='content' AND subject_id=?
                 AND metric_key LIKE 'xhs.note.%'
               ORDER BY observed_at""",
            (row["content_id"],),
        ).fetchall()

    article = _xhs_legacy_article(settings, row, account=account, hit_count=len(hits))
    article.update(
        {
            "article_id": str(row["content_id"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["updated_at"],
            "url": row["canonical_url"] or article["url"],
        }
    )
    groups: dict[str, dict[str, Any]] = {}
    timeline_events: list[dict[str, Any]] = []
    for hit in hits:
        group_key = str(hit["keyword_id"] or hit["keyword"] or "")
        group = groups.setdefault(
            group_key,
            {
                "keyword": str(hit["keyword"] or group_key),
                "keyword_id": hit["keyword_id"],
                "keyword_bucket": hit["keyword_bucket"] or "未分类",
                "topic": hit["topic"] or "未归类",
                "hits": [],
            },
        )
        captured = hit["captured_at"]
        hit_item = {
            "captured_at": captured,
            "captured_at_label": _xhs_time_label(captured),
            "batch_id": hit["snapshot_id"],
            "snapshot_id": hit["snapshot_id"],
            "rank": hit["rank"],
            "article_id": row["content_id"],
        }
        group["hits"].append(hit_item)
        timeline_events.append(
            {
                "label": _xhs_time_label(captured),
                "title": f"{group['keyword']} · 第 {hit['rank']} 名",
                "description": f"{group['keyword_bucket']} · {group['topic']}",
                "captured_at": captured,
            }
        )
    keyword_groups = []
    for group in groups.values():
        group["hit_count"] = len(group["hits"])
        keyword_groups.append(group)
    keyword_groups.sort(key=lambda item: item["keyword"])
    keyword_cloud = [
        {
            "keyword": group["keyword"],
            "hit_count": group["hit_count"],
            "best_rank": min((int(item["rank"]) for item in group["hits"] if item["rank"] is not None), default=None),
        }
        for group in keyword_groups
    ]
    keyword_cloud.sort(key=lambda item: (-item["hit_count"], item["best_rank"] or 999, item["keyword"]))
    timeline_events.sort(key=lambda item: item.get("captured_at") or "")
    metric_names = {
        "xhs.note.like": "liked_count",
        "xhs.note.collect": "collected_count",
        "xhs.note.comment": "comment_count",
        "xhs.note.share": "shared_count",
        "xhs.note.read": "read_count",
    }
    metric_points_by_time: dict[str, dict[str, Any]] = {}
    for observation in observations:
        captured = str(observation["observed_at"] or "")
        point = metric_points_by_time.setdefault(
            captured,
            {
                "captured_at": captured,
                "liked_count": None,
                "collected_count": None,
                "comment_count": None,
                "shared_count": None,
                "read_count": None,
            },
        )
        field = metric_names.get(str(observation["metric_key"]))
        if field:
            point[field] = observation["numeric_value"]
    return JSONResponse(
        content={
            "article": article,
            "account": {
                "account_id": str(account["external_id"] if account is not None else row["creator_id"] or ""),
                "name": _xhs_account_name(account),
                "headimg_url": _xhs_account_avatar(account),
            },
            "keyword_count": len(keyword_groups),
            "hit_count": len(hits),
            "keyword_groups": keyword_groups,
            "keyword_cloud": keyword_cloud,
            "metric_points": list(metric_points_by_time.values()),
            "timeline_events": timeline_events,
            "content_files": [],
            "url_profile": {
                "article_record_count": 1,
                "article_ids": [str(row["content_id"])],
                "title_variants": [str(row["title"] or article["title"] or "")],
            },
            "source_status": {"status": "healthy", "source": "hub_db"},
        }
    )


def _xhs_time_label(value: Any) -> str:
    parsed = _parse_xhs_time(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else str(value or "")


def _xhs_note_detail(request: Request) -> Response:
    note_id = request.query_params.get("note_id", "")
    row = _xhs_article_row(request.app.state.settings, note_id)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "小红书笔记不存在"})
    stored = _xhs_decode_payload(row["payload_json"])
    raw = (
        {**stored, **stored["raw"]}
        if isinstance(stored.get("raw"), dict)
        else stored
    )
    platform_payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    images = raw.get("images_list") or raw.get("images") or []
    images_list = []
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict):
                url = image.get("url_size_large") or image.get("url") or image.get("url_default")
                if url:
                    images_list.append({**image, "url": str(url)})
            elif image:
                images_list.append({"url": str(image)})
    metrics = {
        "liked_count": raw.get("liked_count"),
        "collected_count": raw.get("collected_count"),
        "comment_count": raw.get("comment_count"),
        "shared_count": raw.get("shared_count"),
    }
    return JSONResponse(
        content={
            "title": row["title"] or raw.get("title_raw") or raw.get("title") or "",
            "desc_full": raw.get("desc_full") or raw.get("desc") or raw.get("summary") or "",
            "creator_name": row["author_name"] or raw.get("creator_name_raw") or "",
            "platform_payload": platform_payload,
            "work_type": raw.get("work_type") or "normal",
            "url": row["canonical_url"] or raw.get("url_raw") or "",
            "published_at": row["published_at"] or raw.get("published_at"),
            **metrics,
            "images_list": images_list,
            "source_status": {"status": "healthy", "source": "hub_db"},
        }
    )


def _xhs_creator_detail(request: Request) -> Response:
    account_id = str(request.query_params.get("user_id") or "").strip()
    if not account_id:
        return JSONResponse(status_code=400, content={"error": "user_id is required"})
    payload = XhsService(request.app.state.settings)._hub_bootstrap() or {}
    projected = project_hub_payload(payload)
    account = next(
        (item for item in projected.get("accounts", []) if str(item.get("account_id") or "") == account_id),
        None,
    )
    if account is None:
        return JSONResponse(status_code=404, content={"error": "小红书博主不存在"})
    raw = account.get("payload") if isinstance(account.get("payload"), dict) else {}
    return JSONResponse(
        content={
            "fans": account.get("fans"),
            "description": account.get("description"),
            "avatar": account.get("headimg_url"),
            "total_works": account.get("total_works"),
            "likes": account.get("likes_total"),
            "collects": account.get("collects_total"),
            "follows": account.get("follows_total"),
            "ip_location": account.get("ip_location"),
            "verify_info": account.get("verify_info"),
            "platform_payload": raw,
            "source_status": {"status": "healthy", "source": "hub_db"},
        }
    )


def _xhs_article_covers(request: Request, payload: dict[str, Any]) -> Response:
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return JSONResponse(status_code=400, content={"error": "articles must be a list"})
    result: list[dict[str, Any]] = []
    for item in articles:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("article_id") or "").strip()
        row = _xhs_article_row(request.app.state.settings, article_id)
        cover_url = None
        if row is not None:
            stored = _xhs_decode_payload(row["payload_json"])
            raw = (
                {**stored, **stored["raw"]}
                if isinstance(stored.get("raw"), dict)
                else stored
            )
            cover_url = raw.get("cover_url") or raw.get("cover")
            if isinstance(cover_url, dict):
                cover_url = cover_url.get("url") or cover_url.get("url_size_large")
        result.append(
            {
                "article_id": article_id,
                "cover_url": str(cover_url).strip() if cover_url else None,
                "status": "found" if cover_url else "not_found",
            }
        )
    return JSONResponse(content={"items": result, "source": "hub_db"})


async def _xhs_hub_write(path: str, request: Request, body: bytes) -> Response | None:
    if path == "article-covers":
        try:
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(status_code=400, content={"error": "请求 JSON 无效"})
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON object"})
        # 这是旧页面名为 POST 的兼容性读取，不写入缓存、不访问旧系统，
        # 只从 Hub 已保存的搜索事实返回封面字段。
        return _xhs_article_covers(request, payload)
    del body
    _audit_xhs_legacy_write(
        request.app.state.settings,
        method=request.method,
        path=path,
    )
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "blocked": True,
            "upstream_called": False,
            "freeze_state": "all_frozen",
            "operation": path,
            "error": {
                "code": XHS_FREEZE_CODE,
                "legacy_code": "LEGACY_XHS_WRITE_BLOCKED",
                "message": f"{XHS_FREEZE_MESSAGE}（操作：{path}）",
            },
            "message": f"{XHS_FREEZE_MESSAGE}（操作：{path}）",
        },
    )


async def _xhs_hub_write_unfrozen(path: str, request: Request, body: bytes) -> Response | None:
    try:
        payload = json.loads(body.decode("utf-8") or "{}") if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "请求 JSON 无效"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON object"})

    state = _xhs_state_service(request)
    try:
        if path.startswith("keywords/") and path.endswith("/refresh"):
            keyword_id = path.split("/")[1]
            provider = getattr(request.app.state, "xhs_shadow_provider", None)
            is_live = bool(getattr(provider, "is_live", False))
            if is_live and payload.get("confirm") is not True:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "真实小红书刷新必须明确传入 confirm=true",
                        "blocked": True,
                        "upstream_called": False,
                    },
                )
            key = _xhs_write_key(request, payload, f"refresh-{keyword_id}")
            result = XhsService(request.app.state.settings, provider=provider).shadow_refresh(
                keyword_id,
                dry_run=not is_live,
                confirm=payload.get("confirm") is True,
                idempotency_key=key,
                provider=provider,
            )
            result.update(
                {
                    "job_id": result.get("refresh_job_id"),
                    "status": "done" if not result.get("failed") else "failed",
                    "keyword": payload.get("keyword"),
                }
            )
            return JSONResponse(
                status_code=502 if result.get("failed") else 200,
                content=result,
            )

        if path == "refresh-all":
            key = _xhs_write_key(request, payload, "refresh-all")
            result = _xhs_refresh_service(request).start_batch(
                keyword_ids=payload.get("keyword_ids"),
                key=key,
                source="web_refresh_all",
            )
            _xhs_schedule_batch(request, result["batch_id"], confirm=payload.get("confirm") is True)
            return JSONResponse(status_code=202, content=result)

        if path == "refresh-all/cancel":
            batch_id = str(payload.get("batch_id") or "").strip()
            if not batch_id:
                return JSONResponse(status_code=400, content={"error": "batch_id is required"})
            result = _xhs_refresh_service(request).cancel(
                batch_id=batch_id,
                key=_xhs_write_key(request, payload, f"cancel-{batch_id}"),
            )
            return JSONResponse(content=result)

        if path == "refresh-all/resume":
            batch_id = str(payload.get("batch_id") or "").strip()
            if not batch_id:
                return JSONResponse(status_code=400, content={"error": "batch_id is required"})
            result = _xhs_refresh_service(request).resume(
                batch_id=batch_id,
                key=_xhs_write_key(request, payload, f"resume-{batch_id}"),
                confirm=payload.get("confirm") is True,
            )
            _xhs_schedule_batch(request, result["batch_id"], confirm=payload.get("confirm") is True)
            return JSONResponse(status_code=202, content=result)

        if path == "article-covers":
            return _xhs_article_covers(request, payload)

        if path == "keyword-manage/groups":
            return JSONResponse(content=state.create_group(str(payload.get("label") or "")))
        if path.startswith("keyword-manage/groups/") and request.method == "PATCH":
            group_id = path.split("/")[2]
            return JSONResponse(content=state.update_group(group_id, str(payload.get("label") or "")))
        if path.startswith("keyword-manage/groups/") and request.method == "DELETE":
            return JSONResponse(content=state.delete_group(path.split("/")[2]))
        if path == "keyword-manage/keywords":
            return JSONResponse(
                content=state.create_keyword(
                    str(payload.get("group_id") or ""),
                    str(payload.get("keyword_text") or ""),
                )
            )
        if path.startswith("keyword-manage/keywords/"):
            keyword_id = path.split("/")[2]
            if request.method == "PATCH":
                return JSONResponse(
                    content=state.update_keyword(
                        keyword_id,
                        text=payload.get("keyword_text"),
                        note=payload.get("note"),
                    )
                )
            if request.method == "DELETE":
                return JSONResponse(content=state.archive_keyword(keyword_id))

        if path.startswith("keywords/") and path.endswith(("/pin", "/unpin")):
            parts = path.split("/")
            return JSONResponse(
                content=state.set_flag(parts[1], "pinned", parts[2] == "pin")
            )
        if path.startswith("keywords/") and path.endswith("/topic"):
            return JSONResponse(content=state.set_flag(path.split("/")[1], "topic", payload.get("topic")))
        if path.startswith("keywords/") and path.endswith("/bucket"):
            return JSONResponse(content=state.set_flag(path.split("/")[1], "keyword_bucket", payload.get("keyword_bucket")))
        if path.startswith("keywords/") and path.endswith("/note"):
            return JSONResponse(content=state.set_flag(path.split("/")[1], "note", payload.get("note")))

        return None
    except XhsBatchAlreadyRunningError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": exc.message, "batch": exc.state},
        )
    except (ConflictError, NotFoundError, ValidationAppError) as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


def _xhs_cover_url(raw_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(str(raw_url or "").strip())
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or not (hostname == "xhscdn.com" or hostname.endswith(".xhscdn.com"))
        or not parsed.path
    ):
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), hostname, parsed.path, parsed.query, "")
    )


def _xhs_frozen_cover_image(request: Request) -> Response:
    """Read frozen-page covers directly from the vendor CDN, never from 8766."""
    target = _xhs_cover_url(request.query_params.get("url", ""))
    if target is None:
        return Response(status_code=404, content=b"", media_type="image/gif")
    try:
        upstream_request = urllib.request.Request(
            target,
            headers={"Accept": request.headers.get("accept", "image/*")},
            method=request.method,
        )
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(request.app.state.settings.xhs_source_timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            response_type = upstream.headers.get_content_type() or "application/octet-stream"
            return Response(
                content=payload,
                status_code=int(upstream.status),
                media_type=response_type,
            )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return Response(status_code=404, content=b"", media_type="image/gif")


def _xhs_frozen_get(path: str, request: Request) -> Response | None:
    """Serve the frozen XHS page from Hub facts through a legacy projection.

    This function deliberately calls the Hub-only methods rather than the
    migration-aware public methods: the latter may select the frozen 8766
    adapter in legacy/compare mode.
    """
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        return None
    service = XhsService(request.app.state.settings)
    try:
        if path == "article-cover-image":
            return _xhs_frozen_cover_image(request)
        if path == "note-detail":
            return _xhs_note_detail(request)
        if path == "creator-detail":
            return _xhs_creator_detail(request)
        if path in {"monitor-data", "monitor-data/bootstrap"}:
            if request.query_params.get("summary", "").lower() in {"1", "true", "yes", "on"}:
                payload = service._hub_counts()
                return JSONResponse(
                    content={
                        "source_status": {"status": "healthy", "source": "hub_db"},
                        "counts": payload,
                        "available_fact_arrays": [
                            "accounts",
                            "articles",
                            "keywords",
                            "ranking_hits",
                            "snapshots",
                            "snapshot_terms",
                        ],
                    }
                )
            payload = service._hub_bootstrap()
            if payload is not None:
                return JSONResponse(content=project_hub_payload(payload))
            legacy_payload = _xhs_frozen_monitor_data(request.app.state.settings)
            if legacy_payload is not None:
                fallback = _xhs_frozen_bootstrap_payload(legacy_payload)
                fallback["source_status"] = {
                    "status": "degraded",
                    "source": "frozen_normalized_fallback",
                    "error": "Hub 尚无小红书事实，暂时使用冻结快照",
                }
                return JSONResponse(content=fallback)
            return JSONResponse(content=project_hub_payload({
                "keywords": [],
                "accounts": [],
                "snapshots": [],
                "ranking_hits": [],
                "articles": [],
            }))
        if path.startswith("monitor-data/keyword/"):
            keyword_id = path.split("/", 2)[2]
            payload = service._hub_bootstrap()
            if payload is not None:
                projected = project_hub_payload(payload)
                item = next(
                    (
                        row for row in projected.get("keywords", [])
                        if str(row.get("keyword_id") or "") == keyword_id
                    ),
                    None,
                )
                if item is not None:
                    return JSONResponse(content=item)
            legacy_payload = _xhs_frozen_keyword_payload(request.app.state.settings, keyword_id)
            if legacy_payload is not None:
                return JSONResponse(content=legacy_payload)
            return JSONResponse(content=service._keyword_hub(keyword_id))
        if path.startswith("monitor-data/account/"):
            account_id = path.split("/", 2)[2]
            payload = service._hub_bootstrap()
            if payload is not None:
                projected = project_hub_payload(payload)
                item = next(
                    (
                        row for row in projected.get("accounts", [])
                        if str(row.get("account_id") or "") == account_id
                    ),
                    None,
                )
                if item is not None:
                    return JSONResponse(content=item)
            legacy_payload = _xhs_frozen_account_payload(request.app.state.settings, account_id)
            if legacy_payload is not None:
                return JSONResponse(content=legacy_payload)
            return JSONResponse(content=service._account_hub(account_id))
        if path == "articles":
            return _xhs_article_list(request)
        if path == "articles/accounts":
            return _xhs_article_accounts(request)
        if path == "article-hit-detail":
            return _xhs_hit_detail(request)
        if path == "article-content":
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "blocked": True,
                    "upstream_called": False,
                    "error": {
                        "code": "LEGACY_XHS_READ_BLOCKED",
                        "message": "小红书搜索结果不再生成 Markdown；当前只提供结构化笔记数据和按需详情。",
                    },
                },
            )
        if path in {"keyword-manage", "keyword-manage/groups", "keyword-manage/keywords"}:
            return JSONResponse(content=_xhs_state_service(request).keyword_manage())
        if path == "refresh-all/status":
            batch_id = str(request.query_params.get("batch_id") or "").strip()
            if batch_id:
                payload = _xhs_refresh_service(request).status(batch_id)
                if payload is None:
                    return JSONResponse(status_code=404, content={"error": "batch not found"})
                return JSONResponse(content=payload)
            return JSONResponse(content=_xhs_refresh_service(request).active_status())
        if path == "refresh-all/history":
            return JSONResponse(content=_xhs_refresh_service(request).history())
        if path.startswith("refresh-status/"):
            return _xhs_refresh_status_legacy(request, path.split("/", 1)[1])
    except AppError:
        raise
    except NotImplementedError:
        return JSONResponse(
            status_code=200,
            content={
                "source_status": {"status": "degraded", "source": "hub_frozen"},
                "keywords": [],
                "accounts": [],
                "snapshots": [],
                "ranking_hits": [],
                "articles": [],
                "snapshot_terms": [],
                "counts": {
                    "keywords": 0,
                    "accounts": 0,
                    "snapshots": 0,
                    "ranking_hits": 0,
                    "articles": 0,
                    "snapshot_terms": 0,
                },
            },
        )
    return None


def _wiki_root(settings: Any):
    candidates = [
        root for root in settings.wiki_allowed_roots
        if root != settings.asset_store_path and root.is_dir() and (root / "wiki").is_dir()
    ]
    return sorted(candidates, key=lambda item: str(item))[0] if candidates else None


def _wiki_safe_path(root, relative: str, *, suffix: str | None = None):
    if not isinstance(relative, str) or not relative:
        return None
    normalized = posixpath.normpath(relative.replace("\\", "/")).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        return None
    target = root / normalized
    if suffix and target.suffix.lower() != suffix.lower():
        return None
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve())
        stat = resolved.stat()
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or target.is_symlink():
        return None
    return resolved


def _wiki_is_hidden(name: str) -> bool:
    return name.startswith(".")


def _wiki_md_files(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if not _wiki_is_hidden(name)
            and not (Path(base).resolve() == root.resolve() and name == "wiki-viewer")
            and not (Path(base) / name).is_symlink()
        ]
        for name in files:
            if name.endswith(".md") and not _wiki_is_hidden(name):
                path = Path(base) / name
                if not path.is_symlink():
                    yield path


def _wiki_count_md(path: Path) -> int:
    return sum(1 for _ in _wiki_md_files(path))


def _wiki_list_dir(root, relative: str) -> dict[str, Any] | None:
    base = root if not relative else _wiki_safe_dir(root, relative)
    if base is None or not base.is_dir():
        return None
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, str]] = []
    for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
        if _wiki_is_hidden(entry.name) or entry.is_symlink():
            continue
        rel = entry.relative_to(root).as_posix()
        if entry.is_dir():
            if base == root and entry.name == "wiki-viewer":
                continue
            dirs.append({"name": entry.name, "path": rel, "count": _wiki_count_md(entry)})
        elif entry.is_file() and entry.suffix.lower() == ".md":
            files.append({"name": entry.name, "path": rel})
    return {"dirs": dirs, "files": files}


def _wiki_safe_dir(root, relative: str):
    if not isinstance(relative, str):
        return None
    normalized = posixpath.normpath(relative.replace("\\", "/")).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        return root
    target = root / normalized
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if target.is_symlink() or not resolved.is_dir():
        return None
    return resolved


def _wiki_search(root, query: str, limit: int = 200) -> list[str]:
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[str] = []
    for path in sorted(_wiki_md_files(root), key=lambda item: item.relative_to(root).as_posix().lower()):
        relative = path.relative_to(root).as_posix()
        if needle in relative.lower():
            hits.append(relative)
            if len(hits) >= limit:
                break
    return hits


_IMG_LINE_RE = re.compile(r"^!\[.*?\]\((.+)\)$")


def _url_core(url: str) -> str:
    value = str(url).split("#", 1)[0].split("?", 1)[0]
    return re.sub(r"/\d+$", "", value)


def _image_hits(content: str, core: str) -> list[int]:
    hits: list[int] = []
    for index, line in enumerate(content.splitlines()):
        match = _IMG_LINE_RE.match(line.strip())
        if match and _url_core(match.group(1)) == core:
            hits.append(index)
    return hits


def _load_wiki_ocr(root: Path) -> dict[str, Any]:
    db_path = root / "wiki-viewer" / "ocr-db.json"
    try:
        db_path.resolve().relative_to(root.resolve())
        return json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """原子更新 Wiki OCR 索引，避免删除图片时留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _delete_wiki_ocr_record(root: Path, core: str) -> bool:
    """按图片 core 清理 output_md/wiki-viewer/ocr-db.json。"""
    db_path = root / "wiki-viewer" / "ocr-db.json"
    data = _load_wiki_ocr(root)
    if not data:
        return False
    kept = {
        key: value
        for key, value in data.items()
        if _url_core(key) != core
    }
    if len(kept) == len(data):
        return False
    _atomic_json_write(db_path, kept)
    return True


def _delete_image_ranges(content: str, core: str) -> tuple[str, int]:
    lines = content.splitlines()
    ranges: list[tuple[int, int]] = []
    for start in _image_hits(content, core):
        end = start + 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end < len(lines) and re.match(r"^<!--\s*(插图建议|OCR内容)", lines[end].strip()):
            while end < len(lines) and "-->" not in lines[end]:
                end += 1
            if end < len(lines):
                end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end < len(lines) and lines[end].strip() == "---":
            end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        ranges.append((start, end))
    if not ranges:
        return content, 0
    doomed = {index for start, end in ranges for index in range(start, end)}
    result = "\n".join(line for index, line in enumerate(lines) if index not in doomed)
    return result, len(ranges)


async def proxy_legacy_wiki_api(
    path: str,
    request: Request,
) -> Response:
    """兼容原 Wiki UI 的 API；正文和索引都直接操作 output_md。"""
    settings = request.app.state.settings
    root = _wiki_root(settings)
    if root is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Wiki 数据根不可用"})

    body = await request.body()
    query = urllib.parse.parse_qs(str(request.url.query))
    if path == "list" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("path", [""])[0])
        data = _wiki_list_dir(root, relative)
        return JSONResponse(status_code=200 if data is not None else 404, content=data or {"error": "invalid dir"})

    if path == "search" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("q", [""])[0])
        return JSONResponse(content={"files": _wiki_search(root, relative)})

    if path == "file" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("path", [""])[0])
        target = _wiki_safe_path(root, relative, suffix=".md")
        if target is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        try:
            return JSONResponse(content={"path": relative, "content": target.read_text(encoding="utf-8")})
        except (OSError, UnicodeDecodeError):
            return JSONResponse(status_code=422, content={"error": "file unreadable"})

    if path == "ocr" and request.method == "GET":
        url = urllib.parse.unquote(query.get("url", [""])[0]).strip()
        if not url:
            return JSONResponse(status_code=400, content={"error": "missing url"})
        record = _load_wiki_ocr(root).get(_url_core(url))
        return JSONResponse(content={"ok": bool(record), "ocr": (record or {}).get("ocr", ""), "source": (record or {}).get("source", "")})

    if path == "scan-image" and request.method == "GET":
        core = urllib.parse.unquote(query.get("core", [""])[0]).strip()
        if not core:
            return JSONResponse(status_code=400, content={"error": "missing core"})
        results: list[dict[str, Any]] = []
        total = 0
        for file_path in _wiki_md_files(root):
            try:
                matches = _image_hits(file_path.read_text(encoding="utf-8"), core)
            except (OSError, UnicodeDecodeError):
                continue
            if matches:
                results.append({"path": file_path.relative_to(root).as_posix(), "count": len(matches)})
                total += len(matches)
        return JSONResponse(content={"ok": True, "total": total, "files": results})

    if path == "image-index" and request.method == "GET":
        index: dict[str, dict[str, Any]] = {}
        for file_path in _wiki_md_files(root):
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            counts: dict[str, int] = {}
            for line in lines:
                match = _IMG_LINE_RE.match(line.strip())
                if match:
                    core = _url_core(match.group(1))
                    counts[core] = counts.get(core, 0) + 1
            relative = file_path.relative_to(root).as_posix()
            for core, count in counts.items():
                item = index.setdefault(core, {"total": 0, "files": []})
                item["total"] += count
                item["files"].append({"path": relative, "count": count})
        return JSONResponse(content={"ok": True, "index": index})

    if path == "save" and request.method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "bad json"})
        relative = payload.get("path", "")
        content = payload.get("content", "")
        if not isinstance(content, str):
            return JSONResponse(status_code=400, content={"error": "content must be string"})
        try:
            with writer_lock(settings.lock_path):
                result = WikiService(
                    connection=connect(settings, readonly=False),
                    asset_root=Path(settings.asset_store_path),
                    source_roots=settings.wiki_allowed_roots,
                    lock_path=Path(settings.lock_path),
                ).save_source_ref(relative, body=content, operator="legacy-wiki")
            return JSONResponse(content={"ok": True, "path": relative, "data": result})
        except AppError as exc:
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.message})
        except (FileNotFoundError, ValueError, OSError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    if path == "bulk-delete-image" and request.method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "bad json"})
        core = str(payload.get("core") or "").strip()
        if not core:
            return JSONResponse(status_code=400, content={"error": "missing core"})
        deleted_files = 0
        deleted_images = 0
        try:
            with writer_lock(settings.lock_path):
                service = WikiService(
                    connection=connect(settings, readonly=False),
                    asset_root=Path(settings.asset_store_path),
                    source_roots=settings.wiki_allowed_roots,
                    lock_path=Path(settings.lock_path),
                )
                for file_path in list(_wiki_md_files(root)):
                    try:
                        original = file_path.read_text(encoding="utf-8")
                        updated, count = _delete_image_ranges(original, core)
                    except (OSError, UnicodeDecodeError):
                        continue
                    if not count:
                        continue
                    service.save_source_ref(
                        file_path.relative_to(root).as_posix(),
                        body=updated,
                        operator="legacy-wiki",
                    )
                    deleted_files += 1
                    deleted_images += count
                ocr_db_updated = _delete_wiki_ocr_record(root, core)
            return JSONResponse(content={
                "ok": True,
                "deleted_files": deleted_files,
                "deleted_images": deleted_images,
                "ocr_db_updated": ocr_db_updated,
                "note": "Markdown 已直接写回 output_md，并同步更新 OCR 数据库与审计记录。",
            })
        except AppError as exc:
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.message})
        except (FileNotFoundError, ValueError, OSError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    return JSONResponse(status_code=404, content={"error": "unknown endpoint"})


async def proxy_legacy_wechat_api(
    path: str,
    request: Request,
) -> Response:
    """把原微信关键词页面的 API 原样接到旧服务，页面本身仍由工作台托管。"""
    if path in {
        "list",
        "search",
        "file",
        "ocr",
        "scan-image",
        "image-index",
        "save",
        "bulk-delete-image",
    }:
        return await proxy_legacy_wiki_api(path, request)
    body = await request.body()
    if not _allowed(path):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_ENDPOINT_NOT_ALLOWED",
                    "message": "该旧系统接口未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    referer_kind = legacy_referer_kind(request.headers.get("referer", ""))
    is_xhs = referer_kind == "xhs"
    is_mp = referer_kind == "mp"
    is_geo = referer_kind == "geo"
    if is_xhs:
        source_url = settings.xhs_source_url
        timeout_seconds = settings.xhs_source_timeout_seconds
    elif is_mp:
        source_url = settings.mp_source_url
        timeout_seconds = settings.mp_source_timeout_seconds
    elif is_geo:
        source_url = settings.geo_source_url
        timeout_seconds = 10.0
    else:
        source_url = settings.wechat_source_url
        timeout_seconds = settings.wechat_source_timeout_seconds
    if is_xhs:
        frozen_response = _xhs_frozen_get(path, request)
        if frozen_response is not None:
            return frozen_response
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            hub_response = await _xhs_hub_write(path, request, body)
            if hub_response is not None:
                return hub_response
        else:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "blocked": True,
                    "upstream_called": False,
                    "error": {
                        "code": "LEGACY_XHS_READ_BLOCKED",
                        "message": "该小红书读取接口尚未完成 Hub 投影，已阻断旧系统回源。",
                    },
                },
            )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        legacy_system = "xhs" if is_xhs else "mp" if is_mp else "geo" if is_geo else "wechat"
        _audit_legacy_write_blocked(
            settings, legacy_system=legacy_system, method=request.method, path=path
        )
        code = "LEGACY_XHS_WRITE_BLOCKED" if is_xhs else "LEGACY_WRITE_BLOCKED"
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "blocked": True,
                "upstream_called": False,
                "error": {
                    "code": code,
                    "message": "原版业务岛屿写操作已被工作台阻断；请经对应 Hub 命令接口执行，旧系统不会收到请求。",
                },
            },
        )
    target = _upstream_url(
        str(source_url),
        path,
        str(request.url.query),
    )
    headers = {"Accept": request.headers.get("accept", "application/json")}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    upstream_request = urllib.request.Request(
        target,
        data=body if body else None,
        headers=headers,
        method=request.method,
    )

    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            status = int(upstream.status)
            response_type = upstream.headers.get_content_type() or "application/json"
            if is_mp:
                payload = _redact_json_payload(payload, response_type)
            return Response(
                content=payload,
                status_code=status,
                media_type=response_type,
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        response_type = exc.headers.get_content_type() if exc.headers else "application/json"
        if is_mp:
            payload = _redact_json_payload(payload, response_type)
        return Response(
            content=payload,
            status_code=int(exc.code),
            media_type=response_type or "application/json",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统服务暂时不可用：{exc}",
                },
            },
        )


async def proxy_legacy_geo_page(path: str, request: Request) -> Response:
    """把 GEOProMax 原始服务端页面原样承载到工作台业务岛屿。"""
    if path not in {"", "index.html"}:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_GEO_PAGE_NOT_ALLOWED",
                    "message": "该 GEO 原始页面路径未登记到工作台。",
                },
            },
        )

    settings: Any = request.app.state.settings
    target = f"{str(settings.geo_source_url).rstrip('/')}/"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    upstream_request = urllib.request.Request(
        target,
        headers={"Accept": request.headers.get("accept", "text/html")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(upstream_request, timeout=10.0) as upstream:
            return Response(
                content=upstream.read(),
                status_code=int(upstream.status),
                media_type=upstream.headers.get_content_type() or "text/html",
            )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=int(exc.code),
            media_type=exc.headers.get_content_type() if exc.headers else "text/plain",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"GEO 原系统服务暂时不可用：{exc}",
                },
            },
        )


async def proxy_legacy_xhs_page(request: Request) -> Response:
    """承载小红书原版的辅助页面，保留原页面 DOM 与脚本入口。

    主监控页由静态镜像直接提供；两个从榜单跳转的辅助页没有扩展名，
    因而需要显式映射到镜像 HTML。页面内部仍使用原版 API 契约，
    请求 Referer 会继续落在 /legacy/xhs/，由同一白名单代理承接。
    """
    pages = {
        "article-hit-detail": "article_hit_detail.html",
        "keyword-turnover": "keyword_turnover.html",
    }
    page_key = request.url.path.rsplit("/", 1)[-1]
    filename = pages.get(page_key)
    if filename is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_XHS_PAGE_NOT_ALLOWED",
                    "message": "该小红书辅助页面未登记到工作台镜像。",
                },
            },
        )
    page = request.app.state.settings.workbench_root / "frontend/public/legacy/xhs" / filename
    if not page.is_file():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_XHS_PAGE_MISSING",
                    "message": "小红书辅助页面镜像文件不存在。",
                },
            },
        )
    return FileResponse(page, media_type="text/html")


async def proxy_legacy_static(path: str, request: Request) -> Response:
    """代理公众号旧控制台返回的有限静态资源，不提供通用文件代理。"""
    if path != "logo.svg":
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_STATIC_NOT_ALLOWED",
                    "message": "该旧系统静态资源未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    local_logo = settings.workbench_root / "frontend/public/legacy/mp/static/logo.svg"
    if local_logo.is_file():
        return FileResponse(local_logo, media_type="image/svg+xml")

    target = f"{str(settings.mp_source_url).rstrip('/')}/static/logo.svg"
    upstream_request = urllib.request.Request(
        target,
        headers={"Accept": request.headers.get("accept", "image/svg+xml")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(settings.mp_source_timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            return Response(
                content=payload,
                status_code=int(upstream.status),
                media_type=upstream.headers.get_content_type() or "image/svg+xml",
            )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=int(exc.code),
            media_type=exc.headers.get_content_type() if exc.headers else "text/plain",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统静态资源暂时不可用：{exc}",
                },
            },
        )
