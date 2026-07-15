"""统一内容工作台 · 域层 / ID 规则。

依据：
- 全域内容资产与观测架构方案 v3.2 §十一
- 全域内容工作台开发总计划 v1 §4.3

约束：
- ID 必须可由外部可观察事实生成（deterministic）；
- 合并不能删除旧 ID（保留 identity_merge_map）；
- 所有标准时间字段统一 ISO 8601 UTC，文件名与展示中再切换到业务时区。
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Final

# 与 dev-plan §4.3 一致的 12 个 ID 前缀。
ID_PREFIXES: Final[tuple[str, ...]] = (
    "cnt",  # content
    "crt",  # creator
    "dsc",  # discovery
    "snp",  # snapshot
    "hit",  # hit
    "obs",  # observation
    "cmt",  # comment
    "cev",  # comment event
    "ans",  # GEO answer
    "rel",  # relation
    "sig",  # signal
    "job",  # production job
)

# 工程前缀：摄取、审计与各模块运行层的命令/版本/任务实体。
OPS_PREFIXES: Final[tuple[str, ...]] = (
    "batch",
    "corr",
    "pub",
    "evt",
    "mat",
    "wfv",  # wiki file version
    "cmd",  # hub command run
    "sw",   # migration switch
    "cmp",  # contract comparison
    "dwr",  # dual-write receipt
    "srj",  # search refresh job
    "sri",  # search refresh item
    "mpj",  # mp collection job
    "mpe",  # mp collection event
    "wes",  # wiki edit session
    "wij",  # wiki image job
    "wic",  # wiki OCR record
    "wmp",  # WritingMoney project
    "wme",  # WritingMoney project event
    "wmm",  # WritingMoney material
    "wmt",  # WritingMoney template
    "wpl",  # WritingMoney plan
    "wpk",  # WritingMoney package
    "wmb",  # WritingMoney batch
    "wmk",  # WritingMoney batch keyword
    "wmd",  # WritingMoney draft
    "pqu",  # publish queue
    "pqi",  # publish queue item
    "pev",  # publish event
)

# 已注册的外部 ID 命名空间（v3.2 §十一 + dev-plan §5）。
EXTERNAL_NAMESPACES: Final[dict[str, str]] = {
    "wechat.article_url": "微信文章规范化 URL",
    "wechat.biz_article": "微信 biz + 文章 mid",
    "wechat.mp_account": "微信公众号 fid",
    "xhs.note_id": "小红书 note_id",
    "xhs.user_id": "小红书 user_id",
    "geopromax.source_id": "GEO 来源 20 位 sha256",
    "geopromax.answer_id": "GEO 回答 20 位 sha256",
    "wechat.article.account_feed": "公众号监控内部 article_id",
    "wechat.account.wer": "WeRSS account",
    "mother.frontmatter_asset_id": "Wiki 母文章 asset_id",
    "publish.account_id": "发布账号本地 id",
}

_ULID_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_WECHAT_CANONICAL_RE = re.compile(r"^https?://(?:mp\.weixin\.qq\.com|weixin\.qq\.com|mp\.weixinbridge\.com)/")
_TRACKING_PARAMS: Final[set[str]] = {
    "chksm",
    "scene",
    "clicktime",
    "enterid",
    "from",
    "from_group",
    "version",
    "pass_ticket",
    "ascene",
    "devicetype",
    "lang",
    "uin",
    "key",
    "sticket",
    "style_type",
    "fontsize",
    "subscene",
    "exportkey",
    "trans",
    "fasttmpl",
}
_PLACEHOLDER_HOSTS: Final[set[str]] = {"", "javascript", "about:blank"}


def generate_ulid_like(prefix: str) -> str:
    """生成 26 字符 ULID 风格 ID，前缀必须在 ID_PREFIXES 或 OPS_PREFIXES 内。"""
    if prefix not in ID_PREFIXES and prefix not in OPS_PREFIXES:
        raise ValueError(f"未知 ID 前缀：{prefix}")
    timestamp_ms = int(time.time() * 1000)
    time_chars = _encode_crockford(timestamp_ms, 10)
    random_bits = uuid.uuid4().bytes
    rand_chars = ""
    for byte in random_bits:
        rand_chars += _ULID_ALPHABET[byte & 0x1F]
        if len(rand_chars) >= 16:
            break
    return f"{prefix}_{time_chars}{rand_chars[:16]}"


def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def canonicalize_url(url: str) -> str:
    """规范化 URL：scheme→https、删除 fragment、删除末尾斜杠、删除受控追踪参数。

    保留可能影响内容身份的查询参数（如 mp.weixin.qq.com 的 __biz / mid / idx / sn）。
    """
    if not isinstance(url, str):
        raise ValueError("url 必须为字符串")
    candidate = url.strip()
    if not candidate or candidate.lower() in _PLACEHOLDER_HOSTS:
        return ""
    candidate = re.sub(r"^javascript:", "", candidate, flags=re.IGNORECASE)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", candidate):
        candidate = "https://" + candidate
    protocol_end = candidate.find("://") + 3
    rest = candidate[protocol_end:]
    fragment_index = rest.find("#")
    query_index = rest.find("?")
    if fragment_index != -1 and (query_index == -1 or fragment_index < query_index):
        rest = rest[:fragment_index]
        query_index = -1
    if query_index == -1:
        cleaned = rest
    else:
        path_end, _, query = rest.partition("?")
        params = []
        for pair in query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            if key.lower() in _TRACKING_PARAMS:
                continue
            params.append(f"{key}={value}" if value else key)
        cleaned = path_end
        if params:
            cleaned = cleaned + "?" + "&".join(params)
    if cleaned.endswith("/") and cleaned.count("/") > 1:
        cleaned = cleaned.rstrip("/")
    return "https://" + cleaned


def is_wechat_placeholder(url: str) -> bool:
    """识别微信系统的 placeholder / 占位 URL，方便后续回填。"""
    if not isinstance(url, str):
        return False
    text = url.strip().lower()
    if not text:
        return True
    if text in _PLACEHOLDER_HOSTS:
        return True
    if "javascript:" in text:
        return True
    if "#wechat_redirect" in text or "wechat_redirect" in text:
        return True
    return False


def is_wechat_url(url: str) -> bool:
    """判断是否来自微信域。"""
    if not isinstance(url, str):
        return False
    return bool(_WECHAT_CANONICAL_RE.match(url))


def content_id_from_canonical_url(canonical_url: str, namespace: str = "url") -> str:
    """由规范化 URL + 命名空间生成 deterministic content_id（cnt_ + 16 hex）。"""
    if not canonical_url:
        raise ValueError("canonical_url 不能为空")
    material = f"{namespace}::{canonical_url}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"cnt_{digest}"


def content_id_from_text(*parts: str) -> str:
    """由多段文本拼接生成内容 ID（用于 ai_answer / mother 等不依赖 URL 的对象）。"""
    material = "::".join(parts).encode("utf-8")
    return f"cnt_{hashlib.sha256(material).hexdigest()[:16]}"


def short_id(prefix: str, hash_value: str) -> str:
    """从 16/20 位 hash 截取短码，用于文件名等展示形态。"""
    if not hash_value:
        raise ValueError("hash 不能为空")
    return f"{prefix}_{hash_value[:4].upper()}"


def normalize_external_id(namespace: str, external_id: str) -> str:
    """统一外部 ID 字符串与命名空间。"""
    if not namespace:
        raise ValueError("namespace 不能为空")
    if namespace not in EXTERNAL_NAMESPACES and namespace not in {"unknown"}:
        raise ValueError(f"未注册命名空间：{namespace}")
    return f"{namespace}={external_id}"


def namespace_for(system: str, kind: str) -> str:
    """根据适配器 + 内容种类生成命名空间。"""
    system_token = re.sub(r"[^a-z0-9_]", "_", system.lower())
    kind_token = re.sub(r"[^a-z0-9_]", "_", kind.lower())
    return f"{system_token}.{kind_token}"
