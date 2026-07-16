from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.features.wechat.service import WechatService
from content_hub.repositories.wechat_legacy import WechatLegacyRepository


REAL_FREEZE = (
    Path(__file__).resolve().parents[2]
    / "data/migration/wechat/freeze_20260716T024524+0800/payload"
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _seed_article_compat(settings) -> None:
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                for account_id, name, headimg, score in (
                    ("acct_a", "账号甲", None, 90),
                    ("acct_b", "账号乙", "https://img/b", 80),
                ):
                    con.execute(
                        """
                        INSERT INTO creators(
                            creator_id,canonical_name,platform,external_id,
                            first_seen_at,updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            account_id,
                            name,
                            "wechat-search",
                            account_id,
                            "2026-07-01T00:00:00",
                            "2026-07-16T00:00:00",
                            _json({
                                "account_id": account_id,
                                "canonical_name": name,
                                "headimg_url": headimg,
                            }),
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO wechat_legacy_projections(
                            projection_id,projection_kind,subject_id,payload_json,
                            source_hash,source_manifest_id,source_ref,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"account:{account_id}",
                            "account",
                            account_id,
                            _json({
                                "account_id": account_id,
                                "name": name,
                                "headimg_url": headimg,
                                "score": score,
                            }),
                            "source",
                            "manifest",
                            "manifest://wechat",
                            "2026-07-16T00:00:00",
                        ),
                    )

                for keyword_id, text in (
                    ("kw_a", "关键词甲"),
                    ("kw_b", "关键词乙"),
                ):
                    con.execute(
                        """
                        INSERT INTO keywords(
                            keyword_id,platform,keyword,status,
                            first_seen_at,updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            keyword_id,
                            "wechat-search",
                            text,
                            "active",
                            "2026-07-01T00:00:00",
                            "2026-07-16T00:00:00",
                            "{}",
                        ),
                    )

                articles = (
                    {
                        "article_id": "art_a",
                        "normalized_url": "https://mp.weixin.qq.com/s/duplicate",
                        "raw_url": "https://mp.weixin.qq.com/s/duplicate",
                        "title": "同分文章甲",
                        "account_id": "acct_a",
                        "published_at": "2026-07-15T10:00:00",
                        "read_count": 100,
                        "like_count": 10,
                        "cover_url": "https://cover/a",
                    },
                    {
                        "article_id": "art_b",
                        "normalized_url": "https://mp.weixin.qq.com/s/duplicate",
                        "raw_url": "https://mp.weixin.qq.com/s/duplicate",
                        "title": "同分文章乙",
                        "account_id": "acct_b",
                        "published_at": "2026-07-15T10:00:00",
                        "read_count": 100,
                        "like_count": 10,
                    },
                    {
                        "article_id": "art_old",
                        "raw_url": "placeholder://账号甲/旧文章",
                        "title": "旧文章",
                        "account_id": "acct_a",
                        "published_at": "2026-06-01T10:00:00",
                        "read_count": 50,
                        "like_count": None,
                    },
                    {
                        "article_id": "art_missing",
                        "raw_url": "",
                        "title": "缺字段文章",
                        "account_id": "acct_b",
                        "published_at": None,
                        "read_count": None,
                        "like_count": None,
                    },
                )
                con.execute(
                    """
                    INSERT INTO contents(
                        content_id,content_type,title,canonical_url,creator_id,
                        published_at,first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "canonical_duplicate",
                        "external_article",
                        "canonical",
                        "https://mp.weixin.qq.com/s/duplicate",
                        "acct_a",
                        "2026-07-15T02:00:00Z",
                        "2026-07-15T02:00:00Z",
                        "2026-07-16T00:00:00Z",
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO contents(
                        content_id,content_type,title,creator_id,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "art_missing",
                        "external_article",
                        "缺字段文章",
                        "acct_b",
                        "2026-07-16T00:00:00Z",
                        "2026-07-16T00:00:00Z",
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO contents(
                        content_id,content_type,title,creator_id,published_at,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "art_old",
                        "external_article",
                        "旧文章",
                        "acct_a",
                        "2026-06-01T02:00:00Z",
                        "2026-06-01T02:00:00Z",
                        "2026-07-16T00:00:00Z",
                        "{}",
                    ),
                )
                for article in articles:
                    content_id = (
                        "canonical_duplicate"
                        if article["article_id"] in {"art_a", "art_b"}
                        else article["article_id"]
                    )
                    con.execute(
                        """
                        INSERT INTO content_identifiers(
                            namespace,external_id,content_id,first_seen_at,payload_json
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            "wechat_article",
                            article["article_id"],
                            content_id,
                            "2026-07-16T00:00:00",
                            _json(article),
                        ),
                    )

                snapshots = (
                    (
                        "snap_a1",
                        "kw_a",
                        "2026-07-15",
                        "微信搜索结果/批量抓取/web_20260715_010101/001_甲/a.md",
                    ),
                    (
                        "snap_a2",
                        "kw_b",
                        "2026-07-14",
                        "微信搜索结果/批量抓取/web_20260714_010101/001_乙/b.md",
                    ),
                    (
                        "snap_b",
                        "kw_b",
                        "2026-07-15",
                        "微信搜索结果/批量抓取/web_20260715_020202/001_乙/c.md",
                    ),
                    (
                        "snap_old",
                        "kw_a",
                        "2026-06-01",
                        "微信搜索结果/old.md",
                    ),
                    (
                        "snap_missing",
                        "kw_a",
                        "2026-07-16",
                        "微信搜索结果/missing.md",
                    ),
                )
                for snapshot_id, keyword_id, day, source_path in snapshots:
                    con.execute(
                        """
                        INSERT INTO search_snapshots(
                            snapshot_id,platform,keyword,keyword_id,captured_at,
                            result_count,payload_json
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot_id,
                            "wechat-search",
                            keyword_id,
                            keyword_id,
                            f"{day}T00:00:00Z",
                            1,
                            _json({
                                "snapshot_id": snapshot_id,
                                "keyword_id": keyword_id,
                                "captured_at": f"{day}T08:00:00",
                                "snapshot_date": day,
                                "snapshot_time": "08:00",
                                "source_file_path": source_path,
                            }),
                        ),
                    )
                for index, (snapshot_id, article_id, content_id) in enumerate((
                    ("snap_a1", "art_a", "canonical_duplicate"),
                    ("snap_a2", "art_a", "canonical_duplicate"),
                    ("snap_b", "art_b", "canonical_duplicate"),
                    ("snap_old", "art_old", "art_old"),
                    ("snap_missing", "art_missing", "art_missing"),
                ), 1):
                    con.execute(
                        """
                        INSERT INTO search_hits(
                            hit_id,snapshot_id,rank,content_id,payload_json
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            f"hit_{index}",
                            snapshot_id,
                            1,
                            content_id,
                            _json({
                                "hit_id": f"hit_{index}",
                                "snapshot_id": snapshot_id,
                                "article_id": article_id,
                                "rank": 1,
                            }),
                        ),
                    )

                detail = {
                    "article": {"article_id": "art_a", "title": "同分文章甲"},
                    "account": {
                        "account_id": "acct_a",
                        "name": "",
                        "headimg_url": "",
                        "wechat_biz": "",
                    },
                    "url_profile": {},
                    "keyword_groups": [{
                        "keyword_id": "kw_a",
                        "keyword": "关键词甲",
                        "hits": [{
                            "snapshot_id": "snap_a1",
                            "batch_id": "",
                        }],
                    }],
                    "keyword_cloud": [],
                    "hit_count": 1,
                    "keyword_count": 1,
                    "content_files": [],
                    "metric_points": [],
                    "timeline_events": [],
                }
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "detail:art_a",
                        "article_detail",
                        "art_a",
                        _json(detail),
                        "source",
                        "manifest",
                        "manifest://wechat",
                        "2026-07-16T00:00:00",
                    ),
                )


