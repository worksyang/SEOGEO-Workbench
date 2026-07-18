from __future__ import annotations

from content_hub.features.xhs.legacy_projection import project_hub_payload


def _payload() -> dict:
    return {
        "keywords": [
            {
                "keyword_id": "xhs_keyword_kw-1",
                "platform": "xiaohongshu",
                "keyword": "香港保险",
                "status": "active",
                "payload": {"source_keyword_id": "kw-1", "keyword_text": "香港保险"},
            },
            {"keyword_id": "wechat-kw", "platform": "wechat-search", "keyword": "同名词"},
        ],
        "accounts": [
            {
                "creator_id": "xhs_creator_a",
                "external_id": "acct-a",
                "platform": "xiaohongshu",
                "canonical_name": "作者 A",
                "payload": {"fans": 10},
            },
            {"creator_id": "wechat-creator", "external_id": "acct-a", "platform": "wechat-search", "canonical_name": "微信作者"},
        ],
        "snapshots": [
            {
                "snapshot_id": "snap-1",
                "platform": "xiaohongshu",
                "keyword_id": "xhs_keyword_kw-1",
                "keyword": "香港保险",
                "captured_at": "2026-07-15T10:00:00Z",
                "result_count": 2,
            },
            {
                "snapshot_id": "snap-2",
                "platform": "xiaohongshu",
                "keyword_id": "xhs_keyword_kw-1",
                "keyword": "香港保险",
                "captured_at": "2026-07-16T10:00:00Z",
                "result_count": 1,
            },
        ],
        "articles": [
            {
                "content_id": "note-1",
                "platform": "xiaohongshu",
                "content_type": "social_note",
                "title": "笔记一",
                "creator_id": "xhs_creator_a",
                "payload": {"source_article_id": "xhs_tk_note-1", "normalized_url": "https://www.xiaohongshu.com/explore/note-1"},
            },
            {
                "content_id": "note-1",
                "platform": "xiaohongshu",
                "content_type": "social_note",
                "title": "重复笔记",
                "creator_id": "xhs_creator_a",
                "payload": {"source_article_id": "xhs_tk_note-1"},
            },
        ],
        "ranking_hits": [
            {"hit_id": "hit-2", "platform": "xiaohongshu", "snapshot_id": "snap-2", "rank": 1, "content_id": "note-1", "creator_name_raw": "作者 A"},
            {"hit_id": "hit-1", "platform": "xiaohongshu", "snapshot_id": "snap-1", "rank": 2, "content_id": "note-1", "creator_name_raw": "作者 A"},
        ],
    }


def test_projection_keeps_xhs_isolated_and_projects_runs() -> None:
    result = project_hub_payload(_payload())

    assert result["source_status"]["source"] == "hub_db_projection"
    assert result["counts"] == {
        "keywords": 1,
        "accounts": 1,
        "snapshots": 2,
        "ranking_hits": 2,
        "articles": 1,
        "snapshot_terms": 0,
    }
    assert result["keywords"][0]["keyword_id"] == "kw-1"
    assert len(result["keywords"][0]["runs"]) == 2
    assert result["keywords"][0]["runs"][-1]["best_rank"] == 1
    assert result["keywords"][0]["accounts"][0]["name"] == "作者 A"
    assert len(result["keywords"][0]["history_best"]) == 15
    assert result["accounts"][0]["name"] == "作者 A"
    assert result["accounts"][0]["history"][-1] == 1
    assert result["accounts"][0]["keywords"]["香港保险"]["hit_days"] == 2
    assert result["accounts"][0]["topics"]["香港保险"]["article_count"] == 1
    assert result["accounts"][0]["keywords"]["香港保险"]["articles"][0]["article_id"] == "xhs_tk_note-1"


def test_projection_is_tolerant_of_missing_fields_and_records_warnings() -> None:
    result = project_hub_payload({"keywords": [{"keyword_id": "x", "platform": "xiaohongshu", "keyword": "空"}]})

    assert result["keywords"][0]["runs"] == []
    assert result["keywords"][0]["today_count"] == 0
    assert result["accounts"] == []
    assert result["projection_warnings"]
