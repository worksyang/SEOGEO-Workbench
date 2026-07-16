"""逐字段契约比较工具与 endpoint rule registry。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

MISSING = object()
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)

HTTP_METADATA_KEYS = (
    "status_code", "content_type", "content_encoding", "etag", "cache_control", "vary",
)


@dataclass(frozen=True, slots=True)
class HTTPMetadata:
    """可跨 adapter/BFF 传递的响应元数据。"""

    status_code: int | None = None
    content_type: str | None = None
    content_encoding: str | None = None
    etag: str | None = None
    cache_control: str | None = None
    vary: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "HTTPMetadata":
        value = value or {}
        normalized = {str(key).lower().replace("-", "_"): item for key, item in value.items()}
        headers = normalized.get("headers")
        if isinstance(headers, Mapping):
            normalized.update(
                {str(key).lower().replace("-", "_"): item for key, item in headers.items()}
            )
        return cls(
            status_code=normalized.get("status_code"),
            content_type=normalized.get("content_type"),
            content_encoding=normalized.get("content_encoding"),
            etag=normalized.get("etag"),
            cache_control=normalized.get("cache_control"),
            vary=normalized.get("vary"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in HTTP_METADATA_KEYS}


@dataclass(frozen=True, slots=True)
class PayloadWithHTTPMetadata:
    payload: Any
    metadata: HTTPMetadata = HTTPMetadata()

    @classmethod
    def from_value(
        cls, payload: Any, metadata: HTTPMetadata | Mapping[str, Any] | None = None
    ) -> "PayloadWithHTTPMetadata":
        return cls(payload, metadata if isinstance(metadata, HTTPMetadata) else HTTPMetadata.from_mapping(metadata))


def _pointer_join(pointer: str, token: str) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}" if pointer else f"/{escaped}"


def normalized_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): normalized_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [normalized_value(item) for item in value]
    if isinstance(value, str):
        parsed = _datetime(value)
        if parsed is not None:
            return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return int(value) if float(value).is_integer() else value
    return str(value)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            normalized_value(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractRule:
    pointer: str
    ignore: bool = False
    unordered_by: str | None = None
    severity: str = "error"
    name: str = "default"
    tolerance_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ContractRuleRegistry:
    rules: tuple[ContractRule, ...] = ()

    def for_path(self, pointer: str) -> ContractRule | None:
        for rule in self.rules:
            if rule.pointer == pointer:
                return rule
            if rule.pointer.startswith("/**/") and pointer.endswith(rule.pointer[3:]):
                return rule
            pattern = "^" + re.escape(rule.pointer).replace(r"\*", "[^/]+") + "$"
            if re.match(pattern, pointer):
                return rule
        return None


def default_registry(module_key: str, contract_key: str) -> ContractRuleRegistry:
    rules: list[ContractRule] = []
    page_contracts = {
        "monitor-data", "bootstrap", "bootstrap-summary", "keyword", "keyword-detail",
        "keyword-list", "account", "account-detail", "account-list", "article",
        "article-detail", "article-list", "article-content", "content", "refresh",
        "refresh-status", "history-import", "history-import-status", "search", "summary",
        "detail", "list",
    }
    if contract_key in page_contracts:
        # 页面可见数组显式声明为严格保序；不注册 unordered_by。
        rules.extend(
            ContractRule(pointer=path, name="wechat-page-ordered")
            for path in ("/accounts", "/keywords", "/articles", "/items", "/results")
        )
    if module_key == "wechat-search" and contract_key == "keyword-manage":
        rules.append(
            ContractRule(
                pointer="/updated_at",
                name="keyword-manage-request-clock",
                tolerance_seconds=5.0,
            )
        )
    # 请求审计字段才默认排除；generated_at 等业务时间不忽略。
    # Plan §8.2 仅授权 ``compared_at`` / ``request_id`` 这一类**绝对审计路径**
    # 在 compare 时被吞掉；不允许 ``/**/`` 全深度通配，否则任何深度以
    # ``/request_id`` 或 ``/compared_at`` 结尾的业务字段（例如
    # ``/audit/request_id``、``/search/compared_at``）都会被错误忽略，导致
    # compare 假 matched、掩盖真实业务差异。
    rules.extend(
        ContractRule(pointer=path, ignore=True, name="request-audit")
        for path in (
            "/request_id", "/compared_at",
            "/meta/request_id", "/meta/compared_at",
        )
    )
    return ContractRuleRegistry(tuple(rules))


@dataclass(slots=True)
class DiffResult:
    diffs: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _ISO_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _equal_scalar(
    left: Any,
    right: Any,
    tolerance: float,
    *,
    datetime_tolerance_seconds: float | None = None,
) -> bool:
    if left is MISSING or right is MISSING:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)
    left_dt, right_dt = _datetime(left), _datetime(right)
    if left_dt is not None or right_dt is not None:
        if left_dt is None or right_dt is None:
            return False
        if datetime_tolerance_seconds is not None:
            return abs((left_dt - right_dt).total_seconds()) <= datetime_tolerance_seconds
        return left_dt == right_dt
    return type(left) is type(right) and left == right


def _bounded_json(value: Any, limit: int) -> tuple[str, bool]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    raw = encoded.encode("utf-8")
    if len(raw) <= limit:
        return encoded, False
    return json.dumps(
        {"truncated": True, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ), True


def compare_values(
    legacy: Any,
    hub: Any,
    *,
    registry: ContractRuleRegistry | None = None,
    max_diffs: int = 1000,
    max_value_bytes: int = 8192,
    float_tolerance: float = 1e-6,
) -> DiffResult:
    registry = registry or ContractRuleRegistry()
    result = DiffResult()

    def add(pointer: str, kind: str, left: Any, right: Any, rule: ContractRule | None) -> None:
        if len(result.diffs) >= max_diffs:
            result.truncated = True
            return
        left_json, left_cut = _bounded_json(None if left is MISSING else left, max_value_bytes)
        right_json, right_cut = _bounded_json(None if right is MISSING else right, max_value_bytes)
        result.truncated |= left_cut or right_cut
        result.diffs.append({
            "json_pointer": pointer or "/",
            "kind": kind,
            "legacy_value_json": left_json,
            "hub_value_json": right_json,
            "severity": rule.severity if rule else "error",
            "rule": rule.name if rule else "default",
            "truncated": left_cut or right_cut,
        })

    def walk(left: Any, right: Any, pointer: str = "") -> None:
        rule = registry.for_path(pointer)
        if rule and rule.ignore:
            return
        if left is MISSING or right is MISSING:
            add(pointer, "removed" if right is MISSING else "added", left, right, rule)
        elif isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in sorted(set(left) | set(right), key=str):
                walk(left.get(key, MISSING), right.get(key, MISSING), _pointer_join(pointer, str(key)))
        elif isinstance(left, list) and isinstance(right, list):
            if rule and rule.unordered_by:
                key = rule.unordered_by
                left_map = {item.get(key): item for item in left if isinstance(item, Mapping)}
                right_map = {item.get(key): item for item in right if isinstance(item, Mapping)}
                if len(left_map) == len(left) and len(right_map) == len(right):
                    for item_key in sorted(set(left_map) | set(right_map), key=str):
                        walk(left_map.get(item_key, MISSING), right_map.get(item_key, MISSING),
                             _pointer_join(pointer, str(item_key)))
                    return
            for index, pair in enumerate(zip(left, right)):
                walk(pair[0], pair[1], _pointer_join(pointer, str(index)))
            if len(left) > len(right):
                for index in range(len(right), len(left)):
                    walk(left[index], MISSING, _pointer_join(pointer, str(index)))
            elif len(right) > len(left):
                for index in range(len(left), len(right)):
                    walk(MISSING, right[index], _pointer_join(pointer, str(index)))
        elif isinstance(left, (Mapping, list)) or isinstance(right, (Mapping, list)):
            add(pointer, "changed", left, right, rule)
        elif not _equal_scalar(
            left,
            right,
            float_tolerance,
            datetime_tolerance_seconds=(
                rule.tolerance_seconds if rule else None
            ),
        ):
            add(pointer, "changed", left, right, rule)

    walk(legacy, hub)
    return result


def compare_with_metadata(
    legacy: Any,
    hub: Any,
    *,
    legacy_metadata: HTTPMetadata | Mapping[str, Any] | None = None,
    hub_metadata: HTTPMetadata | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> DiffResult:
    result = compare_values(legacy, hub, **kwargs)
    if legacy_metadata is not None or hub_metadata is not None:
        legacy_metadata = (
            legacy_metadata.to_dict() if isinstance(legacy_metadata, HTTPMetadata) else dict(legacy_metadata or {})
        )
        hub_metadata = (
            hub_metadata.to_dict() if isinstance(hub_metadata, HTTPMetadata) else dict(hub_metadata or {})
        )
        metadata = compare_values(
            legacy_metadata, hub_metadata,
            registry=kwargs.get("registry"),
            max_diffs=max(0, kwargs.get("max_diffs", 1000) - len(result.diffs)),
            max_value_bytes=kwargs.get("max_value_bytes", 8192),
            float_tolerance=kwargs.get("float_tolerance", 1e-6),
        )
        for diff in metadata.diffs:
            diff["json_pointer"] = "/@response" + diff["json_pointer"]
        result.diffs.extend(metadata.diffs)
        result.truncated |= metadata.truncated
    return result