def test_article_identity_timezone_stable_sort_pagination_and_accounts(settings):
    _seed_article_compat(settings)
    repo = WechatLegacyRepository(
        settings,
        clock=lambda: datetime(2026, 7, 16, 12, 0, 0),
    )

    result = repo.articles(time_range=0, page=1, page_size=2, sort="reads")
    assert [item["article_id"] for item in result["articles"]] == [
        "art_a",
        "art_b",
    ]
    assert result["total"] == 4
    assert result["articles"][0]["published_at"] == "2026-07-15T10:00:00"
    assert result["articles"][0]["hit_keywords"] == ["关键词乙", "关键词甲"]
    assert result["articles"][0]["on_rank_days"] == 2
    assert result["articles"][1]["hit_keywords"] == ["关键词乙"]

    second_page = repo.articles(
        time_range=0,
        page=2,
        page_size=2,
        sort="reads",
    )
    assert [item["article_id"] for item in second_page["articles"]] == [
        "art_old",
        "art_missing",
    ]
    assert repo.articles(time_range=7)["total"] == 2
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="likes")[
            "articles"
        ]
    ] == ["art_a", "art_b", "art_old", "art_missing"]
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="hitCount")[
            "articles"
        ]
    ] == ["art_a", "art_b", "art_old", "art_missing"]
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="publishTime")[
            "articles"
        ]
    ] == ["art_a", "art_b", "art_old", "art_missing"]
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="accountScore")[
            "articles"
        ]
    ] == ["art_a", "art_old", "art_b", "art_missing"]
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="todayReads")[
            "articles"
        ]
    ] == ["art_a", "art_b"]
    assert [
        item["article_id"]
        for item in repo.articles(time_range=0, page_size=50, sort="onRankDays")[
            "articles"
        ]
    ] == ["art_a", "art_b", "art_old", "art_missing"]

    accounts = repo.article_accounts()["accounts"]
    assert accounts == [
        {
            "account_id": "acct_b",
            "name": "账号乙",
            "headimg_url": "https://img/b",
            "article_count": 2,
        },
        {
            "account_id": "acct_a",
            "name": "账号甲",
            "headimg_url": None,
            "article_count": 2,
        },
    ]


