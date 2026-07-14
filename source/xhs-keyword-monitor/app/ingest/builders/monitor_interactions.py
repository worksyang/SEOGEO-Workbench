"""小红书可见互动的事实聚合。

仅使用 TikHub 搜索快照中公开的点赞、收藏、评论、分享：
- 绝对值：最新搜索快照内该笔记的四项可见互动；
- 增量：同一笔记相邻两次 *不同搜索快照时间* 的最新值减前值；
- 缺少两次观测时不把增量伪造为 0，而是标记为不可计算。

这不是阅读量、曝光量或外部热度，不能替代上述指标。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


INTERACTION_FIELDS = (
    ("liked_count", "liked_delta"),
    ("collected_count", "collected_delta"),
    ("comment_count", "comment_delta"),
    ("shared_count", "shared_delta"),
)


def _parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def build_note_interaction_deltas(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按笔记返回最近一对 TikHub 搜索快照的可见互动增量。"""
    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if not str(row.get("source") or "").startswith("tikhub_xhs"):
            continue
        article_id = str(row.get("article_id") or "")
        captured_at = _parse_at(row.get("captured_at"))
        if article_id and captured_at:
            by_article[article_id].append(row)

    output: dict[str, dict[str, Any]] = {}
    for article_id, rows in by_article.items():
        # 同一时刻可能有重放记录；按 snapshot 去重，保留最后一条。
        points: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("captured_at") or ""), str(row.get("snapshot_id") or ""))
            points[key] = row
        ordered = sorted(points.values(), key=lambda row: (
            _parse_at(row.get("captured_at")) or datetime.min,
            str(row.get("snapshot_id") or ""),
        ))
        latest = ordered[-1]
        prior = ordered[-2] if len(ordered) >= 2 else None
        deltas: dict[str, int | None] = {}
        for absolute_field, delta_field in INTERACTION_FIELDS:
            latest_value = _number(latest.get(absolute_field))
            prior_value = _number(prior.get(absolute_field)) if prior else None
            # 平台偶发回落不被当作负增长；保留 raw 差异供审计。
            raw_delta = latest_value - prior_value if latest_value is not None and prior_value is not None else None
            deltas[delta_field] = max(0, raw_delta) if raw_delta is not None else None
            deltas[f"{delta_field}_raw"] = raw_delta
        output[article_id] = {
            "observation_count": len(ordered),
            "delta_available": prior is not None,
            "baseline_at": prior.get("captured_at") if prior else None,
            "latest_at": latest.get("captured_at"),
            **deltas,
        }
    return output


def attach_interaction_metrics(
    summaries: list[dict[str, Any]],
    articles_by_id: dict[str, dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    """把绝对值与可计算增量挂到博主、博主笔记和关键词笔记展示对象。"""
    by_note = build_note_interaction_deltas(observations)
    for account in summaries:
        article_ids = {
            str(article.get("article_id") or "")
            for article in account.get("articles", [])
            if article.get("article_id")
        }
        visible = {"liked": 0, "collected": 0, "comment": 0, "shared": 0}
        delta = {"liked": 0, "collected": 0, "comment": 0, "shared": 0}
        delta_note_count = 0
        for article_id in article_ids:
            source = articles_by_id.get(article_id, {})
            for raw_key, target_key in (
                ("liked_count", "liked"),
                ("collected_count", "collected"),
                ("comment_count", "comment"),
                ("shared_count", "shared"),
            ):
                number = _number(source.get(raw_key))
                if number is not None:
                    visible[target_key] += number
            change = by_note.get(article_id, {})
            if change.get("delta_available"):
                delta_note_count += 1
            for delta_key, target_key in (
                ("liked_delta", "liked"),
                ("collected_delta", "collected"),
                ("comment_delta", "comment"),
                ("shared_delta", "shared"),
            ):
                number = _number(change.get(delta_key))
                if number is not None:
                    delta[target_key] += number

        account["interaction_metrics"] = {
            "method": "同一笔记相邻两次 TikHub 搜索快照相减；仅统计可见点赞、收藏、评论、分享，不含阅读/曝光。",
            "article_count": len(article_ids),
            "delta_note_count": delta_note_count,
            "absolute": visible,
            "delta": delta,
            "delta_available": delta_note_count > 0,
        }

        def decorate(items: list[dict[str, Any]]) -> None:
            for item in items:
                article_id = str(item.get("article_id") or "")
                change = by_note.get(article_id)
                if change:
                    item["interaction_delta"] = change

        decorate(account.get("articles", []))
        decorate(account.get("best_articles", []))
        for keyword in (account.get("keywords") or {}).values():
            decorate(keyword.get("articles", []))
        for topic in (account.get("topics") or {}).values():
            decorate(topic.get("articles", []))

