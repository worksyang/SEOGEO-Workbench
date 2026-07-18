import json
import sqlite3

from content_hub.services.wechat_live_projection import (
    BOARD_CONFIGS,
    _score_board_snapshot,
    rebuild,
    write,
)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE keywords(
          keyword_id TEXT PRIMARY KEY, platform TEXT, keyword TEXT, status TEXT,
          topic TEXT, keyword_bucket TEXT, payload_json TEXT
        );
        CREATE TABLE search_keyword_settings(
          setting_id TEXT PRIMARY KEY, system_key TEXT, platform TEXT, keyword_id TEXT,
          pinned INTEGER, note TEXT, group_id TEXT, refresh_strategy TEXT,
          refresh_interval_minutes INTEGER, commercial_value REAL, archived_at TEXT,
          updated_at TEXT, payload_json TEXT
        );
        CREATE TABLE creators(
          creator_id TEXT PRIMARY KEY, canonical_name TEXT, platform TEXT,
          headimg_url TEXT, payload_json TEXT
        );
        CREATE TABLE contents(
          content_id TEXT PRIMARY KEY, title TEXT, canonical_url TEXT, creator_id TEXT,
          author_name TEXT, published_at TEXT, payload_json TEXT
        );
        CREATE TABLE search_snapshots(
          snapshot_id TEXT PRIMARY KEY, platform TEXT, keyword TEXT, keyword_id TEXT,
          captured_at TEXT, trigger_type TEXT, result_count INTEGER,
          features_json TEXT, source_ref TEXT, payload_json TEXT
        );
        CREATE TABLE search_hits(
          hit_id TEXT PRIMARY KEY, snapshot_id TEXT, rank INTEGER, content_id TEXT,
          title_raw TEXT, url_raw TEXT, creator_name_raw TEXT, payload_json TEXT
        );
        CREATE TABLE metric_observations(
          observation_id TEXT PRIMARY KEY, subject_type TEXT, subject_id TEXT,
          metric_key TEXT, observed_at TEXT, numeric_value REAL, text_value TEXT,
          snapshot_id TEXT, source_ref TEXT, payload_json TEXT
        );
        CREATE TABLE wechat_legacy_projections(
          projection_id TEXT PRIMARY KEY, projection_kind TEXT, subject_id TEXT,
          payload_json TEXT, source_hash TEXT, source_manifest_id TEXT,
          source_ref TEXT, updated_at TEXT,
          UNIQUE(projection_kind, subject_id, source_hash)
        );
        """
    )
    con.execute(
        "INSERT INTO keywords VALUES (?,?,?,?,?,?,?)",
        ("kw_1", "wechat-search", "保险", "active", "保险", "默认", "{}"),
    )
    con.execute(
        "INSERT INTO search_keyword_settings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("set_1", "wechat-search", "wechat-search", "kw_1", 1, "", None,
         "manual", None, None, None, "2026-07-15T00:00:00Z",
         '{"keyword_text":"真实关键词"}'),
    )
    con.execute(
        "INSERT INTO creators VALUES (?,?,?,?,?)",
        ("acct_1", "甲账号", "wechat-search", None,
         '{"headimg_url":"https://img/payload-1"}'),
    )
    con.execute(
        "INSERT INTO creators VALUES (?,?,?,?,?)",
        ("acct_zombie", "零命中账号", "wechat-search", "https://img/zombie", "{}"),
    )
    con.execute(
        "INSERT INTO contents VALUES (?,?,?,?,?,?,?)",
        ("art_1", "新文章", "https://example/1", "acct_1", "甲账号",
         "2026-07-18T08:00:00Z", '{"read_count":10,"like_count":2}'),
    )
    for sid, captured, count in (
        ("snap_old", "2026-07-17T08:00:00Z", 1),
        ("snap_new", "2026-07-18T08:00:00Z", 1),
    ):
        con.execute(
            "INSERT INTO search_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "wechat-search", "保险", "kw_1", captured, "manual", count,
             '{"suggestions":["相关"]}', "fixture", "{}"),
        )
    con.execute(
        "INSERT INTO search_hits VALUES (?,?,?,?,?,?,?,?)",
        ("hit_old", "snap_old", 4, "art_1", "旧标题", "https://example/1", "甲账号", "{}"),
    )
    con.execute(
        "INSERT INTO search_hits VALUES (?,?,?,?,?,?,?,?)",
        ("hit_new", "snap_new", 1, "art_1", "新标题", "https://example/1", "甲账号", "{}"),
    )
    con.execute(
        "INSERT INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("obs_1", "content", "art_1", "wechat.read_count",
         "2026-07-18T08:00:00Z", 123, None, "snap_new", "fixture", "{}"),
    )
    con.execute(
        "INSERT INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("obs_2", "content", "art_1", "wechat.like_count",
         "2026-07-18T08:00:00Z", 9, None, "snap_new", "fixture", "{}"),
    )
    con.execute(
        "INSERT INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("obs_kw_1", "keyword", "kw_1", "wechat.keyword.read_delta_estimated",
         "2026-07-18T08:00:00Z", 42, None, None, "fixture",
         '{"recent_vs_baseline_ratio":1.25,"trend_label":"上升"}'),
    )
    con.commit()
    return con


def test_rebuild_uses_latest_canonical_snapshot_for_all_legacy_shapes():
    con = _db()
    payloads = rebuild(con)

    # Legacy UI timestamps are local Asia/Shanghai time; the fixture snapshot
    # is stored as UTC in the canonical table.
    assert payloads["full"]["generated_at"] == "2026-07-18T16:00:00"
    assert payloads["full"]["window_end"] == "2026-07-18"
    keyword = payloads["keyword:kw_1"]
    assert keyword["keyword"] == "真实关键词"
    assert keyword["latest_run"]["id"] == "snap_new"
    assert keyword["today_best"] == 1
    assert keyword["history_best"][-1] == 1
    assert keyword["turnover_runs"][0]["id"] == "snap_new"
    assert keyword["latest_run"]["articles"][0]["read_count"] == 123
    assert keyword["latest_run"]["articles"][0]["like_count"] == 9
    assert keyword["latest_run"]["articles"][0]["account_headimg"] == "https://img/payload-1"
    assert keyword["article_count"] == 1
    assert keyword["keyword_read_delta"]["read_delta_estimated"] == 42
    assert keyword["read_delta_estimated"] == 42
    assert keyword["recent_vs_baseline_ratio"] == 1.25
    assert keyword["trend_label"] == "上升"
    assert keyword["confidence_level"] == "low"
    assert keyword["status"] == "ok"
    assert payloads["bootstrap"]["keywords"][0]["latest_run"]["id"] == "snap_new"
    assert payloads["account:acct_1"]["best_today"] == 1
    assert payloads["account:acct_1"]["today_score"] > 0
    assert payloads["full"]["account_score_method"] == "three_board_breakthrough_v5_1"
    assert "account:acct_zombie" not in payloads
    assert [item["account_id"] for item in payloads["full"]["accounts"]] == ["acct_1"]


def test_write_persists_bootstrap_full_keyword_and_account():
    con = _db()
    payloads = write(con)
    rows = con.execute(
        "SELECT projection_kind,subject_id,payload_json FROM wechat_legacy_projections"
    ).fetchall()
    keys = {(r["projection_kind"], r["subject_id"]) for r in rows}
    assert {("bootstrap", ""), ("full", ""), ("keyword", "kw_1"), ("account", "acct_1")} <= keys
    stored_full = json.loads(next(r["payload_json"] for r in rows if r["projection_kind"] == "full"))
    assert stored_full["generated_at"] == payloads["full"]["generated_at"]


def test_keyword_delta_compat_fields_are_null_when_no_data():
    con = _db()
    con.execute(
        "INSERT INTO keywords VALUES (?,?,?,?,?,?,?)",
        ("kw_empty", "wechat-search", "无数据关键词", "active", "无数据", "默认", "{}"),
    )
    payload = rebuild(con)["keyword:kw_empty"]
    assert payload["keyword_read_delta"]["status"] == "insufficient_data"
    assert payload["status"] == "insufficient_data"
    assert payload["read_delta_estimated"] is None
    assert payload["read_delta_raw"] is None
    assert payload["steady_read_median"] is None
    assert payload["confidence_score"] is None
    assert payload["trend_signal"] is None
    assert payload["confidence_level"] == "insufficient"
    assert payload["trend_label"] == "观察中"
    assert payload["recent_vs_baseline_ratio"] is None


def test_keyword_delta_keeps_insufficient_raw_data_as_provisional():
    con = _db()
    con.execute(
        "DELETE FROM metric_observations WHERE subject_id=? AND metric_key=?",
        ("kw_1", "wechat.keyword.read_delta_estimated"),
    )
    con.execute(
        "DELETE FROM metric_observations WHERE subject_id=? AND metric_key=?",
        ("kw_1", "wechat.keyword.steady_read_median"),
    )
    con.execute(
        "INSERT INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("obs_kw_raw", "keyword", "kw_1", "wechat.keyword.read_delta_raw",
         "2026-07-18T08:00:00Z", 365, None, None, "fixture", "{}"),
    )
    for index, value in enumerate((20, 25), 1):
        con.execute(
            "INSERT INTO metric_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"obs_kw_point_{index}", "keyword", "kw_1",
             "wechat.keyword.daily_read_delta",
             f"2026-07-{14 + index:02d}T08:00:00Z", value, None, None,
             "fixture", json.dumps({"daily_point": {
                 "date": f"2026-07-{14 + index:02d}",
                 "read_delta": value, "observed_component": value,
                 "slot_coverage_ratio": 1, "snapshot_count": 1,
             }})),
        )
    con.commit()

    payload = rebuild(con)["keyword:kw_1"]
    assert payload["keyword_read_delta"]["status"] == "insufficient_data"
    assert payload["keyword_read_delta"]["read_delta_raw"] == 365
    assert payload["keyword_read_delta"]["provisional_steady_read_median"] == 22
    assert payload["keyword_read_delta"]["provisional_read_delta_estimated"] == 338
    assert payload["keyword_read_delta"]["provisional_status"] == "provisional"


def test_keyword_delta_falls_back_to_legacy_import_until_live_facts_mature():
    con = _db()
    con.execute(
        "DELETE FROM metric_observations WHERE subject_type='keyword' AND subject_id=?",
        ("kw_1",),
    )
    legacy_delta = {
        "keyword_id": "kw_1",
        "keyword": "真实关键词",
        "window_start": "2026-07-04T00:00:00",
        "window_end": "2026-07-18T00:30:10",
        "window_days": 15,
        "status": "ok",
        "steady_read_median": 58,
        "read_delta_estimated": 828,
        "read_delta_raw": 1492,
        "confidence_score": 0.4772,
        "confidence_level": "low",
        "trend_signal": 0.1622,
        "trend_label": "平稳",
        "recent_vs_baseline_ratio": 0.3333,
        "daily_read_delta_points": [{"date": "2026-07-18", "read_delta": 63}],
    }
    con.execute(
        "INSERT INTO wechat_legacy_projections VALUES (?,?,?,?,?,?,?,?)",
        (
            "legacy_kw_1", "keyword", "kw_1",
            json.dumps({"keyword_read_delta": legacy_delta}, ensure_ascii=False),
            "legacy-hash", "sm_wechat-search-legacy", "legacy://wechat", "2026-07-18T01:00:00Z",
        ),
    )
    con.commit()

    payload = rebuild(con)["keyword:kw_1"]["keyword_read_delta"]
    assert payload["status"] == "ok"
    assert payload["read_delta_estimated"] == 828
    assert payload["confidence_level"] == "low"
    assert payload["trend_label"] == "平稳"
    assert payload["daily_read_delta_points"] == [{"date": "2026-07-18", "read_delta": 63}]


def test_three_board_scoring_keeps_p99_breakthrough_over_100():
    raw = {
        "raw_axes": {key: 2.0 for key in BOARD_CONFIGS["account"]["weights"]},
        "confidence": 1.0,
    }
    benchmarks = {key: 1.0 for key in BOARD_CONFIGS["account"]["weights"]}
    _, breakthrough = _score_board_snapshot(raw, "account", benchmarks)
    assert breakthrough["score"] > 100
    assert breakthrough["breakthrough"] is True
    assert breakthrough["breakthrough_gate"] is True

    ordinary_raw = {
        "raw_axes": {key: 1.0 for key in BOARD_CONFIGS["account"]["weights"]},
        "confidence": 1.0,
    }
    _, ordinary = _score_board_snapshot(ordinary_raw, "account", benchmarks)
    assert ordinary["score"] == 100
    assert ordinary["breakthrough"] is False
