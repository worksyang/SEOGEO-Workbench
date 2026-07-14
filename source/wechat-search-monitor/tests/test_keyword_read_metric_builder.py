from __future__ import annotations

from statistics import median

from app.ingest.builders.keyword_read_metric_builder import (
    ObservationPoint,
    _build_rate_intervals,
    _estimate_calendar_value,
    _minimum_window_observations,
    _reconstruct_calendar_points,
    build_keyword_read_deltas,
)
from datetime import datetime, timedelta


def _point(date: str, value: float, direct: float | None = None):
    direct_value = value if direct is None else direct
    return {
        "date": date,
        "read_delta": value,
        "observed_component": direct_value,
        "estimated_component": max(0.0, value - direct_value),
        "snapshot_count": 1,
        "slot_coverage_ratio": 1.0,
        "article_count": 5,
    }


def test_sparse_days_are_interpolated_instead_of_flat_filled():
    dates = [f"2026-01-0{day}" for day in range(1, 6)]
    observed = [
        _point("2026-01-01", 10),
        _point("2026-01-04", 40),
    ]
    result = _reconstruct_calendar_points(
        dates,
        observed,
        observed,
        long_term_steady=20,
        frequency_days=3,
        status="ok",
    )
    assert len(result) == 5
    assert result[0]["read_delta"] == 10
    assert result[3]["read_delta"] == 40
    assert result[1]["is_imputed_day"] is True
    assert 10 < result[1]["read_delta"] < 40
    assert result[1]["observed_component"] == 0
    assert result[1]["estimated_component"] == result[1]["read_delta"]


def test_far_low_frequency_point_shrinks_toward_long_term_level():
    estimate = _estimate_calendar_value(
        "2026-01-15",
        [_point("2026-01-01", 100)],
        long_term_steady=10,
        frequency_days=15,
    )
    assert 10 < estimate < 50


def test_required_window_observations_follow_refresh_schedule():
    assert _minimum_window_observations(15, 1) == 3
    assert _minimum_window_observations(15, 3) == 3
    assert _minimum_window_observations(15, 7) == 1
    assert _minimum_window_observations(15, 15) == 1


def test_dense_four_hour_observations_keep_stable_rate_intervals():
    start = datetime(2026, 1, 1, 0, 0, 0)
    dense = [
        ObservationPoint(
            observed_at=start + timedelta(hours=offset),
            read_count=100 + offset * 10,
            batch_id=f"dense_{offset}",
        )
        for offset in range(0, 25, 4)
    ]
    sparse = dense[::2]
    dense_intervals = _build_rate_intervals({"article": dense})["article"]
    sparse_intervals = _build_rate_intervals({"article": sparse})["article"]
    assert len(dense_intervals) == len(sparse_intervals)
    assert [round(item.rate, 6) for item in dense_intervals] == [
        round(item.rate, 6)
        for item in sparse_intervals
    ]


def _synthetic_inputs(*, include_history: bool, include_second_keyword: bool):
    article_ids = [f"article_{index}" for index in range(1, 6)]
    articles = [
        {
            "article_id": article_id,
            "published_at": "2025-12-01T00:00:00",
        }
        for article_id in article_ids
    ]
    observations = []
    for index, article_id in enumerate(article_ids):
        for batch_id, observed_at, reads in (
            ("metric_1", "2026-01-01T12:00:00", 100 + index),
            ("metric_2", "2026-01-10T12:00:00", 120 + index),
            ("metric_3", "2026-01-18T12:00:00", 150 + index),
        ):
            observations.append({
                "article_id": article_id,
                "observed_at": observed_at,
                "read_count": reads,
                "source_batch_id": batch_id,
            })

    snapshots = []
    hits = []
    dates = ["2026-01-16", "2026-01-17", "2026-01-18"]
    if include_history:
        dates.insert(0, "2026-01-01")
    for date in dates:
        batch_id = f"web_{date.replace('-', '')}_010101"
        snapshot_id = f"snapshot_a_{date}"
        snapshots.append({
            "snapshot_id": snapshot_id,
            "keyword_id": "keyword_a",
            "captured_at": f"{date}T12:00:00",
            "status": "success",
            "is_primary": True,
            "source_file_path": f"微信搜索结果/批量抓取/{batch_id}/a.md",
        })
        for rank, article_id in enumerate(article_ids, start=1):
            hits.append({
                "snapshot_id": snapshot_id,
                "article_id": article_id,
                "rank": rank,
            })
        if include_second_keyword and date != "2026-01-01":
            second_snapshot_id = f"snapshot_b_{date}"
            snapshots.append({
                "snapshot_id": second_snapshot_id,
                "keyword_id": "keyword_b",
                "captured_at": f"{date}T12:00:00",
                "status": "success",
                "is_primary": True,
                "source_file_path": f"微信搜索结果/批量抓取/{batch_id}/b.md",
            })
            for rank, article_id in enumerate(article_ids, start=1):
                hits.append({
                    "snapshot_id": second_snapshot_id,
                    "article_id": article_id,
                    "rank": rank,
                })

    keywords = [{
        "keyword_id": "keyword_a",
        "keyword_text": "测试关键词A",
        "refresh_frequency_days": 1,
    }]
    if include_second_keyword:
        keywords.append({
            "keyword_id": "keyword_b",
            "keyword_text": "测试关键词B",
            "refresh_frequency_days": 1,
        })
    return observations, keywords, snapshots, hits, articles


