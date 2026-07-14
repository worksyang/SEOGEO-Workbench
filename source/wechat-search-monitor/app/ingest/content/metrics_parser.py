from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ArticleMetrics:
    read_count: Optional[int] = None
    friends_follow_count: Optional[int] = None
    like_count: Optional[int] = None
    original_article_count: Optional[int] = None


_RE_READ = re.compile(r"阅读(\d+)")
_RE_FRIENDS_FOLLOW = re.compile(r"(\d+)个朋友关注")
_RE_ZAN = re.compile(r"赞(\d+)")
_RE_GUANZHU_RECOMMEND = re.compile(r"关注(\d+)推荐")
_RE_ORIGINAL = re.compile(r"(?<![A-Za-z\d_])(\d{1,5})篇原创内容")


def extract_metrics(content_path: Path) -> ArticleMetrics:
    """从微信文章 Markdown 文件底部提取互动指标。

    底部结构分两层：
    - pre_footer（# 分隔符之前）：含阅读量、原创文章数
    - footer（# 分隔符到 EndFragment 之间）：含朋友关注数、点赞数
    """
    try:
        text = content_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ArticleMetrics()

    end_idx = text.rfind("EndFragment")
    if end_idx == -1:
        footer = text[-800:]
        pre_footer = text[-3000:-800] if len(text) > 3000 else ""
    else:
        hash_idx = text.rfind("#", max(0, end_idx - 500), end_idx)
        if hash_idx == -1:
            footer = text[max(0, end_idx - 500):end_idx]
            pre_footer = text[max(0, end_idx - 3000):max(0, end_idx - 500)]
        else:
            footer = text[hash_idx:end_idx]
            pre_footer = text[max(0, hash_idx - 2000):hash_idx]

    metrics = ArticleMetrics()

    # 阅读量 — 取最后一个匹配（最接近底部的）
    m = _RE_READ.findall(pre_footer)
    if m:
        metrics.read_count = int(m[-1])

    # 原创文章数 — 取最后一个匹配（公众号简介可能出现多次）
    m = _RE_ORIGINAL.findall(pre_footer)
    if m:
        metrics.original_article_count = int(m[-1])

    # 朋友关注数
    m = _RE_FRIENDS_FOLLOW.findall(footer)
    if m:
        metrics.friends_follow_count = int(m[-1])

    # 点赞数 — 两种互斥策略，footer 内零冲突
    # 策略1: "赞N"（底部信息块有"赞"文字时）
    # 策略2: "关注N推荐"（底部信息块无"赞"文字时，N 紧跟在"关注"和"推荐"之间）
    m_zan = _RE_ZAN.findall(footer)
    if m_zan:
        metrics.like_count = int(m_zan[-1])
    else:
        m_gr = _RE_GUANZHU_RECOMMEND.findall(footer)
        if m_gr:
            metrics.like_count = int(m_gr[-1])

    return metrics