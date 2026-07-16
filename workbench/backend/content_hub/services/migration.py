"""BFF 迁移开关与契约对账的共享实现。"""
from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from typing import Any, Callable

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.errors import ConflictError
from content_hub.services.contract_diff import (
    ContractRuleRegistry,
    HTTPMetadata,
    PayloadWithHTTPMetadata,
    compare_with_metadata,
    default_registry,
    payload_hash,
)


MODES = {"legacy", "compare", "hub"}

# HTTP 契约登记不是文档库存：工作台入口用这张表把每个微信操作绑定到
# migration_switches。读端点由各自 resolver 选择响应来源，写/外部端点统一
# 只允许 Hub 执行，避免旧页面绕过运行层。
WECHAT_HTTP_OPERATIONS: tuple[dict[str, str], ...] = (
    *(
        {"method": "GET", "path": path, "contract_key": contract, "kind": "read"}
        for path, contract in (
            ("/api/monitor-data", "monitor-data"),
            ("/api/monitor-data/bootstrap", "bootstrap"),
            ("/api/monitor-data/keyword/{keyword_id}", "keyword"),
            ("/api/monitor-data/account/{account_id}", "account"),
            ("/api/article-content", "article-content"),
            ("/api/article-hit-detail", "article-hit-detail"),
            ("/api/keyword-manage", "keyword-manage"),
            ("/api/keyword-discovery", "keyword-discovery"),
            ("/api/refresh-status/{job_id}", "refresh-status"),
            ("/api/refresh-all/status", "refresh-all-status"),
            ("/api/refresh-all/history", "refresh-all-history"),
            ("/api/scheduler/status", "scheduler-status"),
            ("/api/articles", "articles"),
            ("/api/articles/accounts", "articles-accounts"),
            ("/api/agent/manifest", "agent-manifest"),
            ("/api/agent/daily-brief", "agent-daily-brief"),
            ("/api/agent/metric-dictionary", "agent-metric-dictionary"),
            ("/api/agent/evidence/{evidence_id}", "agent-evidence"),
            ("/api/penalty-signals", "penalty-signals"),
            ("/api/account-aliases", "account-aliases"),
            ("/api/article-cover-image", "article-cover-image"),
            ("/api/aidso/keyword-heat", "aidso-keyword-heat-get"),
        )
    ),
    *(
        {"method": method, "path": path, "contract_key": contract, "kind": "write"}
        for method, path, contract in (
            ("POST", "/api/keywords/{keyword_id}/refresh", "keywords-refresh"),
            ("POST", "/api/refresh-all/cancel", "refresh-all-cancel"),
            ("POST", "/api/refresh-all", "refresh-all"),
            ("POST", "/api/scheduler/config", "scheduler-config"),
            ("POST", "/api/scheduler/trigger", "scheduler-trigger"),
            ("POST", "/api/article-covers", "article-covers"),
            ("POST", "/api/aidso/keyword-heat", "aidso-keyword-heat-post"),
            ("POST", "/api/keywords/{keyword_id}/pin", "keyword-pin"),
            ("POST", "/api/keywords/{keyword_id}/unpin", "keyword-unpin"),
            ("POST", "/api/keywords/{keyword_id}/topic", "keyword-topic"),
            ("POST", "/api/keywords/{keyword_id}/note", "keyword-note"),
            ("POST", "/api/keywords/{keyword_id}/bucket", "keyword-bucket"),
            ("POST", "/api/keyword-manage/groups", "group-create"),
            ("PATCH", "/api/keyword-manage/groups/{group_id}", "group-update"),
            ("DELETE", "/api/keyword-manage/groups/{group_id}", "group-delete"),
            ("POST", "/api/keyword-manage/keywords", "keyword-create"),
            ("PATCH", "/api/keyword-manage/keywords/{keyword_id}", "keyword-update"),
            ("DELETE", "/api/keyword-manage/keywords/{keyword_id}", "keyword-archive"),
            ("PATCH", "/api/keyword-manage/keywords/{keyword_id}/refresh-policy", "keyword-refresh-policy"),
            ("PATCH", "/api/keyword-manage/keywords/{keyword_id}/commercial-value", "keyword-commercial-value"),
            ("PATCH", "/api/keyword-manage/keywords/{keyword_id}/auto-archive-lock", "keyword-auto-archive-lock"),
        )
    ),
    # v1 路由复用已登记的读取/写入契约；history-import 是本地 Hub-only
    # 操作，不进入 MigrationResolver，缺少 switch 时也绝不回退旧 HTTP。
    {"method": "GET", "path": "/api/v1/wechat/bootstrap", "contract_key": "bootstrap", "kind": "read"},
    {"method": "GET", "path": "/api/v1/wechat/keywords/{keyword_id}", "contract_key": "keyword", "kind": "read"},
    {"method": "GET", "path": "/api/v1/wechat/articles/{article_id}", "contract_key": "article-hit-detail", "kind": "read"},
    {"method": "GET", "path": "/api/v1/wechat/articles/{article_id}/content", "contract_key": "article-content", "kind": "read"},
    {"method": "POST", "path": "/api/v1/wechat/keywords/{keyword_id}/refresh", "contract_key": "keywords-refresh", "kind": "write"},
    {"method": "GET", "path": "/api/v1/wechat/refresh-status/{job_id}", "contract_key": "refresh-status", "kind": "read"},
    {"method": "POST", "path": "/api/v1/wechat/import", "contract_key": "history-import", "kind": "hub-only"},
)