def _build_synthetic_with_dates(
    dates: list[str],
    *,
    duplicate_date: str | None = None,
):
    observations, keywords, snapshots, hits, articles = _synthetic_inputs(
        include_history=False,
        include_second_keyword=False,
    )
    snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot["captured_at"][:10] in dates
    ]
    snapshot_ids = {snapshot["snapshot_id"] for snapshot in snapshots}
    hits = [
        hit
        for hit in hits
        if hit["snapshot_id"] in snapshot_ids
    ]
    if duplicate_date is not None:
        original = next(
            snapshot
            for snapshot in snapshots
            if snapshot["captured_at"][:10] == duplicate_date
        )
        duplicate = {
            **original,
            "snapshot_id": f"{original['snapshot_id']}_duplicate",
        }
        snapshots.append(duplicate)
        hits.extend(
            {
                **hit,
                "snapshot_id": duplicate["snapshot_id"],
            }
            for hit in hits
            if hit["snapshot_id"] == original["snapshot_id"]
        )
    rows, _ = build_keyword_read_deltas(
        observations,
        keywords,
        snapshots,
        hits,
        articles=articles,
        snapshot_terms=[],
        window_days=15,
    )
    return rows[0]


def _build_synthetic(*, include_history: bool, include_second_keyword: bool):
    observations, keywords, snapshots, hits, articles = _synthetic_inputs(
        include_history=include_history,
        include_second_keyword=include_second_keyword,
    )
    rows, _ = build_keyword_read_deltas(
        observations,
        keywords,
        snapshots,
        hits,
        articles=articles,
        snapshot_terms=[],
        window_days=15,
    )
    return {
        row["keyword_id"]: row
        for row in rows
    }


def test_keyword_metric_is_independent_of_other_keywords_in_same_batch():
    without_second = _build_synthetic(
        include_history=True,
        include_second_keyword=False,
    )["keyword_a"]
    with_second = _build_synthetic(
        include_history=True,
        include_second_keyword=True,
    )["keyword_a"]
    assert without_second["status"] == "ok"
    assert with_second["status"] == "ok"
    assert (
        without_second["steady_read_median"]
        == with_second["steady_read_median"]
    )
    assert (
        without_second["read_delta_estimated"]
        == with_second["read_delta_estimated"]
    )
    assert without_second["provisional_steady_read_median"] is None
    assert without_second["provisional_read_delta_estimated"] is None
    assert without_second["provisional_sample_count"] is None
    assert without_second["provisional_status"] is None


def test_new_keyword_does_not_backfill_days_before_it_matures():
    row = _build_synthetic(
        include_history=False,
        include_second_keyword=False,
    )["keyword_a"]
    assert row["calendar_span_days"] == 3
    assert row["status"] == "insufficient_data"
    assert row["steady_read_median"] is None
    assert row["read_delta_estimated"] is None


def test_two_observed_slices_expose_only_isolated_provisional_values():
    row = _build_synthetic_with_dates(["2026-01-17", "2026-01-18"])
    observed_values = [
        point["read_delta"]
        for point in row["daily_read_delta_points"]
    ]
    expected_steady = median(observed_values)

    assert row["status"] == "insufficient_data"
    assert row["steady_read_median"] is None
    assert row["read_delta_estimated"] is None
    assert row["provisional_status"] == "provisional"
    assert row["provisional_sample_count"] == 2
    assert row["provisional_steady_read_median"] == round(expected_steady)
    assert row["provisional_read_delta_estimated"] == round(expected_steady * 15)
    assert len(row["daily_read_delta_points"]) == 2


def test_one_observed_slice_keeps_chart_point_without_provisional_numbers():
    row = _build_synthetic_with_dates(["2026-01-18"])

    assert row["status"] == "insufficient_data"
    assert len(row["daily_read_delta_points"]) == 1
    assert row["steady_read_median"] is None
    assert row["read_delta_estimated"] is None
    assert row["provisional_sample_count"] == 1
    assert row["provisional_status"] == "insufficient_sample"
    assert row["provisional_steady_read_median"] is None
    assert row["provisional_read_delta_estimated"] is None


def test_two_same_day_snapshots_provision_one_daily_point():
    row = _build_synthetic_with_dates(
        ["2026-01-18"],
        duplicate_date="2026-01-18",
    )

    assert row["status"] == "insufficient_data"
    assert row["steady_read_median"] is None
    assert row["read_delta_estimated"] is None
    assert row["snapshot_count"] == 2
    assert row["observed_days"] == 1
    assert len(row["daily_read_delta_points"]) == 1
    assert row["daily_read_delta_points"][0]["snapshot_count"] == 2
    assert row["provisional_status"] == "provisional"
    assert row["provisional_sample_count"] == 2
    assert (
        row["provisional_steady_read_median"]
        == row["daily_read_delta_points"][0]["read_delta"]
    )
    assert row["provisional_read_delta_estimated"] == (
        row["provisional_steady_read_median"] * 15
    )
