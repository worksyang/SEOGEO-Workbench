"""域层 ID / 规范化 / 时间戳校验 — 对应矩阵 T041-T052（部分）。

依据 v3.2 §十一、dev-plan §4.3 / §4.4 / §4.6。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from content_hub.domain.ids import (
    EXTERNAL_NAMESPACES,
    canonicalize_url,
    content_id_from_canonical_url,
    content_id_from_text,
    generate_ulid_like,
    is_wechat_placeholder,
    is_wechat_url,
    namespace_for,
    normalize_external_id,
)
from content_hub.validation.timestamps import (
    is_utc_iso8601,
    parse_utc,
    to_business_date,
    utc_now,
    utc_now_iso,
)


# ── ID 生成与解析 ────────────────────────────────────────


def test_t041_same_url_yields_same_content_id():
    url = "https://mp.weixin.qq.com/s?__biz=MzA&mid=100&idx=1&sn=abc"
    canonical = canonicalize_url(url)
    assert canonical
    assert content_id_from_canonical_url(canonical) == content_id_from_canonical_url(canonical)


def test_t042_canonicalizer_strips_tracking():
    url = "https://example.com/a?__biz=1&from=timeline&chksm=xyz#fragment"
    canonical = canonicalize_url(url)
    assert "__biz" in canonical  # 内容身份参数保留
    assert "from=timeline" not in canonical
    assert "chksm" not in canonical
    assert "#fragment" not in canonical


def test_t043_namespace_validator_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_external_id("made_up_namespace", "anything")


def test_t044_namespace_validator_accepts_known():
    namespace = next(iter(EXTERNAL_NAMESPACES))
    assert namespace in normalize_external_id(namespace, "abc")


def test_t045_ulid_format_includes_prefix():
    for prefix in ("cnt", "job", "dsc"):
        token = generate_ulid_like(prefix)
        assert token.startswith(prefix + "_")
        assert len(token.split("_", 1)[1]) >= 20


def test_t046_content_id_from_text_is_deterministic():
    assert content_id_from_text("seed", "a", "b") == content_id_from_text("seed", "a", "b")


def test_t047_short_id_format():
    from content_hub.domain.ids import short_id

    assert short_id("cnt", "01234567abcdef") == "cnt_0123"


def test_t048_wechat_url_detection():
    assert is_wechat_url("https://mp.weixin.qq.com/s/abc")
    assert is_wechat_url("https://weixin.qq.com/cgi-bin/home")
    assert not is_wechat_url("https://example.com")


def test_t049_wechat_placeholder_detection():
    assert is_wechat_placeholder("")
    assert is_wechat_placeholder("javascript:")
    assert is_wechat_placeholder("#wechat_redirect")
    assert not is_wechat_placeholder("https://mp.weixin.qq.com/s/real")


def test_t050_namespace_for_helper():
    # ID namespace 规范化为下划线小写
    assert namespace_for("wechat-search", "Article") == "wechat_search.article"
    assert namespace_for("GEO", "Source") == "geo.source"


def test_t051_canonicalize_url_scheme_normalization():
    assert canonicalize_url("example.com/page").startswith("https://")


def test_t052_canonicalize_url_strips_trailing_slash():
    assert canonicalize_url("https://example.com/foo/") == "https://example.com/foo"


# ── 时间戳 ──────────────────────────────────────────────


def test_t053_utc_now_format():
    now = utc_now()
    assert is_utc_iso8601(now)
    parsed = parse_utc(now)
    assert parsed.tzinfo == timezone.utc


def test_t054_reject_non_z_suffix():
    with pytest.raises(Exception):
        parse_utc("2026-07-14T08:00:00+00:00")


def test_t055_business_date_shanghai_offset():
    iso = "2026-07-14T17:00:00Z"
    assert to_business_date(iso) == "2026-07-15"


def test_t056_is_utc_iso8601_negative_cases():
    assert not is_utc_iso8601("2026-07-14")
    assert not is_utc_iso8601("")
    assert not is_utc_iso8601(123)


def test_t057_utc_now_iso_alias_matches():
    assert utc_now_iso().endswith("Z")