# v1.0 原子总开关只管理原微信页面的 22 个 GET 契约；旧写入/外部动作
# 保持 Hub-only。v1 路由复用这些 contract_key，但不重复计入切换清单。
WECHAT_CUTOVER_READ_CONTRACTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        item["contract_key"]
        for item in WECHAT_HTTP_OPERATIONS
        if item["kind"] == "read"
        and item["path"].startswith("/api/")
        and not item["path"].startswith("/api/v1/")
    )
)
WECHAT_FIXED_HUB_CONTRACTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        item["contract_key"]
        for item in WECHAT_HTTP_OPERATIONS
        if item["kind"] == "write"
        and item["path"].startswith("/api/")
        and not item["path"].startswith("/api/v1/")
    )
)


def wechat_http_operation(method: str, path: str) -> dict[str, str] | None:
    """按实际 HTTP 方法和路径解析微信迁移契约。"""
    import re

    for item in WECHAT_HTTP_OPERATIONS:
        pattern = re.escape(item["path"])
        pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", pattern)
        if item["method"] == method.upper() and re.fullmatch(pattern, path):
            return item
    return None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint_digest(value: str) -> str:
    """审计只保留指纹摘要，避免把调用方误传的请求内容落盘。"""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _audit_error(value: dict[str, Any] | None) -> dict[str, str] | None:
    """审计保留错误类型与消息摘要，不回显可能包含正文/凭据的异常文本。"""
    if not value:
        return None
    return {
        "type": str(value.get("type", "Exception")),
        "message_sha256": _fingerprint_digest(str(value.get("message", ""))),
    }


