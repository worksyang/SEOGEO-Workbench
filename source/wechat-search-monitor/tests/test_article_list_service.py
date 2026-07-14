from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services import article_list_service as service


def _article(
    article_id: str,
    *,
    reads: int | None = 0,
    likes: int | None = 0,
    on_rank_days: int = 0,
    hit_count: int = 1,
    published_at: str | None = "2026-07-13 12:00:00",
    account_id: str = "acct-1",
    account_score: int = 0,
) -> dict:
    return {
        "article_id": article_id,
        "title": article_id,
        "read_count": reads,
        "like_count": likes,
        "on_rank_days": on_rank_days,
        "hit_count": hit_count,
        "published_at": published_at,
        "account_id": account_id,
        "account_score": account_score,
    }


@pytest.fixture()
def articles(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict] = []
    monkeypatch.setattr(service, "_get_cached_articles", lambda: rows)
    return rows


def ids(result: dict) -> list[str]:
    return [article["article_id"] for article in result["articles"]]


def test_reads_and_likes_put_null_values_last(articles: list[dict]) -> None:
    articles.extend(
        [
            _article("reads-null", reads=None),
            _article("reads-10", reads=10),
            _article("reads-2", reads=2),
        ]
    )
    assert ids(service.list_articles(sort="reads", time_range=0)) == [
        "reads-10",
        "reads-2",
        "reads-null",
    ]

    articles.clear()
    articles.extend(
        [
            _article("likes-null", likes=None),
            _article("likes-10", likes=10),
            _article("likes-2", likes=2),
        ]
    )
    assert ids(service.list_articles(sort="likes", time_range=0)) == [
        "likes-10",
        "likes-2",
        "likes-null",
    ]


def test_on_rank_days_is_primary_and_reads_is_secondary(articles: list[dict]) -> None:
    articles.extend(
        [
            _article("days-3-reads-2", on_rank_days=3, reads=2),
            _article("days-3-reads-10", on_rank_days=3, reads=10),
            _article("days-5", on_rank_days=5, reads=1),
        ]
    )
    assert ids(service.list_articles(sort="onRankDays", time_range=0)) == [
        "days-5",
        "days-3-reads-10",
        "days-3-reads-2",
    ]


def test_hit_count_descending(articles: list[dict]) -> None:
    articles.extend(
        [_article("hits-1", hit_count=1), _article("hits-3", hit_count=3), _article("hits-2", hit_count=2)]
    )
    assert ids(service.list_articles(sort="hitCount", time_range=0)) == [
        "hits-3",
        "hits-2",
        "hits-1",
    ]


def test_publish_time_descending_and_null_last(articles: list[dict]) -> None:
    articles.extend(
        [
            _article("publish-null", published_at=None),
            _article("publish-old", published_at="2026-07-01 12:00:00"),
            _article("publish-new", published_at="2026-07-13 12:00:00"),
        ]
    )
    assert ids(service.list_articles(sort="publishTime", time_range=0)) == [
        "publish-new",
        "publish-old",
        "publish-null",
    ]


def test_account_score_limits_each_account_to_three_and_orders_scores(articles: list[dict]) -> None:
    articles.extend(
        [
            _article("low-1", reads=100, account_id="low", account_score=50),
            _article("low-2", reads=90, account_id="low", account_score=50),
            _article("low-3", reads=80, account_id="low", account_score=50),
            _article("low-4", reads=70, account_id="low", account_score=50),
            _article("high-1", reads=30, account_id="high", account_score=90),
            _article("high-2", reads=20, account_id="high", account_score=90),
        ]
    )
    result = service.list_articles(sort="accountScore", time_range=0)
    assert ids(result) == ["high-1", "high-2", "low-1", "low-2", "low-3"]
    assert len([a for a in result["articles"] if a["account_id"] == "low"]) == 3


def test_today_reads_uses_day_order_then_reads_and_caps_at_100(articles: list[dict]) -> None:
    today = service.datetime.now().replace(microsecond=0)
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    articles.extend(
        [
            _article("yesterday-high", reads=999, published_at=yesterday.strftime("%Y-%m-%d 23:00:00")),
            _article("today-low", reads=1, published_at=today.strftime("%Y-%m-%d 10:00:00")),
            _article("today-high", reads=10, published_at=today.strftime("%Y-%m-%d 09:00:00")),
            _article("old", reads=10000, published_at=day_before.strftime("%Y-%m-%d 12:00:00")),
        ]
    )
    day_order = service.list_articles(sort="todayReads", time_range=0)
    assert ids(day_order)[:3] == ["today-high", "today-low", "yesterday-high"]
    articles.clear()
    articles.extend(
        _article(f"today-{i}", reads=i, published_at=today.strftime("%Y-%m-%d 08:00:00"))
        for i in range(3, 104)
    )
    articles.extend(
        [
            _article("today-low", reads=1, published_at=today.strftime("%Y-%m-%d 10:00:00")),
            _article("today-high", reads=10, published_at=today.strftime("%Y-%m-%d 09:00:00")),
        ]
    )
    result = service.list_articles(sort="todayReads", time_range=0, page_size=200)
    assert result["total"] == 100
    assert ids(result)[:3] == ["today-103", "today-102", "today-101"]
    assert ids(result)[-1] == "today-5"


def test_time_range_remains_an_orthogonal_filter(articles: list[dict]) -> None:
    now = datetime.now()
    articles.extend(
        [
            _article("recent", reads=10, published_at=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
            _article("older", reads=100, published_at=(now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")),
        ]
    )
    result = service.list_articles(sort="todayReads", time_range=1)
    assert ids(result) == ["recent"]
