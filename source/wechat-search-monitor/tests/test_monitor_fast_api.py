from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest
from flask import Flask

from app.services import monitor_fast_service
from app.services.monitor_fast_service import MonitorFastStore
from app.web import api as api_module


class _FakeRegistry:
    settings: dict[str, dict] = {}

    def __init__(self, _path: Path) -> None:
        pass

    def load_payload(self) -> dict:
        return {
            "groups": [
                {
                    "group_id": "g1",
                    "label": "香港保险",
                    "keywords": [
                        {"keyword_id": "kw1", "keyword_text": "香港储蓄险"},
                    ],
                }
            ]
        }

    def list_settings(self) -> dict[str, dict]:
        return self.settings


@pytest.fixture()
def fast_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MonitorFastStore:
    monitor_path = tmp_path / "monitor-data.json"
    sqlite_path = tmp_path / "app.db"
    delta_path = tmp_path / "keyword_read_deltas.json"
    meta_path = tmp_path / "article_metric_observations_meta.json"
    sqlite_path.write_bytes(b"sqlite")
    monitor_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-13T12:00:00+08:00",
                "window_days": 15,
                "window_start": "2026-06-29",
                "window_end": "2026-07-13",
                "keywords": [
                    {
                        "keyword_id": "kw1",
                        "keyword": "香港储蓄险",
                        "topic": "香港储蓄险",
                        "today_best": 1,
                        "today_count": 1,
                        "coverage_days": 2,
                        "tracked_accounts": 1,
                        "article_count": 1,
                        "latest_run": {
                            "id": "run2",
                            "date": "2026-07-13",
                            "time": "12:00",
                            "run_at": "2026-07-13T12:00:00+08:00",
                            "result_count": 1,
                        },
                        "runs": [
                            {
                                "id": "run2",
                                "date": "2026-07-13",
                                "time": "12:00",
                                "articles": [
                                    {
                                        "article_id": "art2",
                                        "title": "今天的文章",
                                        "account": "测试账号",
                                        "rank": 1,
                                    }
                                ],
                            },
                            {
                                "id": "run1",
                                "date": "2026-07-12",
                                "time": "12:00",
                                "articles": [
                                    {
                                        "article_id": "art1",
                                        "title": "昨天的文章",
                                        "account": "测试账号",
                                        "rank": 2,
                                    }
                                ],
                            },
                        ],
                        "history_best": [0] * 13 + [2, 1],
                        "history_hits": [0] * 13 + [1, 1],
                        "accounts": [{"name": "测试账号", "hit_days": 2}],
                        "heat_summary": {},
                        "kw_score": {
                            "total": 0.8,
                            "heat": 0.8,
                            "breadth": 0.5,
                            "richness": 0.2,
                            "has_heat": True,
                        },
                    }
                ],
                "accounts": [
                    {
                        "account_id": "acct1",
                        "name": "测试账号",
                        "score": 88,
                        "score_raw": 88.2,
                        "history": [0] * 13 + [2, 1],
                        "day_scores": [0] * 13 + [5, 8],
                        "topics": {
                            "topic1": {
                                "label": "香港保险",
                                "history": [0] * 13 + [2, 1],
                                "keywords": ["香港储蓄险"],
                                "articles": [],
                            }
                        },
                        "keywords": {
                            "香港储蓄险": {
                                "history": [0] * 13 + [2, 1],
                                "articles": [],
                            }
                        },
                        "account_score_hexagon": {"axes": []},
                        "account_score_parts": {"coverage": 1},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    delta_path.write_text(
        json.dumps(
            [
                {
                    "keyword_id": "kw1",
                    "keyword": "香港储蓄险",
                    "status": "insufficient_data",
                    "read_delta_estimated": None,
                    "steady_read_median": None,
                    "provisional_steady_read_median": 12,
                    "provisional_read_delta_estimated": 180,
                    "provisional_sample_count": 2,
                    "provisional_status": "provisional",
                    "snapshot_count": 2,
                    "daily_read_delta_points": [
                        {
                            "date": "2026-07-13",
                            "read_delta": 12,
                            "observed_component": 12,
                            "estimated_component": 0,
                            "snapshot_count": 2,
                            "slot_coverage_ratio": 1,
                            "article_count": 1,
                            "is_imputed_day": False,
                        }
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        monitor_fast_service,
        "KeywordRegistryRepository",
        _FakeRegistry,
    )
    return MonitorFastStore(
        monitor_path,
        sqlite_path,
        delta_path,
        meta_path,
    )


def test_bootstrap_is_small_and_details_are_lossless(fast_store: MonitorFastStore) -> None:
    full_raw, _, _ = fast_store.get_full()
    bootstrap_raw, bootstrap_gzip, _ = fast_store.get_bootstrap()
    full = json.loads(full_raw)
    bootstrap = json.loads(bootstrap_raw)

    assert len(bootstrap_raw) < len(full_raw)
    assert len(bootstrap_gzip) < len(bootstrap_raw)
    assert "runs" not in bootstrap["keywords"][0]
    assert bootstrap["keywords"][0]["turnover_runs"][0]["articles"] == [
        {"article_id": "art2"}
    ]
    assert "topics" not in bootstrap["accounts"][0]
    assert "keywords" not in bootstrap["accounts"][0]
    assert "account_score_hexagon" not in bootstrap["accounts"][0]
    bootstrap_delta = bootstrap["keywords"][0]["keyword_read_delta"]
    assert bootstrap_delta["provisional_steady_read_median"] == 12
    assert bootstrap_delta["provisional_read_delta_estimated"] == 180
    assert bootstrap_delta["provisional_sample_count"] == 2
    assert bootstrap_delta["provisional_status"] == "provisional"
    assert bootstrap_delta["snapshot_count"] == 2
    assert "daily_read_delta_points" not in bootstrap_delta

    keyword_raw, _, _ = fast_store.get_keyword("kw1") or (b"", b"", "")
    account_raw, _, _ = fast_store.get_account("acct1") or (b"", b"", "")
    assert json.loads(keyword_raw)["runs"] == full["keywords"][0]["runs"]
    detail_delta = json.loads(keyword_raw)["keyword_read_delta"]
    assert detail_delta["provisional_steady_read_median"] == 12
    assert detail_delta["provisional_read_delta_estimated"] == 180
    assert detail_delta["provisional_sample_count"] == 2
    assert detail_delta["provisional_status"] == "provisional"
    assert detail_delta["daily_read_delta_points"]
    account = json.loads(account_raw)
    assert account["topics"] == full["accounts"][0]["topics"]
    assert account["keywords"] == full["accounts"][0]["keywords"]
    assert account["_today_article_ids"] == ["art2"]


def test_source_signature_invalidates_on_monitor_and_wal_change(
    fast_store: MonitorFastStore,
) -> None:
    first_etag = fast_store.get_bootstrap()[2]
    monitor_path = fast_store.monitor_data_path
    payload = json.loads(monitor_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-07-13T13:00:00+08:00"
    monitor_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(monitor_path, None)
    second_etag = fast_store.get_bootstrap()[2]
    assert second_etag != first_etag

    previous_signature = fast_store._signature
    Path(f"{fast_store.sqlite_path}-wal").write_bytes(b"changed")
    fast_store.ensure_loaded()
    assert fast_store._signature != previous_signature


@pytest.fixture()
def api_client(fast_store: MonitorFastStore, monkeypatch: pytest.MonkeyPatch):
    app = Flask(__name__)
    app.register_blueprint(api_module.bp)
    monkeypatch.setattr(api_module, "get_fast_store", lambda: fast_store)
    return app.test_client()


def test_monitor_api_gzip_etag_304_and_legacy_full(api_client) -> None:
    identity = api_client.get("/api/monitor-data/bootstrap")
    compressed = api_client.get(
        "/api/monitor-data/bootstrap",
        headers={"Accept-Encoding": "gzip"},
    )
    assert identity.status_code == 200
    assert compressed.status_code == 200
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert compressed.headers["Vary"] == "Accept-Encoding"
    assert compressed.headers["Cache-Control"] == "no-cache, must-revalidate"
    assert gzip.decompress(compressed.data) == identity.data

    not_modified = api_client.get(
        "/api/monitor-data/bootstrap",
        headers={"If-None-Match": identity.headers["ETag"]},
    )
    assert not_modified.status_code == 304
    assert not not_modified.data

    full = api_client.get("/api/monitor-data")
    assert full.status_code == 200
    assert json.loads(full.data)["keywords"][0]["runs"]


def test_monitor_detail_endpoints(api_client) -> None:
    keyword = api_client.get("/api/monitor-data/keyword/kw1")
    account = api_client.get(
        "/api/monitor-data/account/acct1",
        headers={"Accept-Encoding": "gzip"},
    )
    assert keyword.status_code == 200, keyword.data
    assert json.loads(keyword.data)["keyword"] == "香港储蓄险"
    assert account.status_code == 200
    assert json.loads(gzip.decompress(account.data))["name"] == "测试账号"
    assert api_client.get("/api/monitor-data/keyword/missing").status_code == 404
    assert api_client.get("/api/monitor-data/account/missing").status_code == 404