def test_article_time_range_uses_injected_dynamic_cutoff(settings):
    _seed_article_compat(settings)
    clock = {"now": datetime(2026, 7, 16, 10, 0, 0)}
    repo = WechatLegacyRepository(settings, clock=lambda: clock["now"])

    on_boundary = repo.articles(time_range=1)
    assert "art_a" in {item["article_id"] for item in on_boundary["articles"]}

    clock["now"] = datetime(2026, 7, 16, 10, 0, 1)
    after_boundary = repo.articles(time_range=1)
    assert "art_a" not in {item["article_id"] for item in after_boundary["articles"]}


def test_article_hit_detail_restores_account_and_batch(settings):
    _seed_article_compat(settings)
    detail = WechatLegacyRepository(settings).hit_detail("art_a", "")
    assert detail["account"] == {
        "account_id": "acct_a",
        "name": "账号甲",
        "headimg_url": "",
        "wechat_biz": "",
    }
    assert (
        detail["keyword_groups"][0]["hits"][0]["batch_id"]
        == "web_20260715_010101"
    )


def test_article_hit_detail_legacy_payload_hides_internal_asset_path(settings):
    _seed_article_compat(settings)
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO wechat_article_paths(
                        article_id,old_article_id,relative_path,asset_path,source_ref,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        "canonical_duplicate", "art_a",
                        "正文/甲.md", "wechat/internal-hash.md",
                        "manifest://wechat/正文/甲.md", "2026-07-16T00:00:00Z",
                    ),
                )
    detail = WechatLegacyRepository(settings).hit_detail("art_a", "")
    assert detail["content_files"] == [{
        "article_id": "art_a",
        "title": "canonical",
        "path": "正文/甲.md",
        "is_primary": True,
    }]
    assert all("asset_path" not in item for item in detail["content_files"])


