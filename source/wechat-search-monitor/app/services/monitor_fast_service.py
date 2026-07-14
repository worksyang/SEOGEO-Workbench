"""高性能监控数据层。

设计目标：
- 171MB ``monitor-data.json`` 只在源文件变化时解析一次；
- 主页面只下载轻量 bootstrap，关键词/账号详情按需加载；
- JSON 字节、gzip 与 ETag 在进程内复用；
- 保留旧 ``/api/monitor-data`` 的完整数据语义。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.keyword_bucket_resolver import DEFAULT_BUCKET, bucket_options
from app.repositories.keyword_registry_repo import KeywordRegistryRepository


BOOTSTRAP_KEYWORD_FIELDS = frozenset(
    {
        "keyword_id",
        "keyword",
        "topic",
        "keyword_bucket",
        "is_pinned",
        "pin_order",
        "today_best",
        "today_count",
        "coverage_days",
        "tracked_accounts",
        "article_count",
        "history_best",
        "history_hits",
        "heat_summary",
        "kw_score",
    }
)

BOOTSTRAP_READ_DELTA_FIELDS = frozenset(
    {
        "status",
        "read_delta_estimated",
        "steady_read_median",
        "provisional_steady_read_median",
        "provisional_read_delta_estimated",
        "provisional_sample_count",
        "provisional_status",
        "snapshot_count",
        "confidence_level",
        "trend_signal",
        "trend_label",
        "recent_vs_baseline_ratio",
        "observed_share",
        "estimated_share",
        "slot_coverage_ratio",
    }
)

BOOTSTRAP_ACCOUNT_FIELDS = frozenset(
    {
        "account_id",
        "name",
        "headimg_url",
        "score",
        "score_raw",
        "score_yesterday",
        "score_delta",
        "score_level",
        "score_explain",
        "timeliness_score",
        "timeliness_score_raw",
        "timeliness_score_yesterday",
        "timeliness_score_delta",
        "timeliness_score_level",
        "timeliness_explain",
        "today_score",
        "today_score_raw",
        "today_score_yesterday",
        "today_score_delta",
        "today_score_level",
        "today_explain",
        "history",
        "move_summary",
        "recent_hit_days",
        "current_streak",
        "longest_streak",
        "topic_count",
        "bucket_count",
        "kw_count",
        "article_count",
        "today_hit_count",
        "friends_follow_count",
        "original_article_count",
    }
)

LATEST_RUN_FIELDS = frozenset(
    {"id", "date", "time", "run_at", "result_count", "trigger_type"}
)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _etag(payload: bytes) -> str:
    # gzip 与 identity 表示共享同一语义版本，因此使用弱验证器。
    return f'W/"{hashlib.md5(payload).hexdigest()}"'


def _gzip(payload: bytes) -> bytes:
    return gzip.compress(payload, compresslevel=6)


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def _empty_keyword_payload(
    item: dict[str, Any],
    group: dict[str, Any],
    window_days: int,
) -> dict[str, Any]:
    keyword_text = str(item.get("keyword_text") or "").strip()
    return {
        "keyword": keyword_text,
        "keyword_id": str(item.get("keyword_id") or "").strip(),
        "topic": keyword_text,
        "keyword_bucket": group.get("label") or DEFAULT_BUCKET,
        "today_best": None,
        "today_count": 0,
        "coverage_days": 0,
        "tracked_accounts": 0,
        "article_count": 0,
        "latest_run": None,
        "runs": [],
        "history_best": [0] * window_days,
        "history_hits": [0] * window_days,
        "accounts": [],
        "heat_summary": {},
        "kw_score": {
            "total": 0,
            "heat": 0,
            "breadth": 0,
            "richness": 0,
            "has_heat": False,
        },
    }


def _apply_registry_scope(data: dict[str, Any], registry_payload: dict[str, Any]) -> dict[str, Any]:
    monitor_by_id = {
        item.get("keyword_id"): item
        for item in data.get("keywords", [])
        if item.get("keyword_id")
    }
    window_days = int(data.get("window_days") or 15)
    keywords: list[dict[str, Any]] = []
    for group in registry_payload.get("groups", []):
        for item in group.get("keywords", []):
            keyword_id = item.get("keyword_id")
            merged = dict(
                monitor_by_id.get(keyword_id)
                or _empty_keyword_payload(item, group, window_days)
            )
            merged["keyword_id"] = keyword_id
            merged["keyword"] = item.get("keyword_text", merged.get("keyword", ""))
            merged["keyword_bucket"] = (
                merged.get("keyword_bucket")
                or group.get("label")
                or DEFAULT_BUCKET
            )
            keywords.append(merged)
    return {**data, "keywords": keywords}


def _merge_keyword_states(
    data: dict[str, Any],
    settings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    keywords: list[dict[str, Any]] = []
    for item in data.get("keywords", []):
        merged = dict(item)
        state = settings.get(str(item.get("keyword_id") or ""), {})
        merged["is_pinned"] = bool(state.get("is_pinned", False))
        merged["pin_order"] = state.get("pin_order")
        merged["topic"] = (
            state.get("topic")
            or merged.get("topic")
            or merged.get("keyword")
        )
        merged["keyword_bucket"] = (
            state.get("keyword_bucket")
            or merged.get("keyword_bucket")
            or DEFAULT_BUCKET
        )
        keywords.append(merged)
    return {**data, "keywords": keywords}


def _load_keyword_read_deltas(
    delta_path: Path,
    meta_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not delta_path.exists():
        return {}, {
            "available": False,
            "row_count": 0,
            "source": str(delta_path),
        }

    raw = json.loads(delta_path.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("items") or raw.get("keywords") or []
    by_id: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    first_row = rows[0] if rows else {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        keyword_id = str(row.get("keyword_id") or "").strip()
        if keyword_id:
            by_id[keyword_id] = dict(row)
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    generated_at = None
    if meta_path.exists():
        try:
            generated_at = json.loads(meta_path.read_text(encoding="utf-8")).get(
                "generated_at"
            )
        except (OSError, json.JSONDecodeError):
            generated_at = None

    return by_id, {
        "available": True,
        "row_count": len(by_id),
        "source": str(delta_path),
        "generated_at": generated_at,
        "window_start": first_row.get("window_start"),
        "window_end": first_row.get("window_end"),
        "window_days": first_row.get("window_days"),
        "method": first_row.get("method"),
        "status_counts": status_counts,
    }


def _insufficient_read_delta(
    item: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "keyword_id": item.get("keyword_id") or "",
        "keyword": item.get("keyword") or "",
        "window_start": meta.get("window_start"),
        "window_end": meta.get("window_end"),
        "window_days": meta.get("window_days"),
        "method": meta.get("method") or "schedule_adjusted_read_rate_v3",
        "status": "insufficient_data",
        "read_delta_estimated": None,
        "read_delta_raw": None,
        "steady_read_median": None,
        "legacy_steady_read_median": None,
        "confidence_score": 0,
        "confidence_level": "insufficient",
        "trend_signal": 0,
        "trend_label": "观察中",
        "insufficient_reason": "not_in_keyword_read_deltas",
    }


def _merge_read_deltas(
    data: dict[str, Any],
    delta_path: Path,
    meta_path: Path,
) -> dict[str, Any]:
    deltas_by_id, meta = _load_keyword_read_deltas(delta_path, meta_path)
    keywords: list[dict[str, Any]] = []
    for item in data.get("keywords", []):
        merged = dict(item)
        if meta.get("available"):
            row = deltas_by_id.get(str(item.get("keyword_id") or ""))
            merged["keyword_read_delta"] = row or _insufficient_read_delta(merged, meta)
        keywords.append(merged)
    return {
        **data,
        "keywords": keywords,
        "keyword_read_delta_meta": meta,
    }


def _metric_number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _sort_keywords(keywords: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        score = item.get("kw_score") or {}
        if item.get("is_pinned"):
            segment = 0
        elif score.get("has_heat"):
            segment = 1
        else:
            segment = 2
        metric = score.get("heat") if segment == 1 else score.get("richness")
        return segment, -_metric_number(metric), str(item.get("keyword") or "")

    keywords.sort(key=sort_key)
    return keywords


def _prune_latest_run(run: Any) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    return {key: run.get(key) for key in LATEST_RUN_FIELDS if key in run}


def _turnover_runs(keyword: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in keyword.get("runs", []) or []:
        articles: list[dict[str, Any]] = []
        for article in run.get("articles", []) or []:
            item = {"article_id": article.get("article_id")}
            if not item["article_id"]:
                item["title"] = article.get("title")
                item["url"] = article.get("url")
            articles.append(item)
        result.append(
            {
                "id": run.get("id"),
                "date": run.get("date"),
                "time": run.get("time"),
                "articles": articles,
            }
        )
    return result


def _prune_keyword(keyword: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: keyword.get(key)
        for key in BOOTSTRAP_KEYWORD_FIELDS
        if key in keyword
    }
    result["history_best"] = (
        keyword.get("history_best")
        if isinstance(keyword.get("history_best"), list)
        else []
    )
    result["history_hits"] = (
        keyword.get("history_hits")
        if isinstance(keyword.get("history_hits"), list)
        else []
    )
    result["latest_run"] = _prune_latest_run(keyword.get("latest_run"))
    read_delta = keyword.get("keyword_read_delta")
    if isinstance(read_delta, dict):
        result["keyword_read_delta"] = {
            key: read_delta.get(key)
            for key in BOOTSTRAP_READ_DELTA_FIELDS
            if key in read_delta
        }
    result["turnover_runs"] = _turnover_runs(keyword)
    return result


def _prune_account(account: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: account.get(key)
        for key in BOOTSTRAP_ACCOUNT_FIELDS
        if key in account
    }
    result["history"] = (
        account.get("history")
        if isinstance(account.get("history"), list)
        else []
    )
    topic_names = [
        str(info.get("label") or "").strip()
        for info in (account.get("topics") or {}).values()
        if isinstance(info, dict) and str(info.get("label") or "").strip()
    ]
    result["topic_names"] = topic_names[:12]
    result["keyword_names"] = list((account.get("keywords") or {}).keys())
    return result


class MonitorFastStore:
    """线程安全的监控数据缓存与分片索引。"""

    def __init__(
        self,
        monitor_data_path: str | Path,
        sqlite_path: str | Path,
        keyword_read_deltas_path: str | Path,
        article_metric_meta_path: str | Path,
    ) -> None:
        self.monitor_data_path = Path(monitor_data_path)
        self.sqlite_path = Path(sqlite_path)
        self.keyword_read_deltas_path = Path(keyword_read_deltas_path)
        self.article_metric_meta_path = Path(article_metric_meta_path)
        self._lock = threading.RLock()
        self._signature: tuple[tuple[int, int], ...] | None = None
        self._data: dict[str, Any] | None = None
        self._keyword_by_id: dict[str, dict[str, Any]] = {}
        self._account_by_id: dict[str, dict[str, Any]] = {}
        self._full_bytes: bytes | None = None
        self._full_gzip: bytes | None = None
        self._full_etag: str | None = None
        self._bootstrap_bytes: bytes | None = None
        self._bootstrap_gzip: bytes | None = None
        self._bootstrap_etag: str | None = None
        self._keyword_cache: OrderedDict[
            str, tuple[bytes, bytes, str]
        ] = OrderedDict()
        self._account_cache: OrderedDict[
            str, tuple[bytes, bytes, str]
        ] = OrderedDict()
        self._detail_cache_limit = 200

    def _source_signature(self) -> tuple[tuple[int, int], ...]:
        return (
            _file_signature(self.monitor_data_path),
            _file_signature(self.sqlite_path),
            _file_signature(Path(f"{self.sqlite_path}-wal")),
            _file_signature(self.keyword_read_deltas_path),
            _file_signature(self.article_metric_meta_path),
        )

    def ensure_loaded(self) -> None:
        signature = self._source_signature()
        if self._data is not None and signature == self._signature:
            return
        with self._lock:
            signature = self._source_signature()
            if self._data is not None and signature == self._signature:
                return
            self._reload(signature)

    def _reload(self, signature: tuple[tuple[int, int], ...]) -> None:
        if not self.monitor_data_path.exists():
            raise FileNotFoundError(
                f"monitor data not found: {self.monitor_data_path}"
            )
        data = json.loads(self.monitor_data_path.read_bytes())
        registry = KeywordRegistryRepository(self.sqlite_path)
        data = _apply_registry_scope(data, registry.load_payload())
        data = _merge_keyword_states(data, registry.list_settings())
        data = _merge_read_deltas(
            data,
            self.keyword_read_deltas_path,
            self.article_metric_meta_path,
        )
        keywords = _sort_keywords(data.get("keywords", []))
        data["keywords"] = keywords
        data["pinned_keyword_count"] = sum(
            1 for item in keywords if item.get("is_pinned")
        )
        data["keyword_bucket_options"] = bucket_options()
        data["keyword_scope"] = "configured"
        data["keyword_source_total"] = len(keywords)

        self._data = data
        self._keyword_by_id = {
            str(item.get("keyword_id")): item
            for item in keywords
            if item.get("keyword_id")
        }
        today_date = str(data.get("generated_at") or "")[:10]
        today_article_ids: dict[str, set[str]] = {}
        today_article_titles: dict[str, set[str]] = {}
        if today_date:
            for keyword in keywords:
                for run in keyword.get("runs", []) or []:
                    if run.get("date") != today_date:
                        continue
                    for article in run.get("articles", []) or []:
                        account_name = str(article.get("account") or "").strip()
                        if not account_name:
                            continue
                        article_id = str(article.get("article_id") or "").strip()
                        title = str(article.get("title") or "").strip()
                        if article_id:
                            today_article_ids.setdefault(account_name, set()).add(article_id)
                        if title:
                            today_article_titles.setdefault(account_name, set()).add(title)
        self._account_by_id = {
            str(item.get("account_id")): {
                **item,
                "_today_article_ids": sorted(
                    today_article_ids.get(str(item.get("name") or ""), set())
                ),
                "_today_article_titles": sorted(
                    today_article_titles.get(str(item.get("name") or ""), set())
                ),
            }
            for item in data.get("accounts", [])
            if item.get("account_id")
        }
        self._full_bytes = _json_bytes(data)
        self._full_gzip = None
        self._full_etag = _etag(self._full_bytes)

        bootstrap = {
            "generated_at": data.get("generated_at"),
            "window_days": data.get("window_days"),
            "window_start": data.get("window_start"),
            "window_end": data.get("window_end"),
            "account_score_method": data.get("account_score_method"),
            "wso_fit_meta": data.get("wso_fit_meta"),
            "keyword_read_delta_meta": data.get("keyword_read_delta_meta"),
            "keyword_bucket_options": data.get("keyword_bucket_options"),
            "keyword_scope": data.get("keyword_scope"),
            "keyword_source_total": data.get("keyword_source_total"),
            "pinned_keyword_count": data.get("pinned_keyword_count"),
            "keywords": [_prune_keyword(item) for item in keywords],
            "accounts": [
                _prune_account(item)
                for item in data.get("accounts", [])
            ],
        }
        self._bootstrap_bytes = _json_bytes(bootstrap)
        self._bootstrap_gzip = _gzip(self._bootstrap_bytes)
        self._bootstrap_etag = _etag(self._bootstrap_bytes)
        self._keyword_cache.clear()
        self._account_cache.clear()
        self._signature = signature

    def get_full(self) -> tuple[bytes, bytes, str]:
        self.ensure_loaded()
        with self._lock:
            if self._full_gzip is None:
                self._full_gzip = _gzip(self._full_bytes or b"{}")
            return (
                self._full_bytes or b"{}",
                self._full_gzip,
                self._full_etag or _etag(b"{}"),
            )

    def get_full_payload(self) -> dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            return self._data or {}

    def get_bootstrap(self) -> tuple[bytes, bytes, str]:
        self.ensure_loaded()
        with self._lock:
            return (
                self._bootstrap_bytes or b"{}",
                self._bootstrap_gzip or _gzip(b"{}"),
                self._bootstrap_etag or _etag(b"{}"),
            )

    def _detail(
        self,
        item_id: str,
        index: dict[str, dict[str, Any]],
        cache: OrderedDict[str, tuple[bytes, bytes, str]],
    ) -> tuple[bytes, bytes, str] | None:
        with self._lock:
            if item_id in cache:
                cache.move_to_end(item_id)
                return cache[item_id]
            item = index.get(item_id)
            if item is None:
                return None
            raw = _json_bytes(item)
            result = raw, _gzip(raw), _etag(raw)
            cache[item_id] = result
            if len(cache) > self._detail_cache_limit:
                cache.popitem(last=False)
            return result

    def get_keyword(self, keyword_id: str) -> tuple[bytes, bytes, str] | None:
        self.ensure_loaded()
        return self._detail(
            keyword_id,
            self._keyword_by_id,
            self._keyword_cache,
        )

    def get_account(self, account_id: str) -> tuple[bytes, bytes, str] | None:
        self.ensure_loaded()
        return self._detail(
            account_id,
            self._account_by_id,
            self._account_cache,
        )

    def get_keyword_stats(self) -> dict[str, dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            return dict(self._keyword_by_id)

    def size_report(self) -> dict[str, int]:
        self.ensure_loaded()
        with self._lock:
            return {
                "full_raw_bytes": len(self._full_bytes or b""),
                "full_gzip_bytes": len(self._full_gzip or b""),
                "bootstrap_raw_bytes": len(self._bootstrap_bytes or b""),
                "bootstrap_gzip_bytes": len(self._bootstrap_gzip or b""),
            }


_store: MonitorFastStore | None = None
_store_lock = threading.Lock()


def init_fast_store(
    monitor_data_path: str | Path,
    sqlite_path: str | Path,
    keyword_read_deltas_path: str | Path,
    article_metric_meta_path: str | Path,
) -> MonitorFastStore:
    global _store
    with _store_lock:
        paths = (
            Path(monitor_data_path),
            Path(sqlite_path),
            Path(keyword_read_deltas_path),
            Path(article_metric_meta_path),
        )
        current_paths = None
        if _store is not None:
            current_paths = (
                _store.monitor_data_path,
                _store.sqlite_path,
                _store.keyword_read_deltas_path,
                _store.article_metric_meta_path,
            )
        if _store is None or current_paths != paths:
            _store = MonitorFastStore(*paths)
        _store.ensure_loaded()
        return _store


def get_fast_store() -> MonitorFastStore:
    if _store is None:
        raise RuntimeError("MonitorFastStore is not initialized")
    return _store


def try_get_fast_store() -> MonitorFastStore | None:
    return _store


def reset_fast_store_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
