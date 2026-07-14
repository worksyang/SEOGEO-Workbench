"""单元测试：keyword_heat_metric 派生模型。

覆盖：
- 极值、空值/负数
- 同篇去重
- Top10 截断
- 两天观察中
- 4天上升/下降
- 无 NaN/Infinity
- 排序差异
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from app.ingest.builders.monitor_keyword_heat import (
    METHOD, METHOD_VERSION, WINDOW_DAYS,
    WT_LIKE, WT_COLLECT, WT_COMMENT, WT_SHARE,
    _safe_int, _safe_float, _clamp, _median, _rank_weight_fn, _log1p,
    _trend_ratio, _trend_label, _value_label,
    compute_engagement_equivalent, compute_run_heat,
    compute_daily_heat, compute_steady_heat, compute_peak_heat, compute_heat_delta,
    compute_trend, compute_value_signal, compute_confidence,
    compute_current_interactions, compute_top10_completeness,
    compute_daily_heat_points, build_keyword_heat_metric,
    _FULL_TOP10_WEIGHTS, _FULL_TOP10_WEIGHT_SUM,
    TREND_THRESHOLD, VALUE_THRESHOLD,
    MIN_EFFECTIVE_DAYS_FOR_TREND, MIN_EFFECTIVE_DAYS_FOR_VALUE,
    RECENT_MAX_DAYS, BASELINE_MAX_DAYS,
)
from app.services.monitor_service import _empty_keyword_payload, _sort_keywords


def _make_article(article_id, rank, liked=0, collected=0, comment=0, shared=0,
                  is_relevant=True, account_id="u1"):
    return {
        "article_id": article_id,
        "rank": rank,
        "liked_count": liked,
        "collected_count": collected,
        "comment_count": comment,
        "shared_count": shared,
        "is_relevant": is_relevant,
        "account_id": account_id,
    }


def _make_run(date, articles, captured_at=None):
    if captured_at is None:
        captured_at = f"{date}T12:00:00+08:00"
    return {
        "date": date,
        "snapshot_date": date,
        "captured_at": captured_at,
        "articles": articles,
    }


def _window_dates(base=None, days=15):
    if base is None:
        base = datetime.now()
    return [(base - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d") for i in range(days)]


class TestSafeIntFloat(unittest.TestCase):
    """极值、空值/负数测试"""

    def test_safe_int_none(self):
        self.assertEqual(_safe_int(None), 0)

    def test_safe_int_nan(self):
        self.assertEqual(_safe_int(float("nan")), 0)

    def test_safe_int_inf(self):
        self.assertEqual(_safe_int(float("inf")), 0)
        self.assertEqual(_safe_int(-float("inf")), 0)

    def test_safe_int_negative(self):
        self.assertEqual(_safe_int(-5), 0)
        self.assertEqual(_safe_int(-0.1), 0)

    def test_safe_int_float(self):
        self.assertEqual(_safe_int(3.7), 3)

    def test_safe_int_bool(self):
        self.assertEqual(_safe_int(True), 0)
        self.assertEqual(_safe_int(False), 0)

    def test_safe_float_none(self):
        self.assertEqual(_safe_float(None), 0.0)

    def test_safe_float_nan(self):
        self.assertEqual(_safe_float(float("nan")), 0.0)

    def test_safe_float_inf(self):
        self.assertEqual(_safe_float(float("inf")), 0.0)

    def test_safe_float_negative(self):
        self.assertEqual(_safe_float(-5), 0.0)

    def test_safe_float_positive(self):
        self.assertEqual(_safe_float(5), 5.0)


class TestClampMedian(unittest.TestCase):
    def test_clamp_nan(self):
        self.assertEqual(_clamp(float("nan")), 0.0)

    def test_clamp_overflow(self):
        self.assertEqual(_clamp(2.0), 1.0)
        self.assertEqual(_clamp(-2.0), -1.0)

    def test_clamp_mid(self):
        self.assertEqual(_clamp(0.5), 0.5)

    def test_median_empty(self):
        self.assertEqual(_median([]), 0.0)

    def test_median_odd(self):
        self.assertEqual(_median([1.0, 3.0, 2.0]), 2.0)

    def test_median_even(self):
        self.assertEqual(_median([1.0, 4.0, 2.0, 3.0]), 2.5)


class TestComputeEngagementEquivalent(unittest.TestCase):
    def test_basic(self):
        e = compute_engagement_equivalent({"liked_count": 10, "collected_count": 5,
                                           "comment_count": 2, "shared_count": 1})
        expected = 10 * WT_LIKE + 5 * WT_COLLECT + 2 * WT_COMMENT + 1 * WT_SHARE
        self.assertAlmostEqual(e, expected)

    def test_none_fields(self):
        e = compute_engagement_equivalent({"liked_count": None, "collected_count": None,
                                           "comment_count": None, "shared_count": None})
        self.assertEqual(e, 0.0)

    def test_negative_fields(self):
        e = compute_engagement_equivalent({"liked_count": -5, "collected_count": float("nan"),
                                           "comment_count": float("inf"), "shared_count": 0})
        self.assertEqual(e, 0.0)

    def test_partial_fields(self):
        e = compute_engagement_equivalent({"liked_count": 10})
        self.assertAlmostEqual(e, 10 * WT_LIKE)


class TestComputeRunHeat(unittest.TestCase):
    def test_empty_articles(self):
        self.assertEqual(compute_run_heat([]), 0.0)

    def test_all_irrelevant(self):
        arts = [_make_article("a1", 1, is_relevant=False)]
        self.assertEqual(compute_run_heat(arts), 0.0)

    def test_single_article(self):
        arts = [_make_article("a1", 1, liked=100, collected=20, comment=5, shared=2)]
        rh = compute_run_heat(arts)
        self.assertGreater(rh, 0)

    def test_same_article_dedup_best_rank(self):
        """同笔记去重，取最佳 rank"""
        arts = [
            _make_article("a1", 5, liked=100, account_id="u1"),
            _make_article("a1", 1, liked=100, account_id="u1"),
            _make_article("a2", 2, liked=50, account_id="u2"),
        ]
        rh = compute_run_heat(arts)
        self.assertGreater(rh, 0)

    def test_top10_truncation(self):
        """超过10篇相关笔记时截断"""
        arts = [_make_article(f"a{i}", i, liked=i * 10, account_id=f"u{i}") for i in range(1, 20)]
        rh = compute_run_heat(arts)
        self.assertGreater(rh, 0)

    def test_negative_interaction_safe(self):
        arts = [_make_article("a1", 1, liked=-5, collected=float("nan"), comment=None, shared=float("inf"))]
        rh = compute_run_heat(arts)
        self.assertEqual(rh, 0.0)


class TestComputeDailyHeat(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(compute_daily_heat([]), 0.0)

    def test_single(self):
        self.assertAlmostEqual(compute_daily_heat([5.0]), 5.0)

    def test_multiple(self):
        self.assertAlmostEqual(compute_daily_heat([1.0, 3.0, 2.0]), 2.0)


class TestComputeSteadyHeat(unittest.TestCase):
    def test_all_zero(self):
        self.assertEqual(compute_steady_heat([0.0, 0.0, 0.0]), 0.0)

    def test_some_valid(self):
        self.assertAlmostEqual(compute_steady_heat([0.0, 3.0, 0.0, 7.0, 5.0]), 5.0)


class TestComputePeakHeat(unittest.TestCase):
    def test_all_zero(self):
        self.assertEqual(compute_peak_heat([0.0, 0.0]), 0.0)

    def test_with_values(self):
        self.assertAlmostEqual(compute_peak_heat([1.0, 5.0, 0.0, 3.0]), 5.0)


class TestComputeHeatDelta(unittest.TestCase):
    def test_less_than_2_valid(self):
        self.assertEqual(compute_heat_delta([0.0, 5.0, 0.0]), 0.0)

    def test_increasing(self):
        self.assertAlmostEqual(compute_heat_delta([0.0, 2.0, 0.0, 5.0, 7.0]), 5.0)

    def test_decreasing(self):
        self.assertAlmostEqual(compute_heat_delta([10.0, 8.0, 0.0, 3.0]), -7.0)


class TestComputeTrend(unittest.TestCase):
    def test_insufficient_days(self):
        """2 个有效日 → 观察中"""
        heats = [5.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        result = compute_trend(heats, 2)
        self.assertEqual(result["trend_label"], "观察中")

    def test_4_days_upward(self):
        """4 个有效日上升趋势"""
        heats = [1.0, 2.0, 3.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        result = compute_trend(heats, 4)
        # 最近3个(2,3,4)中位数=3, 前1个(1)中位数=1
        # ratio = (3-1)/max(1,1) = 2.0 → clamp to 1.0
        self.assertEqual(result["trend_label"], "上升")
        self.assertAlmostEqual(result["trend_signal"], 1.0)

    def test_4_days_downward(self):
        """4 个有效日下降趋势"""
        heats = [10.0, 8.0, 6.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        result = compute_trend(heats, 4)
        # 最近3个(8,6,4)中位数=6, 前1个(10)中位数=10
        # ratio = (6-10)/max(10,1) = -0.4
        self.assertEqual(result["trend_label"], "下降")
        self.assertAlmostEqual(result["trend_signal"], -0.4)

    def test_5_days_flat(self):
        """5 个有效日平稳"""
        heats = [5.0, 5.0, 5.0, 5.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        result = compute_trend(heats, 5)
        self.assertEqual(result["trend_label"], "平稳")
        self.assertAlmostEqual(result["trend_signal"], 0.0)


class TestComputeValueSignal(unittest.TestCase):
    def test_positive(self):
        result = compute_value_signal(0.5, 0.3, 0.2)
        expected = 0.60 * 0.5 + 0.25 * 0.3 + 0.15 * 0.2
        self.assertAlmostEqual(result["score"], expected)
        self.assertEqual(result["label"], "价值上升")

    def test_negative(self):
        result = compute_value_signal(-0.5, -0.3, -0.2)
        expected = 0.60 * -0.5 + 0.25 * -0.3 + 0.15 * -0.2
        self.assertAlmostEqual(result["score"], expected)
        self.assertEqual(result["label"], "价值下降")

    def test_flat(self):
        result = compute_value_signal(0.0, 0.0, 0.0)
        self.assertEqual(result["label"], "价值平稳")

    def test_clamp_overflow(self):
        result = compute_value_signal(2.0, 2.0, 2.0)
        self.assertAlmostEqual(result["score"], 1.0)


class TestComputeConfidence(unittest.TestCase):
    def test_insufficient(self):
        result = compute_confidence(1, 0.5, 1, 0)
        self.assertEqual(result["confidence_level"], "insufficient")

    def test_low(self):
        result = compute_confidence(3, 0.3, 48, 0)
        self.assertEqual(result["confidence_level"], "low")

    def test_medium(self):
        # day_score=min(40,7*4)=28, completeness=0.6*30=18, freshness=max(0,20-12*20/72)=16.67, comp=min(10,4*2)=8
        # total=28+18+16.67+8=70.67 → 71 → high (>=70)
        result = compute_confidence(7, 0.6, 12, 4)
        self.assertEqual(result["confidence_level"], "high")

    def test_high(self):
        result = compute_confidence(15, 1.0, 1, 12)
        self.assertEqual(result["confidence_level"], "high")


class TestComputeCurrentInteractions(unittest.TestCase):
    def test_empty(self):
        ci = compute_current_interactions([])
        self.assertEqual(ci["note_count"], 0)
        self.assertEqual(ci["likes"], 0)

    def test_filter_irrelevant(self):
        arts = [
            _make_article("a1", 1, liked=100, is_relevant=True, account_id="u1"),
            _make_article("a2", 2, liked=999, is_relevant=False, account_id="u2"),
        ]
        ci = compute_current_interactions(arts)
        self.assertEqual(ci["note_count"], 1)
        self.assertEqual(ci["likes"], 100)

    def test_interaction_structure(self):
        arts = [
            _make_article("a1", 1, liked=100, collected=20, comment=5, shared=2, account_id="u1"),
        ]
        ci = compute_current_interactions(arts)
        self.assertIn("interaction_structure", ci)
        struct = ci["interaction_structure"]
        total = 100 * WT_LIKE + 20 * WT_COLLECT + 5 * WT_COMMENT + 2 * WT_SHARE
        self.assertAlmostEqual(struct["likes_pct"], round(100 * WT_LIKE / total * 100, 1))


class TestComputeTop10Completeness(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(compute_top10_completeness([]), 0.0)

    def test_full(self):
        arts = [_make_article(f"a{i}", i, account_id=f"u{i}") for i in range(1, 11)]
        self.assertAlmostEqual(compute_top10_completeness(arts), 1.0)

    def test_partial(self):
        arts = [_make_article(f"a{i}", i, account_id=f"u{i}") for i in range(1, 6)]
        self.assertAlmostEqual(compute_top10_completeness(arts), 0.5)


class TestBuildKeywordHeatMetric(unittest.TestCase):
    def setUp(self):
        self.wd = _window_dates()

    def test_no_runs(self):
        result = build_keyword_heat_metric([], self.wd)
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["effective_days"], 0)
        self.assertEqual(result["trend_label"], "观察中")
        self.assertEqual(result["confidence_level"], "insufficient")

    def test_interaction_weights_contract(self):
        result = build_keyword_heat_metric([], self.wd)
        self.assertEqual(result["interaction_weights"], {
            "likes": 1.0,
            "collects": 2.5,
            "comments": 3.0,
            "shares": 4.0,
            "method_note": "互动当量 E = 点赞×1.0 + 收藏×2.5 + 评论×3.0 + 分享×4.0；负数或空值按 0 计。",
        })

    def test_empty_payload_interaction_weights_contract(self):
        payload = _empty_keyword_payload(
            {"keyword_id": "kw_empty", "keyword_text": "空关键词"},
            {"label": "测试"},
            15,
        )
        self.assertEqual(
            payload["keyword_heat_metric"]["interaction_weights"],
            build_keyword_heat_metric([], self.wd)["interaction_weights"],
        )

    def test_only_successful_runs_are_included(self):
        base = datetime(2026, 7, 12, 12, 0, 0)
        wd = _window_dates(base)
        old_date = (base - timedelta(days=2)).strftime("%Y-%m-%d")
        completed_date = (base - timedelta(days=1)).strftime("%Y-%m-%d")
        failed_date = base.strftime("%Y-%m-%d")
        runs = [
            _make_run(
                old_date,
                [_make_article("missing-status", 1, liked=10, account_id="u1")],
                f"{old_date}T10:00:00+08:00",
            ),
            {
                **_make_run(
                    completed_date,
                    [_make_article("completed", 1, liked=20, account_id="u2")],
                    f"{completed_date}T10:00:00+08:00",
                ),
                "status": "completed",
            },
            {
                **_make_run(
                    failed_date,
                    [_make_article("failed", 1, liked=999999, account_id="u3")],
                    f"{failed_date}T23:59:00+08:00",
                ),
                "status": "failed",
            },
        ]

        result = build_keyword_heat_metric(runs, wd)
        points = {point["date"]: point for point in result["daily_heat_points"]}

        self.assertEqual(result["effective_days"], 2)
        self.assertEqual(points[failed_date]["run_count"], 0)
        self.assertEqual(points[failed_date]["heat"], 0.0)
        self.assertEqual(result["current_interactions"]["likes"], 20)
        self.assertEqual(result["current_interactions"]["note_count"], 1)

    def test_only_2_days(self):
        """只有约2天数据时趋势和价值均为观察中"""
        base = datetime.now()
        wd = _window_dates(base)
        runs = []
        for i in range(2):
            date = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            runs.append(_make_run(date, [
                _make_article("a1", 1, liked=100, collected=20, comment=5, shared=2, account_id="u1"),
            ]))
        result = build_keyword_heat_metric(runs, wd)
        self.assertEqual(result["effective_days"], 2)
        self.assertEqual(result["trend_label"], "观察中")
        self.assertEqual(result["value_signal"]["label"], "观察中")
        # confidence: effective_days=2 >= 2, so not insufficient
        self.assertIn(result["confidence_level"], ["low", "medium", "high"])

    def test_4_days_rising(self):
        """4天上升趋势"""
        base = datetime.now()
        wd = _window_dates(base)
        runs = []
        for i in range(4):
            date = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            # i=0(most recent)=1000, i=3(oldest)=10 → 上升
            likes = 1000 if i < 3 else 10
            runs.append(_make_run(date, [
                _make_article("a1", 1, liked=likes, collected=likes // 5, comment=likes // 20,
                              shared=likes // 50, account_id="u1"),
                _make_article("a2", 2, liked=likes // 2, collected=likes // 10, comment=likes // 40,
                              shared=likes // 100, account_id="u2"),
            ]))
        result = build_keyword_heat_metric(runs, wd)
        self.assertGreaterEqual(result["effective_days"], 4)
        # recent(1000,1000,1000) heat > baseline(10) heat → 上升
        self.assertEqual(result["trend_label"], "上升")

    def test_4_days_falling(self):
        """4天下降趋势"""
        base = datetime.now()
        wd = _window_dates(base)
        runs = []
        for i in range(4):
            date = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            # i=0(most recent)=10, i=3(oldest)=1000 → 下降
            likes = 10 if i < 3 else 1000
            runs.append(_make_run(date, [
                _make_article("a1", 1, liked=likes, collected=likes // 5, comment=likes // 20,
                              shared=likes // 50, account_id="u1"),
            ]))
        result = build_keyword_heat_metric(runs, wd)
        self.assertGreaterEqual(result["effective_days"], 4)
        # recent(10,10,10) median=10.895, baseline(1000)=31.3903 → ratio=-0.65 → 下降
        self.assertEqual(result["trend_label"], "下降")

    def test_no_nan_infinity(self):
        """所有字段无 NaN/Infinity"""
        result = build_keyword_heat_metric([], self.wd)
        self._assert_no_nan_inf(result)

    def _assert_no_nan_inf(self, d, path=""):
        if isinstance(d, dict):
            for k, v in d.items():
                self._assert_no_nan_inf(v, f"{path}.{k}")
        elif isinstance(d, list):
            for i, v in enumerate(d):
                self._assert_no_nan_inf(v, f"{path}[{i}]")
        elif isinstance(d, float):
            self.assertFalse(math.isnan(d), f"NaN at {path}")
            self.assertFalse(math.isinf(d), f"Inf at {path}")

    def test_monitor_service_sorting(self):
        """pinned 第一；其余 steady desc、peak desc、名称；无数据最后。"""
        keywords = [
            {"keyword": "无数据", "is_pinned": False,
             "keyword_heat_metric": {"effective_days": 0, "steady_heat": 999, "peak_heat": 999}},
            {"keyword": "B", "is_pinned": False,
             "keyword_heat_metric": {"effective_days": 2, "steady_heat": 10, "peak_heat": 50}},
            {"keyword": "C", "is_pinned": False,
             "keyword_heat_metric": {"effective_days": 2, "steady_heat": 10, "peak_heat": 60}},
            {"keyword": "A", "is_pinned": False,
             "keyword_heat_metric": {"effective_days": 2, "steady_heat": 10, "peak_heat": 60}},
            {"keyword": "高常态", "is_pinned": False,
             "keyword_heat_metric": {"effective_days": 2, "steady_heat": 20, "peak_heat": 20}},
            {"keyword": "置顶", "is_pinned": True,
             "keyword_heat_metric": {"effective_days": 0, "steady_heat": 0, "peak_heat": 0}},
        ]

        ordered = _sort_keywords(keywords)

        self.assertEqual(
            [item["keyword"] for item in ordered],
            ["置顶", "高常态", "A", "C", "B", "无数据"],
        )

    def test_peak_date(self):
        """峰值日期正确"""
        base = datetime(2026, 7, 12, 12, 0, 0)
        wd = _window_dates(base)
        peak_date = (base - timedelta(days=2)).strftime("%Y-%m-%d")
        runs = []
        for i in range(4):
            date = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            likes = 50 if i != 2 else 500
            runs.append(_make_run(date, [
                _make_article("a1", 1, liked=likes, account_id="u1"),
            ]))
        result = build_keyword_heat_metric(runs, wd)
        # i=2 is the peak day (likes=500)
        self.assertEqual(result["peak_date"], peak_date,
                         f"Expected peak_date={peak_date}, got {result['peak_date']}")


class TestComputeDailyHeatPoints(unittest.TestCase):
    def test_structure(self):
        wd = _window_dates()
        points = compute_daily_heat_points(
            {d: 1.0 for d in wd},
            {d: 2 for d in wd},
            {d: 3 for d in wd},
            {d: 4 for d in wd},
            {d: 100 for d in wd},
            {d: 20 for d in wd},
            {d: 5 for d in wd},
            {d: 2 for d in wd},
            {d: 150.0 for d in wd},
            wd,
        )
        self.assertEqual(len(points), 15)
        for p in points:
            self.assertIn("date", p)
            self.assertIn("heat", p)
            self.assertIn("run_count", p)
            self.assertIn("note_count", p)
            self.assertIn("creator_count", p)
            self.assertIn("likes", p)
            self.assertIn("collects", p)
            self.assertIn("comments", p)
            self.assertIn("shares", p)
            self.assertIn("equivalent", p)


if __name__ == "__main__":
    unittest.main()