def test_article_hit_keywords_restore_source_registry_text(settings):
    _seed_article_compat(settings)
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "kw_legacy",
                        "wechat-search",
                        "kw_legacy",
                        "active",
                        "2026-07-01T00:00:00",
                        "2026-07-16T00:00:00",
                        _json({"source": "snapshot_reference"}),
                    ),
                )
                con.execute(
                    """
                    INSERT INTO search_keyword_settings(
                        setting_id,system_key,platform,keyword_id,
                        updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        "wechat-search:kw_legacy",
                        "wechat-search",
                        "wechat-search",
                        "kw_legacy",
                        "2026-07-16T00:00:00",
                        _json({
                            "keyword_id": "kw_legacy",
                            "keyword_text": "关键词丙",
                            "status": "archived",
                        }),
                    ),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "snap_legacy",
                        "wechat-search",
                        "kw_legacy",
                        "kw_legacy",
                        "2026-07-13T00:00:00Z",
                        1,
                        _json({
                            "snapshot_id": "snap_legacy",
                            "keyword_id": "kw_legacy",
                            "snapshot_date": "2026-07-13",
                        }),
                    ),
                )
                con.execute(
                    """
                    INSERT INTO search_hits(
                        hit_id,snapshot_id,rank,content_id,payload_json
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        "hit_legacy",
                        "snap_legacy",
                        1,
                        "canonical_duplicate",
                        _json({
                            "hit_id": "hit_legacy",
                            "snapshot_id": "snap_legacy",
                            "article_id": "art_a",
                            "rank": 1,
                        }),
                    ),
                )

    result = WechatLegacyRepository(settings).articles(
        time_range=0,
        search="同分文章甲",
    )
    assert result["total"] == 1
    assert result["articles"][0]["hit_keywords"] == sorted(
        ["关键词甲", "关键词乙", "关键词丙"]
    )
    assert all(
        value != "kw_legacy"
        for value in result["articles"][0]["hit_keywords"]
    )


def test_keyword_manage_dynamic_refresh_fields(settings):
    payload = {
        "groups": [{
            "group_id": "g1",
            "keywords": [
                {
                    "keyword_id": "kw_established",
                    "refresh_frequency_days": 1,
                    "refresh_frequency_source": "auto",
                    "lifecycle_stage": "established",
                    "last_refresh_at": "2026-07-15T03:13:57",
                    "next_refresh_at": None,
                    "refresh_age_days": None,
                    "is_refresh_due": True,
                },
                {
                    "keyword_id": "kw_observing",
                    "refresh_frequency_days": 15,
                    "refresh_frequency_source": "auto",
                    "lifecycle_stage": "observing",
                    "last_refresh_at": "2026-07-16T10:00:00",
                },
            ],
        }],
        "total": 2,
        "updated_at": "2026-07-15T21:33:10",
    }
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "manage",
                        "keyword_manage",
                        "",
                        _json(payload),
                        "source",
                        "manifest",
                        "manifest://wechat",
                        "2026-07-16T00:00:00",
                    ),
                )
    result = WechatLegacyRepository(
        settings,
        clock=lambda: datetime(2026, 7, 16, 12, 0, 0),
    ).keyword_manage()
    assert result["updated_at"] == "2026-07-16T12:00:00"
    established, observing = result["groups"][0]["keywords"]
    assert established["next_refresh_at"] == "2026-07-16T03:13:57"
    assert established["refresh_age_days"] == 1
    assert established["is_refresh_due"] is True
    assert observing["effective_refresh_interval_hours"] == 3.0
    assert observing["next_refresh_at"] == "2026-07-16T13:00:00"
    assert observing["refresh_age_days"] == 0
    assert observing["is_refresh_due"] is False


