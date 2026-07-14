"""monitor_fast_service — 高性能分片数据层。

单次加载 154MB monitor-data.json，建立引用索引，预计算 bootstrap 载荷
并预压缩 gzip，按 ID 做有界 LRU 字节缓存。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import gzip
from collections import OrderedDict
from pathlib import Path
from typing import Any


# ── bootstrap 场的字段白名单 ──────────────────────────
# 基于前端 monitor.js 实际使用的 keyword 列表字段
BOOTSTRAP_KEYWORD_FIELDS = frozenset({
    "keyword_id", "keyword", "topic", "keyword_bucket", "note",
    "today_best", "today_count", "yesterday_hits",
    "coverage_days", "tracked_accounts", "article_count",
    "latest_run", "is_pinned", "pin_order", "is_relevant_count",
    "today_relevant_count", "enabled",
    "kw_score", "keyword_read_delta", "move_summary",
    "keyword_heat_metric",
    "history_best", "history_hits",
})

# 前端实际使用的 account 列表字段
BOOTSTRAP_ACCOUNT_FIELDS = frozenset({
    "account_id", "name", "headimg_url", "platform",
    "fans", "total_works", "likes_total",
    "collects_total", "follows_total",
    "score", "score_raw", "score_yesterday", "score_delta", "score_level",
    "timeliness_score", "timeliness_score_raw",
    "timeliness_score_yesterday", "timeliness_score_delta",
    "timeliness_score_level",
    "today_score", "today_score_raw",
    "today_score_yesterday", "today_score_delta", "today_score_level",
    "current_streak",
    "history_keyword_count", "history_article_count",
    "today_hit_count", "recent_hit_days",
    "topic_count", "bucket_count", "kw_count", "article_count",
    "move_summary", "is_relevant_count",
})

# 前端 keyword 列表排序 / 筛选需要的字段（在 keyword_heat_metric 内的子集）
BOOTSTRAP_KHM_FIELDS = frozenset({
    "status", "method", "version", "window_days", "effective_days",
    "steady_heat", "peak_heat", "heat_delta_15d", "peak_date",
    "trend_signal", "trend_ratio", "trend_label",
    "confidence_score", "confidence_level",
    "interaction_weights", "value_signal", "current_interactions",
})


def _prune_keyword_for_bootstrap(kw: dict) -> dict:
    """裁剪 keyword 到 bootstrap 白名单，并对 keyword_heat_metric 做子集裁剪。"""
    out = {k: kw[k] for k in BOOTSTRAP_KEYWORD_FIELDS if k in kw}
    # 子集裁剪 keyword_heat_metric 去掉 daily_heat_points（15 个浮点数组）
    khm = out.get("keyword_heat_metric")
    if isinstance(khm, dict):
        out["keyword_heat_metric"] = {k: khm[k] for k in BOOTSTRAP_KHM_FIELDS if k in khm}
    return out


def _prune_account_for_bootstrap(acct: dict) -> dict:
    """裁剪 account 到 bootstrap 白名单。"""
    return {k: acct[k] for k in BOOTSTRAP_ACCOUNT_FIELDS if k in acct}


def _etag_of(payload_bytes: bytes) -> str:
    return f'"{hashlib.md5(payload_bytes).hexdigest()}"'


def _gzip_compress(payload: bytes, level: int = 6) -> bytes:
    return gzip.compress(payload, compresslevel=level)


class MonitorFastStore:
    """进程内只读一次 154MB monitor-data.json，建立索引并提供快速访问。

    线程安全：所有公共方法加锁，装载仅在首次访问时发生。
    """

    def __init__(self, monitor_data_path: str | Path, sqlite_path: str | Path,
                 keywords_config_path: str | Path) -> None:
        self._monitor_data_path = Path(monitor_data_path)
        self._sqlite_path = Path(sqlite_path)
        self._keywords_config_path = Path(keywords_config_path)
        self._lock = threading.Lock()
        # 惰性装载状态
        self._loaded = False
        self._data: dict | None = None
        # 文件签名：mtime + size，用于自动失效
        self._monitor_sig: tuple[float, int] | None = None
        self._keywords_sig: tuple[float, int] | None = None
        self._sqlite_sig: tuple[float, int] | None = None
        # 索引
        self._keyword_by_id: dict[str, dict] | None = None
        self._account_by_id: dict[str, dict] | None = None
        # 预计算 bootstrap
        self._bootstrap_bytes: bytes | None = None
        self._bootstrap_gz: bytes | None = None
        self._bootstrap_etag: str | None = None
        # detail LRU 字节缓存（每个端点独立）
        self._detail_keyword_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._detail_account_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._detail_cache_max = 200  # max entries per cache

    # ── 文件签名 ────────────────────────────────────

    @staticmethod
    def _file_signature(path: Path) -> tuple[float, int]:
        try:
            st = path.stat()
            return st.st_mtime, st.st_size
        except OSError:
            return 0.0, 0

    def _signatures_changed(self) -> bool:
        """检查三个源文件是否有任何变化。"""
        mon_sig = self._file_signature(self._monitor_data_path)
        kw_sig = self._file_signature(self._keywords_config_path)
        sql_sig = self._file_signature(self._sqlite_path)
        if self._monitor_sig is None:
            return True
        return (mon_sig != self._monitor_sig or
                kw_sig != self._keywords_sig or
                sql_sig != self._sqlite_sig)

    # ── 装载 ────────────────────────────────────────

    def _load(self) -> None:
        """线程安全装载或重载数据。"""
        with self._lock:
            self._do_load()

    def _do_load(self) -> None:
        """实际装载（持有锁时调用）。"""
        # 检查文件签名
        mon_sig = self._file_signature(self._monitor_data_path)
        kw_sig = self._file_signature(self._keywords_config_path)
        sql_sig = self._file_signature(self._sqlite_path)

        if (self._loaded and
                mon_sig == self._monitor_sig and
                kw_sig == self._keywords_sig and
                sql_sig == self._sqlite_sig):
            return  # 无变化

        # 全量装载 monitor-data.json
        if not self._monitor_data_path.exists():
            raise FileNotFoundError(f"monitor data not found: {self._monitor_data_path}")

        raw = self._monitor_data_path.read_bytes()
        data = json.loads(raw)

        # 装载 keyword config 范围
        data = self._apply_keyword_config_scope(data)

        # 装载 SQLite 设置并合并
        settings = self._load_settings()
        data = self._merge_keyword_states(data, settings)

        # 排序 keyword
        keywords = self._sort_keywords(data["keywords"])
        data["keywords"] = keywords
        data["pinned_keyword_count"] = sum(1 for k in keywords if k.get("is_pinned"))

        from app.keyword_bucket_resolver import bucket_options
        data["keyword_bucket_options"] = bucket_options()
        data["keyword_scope"] = "configured"
        data["keyword_source_total"] = len(keywords)

        # 建立索引
        self._keyword_by_id = {kw["keyword_id"]: kw for kw in data["keywords"] if kw.get("keyword_id")}
        self._account_by_id = {acct["account_id"]: acct for acct in data.get("accounts", []) if acct.get("account_id")}

        # 预计算 bootstrap
        bootstrap = self._build_bootstrap(data)
        self._bootstrap_bytes = json.dumps(bootstrap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bootstrap_gz = _gzip_compress(self._bootstrap_bytes)
        self._bootstrap_etag = _etag_of(self._bootstrap_bytes)

        # 更新签名
        self._monitor_sig = mon_sig
        self._keywords_sig = kw_sig
        self._sqlite_sig = sql_sig
        self._data = data
        self._loaded = True

        # 清除 detail 缓存（数据已变）
        self._detail_keyword_cache.clear()
        self._detail_account_cache.clear()

    # ── 关键词配置范围合并 ──────────────────────────────

    def _load_keyword_config(self) -> dict:
        from app.repositories.keyword_config_repo import KeywordConfigRepository
        return KeywordConfigRepository(self._keywords_config_path).load()

    def _load_settings(self) -> dict[str, dict]:
        from app.repositories.keyword_settings_repo import KeywordSettingsRepository
        return KeywordSettingsRepository(self._sqlite_path).list_all()

    def _apply_keyword_config_scope(self, data: dict) -> dict:
        payload = self._load_keyword_config()
        monitor_by_id = {item.get("keyword_id"): item for item in data.get("keywords", []) if item.get("keyword_id")}
        window_days = int(data.get("window_days") or 15)
        keywords = []
        for group in payload.get("groups", []):
            for item in group.get("keywords", []):
                kid = item.get("keyword_id")
                merged = dict(monitor_by_id.get(kid) or self._empty_keyword_payload(item, group, window_days))
                merged["keyword_id"] = kid
                merged["keyword"] = item.get("keyword_text", merged.get("keyword", ""))
                merged["keyword_bucket"] = merged.get("keyword_bucket") or group.get("label") or "未分类"
                keywords.append(merged)
        return {**data, "keywords": keywords}

    @staticmethod
    def _empty_keyword_payload(item: dict, group: dict, window_days: int) -> dict[str, Any]:
        from app.ingest.builders.monitor_keyword_heat import interaction_weights_payload
        return {
            "keyword": item.get("keyword_text", ""),
            "keyword_id": item.get("keyword_id", ""),
            "topic": item.get("keyword_text", ""),
            "keyword_bucket": group.get("label", "未分类"),
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
            "kw_score": {"total": 0, "heat": 0, "breadth": 0, "richness": 0, "has_heat": False},
            "keyword_heat_metric": {
                "status": "no_data", "method": "xhs_visible_interaction_heat_v1",
                "version": "1.0.0", "window_days": window_days,
                "effective_days": 0, "steady_heat": 0.0, "peak_heat": 0.0,
                "heat_delta_15d": 0.0, "peak_date": None,
                "trend_signal": 0.0, "trend_ratio": 0.0, "trend_label": "观察中",
                "confidence_score": 0, "confidence_level": "insufficient",
                "interaction_weights": interaction_weights_payload(),
                "value_signal": {"score": 0.0, "label": "观察中",
                                 "components": {"heat_trend_ratio": 0.0, "note_supply_trend_ratio": 0.0,
                                                "creator_breadth_trend_ratio": 0.0}},
                "current_interactions": {"likes": 0, "collects": 0, "comments": 0, "shares": 0,
                                          "equivalent": 0.0, "note_count": 0, "creator_count": 0,
                                          "interaction_structure": {"likes_pct": 0.0, "collects_pct": 0.0,
                                                                     "comments_pct": 0.0, "shares_pct": 0.0}},
                "daily_heat_points": [],
            },
            "keyword_read_delta": {
                "status": "insufficient_data", "reason": "xhs_no_reading_proxy",
                "trend_label": "观察中",
            },
        }

    @staticmethod
    def _merge_keyword_states(data: dict, settings: dict[str, dict]) -> dict:
        keywords = []
        for item in data.get("keywords", []):
            merged = dict(item)
            st = settings.get(item.get("keyword_id", ""), {})
            merged["is_pinned"] = bool(st.get("is_pinned", False))
            merged["pin_order"] = st.get("pin_order")
            merged["topic"] = st.get("topic") or merged.get("topic") or merged.get("keyword")
            merged["keyword_bucket"] = st.get("keyword_bucket") or merged.get("keyword_bucket") or "未分类"
            keywords.append(merged)
        return {**data, "keywords": keywords}

    @staticmethod
    def _sort_keywords(keywords: list[dict]) -> list[dict]:
        def metric_number(value: Any) -> float:
            try:
                number = float(value or 0)
            except (TypeError, ValueError):
                return 0.0
            return number if math.isfinite(number) else 0.0

        def sort_key(item: dict):
            km = item.get("keyword_heat_metric") or {}
            if item.get("is_pinned"):
                segment = 0
            elif km.get("effective_days", 0) > 0:
                segment = 1
            else:
                segment = 2
            return (
                segment,
                -metric_number(km.get("steady_heat")),
                -metric_number(km.get("peak_heat")),
                str(item.get("keyword") or ""),
            )

        keywords.sort(key=sort_key)
        return keywords

    # ── bootstrap 构建 ──────────────────────────────

    @staticmethod
    def _build_bootstrap(data: dict) -> dict:
        """构建轻量 bootstrap 载荷。"""
        return {
            "generated_at": data.get("generated_at"),
            "window_days": data.get("window_days"),
            "window_start": data.get("window_start"),
            "window_end": data.get("window_end"),
            "platform": data.get("platform"),
            "account_score_method": data.get("account_score_method"),
            "hexagon_axes": data.get("hexagon_axes"),
            "timeliness_axes": data.get("timeliness_axes"),
            "today_axes": data.get("today_axes"),
            "wso_fit_meta": data.get("wso_fit_meta"),
            "keyword_bucket_options": data.get("keyword_bucket_options"),
            "keyword_scope": data.get("keyword_scope"),
            "keyword_source_total": data.get("keyword_source_total"),
            "pinned_keyword_count": data.get("pinned_keyword_count"),
            "keywords": [_prune_keyword_for_bootstrap(kw) for kw in data.get("keywords", [])],
            "accounts": [_prune_account_for_bootstrap(acct) for acct in data.get("accounts", [])],
        }

    # ── 公共 API ────────────────────────────────────

    def ensure_loaded(self) -> None:
        """确保数据已装载（首次调用时触发）。"""
        if not self._loaded:
            self._load()
        elif self._signatures_changed():
            self._load()

    def get_bootstrap(self) -> tuple[bytes, bytes, str]:
        """返回 (json_bytes, gzip_bytes, etag)。"""
        self.ensure_loaded()
        with self._lock:
            return self._bootstrap_bytes, self._bootstrap_gz, self._bootstrap_etag

    def get_keyword(self, keyword_id: str) -> tuple[bytes | None, str | None]:
        """返回单个完整 keyword 的 (json_bytes, etag)，None 如果不存在。"""
        self.ensure_loaded()
        with self._lock:
            # 检查 LRU 缓存
            if keyword_id in self._detail_keyword_cache:
                etag, body = self._detail_keyword_cache[keyword_id]
                self._detail_keyword_cache.move_to_end(keyword_id)
                return body, etag

            kw = self._keyword_by_id.get(keyword_id) if self._keyword_by_id is not None else None
            if kw is None:
                return None, None

            body = json.dumps(kw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            etag = _etag_of(body)

            # 存入 LRU
            self._detail_keyword_cache[keyword_id] = (etag, body)
            if len(self._detail_keyword_cache) > self._detail_cache_max:
                self._detail_keyword_cache.popitem(last=False)

            return body, etag

    def get_account(self, account_id: str) -> tuple[bytes | None, str | None]:
        """返回单个完整 account 的 (json_bytes, etag)，None 如果不存在。"""
        self.ensure_loaded()
        with self._lock:
            if account_id in self._detail_account_cache:
                etag, body = self._detail_account_cache[account_id]
                self._detail_account_cache.move_to_end(account_id)
                return body, etag

            acct = self._account_by_id.get(account_id) if self._account_by_id is not None else None
            if acct is None:
                return None, None

            body = json.dumps(acct, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            etag = _etag_of(body)

            self._detail_account_cache[account_id] = (etag, body)
            if len(self._detail_account_cache) > self._detail_cache_max:
                self._detail_account_cache.popitem(last=False)

            return body, etag

    def get_metadata(self) -> dict | None:
        """返回顶层 metadata（免测试用）。"""
        self.ensure_loaded()
        with self._lock:
            if self._data is None:
                return None
            return {
                "generated_at": self._data.get("generated_at"),
                "window_days": self._data.get("window_days"),
                "window_start": self._data.get("window_start"),
                "window_end": self._data.get("window_end"),
                "platform": self._data.get("platform"),
                "keyword_count": len(self._data.get("keywords", [])),
                "account_count": len(self._data.get("accounts", [])),
            }

    def get_bootstrap_size_report(self) -> dict:
        """返回 bootstrap 尺寸报告。"""
        self.ensure_loaded()
        with self._lock:
            raw = len(self._bootstrap_bytes)
            gz = len(self._bootstrap_gz)
            return {
                "raw_bytes": raw,
                "gzip_bytes": gz,
                "raw_mb": round(raw / 1024 / 1024, 2),
                "gzip_mb": round(gz / 1024 / 1024, 2),
                "ratio": round(gz / raw * 100, 1) if raw else 0,
            }


# ── 全局单例 ──────────────────────────────────────────
_store: MonitorFastStore | None = None
_store_lock = threading.Lock()


def get_fast_store() -> MonitorFastStore:
    global _store
    if _store is None:
        raise RuntimeError("MonitorFastStore not initialized; call init_fast_store(app) first")
    return _store


def init_fast_store(monitor_data_path: str | Path, sqlite_path: str | Path,
                    keywords_config_path: str | Path) -> MonitorFastStore:
    global _store
    with _store_lock:
        if _store is not None:
            return _store
        _store = MonitorFastStore(monitor_data_path, sqlite_path, keywords_config_path)
        # 首次装载（预热）
        _store.ensure_loaded()
        return _store