def _coerce_response(
    value: Any,
    metadata: HTTPMetadata | dict[str, Any] | None,
) -> PayloadWithHTTPMetadata:
    if isinstance(value, PayloadWithHTTPMetadata) and metadata is not None:
        # Explicit hub defaults must not erase a resolver's known HTTP error
        # contract (for example a Hub-side 404 represented as a payload).
        supplied = value.metadata
        if supplied.status_code is not None and supplied.status_code >= 400:
            metadata = supplied
    response = (
        value
        if isinstance(value, PayloadWithHTTPMetadata) and metadata is None
        else PayloadWithHTTPMetadata.from_value(
            value.payload if isinstance(value, PayloadWithHTTPMetadata) else value,
            metadata if metadata is not None else (
                value.metadata if isinstance(value, PayloadWithHTTPMetadata) else None
            ),
        )
    )
    if response.metadata.etag != "__AUTO__":
        return response
    # ``legacy_read_router._json_response`` computes core ETags from the exact
    # response bytes, not from the normalized comparison hash.  Materialize
    # that value before comparison so __AUTO__ is never treated as a wildcard.
    raw = json.dumps(
        response.payload, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode()
    varies_encoding = any(
        item.strip().lower() == "accept-encoding"
        for item in (response.metadata.vary or "").split(",")
    )
    if not varies_encoding and (response.metadata.content_type or "").lower().startswith(
        "application/json"
    ):
        raw += b"\n"
    etag = 'W/"' + hashlib.md5(raw).hexdigest() + '"'
    return PayloadWithHTTPMetadata.from_value(
        response.payload,
        HTTPMetadata(
            status_code=response.metadata.status_code,
            content_type=response.metadata.content_type,
            content_encoding=response.metadata.content_encoding,
            etag=etag,
            cache_control=response.metadata.cache_control,
            vary=response.metadata.vary,
        ),
    )


class MigrationResolver:
    """按 module/contract 读取 migration_switches，并执行三模式读取。"""

    def __init__(self, settings: Any, *, module_key: str, contract_key: str) -> None:
        self.settings = settings
        self.module_key = module_key
        self.contract_key = contract_key

    def mode(self) -> str:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT data_mode FROM migration_switches
                WHERE module_key=? AND contract_key=? AND enabled=1
                """,
                (self.module_key, self.contract_key),
            ).fetchone()
        return str(row["data_mode"]) if row and row["data_mode"] in MODES else "legacy"

    def describe(self) -> dict[str, str]:
        return {
            "module_key": self.module_key,
            "contract_key": self.contract_key,
            "data_mode": self.mode(),
        }

    def compare(
        self,
        *,
        request_fingerprint: str,
        legacy: Callable[[], Any],
        hub: Callable[[], Any],
        registry: ContractRuleRegistry | None = None,
        legacy_metadata: HTTPMetadata | dict[str, Any] | None = None,
        hub_metadata: HTTPMetadata | dict[str, Any] | None = None,
        max_diffs: int = 1000,
        max_value_bytes: int = 8192,
        float_tolerance: float = 1e-6,
        preserve_response: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        legacy_value = hub_value = None
        legacy_response = hub_response = None
        legacy_error = hub_error = None
        try:
            legacy_response = _coerce_response(legacy(), legacy_metadata)
            legacy_value = legacy_response.payload
        except Exception as exc:  # 对账必须保留真实错误，不能吞成成功
            legacy_error = {"type": type(exc).__name__, "message": str(exc)}
        try:
            hub_response = _coerce_response(hub(), hub_metadata)
            hub_value = hub_response.payload
        except Exception as exc:
            hub_error = {"type": type(exc).__name__, "message": str(exc)}

        if legacy_error:
            status = "legacy_error"
        elif hub_error:
            status = "hub_error"
        else:
            status = "matched" if payload_hash(legacy_value) == payload_hash(hub_value) else "different"
        comparison = self._record_comparison(
            request_fingerprint=request_fingerprint,
            legacy_value=legacy_value,
            hub_value=hub_value,
            status=status,
            legacy_error=legacy_error,
            hub_error=hub_error,
            registry=registry or default_registry(self.module_key, self.contract_key),
            legacy_metadata=legacy_metadata,
            hub_metadata=hub_metadata,
            max_diffs=max_diffs,
            max_value_bytes=max_value_bytes,
            float_tolerance=float_tolerance,
            legacy_response=legacy_response,
            hub_response=hub_response,
        )
        if legacy_error:
            raise ConflictError(f"{self.module_key}/{self.contract_key} legacy 读取失败：{legacy_error['message']}")
        selected = legacy_response if preserve_response and legacy_response is not None else legacy_value
        return selected, {"mode": "compare", "comparison": comparison}

    def read(
        self,
        *,
        request_fingerprint: str,
        legacy: Callable[[], Any],
        hub: Callable[[], Any],
        legacy_metadata: HTTPMetadata | dict[str, Any] | None = None,
        hub_metadata: HTTPMetadata | dict[str, Any] | None = None,
        registry: ContractRuleRegistry | None = None,
        max_diffs: int = 1000,
        max_value_bytes: int = 8192,
        float_tolerance: float = 1e-6,
        preserve_response: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        selected = self.mode()
        if selected == "legacy":
            response = _coerce_response(legacy(), legacy_metadata)
            return (response if preserve_response else response.payload), {"mode": "legacy"}
        if selected == "hub":
            try:
                value = hub()
            except NotImplementedError as exc:
                raise ConflictError(
                    f"{self.module_key}/{self.contract_key} 的 hub 模式尚未实现：{exc}"
                ) from exc
            response = _coerce_response(value, hub_metadata)
            return (response if preserve_response else response.payload), {"mode": "hub"}
        return self.compare(
            request_fingerprint=request_fingerprint, legacy=legacy, hub=hub,
            legacy_metadata=legacy_metadata, hub_metadata=hub_metadata,
            registry=registry, max_diffs=max_diffs, max_value_bytes=max_value_bytes,
            float_tolerance=float_tolerance, preserve_response=preserve_response,
        )

    def require_mode(self, expected: str) -> None:
        selected = self.mode()
        if selected != expected:
            raise ConflictError(
                f"{self.module_key}/{self.contract_key} 当前为 {selected} 模式，不能执行 {expected} 专用操作。"
            )

    def _record_comparison(
        self,
        *,
        request_fingerprint: str,
        legacy_value: Any,
        hub_value: Any,
        status: str,
        legacy_error: dict[str, Any] | None,
        hub_error: dict[str, Any] | None,
        registry: ContractRuleRegistry,
        legacy_metadata: HTTPMetadata | dict[str, Any] | None,
        hub_metadata: HTTPMetadata | dict[str, Any] | None,
        max_diffs: int,
        max_value_bytes: int,
        float_tolerance: float,
        legacy_response: PayloadWithHTTPMetadata | None,
        hub_response: PayloadWithHTTPMetadata | None,
    ) -> dict[str, Any]:
        now = utc_now()
        legacy_hash = None if legacy_error else payload_hash(legacy_value)
        hub_hash = None if hub_error else payload_hash(hub_value)
        rules_summary = [
            {
                "pointer": rule.pointer,
                "ignore": rule.ignore,
                "unordered_by": rule.unordered_by,
                "name": rule.name,
                "tolerance_seconds": rule.tolerance_seconds,
            }
            for rule in registry.rules
        ]
        diff = {
            "legacy_error": legacy_error,
            "hub_error": hub_error,
            "hash_equal": legacy_hash == hub_hash if legacy_hash and hub_hash else False,
            "comparison_basis": "payload+http_metadata",
            "rules": rules_summary,
            "tolerance": {"float_abs": float_tolerance, "iso_datetime": "same-instant"},
            "diff_count_semantics": "field_diffs_only; errors_are_summary_only",
        }
        if legacy_response is not None:
            diff["legacy_metadata"] = legacy_response.metadata.to_dict()
        if hub_response is not None:
            diff["hub_metadata"] = hub_response.metadata.to_dict()
        comparison_diff = None
        if not legacy_error and not hub_error:
            comparison_diff = compare_with_metadata(
                legacy_value,
                hub_value,
                registry=registry,
                legacy_metadata=legacy_response.metadata if legacy_response else legacy_metadata,
                hub_metadata=hub_response.metadata if hub_response else hub_metadata,
                max_diffs=max_diffs,
                max_value_bytes=max_value_bytes,
                float_tolerance=float_tolerance,
            )
            diff["diff_count"] = len(comparison_diff.diffs)
            diff["truncated"] = comparison_diff.truncated
            status = "matched" if not comparison_diff.diffs and not comparison_diff.truncated else "different"
        diff_count = len(comparison_diff.diffs) if comparison_diff else 0
        diffs_truncated = int(comparison_diff.truncated) if comparison_diff else 0
        comparison_id = generate_ulid_like("cmp")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    con.execute(
                        """
                        INSERT INTO contract_comparisons(
                            comparison_id,module_key,contract_key,request_fingerprint,
                            legacy_hash,hub_hash,status,diff_json,compared_at,
                            diff_count,diffs_truncated
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            comparison_id,
                            self.module_key,
                            self.contract_key,
                            request_fingerprint[:200],
                            legacy_hash,
                            hub_hash,
                            status,
                            json.dumps(diff, ensure_ascii=False, sort_keys=True, default=str),
                            now,
                            diff_count,
                            diffs_truncated,
                        ),
                    )
                    if comparison_diff:
                        con.executemany(
                            """
                            INSERT INTO contract_comparison_diffs(
                                comparison_id,json_pointer,kind,legacy_value_json,
                                hub_value_json,severity,rule,truncated
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            [
                                (
                                    comparison_id,
                                    item["json_pointer"],
                                    item["kind"],
                                    item["legacy_value_json"],
                                    item["hub_value_json"],
                                    item["severity"],
                                    item["rule"],
                                    int(item["truncated"]),
                                )
                                for item in comparison_diff.diffs
                            ],
                        )
                    con.execute(
                        """
                        INSERT INTO audit_log(
                            audit_id, occurred_at, actor_type, actor_id, action,
                            subject_type, subject_id, request_id, outcome, details_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"audit_{comparison_id}",
                            now,
                            "system",
                            "content_hub",
                            "migration.compare",
                            "contract_comparison",
                            comparison_id,
                            None,
                            "failed" if (legacy_error or hub_error) else "succeeded",
                            json.dumps(
                                {
                                    "module": self.module_key,
                                    "contract": self.contract_key,
                                    "request_fingerprint": _fingerprint_digest(request_fingerprint),
                                    "comparison_id": comparison_id,
                                    "status": status,
                                    "diff_count": diff_count,
                                    "diffs_truncated": bool(diffs_truncated),
                                    "legacy_error": _audit_error(legacy_error),
                                    "hub_error": _audit_error(hub_error),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            ),
                        ),
                    )
        return {
            "comparison_id": comparison_id,
            "status": status,
            "legacy_hash": legacy_hash,
            "hub_hash": hub_hash,
            "compared_at": now,
            "diff_count": diff_count,
            "diffs_truncated": bool(diffs_truncated),
            "hash_equal": diff["hash_equal"],
            "comparison_basis": diff["comparison_basis"],
            "rules": diff["rules"],
            "tolerance": diff["tolerance"],
        }