@pytest.mark.skipif(
    os.getenv("RUN_WECHAT_8774_KEYWORD_MANAGE") != "1",
    reason="8774 顶层 updated_at 请求级专项需显式运行",
)
def test_keyword_manage_updated_at_matches_8774_request_precision(settings):
    def legacy_updated_at() -> str:
        with urllib.request.urlopen(
            "http://127.0.0.1:8774/api/keyword-manage",
            timeout=10,
        ) as response:
            return str(json.load(response)["updated_at"])

    first = legacy_updated_at()
    deadline = time.monotonic() + 3
    second = first
    while second == first and time.monotonic() < deadline:
        time.sleep(0.15)
        second = legacy_updated_at()
    assert second != first

    first_dt = datetime.fromisoformat(first)
    second_dt = datetime.fromisoformat(second)
    for raw, parsed in ((first, first_dt), (second, second_dt)):
        assert parsed.tzinfo is None
        assert parsed.microsecond == 0
        assert parsed.isoformat(timespec="seconds") == raw

    payload = {
        "groups": [],
        "total": 0,
        "ranked_total": 0,
        "not_ranked_total": 0,
        "updated_at": "2026-07-15T21:33:10",
    }
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "manage-8774-updated-at",
                        "keyword_manage",
                        "",
                        _json(payload),
                        "source",
                        "manifest",
                        "manifest://wechat",
                        "2026-07-16T00:00:00",
                    ),
                )

    current = [first_dt]
    repo = WechatLegacyRepository(settings, clock=lambda: current[0])
    assert repo.keyword_manage()["updated_at"] == first
    current[0] = second_dt
    assert repo.keyword_manage()["updated_at"] == second


