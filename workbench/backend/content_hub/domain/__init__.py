"""统一内容工作台 · 域层（ID / 模型 / 指标 / 词典）。

依据：
- 全域内容资产与观测架构方案 v3.2 §十一
- 全域内容工作台开发总计划 v1 §4
"""
from .ids import (
    EXTERNAL_NAMESPACES,
    ID_PREFIXES,
    OPS_PREFIXES,
    canonicalize_url,
    content_id_from_canonical_url,
    content_id_from_text,
    generate_ulid_like,
    is_wechat_placeholder,
    is_wechat_url,
    namespace_for,
    normalize_external_id,
    short_id,
)

__all__ = [
    "EXTERNAL_NAMESPACES",
    "ID_PREFIXES",
    "OPS_PREFIXES",
    "canonicalize_url",
    "content_id_from_canonical_url",
    "content_id_from_text",
    "generate_ulid_like",
    "is_wechat_placeholder",
    "is_wechat_url",
    "namespace_for",
    "normalize_external_id",
    "short_id",
]