def test_keyword_manage_restores_legacy_keyword_order(settings):
    source_rows = (
        ("kw_order_4", "国寿保费融资产品", 4),
        ("kw_order_2", "国寿海外保险", 2),
        ("kw_order_10", "宏利分红达成率", 10),
        ("kw_order_9", "宏利历史分红实现率", 9),
    )
    payload = {
        "groups": [{
            "group_id": "grp_ced9f780",
            "label": "动态发现候选词",
            "order": 18,
            "keywords": [
                {
                    "keyword_id": keyword_id,
                    "keyword_text": keyword_text,
                    "last_refresh_at": None,
                }
                for keyword_id, keyword_text, _ in source_rows
            ],
        }],
        "total": 4,
    }
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "manage-order",
                        "keyword_manage",
                        "",
                        _json(payload),
                        "source",
                        "manifest",
                        "manifest://wechat",
                        "2026-07-16T00:00:00",
                    ),
                )
                for keyword_id, keyword_text, keyword_order in source_rows:
                    con.execute(
                        """
                        INSERT INTO keywords(
                            keyword_id,platform,keyword,status,
                            first_seen_at,updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            keyword_id,
                            "wechat-search",
                            keyword_text,
                            "active",
                            "2026-07-01T00:00:00",
                            "2026-07-16T00:00:00",
                            "{}",
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO search_keyword_settings(
                            setting_id,system_key,platform,keyword_id,
                            updated_at,payload_json
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            f"wechat-search:{keyword_id}",
                            "wechat-search",
                            "wechat-search",
                            keyword_id,
                            "2026-07-16T00:00:00",
                            _json({
                                "keyword_id": keyword_id,
                                "keyword_text": keyword_text,
                                "keyword_order": keyword_order,
                                "status": "active",
                            }),
                        ),
                    )

    group = WechatLegacyRepository(settings).keyword_manage()["groups"][0]
    assert [item["keyword_id"] for item in group["keywords"]] == [
        "kw_order_2",
        "kw_order_4",
        "kw_order_9",
        "kw_order_10",
    ]


@pytest.mark.skipif(
    os.getenv("RUN_WECHAT_ARTICLE_FREEZE_COMPAT") != "1",
    reason="真实 freeze 全量临时导入专项需显式运行，耗时且占用较大临时空间",
)
def test_real_freeze_import_restores_article_keywords_and_manage_order(
    settings,
    tmp_path,
):
    assert REAL_FREEZE.is_dir()
    configured = replace(
        settings,
        wechat_source_root=REAL_FREEZE,
        wechat_source_url="http://127.0.0.1:1",
        asset_store_path=tmp_path / "asset_store",
    )
    imported = WechatService(configured).import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="article-compat-real-freeze",
    )
    assert imported["counts"]["search_hits"] == 101348

    repo = WechatLegacyRepository(
        configured,
        clock=lambda: datetime(2026, 7, 16, 5, 54, 0),
    )
    article_rows, _ = repo._legacy_article_rows()
    by_article = {item["article_id"]: item for item in article_rows}
    assert by_article["art_f99dbc984bdb"]["hit_keywords"] == [
        "港险分红实现",
        "港险分红实现率",
        "港险分红实现率2025",
        "港险分红实现率怎么看",
        "港险分红实现率排名",
        "港险历年分红实现率",
        "港险的分红实现率",
        "香港保险分红实现率图",
        "香港分红险实现率排名",
        "香港美元保单",
    ]
    assert not any(
        keyword.startswith("kw_")
        for article in article_rows
        for keyword in article["hit_keywords"]
    )

    group = next(
        item
        for item in repo.keyword_manage()["groups"]
        if item["group_id"] == "grp_ced9f780"
    )
    assert [item["keyword_id"] for item in group["keywords"][:7]] == [
        "kw_42f22910",
        "kw_c1afedd5",
        "kw_a52c614b",
        "kw_10768f8a",
        "kw_36728b48",
        "kw_9f209faf",
        "kw_72e36799",
    ]


def test_discovery_flattens_payload_filters_and_uses_legacy_order(settings):
    probes = [
        {
            "probe_id": "probe_b",
            "probe_text": "第二个",
            "status": "searched",
            "proposed_at": "2026-07-12T09:00:00",
            "warming_facts_json": "[\"b\"]",
            "replacement_candidate_ids_json": "[]",
        },
        {
            "probe_id": "probe_a",
            "probe_text": "第一个",
            "status": "proposed",
            "proposed_at": "2026-07-12T08:00:00",
            "warming_facts_json": "[\"a\"]",
            "replacement_candidate_ids_json": "[\"cand_observing\"]",
        },
    ]
    candidates = [
        {
            "candidate_id": "cand_discovered",
            "candidate_text": "普通候选",
            "status": "discovered",
            "validation_score": 10,
            "related_parent_probe_count": 5,
            "related_occurrence_count": 5,
            "first_seen_at": "2026-07-12T08:00:00",
            "source_probe_ids_json": "[]",
            "source_article_ids_json": "[]",
        },
        {
            "candidate_id": "cand_observing",
            "candidate_text": "观察候选",
            "status": "observing",
            "validation_score": 71,
            "related_parent_probe_count": 1,
            "related_occurrence_count": 1,
            "first_seen_at": "2026-07-12T08:37:15",
            "source_probe_ids_json": "[\"probe_a\"]",
            "source_article_ids_json": "[\"art_a\"]",
        },
    ]
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                for item in probes:
                    con.execute(
                        """
                        INSERT INTO wechat_discovery_probes(
                            probe_id,probe_text,status,payload_json,updated_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            item["probe_id"],
                            item["probe_text"],
                            item["status"],
                            _json(item),
                            item["proposed_at"],
                        ),
                    )
                for item in candidates:
                    con.execute(
                        """
                        INSERT INTO wechat_discovery_candidates(
                            candidate_id,candidate_text,status,payload_json,updated_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            item["candidate_id"],
                            item["candidate_text"],
                            item["status"],
                            _json(item),
                            item["first_seen_at"],
                        ),
                    )
    repo = WechatLegacyRepository(settings)
    result = repo.discovery(limit=1)
    assert set(result) == {"summary", "probes", "candidates"}
    assert result["probes"][0]["probe_id"] == "probe_a"
    assert result["probes"][0]["warming_facts"] == ["a"]
    assert result["candidates"][0]["candidate_id"] == "cand_observing"
    assert result["candidates"][0]["source_probe_ids"] == ["probe_a"]
    assert "payload" not in result["candidates"][0]
    assert "payload_json" not in result["candidates"][0]
    assert result["summary"] == {
        "probes": {"proposed": 1, "searched": 1},
        "candidates": {"discovered": 1, "observing": 1},
    }

    filtered = repo.discovery(
        status=["searched"],
        candidate_status=["discovered"],
        limit=10,
    )
    assert [item["probe_id"] for item in filtered["probes"]] == ["probe_b"]
    assert [item["candidate_id"] for item in filtered["candidates"]] == [
        "cand_discovered"
    ]
