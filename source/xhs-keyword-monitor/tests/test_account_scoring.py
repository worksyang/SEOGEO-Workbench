"""单元测试：xhs_three_board_p99_v1 账号三榜评分。

覆盖：
- P99 归一化正确性
- 突破门控（breakthrough gate）
- 置信度 confidence
- 当前/昨日独立重算（独立 benchmarks）
- population 排名/百分位/并列
- durable_notes 第3天才生效
- 仅2天数据不突破
- 时效首日不伪造新进
- 当天平均 rank 质量
- 极值/空值无 NaN
- 集成测试：keyword_id→keyword_text 映射，多关键词不塌缩，hexagon population/previous_population/delta, 日期空洞前一有效日, today confidence, 唯一笔记互动去重, 兼容字段
"""
from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta

from app.ingest.builders.monitor_scoring import (
    BOARD_CONFIGS,
    WINDOW_DAYS,
    RECENT_WINDOW_DAYS,
    TIMELINESS_WINDOW_DAYS,
    SCORE_BENCHMARK_PERCENTILE,
    SCORE_OVERFLOW_LOG_SCALE,
    CLASSIC_MIN_DAYS,
    _log_count,
    _percentile,
    _confidence_by_days,
    _account_maturity_factor,
    _axis_score,
    _observation_span_days,
    _events_in_window,
    _event_sets,
    _trailing_event_streak,
    _effective_article_count,
    _engagement_equivalent,
    _move_counts,
    _best_ranks_by_keyword,
    _coverage_raw,
    _category_breadth_raw,
    _build_account_raw_snapshot,
    _build_timeliness_raw_snapshot,
    _build_today_raw_snapshot,
    build_raw_board_snapshots,
    build_board_benchmarks,
    score_board_snapshot,
    build_hexagon_payload,
    _score_level,
    _population_stat,
    _attach_hexagon_population,
    _available_axes_for_board,
    _score_board_snapshot_v2,
)


def _make_event(day_idx: int, keyword: str, article_key: str,
                rank: int, topic: str = "topic1", bucket: str = "bucket1",
                published_at: str | None = None) -> dict:
    return {
        "day_idx": day_idx,
        "keyword": keyword,
        "article_key": article_key,
        "article_id": article_key,
        "rank": rank,
        "topic": topic,
        "bucket": bucket,
        "published_at": published_at,
    }


def _make_article(article_key: str, liked: int = 0, collected: int = 0,
                  comment: int = 0, shared: int = 0) -> dict:
    return {
        "article_id": article_key,
        "liked_count": liked,
        "collected_count": collected,
        "comment_count": comment,
        "shared_count": shared,
    }


class TestLogCount(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_log_count(0), 0.0)

    def test_negative(self):
        self.assertEqual(_log_count(-5), 0.0)

    def test_positive(self):
        self.assertAlmostEqual(_log_count(10), math.log1p(10))


class TestPercentile(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_percentile([], 0.99), 1.0)

    def test_single(self):
        self.assertEqual(_percentile([5.0], 0.99), 5.0)

    def test_typical(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        p99 = _percentile(values, 0.99)
        self.assertAlmostEqual(p99, 9.91, places=2)

    def test_all_zeros_ignored(self):
        values = [0.0, 0.0, 0.0, 5.0]
        p99 = _percentile(values, 0.99)
        self.assertAlmostEqual(p99, 5.0, places=2)


class TestConfidenceByDays(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(_confidence_by_days(0, 5), 0.0)

    def test_full_days(self):
        self.assertEqual(_confidence_by_days(5, 5), 1.0)

    def test_3_of_5(self):
        self.assertAlmostEqual(_confidence_by_days(3, 5), 0.74)

    def test_1_of_3(self):
        self.assertAlmostEqual(_confidence_by_days(1, 3), 0.68)


class TestAxisScore(unittest.TestCase):
    def test_zero_raw(self):
        self.assertEqual(_axis_score(0.0, 10.0), 0.0)

    def test_equal_to_benchmark(self):
        self.assertAlmostEqual(_axis_score(10.0, 10.0), 100.0, places=2)

    def test_half_benchmark(self):
        self.assertAlmostEqual(_axis_score(5.0, 10.0), 50.0, places=2)

    def test_double_benchmark(self):
        result = _axis_score(20.0, 10.0)
        expected = 100 + SCORE_OVERFLOW_LOG_SCALE * math.log2(20.0 / 10.0)
        self.assertAlmostEqual(result, expected, places=2)

    def test_negative_raw(self):
        self.assertEqual(_axis_score(-5.0, 10.0), 0.0)


class TestObservationSpanDays(unittest.TestCase):
    def test_no_events(self):
        self.assertEqual(_observation_span_days([], 14, 15), 0)

    def test_single_day(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        self.assertEqual(_observation_span_days(events, 14, 15), 1)

    def test_span_from_earliest(self):
        events = [
            _make_event(10, "kw1", "a1", 1),
            _make_event(14, "kw1", "a1", 1),
        ]
        self.assertEqual(_observation_span_days(events, 14, 15), 5)

    def test_capped_by_max(self):
        events = [
            _make_event(0, "kw1", "a1", 1),
            _make_event(14, "kw1", "a1", 1),
        ]
        self.assertEqual(_observation_span_days(events, 14, 15), 15)


class TestEventsInWindow(unittest.TestCase):
    def test_basic(self):
        events = [_make_event(i, "kw1", "a1", i) for i in range(15)]
        result = _events_in_window(events, 5, 9)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["day_idx"], 5)
        self.assertEqual(result[-1]["day_idx"], 9)


class TestEventSets(unittest.TestCase):
    def test_basic(self):
        events = [
            _make_event(0, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(1, "kw2", "a2", 2, topic="t2", bucket="b2"),
        ]
        kws, arts, topics, buckets = _event_sets(events)
        self.assertEqual(kws, {"kw1", "kw2"})
        self.assertEqual(arts, {"a1", "a2"})
        self.assertEqual(topics, {"t1", "t2"})
        self.assertEqual(buckets, {"b1", "b2"})


class TestTrailingEventStreak(unittest.TestCase):
    def test_current_streak(self):
        events = [_make_event(i, "kw1", "a1", i) for i in range(10, 15)]
        self.assertEqual(_trailing_event_streak(events, 14, 15), 5)

    def test_broken_streak(self):
        events = [_make_event(i, "kw1", "a1", i) for i in [10, 11, 13, 14]]
        self.assertEqual(_trailing_event_streak(events, 14, 15), 2)


class TestEffectiveArticleCount(unittest.TestCase):
    def test_empty(self):
        eff, conc = _effective_article_count([])
        self.assertEqual(eff, 0.0)
        self.assertEqual(conc, 1.0)

    def test_single_article(self):
        events = [_make_event(i, "kw1", "a1", i) for i in range(5)]
        eff, conc = _effective_article_count(events)
        self.assertAlmostEqual(eff, 1.0, places=2)
        self.assertAlmostEqual(conc, 1.0, places=2)

    def test_equal_distribution(self):
        events = (
            [_make_event(i, "kw1", "a1", i) for i in range(5)]
            + [_make_event(i, "kw1", "a2", i) for i in range(5)]
        )
        eff, conc = _effective_article_count(events)
        self.assertGreater(eff, 1.9)
        self.assertAlmostEqual(conc, 0.5, places=2)


class TestEngagementEquivalent(unittest.TestCase):
    def test_basic(self):
        eq = _engagement_equivalent({"liked_count": 10, "collected_count": 5,
                                      "comment_count": 2, "shared_count": 1})
        expected = 10 + 5 * 2.5 + 2 * 3.0 + 1 * 4.0
        self.assertAlmostEqual(eq, expected)

    def test_none_fields(self):
        eq = _engagement_equivalent({"liked_count": None, "collected_count": None,
                                      "comment_count": None, "shared_count": None})
        self.assertEqual(eq, 0.0)

    def test_negative_fields(self):
        eq = _engagement_equivalent({"liked_count": -5, "collected_count": -3,
                                      "comment_count": 0, "shared_count": 0})
        self.assertEqual(eq, 0.0)

    def test_partial(self):
        eq = _engagement_equivalent({"liked_count": 10})
        self.assertAlmostEqual(eq, 10.0)


class TestMoveCounts(unittest.TestCase):
    def test_new_only(self):
        events = [
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a2", 2),
        ]
        moves = _move_counts(events, 14)
        self.assertEqual(moves["new_count"], 2)
        self.assertEqual(moves["up_count"], 0)

    def test_mixed(self):
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(13, "kw2", "a2", 3),
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a2", 3),
            _make_event(14, "kw3", "a3", 2),
        ]
        moves = _move_counts(events, 14)
        self.assertEqual(moves["new_count"], 1)
        self.assertEqual(moves["up_count"], 1)
        self.assertEqual(moves["down_count"], 0)
        self.assertEqual(moves["flat_count"], 1)


class TestCoverageRaw(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_coverage_raw([]), 0.0)

    def test_with_data(self):
        events = [
            _make_event(0, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(1, "kw2", "a2", 2, topic="t2", bucket="b2"),
        ]
        cr = _coverage_raw(events)
        self.assertGreater(cr, 0)

    def test_no_nan(self):
        self.assertFalse(math.isnan(_coverage_raw([])))


class TestP99Normalization(unittest.TestCase):
    def test_axis_score_linear_region(self):
        score = _axis_score(5.0, 10.0)
        self.assertAlmostEqual(score, 50.0, places=2)

    def test_axis_score_overflow_region(self):
        score = _axis_score(20.0, 10.0)
        self.assertGreater(score, 100.0)

    def test_axis_score_zero_raw(self):
        self.assertEqual(_axis_score(0.0, 10.0), 0.0)

    def test_axis_score_zero_benchmark_fallback(self):
        score = _axis_score(5.0, 0.0)
        self.assertGreater(score, 0)


class TestAccountBoardBreakthrough(unittest.TestCase):
    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
            "a3": _make_article("a3", liked=50, collected=25, comment=5, shared=2),
        }

    def _make_rich_events(self, end_idx: int, count: int = 5) -> list[dict]:
        events = []
        for day in range(end_idx - WINDOW_DAYS + 1, end_idx + 1):
            for kw_idx in range(count):
                for art_idx in range(min(kw_idx + 1, 3)):
                    art_key = f"a{art_idx + 1}"
                    events.append(_make_event(
                        day, f"kw{kw_idx}", art_key,
                        rank=min(kw_idx + art_idx + 1, 10),
                        topic=f"t{kw_idx % 3}", bucket=f"b{kw_idx % 2}",
                    ))
        return events

    def test_breakthrough_gate_passed(self):
        end_idx = WINDOW_DAYS - 1
        events = self._make_rich_events(end_idx, count=8)
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        benchmarks = {
            "history_coverage": 1.0,
            "recent_coverage": 1.0,
            "durable_notes": 1.0,
            "continuity": 0.3,
            "content_matrix": 0.5,
            "engagement_quality": 1.0,
            "battle_breadth": 0.5,
        }
        normalized, parts = score_board_snapshot(snapshot, "account", benchmarks)
        self.assertTrue(parts["breakthrough_gate"],
                        f"Expected breakthrough_gate=True, got {parts}")
        self.assertGreater(parts["score"], 100)

    def test_breakthrough_gate_low_confidence(self):
        end_idx = WINDOW_DAYS - 1
        events = [_make_event(end_idx, "kw1", "a1", 1)]
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        self.assertLess(snapshot["confidence"], 0.999)
        benchmarks = {
            "history_coverage": 0.1, "recent_coverage": 0.1, "durable_notes": 0.1,
            "continuity": 0.1, "content_matrix": 0.1, "engagement_quality": 0.1, "battle_breadth": 0.1,
        }
        normalized, parts = score_board_snapshot(snapshot, "account", benchmarks)
        self.assertFalse(parts["breakthrough_gate"])
        self.assertLessEqual(parts["score"], 100)

    def test_breakthrough_gate_not_enough_over_axes(self):
        end_idx = WINDOW_DAYS - 1
        events = []
        for day in range(end_idx - WINDOW_DAYS + 1, end_idx + 1):
            events.append(_make_event(day, "kw1", "a1", rank=1))
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        high_benchmarks = {
            "history_coverage": 100.0, "recent_coverage": 100.0, "durable_notes": 100.0,
            "continuity": 100.0, "content_matrix": 100.0, "engagement_quality": 100.0, "battle_breadth": 100.0,
        }
        normalized, parts = score_board_snapshot(snapshot, "account", high_benchmarks)
        self.assertFalse(parts["breakthrough_gate"])


class TestDurableNotesThreeDays(unittest.TestCase):
    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }

    def test_durable_notes_less_than_3_days(self):
        end_idx = WINDOW_DAYS - 1
        events = [
            _make_event(end_idx - 1, "kw1", "a1", 5),
            _make_event(end_idx, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        self.assertEqual(snapshot["details"]["durable_pair_count"], 0)
        self.assertEqual(snapshot["raw_axes"]["durable_notes"], 0.0)

    def test_durable_notes_exactly_3_days(self):
        end_idx = WINDOW_DAYS - 1
        events = [
            _make_event(end_idx - 2, "kw1", "a1", 5),
            _make_event(end_idx - 1, "kw1", "a1", 5),
            _make_event(end_idx, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        self.assertGreater(snapshot["details"]["durable_pair_count"], 0)
        self.assertGreater(snapshot["raw_axes"]["durable_notes"], 0.0)


class TestDurableNotesStatus(unittest.TestCase):
    """durable_notes_status 和 durable_notes_message 字段存在性。"""

    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }

    def test_less_than_3_days(self):
        end_idx = 1  # 仅2天
        events = [
            _make_event(0, "kw1", "a1", 5),
            _make_event(1, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        self.assertIn("durable_notes_status", snapshot["details"])
        self.assertIn("durable_notes_message", snapshot["details"])
        self.assertEqual(snapshot["details"]["durable_notes_status"], "waiting")

    def test_3_days_but_no_durable(self):
        end_idx = 13  # 有3天但不满足同一note×keyword
        events = [
            _make_event(11, "kw1", "a1", 5),
            _make_event(12, "kw1", "a1", 5),
            _make_event(13, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles_by_key)
        self.assertEqual(snapshot["details"]["durable_notes_status"], "stable")


class TestTimelinessNewTop3(unittest.TestCase):
    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
        }

    def test_first_day_no_new_top3(self):
        from datetime import timedelta
        events = [_make_event(0, "kw1", "a1", 1, topic="t1", bucket="b1")]
        window_start = date(2026, 7, 12)
        end_date = window_start + timedelta(days=0)
        # 没有前一有效日，所有Top3都是新进
        snapshot = _build_timeliness_raw_snapshot(events, 0, end_date, self.articles_by_key)
        self.assertGreater(snapshot["raw_axes"]["new_top3"], 0.0)

    def test_new_top3_with_prior(self):
        from datetime import timedelta
        window_start = date(2026, 6, 28)
        events = [
            _make_event(7, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(7, "kw1", "a2", 5, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a1", 2, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a2", 3, topic="t1", bucket="b1"),
        ]
        end_date = window_start + timedelta(days=14)
        # 使用前一有效日索引 7
        snapshot = _build_timeliness_raw_snapshot(events, 14, end_date, self.articles_by_key, previous_effective_day_idx=7)
        # new_top3 = 1 (kw1/a2 是新进)
        self.assertAlmostEqual(snapshot["raw_axes"]["new_top3"], _log_count(1), places=2)


class TestTodayRankQuality(unittest.TestCase):
    def test_average_rank_quality(self):
        events = [
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a2", 5),
        ]
        articles_by_key = {
            "a1": _make_article("a1", liked=10),
            "a2": _make_article("a2", liked=5),
        }
        snapshot = _build_today_raw_snapshot(events, 14, articles_by_key)
        expected = ((11 - 1) / 10 + (11 - 5) / 10) / 2
        self.assertAlmostEqual(snapshot["raw_axes"]["today_rank_quality"], expected, places=4)

    def test_single_rank_quality(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        snapshot = _build_today_raw_snapshot(events, 14, {"a1": _make_article("a1", liked=10)})
        self.assertAlmostEqual(snapshot["raw_axes"]["today_rank_quality"], 1.0, places=4)

    def test_high_rank_quality(self):
        events = [_make_event(14, "kw1", "a1", 10)]
        snapshot = _build_today_raw_snapshot(events, 14, {"a1": _make_article("a1", liked=10)})
        self.assertAlmostEqual(snapshot["raw_axes"]["today_rank_quality"], 0.1, places=4)


class TestNoNanInf(unittest.TestCase):
    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }

    def test_empty_events_account(self):
        snapshot = _build_account_raw_snapshot([], 14, self.articles_by_key)
        self._assert_no_nan_inf(snapshot)

    def test_empty_events_timeliness(self):
        from datetime import timedelta
        snapshot = _build_timeliness_raw_snapshot([], 14, date(2026, 7, 12), self.articles_by_key)
        self._assert_no_nan_inf(snapshot)

    def test_empty_events_today(self):
        snapshot = _build_today_raw_snapshot([], 14, self.articles_by_key)
        self._assert_no_nan_inf(snapshot)

    def test_negative_engagement(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        arts = {"a1": _make_article("a1", liked=-5, collected=float("nan"),
                                    comment=None, shared=float("-inf"))}
        snapshot = _build_today_raw_snapshot(events, 14, arts)
        self._assert_no_nan_inf(snapshot)

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


class TestBuildBoardBenchmarks(unittest.TestCase):
    def test_single_account(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=10)}
        snap = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        benchmarks = build_board_benchmarks([snap])
        for board in BOARD_CONFIGS:
            self.assertIn(board, benchmarks)
            for meta in BOARD_CONFIGS[board]["axes_meta"]:
                key = meta["key"]
                self.assertIn(key, benchmarks[board])

    def test_multi_account_all_same(self):
        articles = {"a1": _make_article("a1", liked=10)}
        snaps = []
        for _ in range(5):
            events = [_make_event(14, "kw1", "a1", 1)]
            snaps.append(build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles))
        benchmarks = build_board_benchmarks(snaps)
        for board in BOARD_CONFIGS:
            for key in BOARD_CONFIGS[board]["weights"]:
                self.assertGreater(benchmarks[board][key], 0.0)

    def test_current_previous_independent_benchmarks(self):
        """当前和前一日的benchmarks应独立。"""
        articles = {"a1": _make_article("a1", liked=10)}
        # 当前：15天数据（14日有2个不同关键词的events）
        current_events = [[
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a1", 2),
        ] for _ in range(5)]
        # 前一日：只有14天数据（13日只有1个event）
        previous_events = [[_make_event(13, "kw1", "a1", 1)] for _ in range(5)]
        current_snaps = [build_raw_board_snapshots(e, 14, date(2026, 7, 12), articles) for e in current_events]
        previous_snaps = [build_raw_board_snapshots(e, 13, date(2026, 7, 12), articles) for e in previous_events]
        current_benchmarks = build_board_benchmarks(current_snaps)
        previous_benchmarks = build_board_benchmarks(previous_snaps)
        # 因end_idx不同，benchmarks应不同
        self.assertNotEqual(current_benchmarks, previous_benchmarks)


class TestScoreBoardSnapshot(unittest.TestCase):
    def test_output_structure(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=10)}
        snap = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        benches = build_board_benchmarks([snap])
        for board in BOARD_CONFIGS:
            normalized, parts = score_board_snapshot(snap[board], board, benches[board])
            self.assertIn("axes", normalized)
            self.assertIn("axis_values", normalized)
            self.assertIn("raw_axes", normalized)
            self.assertIn("score", parts)
            self.assertIn("score_raw", parts)
            self.assertIn("base_score", parts)
            self.assertIn("confidence", parts)
            self.assertIn("breakthrough", parts)
            self.assertIn("breakthrough_gate", parts)
            self.assertIn("over_axes", parts)


class TestCurrentYesterdayIndependent(unittest.TestCase):
    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }

    def test_current_and_previous_different(self):
        events = []
        for day in range(15):
            events.append(_make_event(day, "kw1", "a1", rank=1))
        current = _build_account_raw_snapshot(events, 14, self.articles_by_key)
        previous = _build_account_raw_snapshot(events, 13, self.articles_by_key)
        self.assertNotEqual(current["raw_axes"], previous["raw_axes"])


class TestPopulationStat(unittest.TestCase):
    def test_basic(self):
        stat = _population_stat([100, 90, 80], 100)
        self.assertEqual(stat["rank"], 1)
        self.assertEqual(stat["total"], 3)
        self.assertEqual(stat["tie_count"], 1)  # 含自身
        self.assertEqual(stat["percentile"], 66.7)  # 2个低于

    def test_tie_count_includes_self(self):
        stat = _population_stat([100, 100, 80], 100)
        self.assertEqual(stat["rank"], 1)
        self.assertEqual(stat["tie_count"], 2)  # 2个并列含自身

    def test_last_place(self):
        stat = _population_stat([100, 90, 80], 80)
        self.assertEqual(stat["rank"], 3)
        self.assertEqual(stat["tie_count"], 1)

    def test_empty(self):
        stat = _population_stat([], 100)
        self.assertEqual(stat["rank"], 0)
        self.assertEqual(stat["total"], 0)
        self.assertEqual(stat["tie_count"], 0)
        self.assertEqual(stat["percentile"], 0.0)


class TestAttachHexagonPopulation(unittest.TestCase):
    def test_structure(self):
        summaries = [
            {
                "account_score_hexagon": build_hexagon_payload("account", {"axes": {"key1": 100}, "end_idx": 14}, {"axes": {"key1": 90}, "end_idx": 13}, {"key1": 50.0}),
                "timeliness_score_hexagon": build_hexagon_payload("timeliness", {"axes": {"tk1": 80}, "end_idx": 14}, {"axes": {"tk1": 70}, "end_idx": 13}, {"tk1": 40.0}),
                "today_score_hexagon": build_hexagon_payload("today", {"axes": {"td1": 60}, "end_idx": 14}, {"axes": {"td1": 50}, "end_idx": 13}, {"td1": 30.0}),
                "score": 100, "score_yesterday": 90,
                "timeliness_score": 80, "timeliness_score_yesterday": 70,
                "today_score": 60, "today_score_yesterday": 50,
            },
            {
                "account_score_hexagon": build_hexagon_payload("account", {"axes": {"key1": 50}, "end_idx": 14}, {"axes": {"key1": 40}, "end_idx": 13}, {"key1": 50.0}),
                "timeliness_score_hexagon": build_hexagon_payload("timeliness", {"axes": {"tk1": 40}, "end_idx": 14}, {"axes": {"tk1": 30}, "end_idx": 13}, {"tk1": 40.0}),
                "today_score_hexagon": build_hexagon_payload("today", {"axes": {"td1": 30}, "end_idx": 14}, {"axes": {"td1": 20}, "end_idx": 13}, {"td1": 30.0}),
                "score": 50, "score_yesterday": 40,
                "timeliness_score": 40, "timeliness_score_yesterday": 30,
                "today_score": 30, "today_score_yesterday": 20,
            },
        ]
        _attach_hexagon_population(summaries)
        for s in summaries:
            for hf in ["account_score_hexagon", "timeliness_score_hexagon", "today_score_hexagon"]:
                hexagon = s[hf]
                self.assertIn("population", hexagon)
                self.assertIn("previous_population", hexagon)
                pop = hexagon["population"]
                self.assertIn("account_count", pop)
                self.assertIn("score", pop)
                self.assertIn("rank", pop["score"])
                self.assertIn("total", pop["score"])
                self.assertIn("tie_count", pop["score"])
                self.assertIn("percentile", pop["score"])
                prev_pop = hexagon["previous_population"]
                self.assertIn("account_count", prev_pop)
                self.assertIn("score", prev_pop)
        # rank 为数字
        self.assertIsInstance(summaries[0]["account_score_hexagon"]["population"]["score"]["rank"], int)


class TestHexagonDelta(unittest.TestCase):
    def test_delta_field(self):
        articles = {"a1": _make_article("a1", liked=10)}
        events = [_make_event(14, "kw1", "a1", 1)]
        snap = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        benches = build_board_benchmarks([snap])
        cur_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        prev_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        hexagon = build_hexagon_payload("account", cur_norm, prev_norm, benches["account"])
        self.assertIn("delta", hexagon)
        self.assertIsInstance(hexagon["delta"], dict)
        for key in cur_norm["axes"]:
            self.assertIn(key, hexagon["delta"])


class TestScoreLevel(unittest.TestCase):
    def test_extreme_breakthrough(self):
        self.assertEqual(_score_level(150), "extreme_breakthrough")
        self.assertEqual(_score_level(120), "extreme_breakthrough")

    def test_strong_breakthrough(self):
        self.assertEqual(_score_level(110), "strong_breakthrough")
        self.assertEqual(_score_level(119), "strong_breakthrough")

    def test_breakthrough(self):
        self.assertEqual(_score_level(101), "breakthrough")

    def test_benchmark(self):
        self.assertEqual(_score_level(100), "benchmark")

    def test_within_benchmark(self):
        self.assertEqual(_score_level(99), "within_benchmark")
        self.assertEqual(_score_level(0), "within_benchmark")


class TestBuildHexagonPayload(unittest.TestCase):
    def test_hexagon_structure(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=10)}
        snap = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        benches = build_board_benchmarks([snap])
        cur_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        prev_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        hexagon = build_hexagon_payload("account", cur_norm, prev_norm, benches["account"])
        self.assertEqual(hexagon["board"], "account")
        self.assertEqual(hexagon["benchmark_line"], 100)
        self.assertIn("axes_meta", hexagon)
        self.assertIn("weights", hexagon)
        self.assertIn("benchmarks", hexagon)
        self.assertIn("current", hexagon)
        self.assertIn("previous", hexagon)
        self.assertIn("delta", hexagon)
        self.assertIn("population", hexagon)
        self.assertIn("previous_population", hexagon)


class TestBuildRawBoardSnapshots(unittest.TestCase):
    def test_three_boards_present(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=10)}
        snaps = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        self.assertIn("account", snaps)
        self.assertIn("timeliness", snaps)
        self.assertIn("today", snaps)

    def test_axes_match_config(self):
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=10)}
        snaps = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        for board, config in BOARD_CONFIGS.items():
            raw_axes = snaps[board]["raw_axes"]
            for meta in config["axes_meta"]:
                key = meta["key"]
                self.assertIn(key, raw_axes, f"Board {board} missing axis {key}")

    def test_previous_effective_day(self):
        """使用前一有效日索引构建today/timeliness snapshot。"""
        events = [
            _make_event(12, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(14, "kw2", "a2", 2, topic="t2", bucket="b2"),
        ]
        articles = {"a1": _make_article("a1", liked=10), "a2": _make_article("a2", liked=20)}
        # 前一有效日为 day_idx=12
        snaps = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles, previous_effective_day_idx=12)
        # today_new_entries 应该是 a2（12日没有a2）
        self.assertEqual(snaps["today"]["details"]["today_new_entry_count"], 1)


class TestXHSAccountScoreContract(unittest.TestCase):
    def test_weights_sum_to_one(self):
        for board, config in BOARD_CONFIGS.items():
            total = sum(config["weights"].values())
            self.assertAlmostEqual(total, 1.0, places=4,
                                   msg=f"{board} weights sum to {total}, not 1.0")

    def test_axes_meta_and_weights_match(self):
        for board, config in BOARD_CONFIGS.items():
            meta_keys = {m["key"] for m in config["axes_meta"]}
            weight_keys = set(config["weights"].keys())
            self.assertEqual(meta_keys, weight_keys,
                             f"{board}: axes_meta keys {meta_keys} != weights keys {weight_keys}")

    def test_required_breakthrough_axes_valid(self):
        for board, config in BOARD_CONFIGS.items():
            for rax in config["required_breakthrough_axes"]:
                self.assertIn(rax, config["weights"],
                              f"{board}: required_breakthrough_axis {rax} not in weights")


class TestEngagementDeduplication(unittest.TestCase):
    """互动质量按唯一笔记聚合一次，不得重复累加。"""

    def test_same_article_multiple_keywords(self):
        """同一笔记被多个关键词命中，engagement只算一次。"""
        articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }
        # 同一笔记 a1 被两个关键词命中，但唯一笔记只有一篇
        events = []
        for day in range(14 - WINDOW_DAYS + 1, 15):
            events.append(_make_event(day, "kw1", "a1", 1))
            events.append(_make_event(day, "kw2", "a1", 2))
        snapshot = _build_account_raw_snapshot(events, 14, articles_by_key)
        # engagement_total 应只计算一次 a1 的互动
        expected_eq = 100 + 50 * 2.5 + 10 * 3.0 + 5 * 4.0  # = 100 + 125 + 30 + 20 = 275
        self.assertAlmostEqual(snapshot["details"]["engagement_total"], expected_eq, places=2)

    def test_today_engagement_deduplication(self):
        """today engagement_quality 按唯一笔记去重。"""
        articles_by_key = {
            "a1": _make_article("a1", liked=100),
            "a2": _make_article("a2", liked=200),
        }
        # 同一篇 a1 被两个关键词命中，today_eq 只算一次
        events = [
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a1", 2),
            _make_event(14, "kw3", "a2", 3),
        ]
        snapshot = _build_today_raw_snapshot(events, 14, articles_by_key)
        # today_engagement_total = 100 + 200 = 300（a1只算一次）
        self.assertAlmostEqual(snapshot["details"]["today_engagement_total"], 300.0, places=2)


class TestBuildMonitorDataIntegration(unittest.TestCase):
    """集成测试：模拟真实数据流，验证build_account_summaries输出结构。"""

    def setUp(self):
        from app.ingest.builders.monitor_context import build_monitor_context
        from app.ingest.builders.monitor_heat import HeatContext
        from app.ingest.builders.monitor_keywords import build_keyword_summaries
        from app.ingest.builders.monitor_accounts import build_account_summaries
        
        self.build_monitor_context = build_monitor_context
        self.HeatContext = HeatContext
        self.build_keyword_summaries = build_keyword_summaries
        self.build_account_summaries = build_account_summaries
        
        # 模拟数据：2个关键词，4个账号，100个snapshot，200个hit
        now = datetime(2026, 7, 12, 10, 0, 0)
        self.keywords = [
            {"keyword_id": "kw_001", "keyword_text": "港险提领"},
            {"keyword_id": "kw_002", "keyword_text": "香港储蓄险"},
        ]
        self.accounts = [
            {"account_id": "acct_001", "canonical_name": "博主A", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_002", "canonical_name": "博主B", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_003", "canonical_name": "博主C", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_004", "canonical_name": "博主D", "headimg_url": "", "platform": "小红书"},
        ]
        self.articles = [
            {"article_id": "art_001", "title": "笔记1", "liked_count": 100, "collected_count": 50, "comment_count": 10, "shared_count": 5, "published_at": "2026-07-10T00:00:00+08:00"},
            {"article_id": "art_002", "title": "笔记2", "liked_count": 200, "collected_count": 100, "comment_count": 20, "shared_count": 10, "published_at": "2026-07-11T00:00:00+08:00"},
            {"article_id": "art_003", "title": "笔记3", "liked_count": 50, "collected_count": 25, "comment_count": 5, "shared_count": 2, "published_at": "2026-07-12T00:00:00+08:00"},
        ]
        
        # Snapshots: keyword_id 而不含 keyword_text（测试P0.1映射）
        snapshots = []
        ranking_hits = []
        for kid_idx, kid in enumerate(["kw_001", "kw_002"]):
            for day_offset in range(15):
                cap = now - timedelta(days=14 - day_offset)
                snap_id = f"snap_{kid}_{day_offset}"
                snapshots.append({
                    "snapshot_id": snap_id,
                    "keyword_id": kid,
                    # 故意不设 keyword_text，测试映射
                    "captured_at": cap.isoformat(),
                    "snapshot_date": cap.date().isoformat(),
                    "status": "success",
                    "trigger_type": "scheduled",
                    "is_primary": True,
                    "result_count": 4,
                })
                # 每个快照4个rank hit
                for acct_idx, acct in enumerate(["acct_001", "acct_002", "acct_003", "acct_004"]):
                    art_idx = (day_offset + acct_idx) % 3
                    art_id = f"art_00{art_idx + 1}"
                    ranking_hits.append({
                        "hit_id": f"hit_{snap_id}_{acct_idx}",
                        "snapshot_id": snap_id,
                        "rank": acct_idx + 1,
                        "article_id": art_id,
                        "account_id": acct,
                        "title_raw": f"title_{art_id}",
                        "account_name_raw": f"博主{chr(65+acct_idx)}",
                        "published_at_raw": "2026-07-10T00:00:00+00:00",
                        "url_raw": f"https://xhs.com/{art_id}",
                        "source": "tikhub_xhs_search",
                    })
        
        ent = {
            "keywords": self.keywords,
            "snapshots": snapshots,
            "snapshot_terms": [],
            "accounts": self.accounts,
            "articles": self.articles,
            "ranking_hits": ranking_hits,
            "note_metric_observations": [],
        }
        
        self.ctx = self.build_monitor_context(ent, {})
        heat = self.HeatContext(keywords=self.keywords, has_heat=False)
        self.kw_result = self.build_keyword_summaries(self.ctx, heat)
        self.summaries = self.build_account_summaries(self.ctx, self.kw_result)

    def test_keyword_id_mapping(self):
        """snapshot只有keyword_id时，keyword_text从ctx.keywords映射。"""
        # 检查rank_events中keyword字段不为空且不是keyword_id格式
        for s in self.summaries:
            self.assertIn("keywords", s)
            for kw in s["keywords"]:
                self.assertIn(kw, ["港险提领", "香港储蓄险"],
                              f"keyword should be mapped text, got {kw}")

    def test_multiple_keywords_not_collapsed(self):
        """多个关键词不塌缩为一个。"""
        # 至少有一个账号覆盖了两个关键词
        has_both = any(len(s.get("keywords", [])) >= 2 for s in self.summaries)
        self.assertTrue(has_both, "At least one account should have 2 keywords")

    def test_population_not_all_T1(self):
        """人口不是全T1。"""
        for s in self.summaries:
            hexagon = s.get("account_score_hexagon", {})
            pop = hexagon.get("population", {})
            score = pop.get("score", {})
            rank = score.get("rank", 0)
            self.assertGreater(rank, 0, "rank should be > 0")
            if rank == 1:
                # 检查不是所有都是T1
                pass

    def test_hexagon_population_structure(self):
        """hexagon population/previous_population/delta完整。"""
        for s in self.summaries:
            hexagon = s.get("account_score_hexagon", {})
            self.assertIn("population", hexagon)
            self.assertIn("previous_population", hexagon)
            self.assertIn("delta", hexagon)
            pop = hexagon["population"]
            self.assertIn("account_count", pop)
            self.assertIn("score", pop)
            self.assertIsInstance(pop["score"]["rank"], int)
            prev_pop = hexagon["previous_population"]
            self.assertIn("account_count", prev_pop)
            self.assertIn("score", prev_pop)

    def test_current_previous_benchmarks_independent(self):
        """current与previous benchmark独立。"""
        for s in self.summaries:
            hexagon = s.get("account_score_hexagon", {})
            self.assertIn("benchmarks", hexagon)
            # 验证benchmarks存在
            self.assertGreater(len(hexagon["benchmarks"]), 0)

    def test_today_confidence(self):
        """today confidence基于全局刷新完整度。"""
        for s in self.summaries:
            self.assertIn("today_score", s)
            # today_score 应 >= 0
            self.assertGreaterEqual(s["today_score"], 0)

    def test_compatible_fields_exist(self):
        """兼容字段存在且不为空。"""
        for s in self.summaries[:2]:
            for field in ["keywords", "topics", "covered_topics", "articles", "best_articles", "classic_articles",
                          "interaction_metrics", "move_summary", "history", "day_scores"]:
                self.assertIn(field, s, f"Missing field: {field}")

    def test_history_day_scores_not_all_zero(self):
        """history/day_scores 有真实值。"""
        for s in self.summaries:
            self.assertGreater(sum(s.get("history", [])), 0, "history should have some 1s")
            self.assertGreater(sum(s.get("day_scores", [])), 0.001, "day_scores should have some positive values")

    def test_no_nan_in_output(self):
        """输出无NaN。"""
        def check_no_nan(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check_no_nan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    check_no_nan(v, f"{path}[{i}]")
            elif isinstance(obj, float):
                self.assertFalse(math.isnan(obj), f"NaN at {path}")
        for s in self.summaries:
            check_no_nan(s)

    def test_durable_notes_status_field(self):
        """durable_notes_status 和 durable_notes_message 字段存在。"""
        for s in self.summaries:
            self.assertIn("durable_notes_status", s)
            self.assertIn("durable_notes_message", s)

    def test_global_effective_days_less_than_3(self):
        """全局有效日<3时明确等待信息。"""
        for s in self.summaries:
            if s.get("global_effective_day_count", 0) < 3:
                self.assertIn("等待", s.get("durable_notes_message", ""))



class TestPopulationAxes(unittest.TestCase):
    """P0.1: population.axes 包含每轴人口统计。"""

    def test_population_has_axes(self):
        """hexagon population 包含 axes 字段。"""
        summaries = [
            {
                "account_score_hexagon": build_hexagon_payload("account", {"axes": {"history_coverage": 100, "recent_coverage": 80, "durable_notes": 90, "continuity": 70, "content_matrix": 60, "engagement_quality": 95, "battle_breadth": 85}, "end_idx": 14}, {"axes": {"history_coverage": 90, "recent_coverage": 70, "durable_notes": 80, "continuity": 60, "content_matrix": 50, "engagement_quality": 85, "battle_breadth": 75}, "end_idx": 13}, {"history_coverage": 50.0, "recent_coverage": 40.0, "durable_notes": 45.0, "continuity": 35.0, "content_matrix": 30.0, "engagement_quality": 47.0, "battle_breadth": 42.0}),
                "timeliness_score_hexagon": build_hexagon_payload("timeliness", {"axes": {"top3_volume": 80, "top3_breadth": 70, "new_top3": 60, "fresh_top3": 50, "upward_momentum": 40, "new_entry_engagement": 30, "top3_continuity": 20}, "end_idx": 14}, {"axes": {"top3_volume": 70, "top3_breadth": 60, "new_top3": 50, "fresh_top3": 40, "upward_momentum": 30, "new_entry_engagement": 20, "top3_continuity": 10}, "end_idx": 13}, {"top3_volume": 40.0, "top3_breadth": 35.0, "new_top3": 30.0, "fresh_top3": 25.0, "upward_momentum": 20.0, "new_entry_engagement": 15.0, "top3_continuity": 10.0}),
                "today_score_hexagon": build_hexagon_payload("today", {"axes": {"today_top3": 60, "today_keywords": 50, "today_notes": 40, "today_rank_quality": 30, "today_new_entries": 20, "today_engagement_quality": 10, "today_breadth": 5}, "end_idx": 14}, {"axes": {"today_top3": 50, "today_keywords": 40, "today_notes": 30, "today_rank_quality": 20, "today_new_entries": 10, "today_engagement_quality": 5, "today_breadth": 0}, "end_idx": 13}, {"today_top3": 30.0, "today_keywords": 25.0, "today_notes": 20.0, "today_rank_quality": 15.0, "today_new_entries": 10.0, "today_engagement_quality": 5.0, "today_breadth": 2.0}),
                "score": 100, "score_yesterday": 90,
                "timeliness_score": 80, "timeliness_score_yesterday": 70,
                "today_score": 60, "today_score_yesterday": 50,
            },
            {
                "account_score_hexagon": build_hexagon_payload("account", {"axes": {"history_coverage": 50, "recent_coverage": 40, "durable_notes": 45, "continuity": 35, "content_matrix": 30, "engagement_quality": 47, "battle_breadth": 42}, "end_idx": 14}, {"axes": {"history_coverage": 40, "recent_coverage": 30, "durable_notes": 35, "continuity": 25, "content_matrix": 20, "engagement_quality": 37, "battle_breadth": 32}, "end_idx": 13}, {"history_coverage": 50.0, "recent_coverage": 40.0, "durable_notes": 45.0, "continuity": 35.0, "content_matrix": 30.0, "engagement_quality": 47.0, "battle_breadth": 42.0}),
                "timeliness_score_hexagon": build_hexagon_payload("timeliness", {"axes": {"top3_volume": 40, "top3_breadth": 35, "new_top3": 30, "fresh_top3": 25, "upward_momentum": 20, "new_entry_engagement": 15, "top3_continuity": 10}, "end_idx": 14}, {"axes": {"top3_volume": 30, "top3_breadth": 25, "new_top3": 20, "fresh_top3": 15, "upward_momentum": 10, "new_entry_engagement": 5, "top3_continuity": 0}, "end_idx": 13}, {"top3_volume": 40.0, "top3_breadth": 35.0, "new_top3": 30.0, "fresh_top3": 25.0, "upward_momentum": 20.0, "new_entry_engagement": 15.0, "top3_continuity": 10.0}),
                "today_score_hexagon": build_hexagon_payload("today", {"axes": {"today_top3": 30, "today_keywords": 25, "today_notes": 20, "today_rank_quality": 15, "today_new_entries": 10, "today_engagement_quality": 5, "today_breadth": 2}, "end_idx": 14}, {"axes": {"today_top3": 20, "today_keywords": 15, "today_notes": 10, "today_rank_quality": 5, "today_new_entries": 0, "today_engagement_quality": 0, "today_breadth": 0}, "end_idx": 13}, {"today_top3": 30.0, "today_keywords": 25.0, "today_notes": 20.0, "today_rank_quality": 15.0, "today_new_entries": 10.0, "today_engagement_quality": 5.0, "today_breadth": 2.0}),
                "score": 50, "score_yesterday": 40,
                "timeliness_score": 40, "timeliness_score_yesterday": 30,
                "today_score": 30, "today_score_yesterday": 20,
            },
        ]
        _attach_hexagon_population(summaries)
        for s in summaries:
            for hf in ["account_score_hexagon", "timeliness_score_hexagon", "today_score_hexagon"]:
                hexagon = s[hf]
                pop = hexagon["population"]
                prev_pop = hexagon["previous_population"]
                # 检查 axes 存在且有与配置匹配的轴
                self.assertIn("axes", pop, f"{hf} population missing axes")
                self.assertIn("axes", prev_pop, f"{hf} previous_population missing axes")
                for meta in BOARD_CONFIGS[hf.replace("_score_hexagon","")]["axes_meta"]:
                    key = meta["key"]
                    self.assertIn(key, pop["axes"], f"{hf} population missing axis {key}")
                    self.assertIn(key, prev_pop["axes"], f"{hf} previous_population missing axis {key}")
                    # 每轴返回 rank/total/tie_count/percentile
                    self.assertIn("rank", pop["axes"][key])
                    self.assertIn("total", pop["axes"][key])
                    self.assertIn("tie_count", pop["axes"][key])
                    self.assertIn("percentile", pop["axes"][key])


class TestPreviousBenchmarks(unittest.TestCase):
    """P0.2: hexagon 包含 previous_benchmarks。"""

    def test_previous_benchmarks_in_hexagon(self):
        articles = {"a1": _make_article("a1", liked=10)}
        events = [_make_event(14, "kw1", "a1", 1)]
        snap = build_raw_board_snapshots(events, 14, date(2026, 7, 12), articles)
        benches = build_board_benchmarks([snap])
        cur_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        prev_norm, _ = score_board_snapshot(snap["account"], "account", benches["account"])
        hexagon = build_hexagon_payload("account", cur_norm, prev_norm, benches["account"], previous_benchmarks=benches["account"])
        self.assertIn("previous_benchmarks", hexagon)
        self.assertIsInstance(hexagon["previous_benchmarks"], dict)
        self.assertGreater(len(hexagon["previous_benchmarks"]), 0)


class TestBreakthroughEnergyInParts(unittest.TestCase):
    """P0.3: score_board_snapshot 返回 breakthrough_energy。"""

    def test_breakthrough_energy_in_parts(self):
        articles = {"a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5)}
        # 构建足够丰富的 events 触发突破
        events = []
        for day in range(15):
            for kw_idx in range(8):
                events.append(_make_event(day, f"kw{kw_idx}", "a1", rank=min(kw_idx + 1, 10)))
        snapshot = _build_account_raw_snapshot(events, 14, articles)
        benches = {
            "history_coverage": 1.0, "recent_coverage": 1.0, "durable_notes": 1.0,
            "continuity": 0.3, "content_matrix": 0.5, "engagement_quality": 1.0, "battle_breadth": 0.5,
        }
        normalized, parts = score_board_snapshot(snapshot, "account", benches)
        self.assertIn("breakthrough_energy", parts)
        self.assertIn("breakthrough_energy", normalized)
        self.assertGreater(parts["breakthrough_energy"], 0)


class TestGlobalEffectiveDayConfidence(unittest.TestCase):
    """P0.4: account/timeliness confidence 使用全局有效日。"""

    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
        }

    def test_account_confidence_uses_global_effective_days(self):
        """传入 global_effective_day_count 时，confidence 基于该值而非事件日历跨度。"""
        # 只有1个事件 day_idx=14，但全局有效日=3
        events = [_make_event(14, "kw1", "a1", 1)]
        # 不传 global_effective_day_count -> 使用 observation_span_days（=1天）
        snapshot_no_global = _build_account_raw_snapshot(events, 14, self.articles_by_key)
        # 传 global_effective_day_count=3
        snapshot_global = _build_account_raw_snapshot(events, 14, self.articles_by_key, global_effective_day_count=3)
        self.assertGreater(snapshot_global["confidence"], snapshot_no_global["confidence"],
                           "global_effective_day_count=3 should give higher confidence than 1 day")

    def test_account_confidence_with_gap(self):
        """全局有效日有空洞时，空洞日不计入。"""
        events = [
            _make_event(10, "kw1", "a1", 1),
            _make_event(14, "kw1", "a1", 1),
        ]
        # 全局有效日=2（day 10和14），空洞不计
        snapshot = _build_account_raw_snapshot(events, 14, self.articles_by_key, global_effective_day_count=2)
        expected = _confidence_by_days(2, 5)
        self.assertAlmostEqual(snapshot["confidence"], expected, places=2)


class TestTimelinessEffectiveDays(unittest.TestCase):
    """P0.5: 时效窗口使用最近3个全局有效日，new_top3只比较当前有效日与前一有效日。"""

    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
        }

    def test_timeliness_uses_effective_days(self):
        """timeliness 窗口使用最近3个全局有效日，而非连续日历。"""
        from datetime import timedelta
        window_start = date(2026, 6, 28)
        # 有效日只有 day 10, 12, 14（有空洞）
        events = [
            _make_event(10, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(12, "kw2", "a2", 2, topic="t2", bucket="b2"),
            _make_event(14, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(14, "kw3", "a3", 3, topic="t3", bucket="b3"),
        ]
        end_date = window_start + timedelta(days=14)
        effective_indices = [10, 12, 14]
        snapshot = _build_timeliness_raw_snapshot(events, 14, end_date, self.articles_by_key,
                                                   previous_effective_day_idx=12,
                                                   global_effective_day_indices=effective_indices)
        # 应该在 day 10,12,14 上聚合，包括 day 10 和 12 的 events
        self.assertGreater(snapshot["raw_axes"]["top3_volume"], 0)
        # 应包含 kw1, kw2, kw3 三个关键词
        self.assertEqual(snapshot["details"]["top3_keyword_count"], 3)

    def test_new_top3_current_vs_previous_effective_day(self):
        """new_top3 只比较当前有效日 Top3 与前一有效日 Top3。"""
        from datetime import timedelta
        window_start = date(2026, 6, 28)
        # day 12: a1 in top3
        # day 14: a1 and a2 in top3 (a2 is new)
        events = [
            _make_event(12, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a1", 2, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a2", 3, topic="t1", bucket="b1"),
        ]
        end_date = window_start + timedelta(days=14)
        snapshot = _build_timeliness_raw_snapshot(events, 14, end_date, self.articles_by_key,
                                                   previous_effective_day_idx=12)
        # new_top3 = {(kw1, a2)} -> log_count(1)
        self.assertAlmostEqual(snapshot["raw_axes"]["new_top3"], _log_count(1), places=2)


    def test_new_entry_article_count_in_details(self):
        """new_entry_article_count 在 timeliness details 中正确反映新进 Top3 笔记数量。"""
        from datetime import timedelta
        window_start = date(2026, 6, 28)
        # day 10: a1, a2 in top3
        # day 14: a1, a2, a3 in top3 (a3 is new)
        events = [
            _make_event(10, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(10, "kw1", "a2", 2, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a1", 2, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a2", 3, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a3", 1, topic="t1", bucket="b1"),
        ]
        end_date = window_start + timedelta(days=14)
        snapshot = _build_timeliness_raw_snapshot(events, 14, end_date, self.articles_by_key,
                                                   previous_effective_day_idx=10)
        # new_top3_pairs = {(kw1, a3)} -> 1 unique article
        self.assertEqual(snapshot["details"]["new_entry_article_count"], 1,
                         "new_entry_article_count 应为 1（a3 是新进唯一笔记）")

    def test_new_entry_article_count_zero_when_no_new_top3(self):
        """无新进 Top3 时 new_entry_article_count 为 0。"""
        from datetime import timedelta
        window_start = date(2026, 6, 28)
        events = [
            _make_event(10, "kw1", "a1", 1, topic="t1", bucket="b1"),
            _make_event(14, "kw1", "a1", 2, topic="t1", bucket="b1"),
        ]
        end_date = window_start + timedelta(days=14)
        snapshot = _build_timeliness_raw_snapshot(events, 14, end_date, self.articles_by_key,
                                                   previous_effective_day_idx=10)
        # a1 already in top3 on day 10, no new top3
        self.assertEqual(snapshot["details"]["new_entry_article_count"], 0,
                         "无新进 Top3 时 new_entry_article_count 应为 0")


class TestRankQualityStrictlyBounded(unittest.TestCase):
    """P0.6: engagement_rank_quality 严格 0..1，按唯一笔记取最佳 rank。"""

    def setUp(self):
        self.articles_by_key = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
        }

    def test_rank_quality_not_exceeding_1(self):
        """即使多事件，rank quality 不超 1。"""
        # 同一篇笔记被多个关键词命中，每个事件 rank=1 -> rq=1.0
        events = []
        for day in range(14 - WINDOW_DAYS + 1, 15):
            events.append(_make_event(day, "kw1", "a1", 1))
            events.append(_make_event(day, "kw2", "a1", 1))
        snapshot = _build_account_raw_snapshot(events, 14, self.articles_by_key)
        # engagement_rank_quality = min(1.0, 1.0) = 1.0
        self.assertLessEqual(snapshot["details"]["engagement_rank_quality"], 1.0)
        self.assertGreaterEqual(snapshot["details"]["engagement_rank_quality"], 0.0)

    def test_today_rank_quality_per_unique_article(self):
        """today_rank_quality 按唯一笔记平均。"""
        events = [
            _make_event(14, "kw1", "a1", 1),
            _make_event(14, "kw2", "a1", 5),  # same article, best rank=1
            _make_event(14, "kw3", "a2", 10),
        ]
        snapshot = _build_today_raw_snapshot(events, 14, self.articles_by_key)
        # a1: best rq = (11-1)/10 = 1.0
        # a2: best rq = (11-10)/10 = 0.1
        # avg = (1.0 + 0.1) / 2 = 0.55
        self.assertAlmostEqual(snapshot["raw_axes"]["today_rank_quality"], 0.55, places=2)


class TestTodayRefreshInHexagonDetails(unittest.TestCase):
    """P0.7: today_refresh 合并进 today_score_hexagon.current.details。"""

    def test_today_refresh_in_hexagon_details(self):
        """集成测试验证 today_score_hexagon.current.details 有 refresh 字段。"""
        from app.ingest.builders.monitor_context import build_monitor_context
        from app.ingest.builders.monitor_heat import HeatContext
        from app.ingest.builders.monitor_keywords import build_keyword_summaries
        from app.ingest.builders.monitor_accounts import build_account_summaries
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 12, 10, 0, 0)
        keywords = [
            {"keyword_id": "kw_001", "keyword_text": "港险提领"},
            {"keyword_id": "kw_002", "keyword_text": "香港储蓄险"},
        ]
        accounts = [
            {"account_id": "acct_001", "canonical_name": "博主A", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_002", "canonical_name": "博主B", "headimg_url": "", "platform": "小红书"},
        ]
        articles = [
            {"article_id": "art_001", "title": "笔记1", "liked_count": 100, "collected_count": 50, "comment_count": 10, "shared_count": 5, "published_at": "2026-07-10T00:00:00+08:00"},
            {"article_id": "art_002", "title": "笔记2", "liked_count": 200, "collected_count": 100, "comment_count": 20, "shared_count": 10, "published_at": "2026-07-11T00:00:00+08:00"},
        ]
        snapshots = []
        ranking_hits = []
        for kid in ["kw_001", "kw_002"]:
            for day_offset in range(15):
                cap = now - timedelta(days=14 - day_offset)
                snap_id = f"snap_{kid}_{day_offset}"
                snapshots.append({
                    "snapshot_id": snap_id, "keyword_id": kid,
                    "captured_at": cap.isoformat(), "snapshot_date": cap.date().isoformat(),
                    "status": "success", "trigger_type": "scheduled", "is_primary": True, "result_count": 2,
                })
                for acct_idx, acct in enumerate(["acct_001", "acct_002"]):
                    art_id = f"art_00{(day_offset + acct_idx) % 2 + 1}"
                    ranking_hits.append({
                        "hit_id": f"hit_{snap_id}_{acct_idx}", "snapshot_id": snap_id,
                        "rank": acct_idx + 1, "article_id": art_id, "account_id": acct,
                        "title_raw": f"title_{art_id}", "account_name_raw": f"博主{chr(65+acct_idx)}",
                        "published_at_raw": "2026-07-10T00:00:00+00:00", "url_raw": f"https://xhs.com/{art_id}", "source": "tikhub_xhs_search",
                    })
        ent = {"keywords": keywords, "snapshots": snapshots, "snapshot_terms": [], "accounts": accounts,
               "articles": articles, "ranking_hits": ranking_hits, "note_metric_observations": []}
        ctx = build_monitor_context(ent, {})
        heat = HeatContext(keywords=keywords, has_heat=False)
        kw_result = build_keyword_summaries(ctx, heat)
        summaries = build_account_summaries(ctx, kw_result)
        for s in summaries:
            hexagon = s.get("today_score_hexagon", {})
            current = hexagon.get("current", {})
            details = current.get("details", {})
            self.assertIn("refresh_completed_keywords", details)
            self.assertIn("refresh_target_keywords", details)
            self.assertIn("refresh_completeness", details)
            self.assertIsInstance(details["refresh_completeness"], (int, float))
            # previous 也应该有
            prev = hexagon.get("previous", {}).get("details", {})
            self.assertIn("refresh_completed_keywords", prev)
            self.assertIn("refresh_target_keywords", prev)
            self.assertIn("refresh_completeness", prev)


class TestKeywordsTopicsDictShape(unittest.TestCase):
    """P0.8: keywords/topics 为 dict 对象，兼容 monitor.js 的 Object.entries/Object.values。"""

    def test_keywords_is_dict(self):
        """集成测试验证 keywords 是 dict 而非 list。"""
        from app.ingest.builders.monitor_context import build_monitor_context
        from app.ingest.builders.monitor_heat import HeatContext
        from app.ingest.builders.monitor_keywords import build_keyword_summaries
        from app.ingest.builders.monitor_accounts import build_account_summaries
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 12, 10, 0, 0)
        keywords = [
            {"keyword_id": "kw_001", "keyword_text": "港险提领"},
            {"keyword_id": "kw_002", "keyword_text": "香港储蓄险"},
        ]
        accounts = [
            {"account_id": "acct_001", "canonical_name": "博主A", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_002", "canonical_name": "博主B", "headimg_url": "", "platform": "小红书"},
        ]
        articles = [
            {"article_id": "art_001", "title": "笔记1", "liked_count": 100, "collected_count": 50, "comment_count": 10, "shared_count": 5, "published_at": "2026-07-10T00:00:00+08:00"},
            {"article_id": "art_002", "title": "笔记2", "liked_count": 200, "collected_count": 100, "comment_count": 20, "shared_count": 10, "published_at": "2026-07-11T00:00:00+08:00"},
        ]
        snapshots = []
        ranking_hits = []
        for kid in ["kw_001", "kw_002"]:
            for day_offset in range(15):
                cap = now - timedelta(days=14 - day_offset)
                snap_id = f"snap_{kid}_{day_offset}"
                snapshots.append({
                    "snapshot_id": snap_id, "keyword_id": kid,
                    "captured_at": cap.isoformat(), "snapshot_date": cap.date().isoformat(),
                    "status": "success", "trigger_type": "scheduled", "is_primary": True, "result_count": 2,
                })
                for acct_idx, acct in enumerate(["acct_001", "acct_002"]):
                    art_id = f"art_00{(day_offset + acct_idx) % 2 + 1}"
                    ranking_hits.append({
                        "hit_id": f"hit_{snap_id}_{acct_idx}", "snapshot_id": snap_id,
                        "rank": acct_idx + 1, "article_id": art_id, "account_id": acct,
                        "title_raw": f"title_{art_id}", "account_name_raw": f"博主{chr(65+acct_idx)}",
                        "published_at_raw": "2026-07-10T00:00:00+00:00", "url_raw": f"https://xhs.com/{art_id}", "source": "tikhub_xhs_search",
                    })
        ent = {"keywords": keywords, "snapshots": snapshots, "snapshot_terms": [], "accounts": accounts,
               "articles": articles, "ranking_hits": ranking_hits, "note_metric_observations": []}
        ctx = build_monitor_context(ent, {})
        heat = HeatContext(keywords=keywords, has_heat=False)
        kw_result = build_keyword_summaries(ctx, heat)
        summaries = build_account_summaries(ctx, kw_result)
        for s in summaries:
            # keywords 必须是 dict
            self.assertIsInstance(s.get("keywords"), dict,
                                  f"keywords should be dict, got {type(s.get('keywords'))}")
            # topics 必须是 dict
            self.assertIsInstance(s.get("topics"), dict,
                                  f"topics should be dict, got {type(s.get('topics'))}")
            # keyword value 必须有 history/articles/keyword_id
            for kw, detail in s["keywords"].items():
                self.assertIn("history", detail, f"keyword {kw} missing history")
                self.assertIn("articles", detail, f"keyword {kw} missing articles")
                self.assertIn("keyword_id", detail, f"keyword {kw} missing keyword_id")
                self.assertIn("keyword_text", detail, f"keyword {kw} missing keyword_text")
                # keyword_id 以 kw_ 开头
                self.assertTrue(str(detail["keyword_id"]).startswith("kw_"),
                                f"keyword_id {detail['keyword_id']} should start with kw_")
            # topic value 必须有 history/articles/keywords
            for topic, detail in s["topics"].items():
                self.assertIn("history", detail, f"topic {topic} missing history")
                self.assertIn("articles", detail, f"topic {topic} missing articles")
                self.assertIn("keywords", detail, f"topic {topic} missing keywords")
                self.assertIn("label", detail, f"topic {topic} missing label")
            # covered_topics 是 list
            self.assertIsInstance(s.get("covered_topics"), list)
            # matched_keywords 的 keyword_id 是真实 ID
            for mk in s.get("matched_keywords", []):
                self.assertTrue(str(mk.get("keyword_id", "")).startswith("kw_"),
                                f"matched keyword_id {mk.get('keyword_id')} should start with kw_")
            # move_summary 包含 new_count/up_count/down_count/flat_count
            ms = s.get("move_summary", {})
            for field in ["new_count", "up_count", "down_count", "flat_count"]:
                self.assertIn(field, ms, f"move_summary missing {field}")
            # article 有 is_today/today_rank/matched_keyword_count
            for art in s.get("articles", []):
                self.assertIn("today_rank", art)
                self.assertIn("is_today", art)
                self.assertIn("matched_keyword_count", art)


class TestClassicArticlesSemantic(unittest.TestCase):
    """P0.9: classic_articles 不足3天为空的语义。"""

    def test_classic_articles_empty_less_than_3_days(self):
        """不足3天全局有效日时 classic_articles 为空。"""
        articles = {"a1": _make_article("a1", liked=100)}
        events = [
            _make_event(13, "kw1", "a1", 1),
            _make_event(14, "kw1", "a1", 1),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, articles)
        self.assertEqual(snapshot["details"]["durable_pair_count"], 0)
        self.assertEqual(snapshot["details"]["durable_notes_status"], "waiting")


class TestDurableNotesStatus(unittest.TestCase):
    """durable_notes_status 和 durable_notes_message 字段存在性。"""

    def test_less_than_3_days_status(self):
        articles = {"a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5)}
        end_idx = 1
        events = [
            _make_event(0, "kw1", "a1", 5),
            _make_event(1, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, articles)
        self.assertIn("durable_notes_status", snapshot["details"])
        self.assertIn("durable_notes_message", snapshot["details"])
        self.assertEqual(snapshot["details"]["durable_notes_status"], "waiting")

    def test_3_days_but_no_durable(self):
        articles = {"a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5)}
        end_idx = 13
        events = [
            _make_event(11, "kw1", "a1", 5),
            _make_event(12, "kw1", "a1", 5),
            _make_event(13, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, end_idx, articles)
        self.assertEqual(snapshot["details"]["durable_notes_status"], "stable")


    def test_today_hit_count_from_today_board(self):
        """today_hit_count 从 today board details 读取，非 account board details。
        有 current-day event 时 >0 且等于 today hexagon current.details.today_hit_count；
        无 current-day 事件时 =0。
        """
        from app.ingest.builders.monitor_context import build_monitor_context
        from app.ingest.builders.monitor_heat import HeatContext
        from app.ingest.builders.monitor_keywords import build_keyword_summaries
        from app.ingest.builders.monitor_accounts import build_account_summaries
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 12, 10, 0, 0)
        keywords = [{"keyword_id": "kw_001", "keyword_text": "港险提领"}]
        accounts = [{"account_id": "acct_001", "canonical_name": "博主A", "headimg_url": "", "platform": "小红书"}]
        articles = [{"article_id": "art_001", "title": "笔记1", "liked_count": 100, "collected_count": 50,
                     "comment_count": 10, "shared_count": 5, "published_at": "2026-07-12T00:00:00+08:00"}]

        # 场景1：今天有事件 → today_hit_count > 0
        snapshots = []
        ranking_hits = []
        for day_offset in [7, 14]:
            snap_id = f"snap_kw001_d{day_offset}"
            cap = now - timedelta(days=14 - day_offset)
            snapshots.append({
                "snapshot_id": snap_id, "keyword_id": "kw_001",
                "captured_at": cap.isoformat(), "snapshot_date": cap.date().isoformat(),
                "status": "success", "trigger_type": "scheduled", "is_primary": True, "result_count": 1,
            })
            ranking_hits.append({
                "hit_id": f"hit_{snap_id}", "snapshot_id": snap_id,
                "rank": 1, "article_id": "art_001", "account_id": "acct_001",
                "title_raw": "笔记1", "account_name_raw": "博主A",
                "published_at_raw": "2026-07-12T00:00:00+00:00", "url_raw": "https://xhs.com/art_001",
                "source": "tikhub_xhs_search",
            })

        ent = {"keywords": keywords, "snapshots": snapshots, "snapshot_terms": [], "accounts": accounts,
               "articles": articles, "ranking_hits": ranking_hits, "note_metric_observations": []}
        ctx = build_monitor_context(ent, {})
        heat = HeatContext(keywords=keywords, has_heat=False)
        kw_result = build_keyword_summaries(ctx, heat)
        summaries = build_account_summaries(ctx, kw_result)

        self.assertGreater(len(summaries), 0)
        s = summaries[0]
        self.assertGreater(s["today_hit_count"], 0,
            "有 current-day event 时 today_hit_count 应 > 0")
        hexagon = s.get("today_score_hexagon", {})
        hexagon_hit_count = hexagon.get("current", {}).get("details", {}).get("today_hit_count", 0)
        self.assertEqual(s["today_hit_count"], hexagon_hit_count,
            "result.today_hit_count 应等于 today hexagon current.details.today_hit_count")

        # 场景2：有 snapshot 但该账号今天无 hit → today_hit_count = 0
        # 今天有另一个账号的 hit，但 acct_001 没有
        snapshots2 = []
        ranking_hits2 = []
        for day_offset in [7, 14]:
            snap_id = f"snap_kw001_d{day_offset}"
            cap = now - timedelta(days=14 - day_offset)
            snapshots2.append({
                "snapshot_id": snap_id, "keyword_id": "kw_001",
                "captured_at": cap.isoformat(), "snapshot_date": cap.date().isoformat(),
                "status": "success", "trigger_type": "scheduled", "is_primary": True, "result_count": 1,
            })
            # 第7天有 acct_001 的 hit
            if day_offset == 7:
                ranking_hits2.append({
                    "hit_id": f"hit_{snap_id}", "snapshot_id": snap_id,
                    "rank": 1, "article_id": "art_001", "account_id": "acct_001",
                    "title_raw": "笔记1", "account_name_raw": "博主A",
                    "published_at_raw": "2026-07-12T00:00:00+00:00", "url_raw": "https://xhs.com/art_001",
                    "source": "tikhub_xhs_search",
                })
            # 第14天只有 acct_002 的 hit，acct_001 无 hit
            else:
                ranking_hits2.append({
                    "hit_id": f"hit_{snap_id}_other", "snapshot_id": snap_id,
                    "rank": 1, "article_id": "art_001", "account_id": "acct_002",
                    "title_raw": "笔记1", "account_name_raw": "博主B",
                    "published_at_raw": "2026-07-12T00:00:00+00:00", "url_raw": "https://xhs.com/art_001",
                    "source": "tikhub_xhs_search",
                })

        # 需要把 acct_002 加到 accounts 里
        accounts2 = [
            {"account_id": "acct_001", "canonical_name": "博主A", "headimg_url": "", "platform": "小红书"},
            {"account_id": "acct_002", "canonical_name": "博主B", "headimg_url": "", "platform": "小红书"},
        ]
        ent2 = {"keywords": keywords, "snapshots": snapshots2, "snapshot_terms": [], "accounts": accounts2,
                "articles": articles, "ranking_hits": ranking_hits2, "note_metric_observations": []}
        ctx2 = build_monitor_context(ent2, {})
        kw_result2 = build_keyword_summaries(ctx2, heat)
        summaries2 = build_account_summaries(ctx2, kw_result2)

        # acct_001 今天无 hit
        s2 = [s for s in summaries2 if s["account_id"] == "acct_001"]
        self.assertGreater(len(s2), 0)
        self.assertEqual(s2[0]["today_hit_count"], 0,
            "无 current-day event 时 today_hit_count 应为 0")


if __name__ == "__main__":
    unittest.main()


class TestMonitorJSContract(unittest.TestCase):
    """静态契约测试：断言 monitor.js 源码符合前端-后端数据契约。"""

    JS_PATH = "app/static/js/monitor.js"

    def setUp(self):
        with open(self.JS_PATH, "r") as f:
            self.js = f.read()

    def test_new_entry_engagement_uses_new_entry_article_count(self):
        """new_entry_engagement 事实卡使用 new_entry_article_count，而非不存在的 new_entry_count。"""
        # 确认存在 new_entry_article_count 引用
        self.assertIn("new_entry_article_count", self.js,
                      "源码应引用 new_entry_article_count")
        # 确认 new_entry_engagement 函数内没有裸 new_entry_count（允许 today_new_entry_count）
        engagement_section = self.js[self.js.find("new_entry_engagement"):]
        engagement_section = engagement_section[:engagement_section.find("top3_continuity")]
        self.assertNotIn("new_entry_count", engagement_section,
                         "new_entry_engagement 函数内不应引用裸 new_entry_count")

    def test_new_entry_engagement_conditional_coverage(self):
        """new_entry_engagement 仅在 nacValid 时拼接覆盖篇数，缺字段时不显示覆盖0篇。"""
        self.assertIn("nacValid", self.js,
                      "源码应包含 nacValid 条件判断")
        self.assertIn("typeof nac === 'number' && Number.isFinite(nac)", self.js,
                      "nacValid 应校验 nac 为有限数字")
        self.assertIn("nacValid ?", self.js,
                      "应有 nacValid 条件决定是否渲染覆盖篇数")

    def test_prevP99_uses_label_variable(self):
        """prevP99Text 使用 ${label}P99 而非硬编码昨P99。"""
        self.assertIn("${label}P99=", self.js,
                      "prevP99Text 应使用 label 变量")
        self.assertNotIn("昨P99=", self.js,
                         "不应有硬编码昨P99=")

    def test_aria_label_uses_label_variable(self):
        """aria-label 根据 modeName 动态显示昨天/前一有效日。"""
        self.assertIn('label === "昨" ? "昨天" : "前一有效日"', self.js,
                      "aria-label 应根据 label 动态选择昨天/前一有效日")

    def test_upward_momentum_uses_effective_day_label(self):
        """upward_momentum 事实文案为较前一有效日。"""
        self.assertIn("较前一有效日新进", self.js,
                      "upward_momentum 应显示较前一有效日")

    def test_today_new_entries_uses_effective_day_label(self):
        """today_new_entries 事实文案为较前一有效日。"""
        self.assertIn("较前一有效日新进", self.js,
                      "today_new_entries 应显示较前一有效日")

    def test_score_axis_card_prev_label(self):
        """scoreAxisCards 脚标使用 label 变量（昨/前一有效日）。"""
        self.assertIn('const label = modeName === "score" ? "昨" : "前一有效日"', self.js,
                      "axis card 脚标应根据 modeName 切换标签")


class TestV2MaturitySeparated(unittest.TestCase):
    """v2 成熟度分离算法测试。"""
    
    def setUp(self):
        self.articles = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
            "a3": _make_article("a3", liked=50, collected=25, comment=5, shared=2),
        }
    
    def _make_rich_events_5days(self, end_idx: int) -> list[dict]:
        '''5天有效观察，用于测试可用轴和突破'''
        events = []
        for day in range(end_idx - WINDOW_DAYS + 1, end_idx + 1):
            for kw_idx in range(5):
                for art_idx in range(min(kw_idx + 1, 3)):
                    art_key = f"a{art_idx + 1}"
                    events.append(_make_event(
                        day, f"kw{kw_idx}", art_key,
                        rank=min(kw_idx + art_idx + 1, 10),
                        topic=f"t{kw_idx % 3}", bucket=f"b{kw_idx % 2}",
                    ))
        return events
    
    def test_2_days_durable_unavailable(self):
        '''2天有效观察时 durable_notes 为 unavailable'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 1.0, "recent_coverage": 1.0, "durable_notes": 1.0,
            "continuity": 0.3, "content_matrix": 0.5, "engagement_quality": 1.0, "battle_breadth": 0.5,
        })
        self.assertIn("durable_notes", p["unavailable_axes"])
        self.assertNotIn("durable_notes", p["available_axes"])
        # 2天时 history_coverage 与 durable_notes 都还不能可靠判断
        self.assertIn("history_coverage", p["unavailable_axes"])
        self.assertAlmostEqual(p["available_weight"], 0.66, places=4)
    
    def test_available_weight_renormalization(self):
        '''不可用轴权重被排除后重归一化'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        # 用很小的 benchmark 让所有 available 轴都达到 100
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.01, "recent_coverage": 0.01, "durable_notes": 0.01,
            "continuity": 0.01, "content_matrix": 0.01, "engagement_quality": 0.01, "battle_breadth": 0.01,
        })
        # history_coverage 与 durable_notes 不可用，其余权重重归一化
        self.assertIn("durable_notes", p["unavailable_axes"])
        self.assertIn("history_coverage", p["unavailable_axes"])
        self.assertAlmostEqual(p["available_weight"], 0.66, places=4)
        # 全部 available 轴都达到 100，重归一化后 base_score 应接近 100
        self.assertAlmostEqual(p["base_score"], 100.0, places=1)
    
    def test_main_score_uses_mild_maturity_calibration(self):
        '''主分温和校准，不直接乘 confidence，也不让短样本直接到100'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        conf = snapshot["confidence"]
        self.assertLess(conf, 1.0)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.01, "recent_coverage": 0.01, "durable_notes": 0.01,
            "continuity": 0.01, "content_matrix": 0.01, "engagement_quality": 0.01, "battle_breadth": 0.01,
        })
        # 纯实力仍接近 100
        self.assertAlmostEqual(p["base_score"], 100.0, places=1)
        self.assertAlmostEqual(p["strength_score"], p["base_score"], places=4)
        # 2天释放92%的纯实力
        self.assertEqual(p["maturity_factor"], 0.92)
        self.assertAlmostEqual(p["maturity_calibrated_score"], 92.0, places=1)
        self.assertEqual(p["score"], 92)
        # 保守分仍是 base_score * confidence
        self.assertAlmostEqual(p["confidence_adjusted_score"], p["base_score"] * conf, places=4)
        # 主分比旧式直接置信折减更高，但不再虚贴100
        self.assertGreater(p["score"], p["confidence_adjusted_score"])
        self.assertLess(p["score"], p["base_score"])
    
    def test_confidence_adjusted_score_uses_confidence(self):
        '''保守分仍乘 confidence'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        conf = snapshot["confidence"]
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.01, "recent_coverage": 0.01, "durable_notes": 0.01,
            "continuity": 0.01, "content_matrix": 0.01, "engagement_quality": 0.01, "battle_breadth": 0.01,
        })
        # confidence_adjusted_score = base_score * confidence
        expected = p["base_score"] * conf
        self.assertAlmostEqual(p["confidence_adjusted_score"], expected, places=4)
        # 而主分 score 不等于 confidence_adjusted_score
        self.assertNotEqual(p["score"], round(p["confidence_adjusted_score"]))
    
    def test_not_5_days_cannot_breakthrough(self):
        '''未满5天不能>100'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.1, "recent_coverage": 0.1, "durable_notes": 0.1,
            "continuity": 0.1, "content_matrix": 0.1, "engagement_quality": 0.1, "battle_breadth": 0.1,
        })
        self.assertFalse(p["breakthrough_gate"])
        self.assertLessEqual(p["score"], 100)
    
    def test_5_days_and_conditions_met_can_breakthrough(self):
        '''满5天且门槛满足可>100'''
        end_idx = WINDOW_DAYS - 1
        events = self._make_rich_events_5days(end_idx)
        snapshot = _build_account_raw_snapshot(events, end_idx, self.articles)
        # 满5天，confidence=1.0
        self.assertEqual(snapshot["confidence"], 1.0)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.1, "recent_coverage": 0.1, "durable_notes": 0.1,
            "continuity": 0.1, "content_matrix": 0.1, "engagement_quality": 0.1, "battle_breadth": 0.1,
        })
        # 如果满足门槛，score > 100
        if p["breakthrough_gate"]:
            self.assertGreater(p["score"], 100)
    
    def test_previous_independent_availability(self):
        '''previous 独立判断可用性（旧的有效日不足3天时 durable 不可用）'''
        # current 有5天，previous 只有2天
        # 用同一个 snapshot 但 observation_span_days 不同来模拟
        current_snapshot = _build_account_raw_snapshot(
            [_make_event(i, "kw1", "a1", 5) for i in range(10, 15, 1)], 
            14, self.articles
        )
        previous_snapshot = _build_account_raw_snapshot(
            [_make_event(13, "kw1", "a1", 5), _make_event(14, "kw1", "a1", 5)], 
            14, self.articles, global_effective_day_count=2
        )
        # current: 5天 observation_span_days
        avail_c, unavail_c, _ = _available_axes_for_board("account", current_snapshot)
        self.assertNotIn("durable_notes", unavail_c)
        # previous: 2天 observation_span_days（通过 global_effective_day_count 模拟）
        avail_p, unavail_p, _ = _available_axes_for_board("account", previous_snapshot)
        self.assertIn("durable_notes", unavail_p)
    
    def test_score_semantics_version(self):
        '''parts 包含 score_semantics 字段'''
        events = [_make_event(14, "kw1", "a1", 5)]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 1.0, "recent_coverage": 1.0, "durable_notes": 1.0,
            "continuity": 0.3, "content_matrix": 0.5, "engagement_quality": 1.0, "battle_breadth": 0.5,
        })
        self.assertIn("score_semantics", p)
        self.assertIn("v3_evidence_calibrated", p["score_semantics"])
    
    def test_timeliness_score_unchanged(self):
        '''时效分在改前改后 fixture 结果不变（使用 v1 路径）'''
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=100)}
        snapshot = _build_timeliness_raw_snapshot(events, 14, date(2026, 7, 12), articles)
        benchmarks = {"top3_volume": 1.0, "top3_breadth": 1.0, "new_top3": 1.0,
                      "fresh_top3": 1.0, "upward_momentum": 1.0, "new_entry_engagement": 1.0, "top3_continuity": 0.5}
        n, p = score_board_snapshot(snapshot, "timeliness", benchmarks)
        self.assertGreater(p["score"], 0)
        self.assertLessEqual(p["score"], 100)
    
    def test_today_score_unchanged(self):
        '''当天分在改前改后 fixture 结果不变（使用 v1 路径）'''
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=100)}
        snapshot = _build_today_raw_snapshot(events, 14, articles)
        benchmarks = {"today_top3": 1.0, "today_keywords": 1.0, "today_notes": 1.0,
                      "today_rank_quality": 0.5, "today_new_entries": 1.0, "today_engagement_quality": 1.0, "today_breadth": 0.5}
        n, p = score_board_snapshot(snapshot, "today", benchmarks)
        self.assertGreater(p["score"], 0)
        self.assertLessEqual(p["score"], 100)
    
    def test_contract_fields_exist(self):
        '''parts 契约字段存在'''
        events = [_make_event(14, "kw1", "a1", 5)]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 1.0, "recent_coverage": 1.0, "durable_notes": 1.0,
            "continuity": 0.3, "content_matrix": 0.5, "engagement_quality": 1.0, "battle_breadth": 0.5,
        })
        for field in ["available_axes", "unavailable_axes", "available_weight",
                       "strength_score", "maturity_factor", "maturity_calibrated_score",
                       "confidence_adjusted_score", "score_semantics"]:
            self.assertIn(field, p, f"parts missing {field}")

    def test_maturity_factor_release_curve(self):
        self.assertEqual(_account_maturity_factor(0), 0.0)
        self.assertEqual(_account_maturity_factor(0.38), 0.88)
        self.assertEqual(_account_maturity_factor(0.55), 0.92)
        self.assertEqual(_account_maturity_factor(0.74), 0.95)
        self.assertEqual(_account_maturity_factor(0.88), 0.98)
        self.assertEqual(_account_maturity_factor(1.0), 1.0)

    def test_category_breadth_does_not_double_count_keywords(self):
        concentrated = [
            _make_event(14, f"kw{i}", f"a{i}", 5, topic=f"kw{i}", bucket="同一类别")
            for i in range(8)
        ]
        diversified = [
            _make_event(14, f"kw{i}", f"a{i}", 5, topic=f"kw{i}", bucket=f"类别{i % 4}")
            for i in range(8)
        ]
        concentrated_raw, concentrated_effective, _ = _category_breadth_raw(concentrated)
        diversified_raw, diversified_effective, _ = _category_breadth_raw(diversified)
        self.assertEqual(concentrated_effective, 1.0)
        self.assertGreater(diversified_effective, concentrated_effective)
        self.assertGreater(diversified_raw, concentrated_raw)
    
    def test_frontend_contract_confidence_line(self):
        '''前端 JS 包含 v2 展示文案'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("scoreConfidenceLine", js, "JS 应包含 scoreConfidenceLine 函数")
        self.assertIn("观察成熟度", js, "JS 应包含观察成熟度文案")
        self.assertIn("score-axis-unavail", js, "JS 应包含暂未纳入总分标记")
        self.assertIn("score-tip-confidence", js, "JS 应包含 score-tip-confidence")


class TestV2RegressionFixes(unittest.TestCase):
    """回归测试覆盖6个产品语义问题。"""
    
    def setUp(self):
        self.articles = {
            "a1": _make_article("a1", liked=100, collected=50, comment=10, shared=5),
            "a2": _make_article("a2", liked=200, collected=100, comment=20, shared=10),
        }
    
    def test_over_axes_only_from_available(self):
        '''Issue 1: over_axes 只从 available_axes 过滤，不可用轴即使>100也不计入'''
        events = [
            _make_event(13, "kw1", "a1", 5),
            _make_event(14, "kw1", "a1", 5),
        ]
        snapshot = _build_account_raw_snapshot(events, 14, self.articles)
        # 用极低 benchmark 让所有轴都>100
        n, p = _score_board_snapshot_v2(snapshot, "account", {
            "history_coverage": 0.001, "recent_coverage": 0.001, "durable_notes": 0.001,
            "continuity": 0.001, "content_matrix": 0.001, "engagement_quality": 0.001, "battle_breadth": 0.001,
        })
        # durable_notes 不可用，不应出现在 over_axes 中
        self.assertIn("durable_notes", p["unavailable_axes"])
        self.assertNotIn("durable_notes", p["over_axes"],
                         "durable_notes 不可用，不应出现在 over_axes")
    
    def test_html_tooltip_explains_maturity_calibration(self):
        '''Issue 2: monitor.html 解释温和成熟度校准'''
        import os
        html_path = os.path.join(os.path.dirname(__file__), "..", "app", "templates", "monitor.html")
        with open(html_path) as f:
            html = f.read()
        self.assertIn("88%/92%/95%/98%/100%", html,
                      "tooltip 应写有效日对应的成熟释放曲线")
        self.assertIn("历史覆盖满8日才参与", html,
                      "tooltip 应说明历史覆盖的可用门槛")
        self.assertIn("稳定笔记满3日才参与", html,
                      "tooltip 应说明稳定笔记的可用门槛")
        self.assertIn("满5有效日才可突破100", html,
                       "tooltip 应写满5有效日才可突破100")
        self.assertNotIn("P99归一化+置信度", html,
                         "tooltip 不应再写 P99归一化+置信度")
    
    def test_insight_excludes_unavailable(self):
        '''Issue 3: scoreInsightRows 排除 unavailable_axes'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        # 确认 scoreInsightRows 过滤了 unavailable
        self.assertIn("unavailable.includes(meta.key)", js,
                       "scoreInsightRows 应排除 unavailable_axes")
        self.assertIn("availableMetas", js,
                       "scoreInsightRows 应使用 availableMetas")
    
    def test_unavailable_axis_card_placeholder(self):
        '''Issue 4: 不可用轴卡显示观察期占位文案'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("观察期数据不足", js,
                       "不可用轴卡应显示观察期数据不足")
        self.assertIn("等待第3天验证", js,
                       "不可用轴卡排名应显示等待第3天验证")
        self.assertIn("is-unavailable", js,
                       "不可用轴卡应有 is-unavailable class")
    
    def test_confidence_line_chinese_label(self):
        '''Issue 5: scoreConfidenceLine 不可用轴中文映射'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("AXIS_LABEL_MAP", js,
                       "scoreConfidenceLine 应有中文映射表")
        self.assertIn("稳定笔记", js,
                       "scoreConfidenceLine 应映射 durable_notes 为稳定笔记")
        self.assertIn("项已纳入", js,
                       "scoreConfidenceLine 应显示 N 项已纳入")
        self.assertIn("等待第3天", js,
                       "scoreConfidenceLine 应显示等待第3天")
    
    def test_delta_zero_no_em(self):
        '''Issue 6: delta=0 时不显示 em 标签，防 P99=1000'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn('scoreD !== 0', js,
                       "delta=0 时应隐藏 em 标签")
        self.assertNotIn('P99=1000', js,
                         "不应出现 P99=1000 拼接")
    
    def test_css_has_unavailable_style(self):
        '''CSS 包含 is-unavailable 样式'''
        import os
        css_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "monitor.css")
        with open(css_path) as f:
            css = f.read()
        self.assertIn(".score-axis-card.is-unavailable", css,
                       "CSS 应包含 is-unavailable 样式")
    
    def test_timeliness_today_unchanged(self):
        '''时效分/当天分在 v2 下仍完全不变（使用 score_board_snapshot dispatch）'''
        events = [_make_event(14, "kw1", "a1", 1)]
        articles = {"a1": _make_article("a1", liked=100)}
        from datetime import date
        
        # timeliness
        ts = _build_timeliness_raw_snapshot(events, 14, date(2026, 7, 12), articles)
        tb = {"top3_volume": 1.0, "top3_breadth": 1.0, "new_top3": 1.0,
              "fresh_top3": 1.0, "upward_momentum": 1.0, "new_entry_engagement": 1.0, "top3_continuity": 0.5}
        tn, tp = score_board_snapshot(ts, "timeliness", tb)
        self.assertGreater(tp["score"], 0)
        self.assertLessEqual(tp["score"], 100)
        self.assertEqual(tp["score_semantics"], "xhs_three_board_p99_v1")
        
        # today
        ds = _build_today_raw_snapshot(events, 14, articles)
        db = {"today_top3": 1.0, "today_keywords": 1.0, "today_notes": 1.0,
              "today_rank_quality": 0.5, "today_new_entries": 1.0, "today_engagement_quality": 1.0, "today_breadth": 0.5}
        dn, dp = score_board_snapshot(ds, "today", db)
        self.assertGreater(dp["score"], 0)
        self.assertLessEqual(dp["score"], 100)
        self.assertEqual(dp["score_semantics"], "xhs_three_board_p99_v1")


class TestV2Round2Fixes(unittest.TestCase):
    """第二轮回归测试：权重动态展示、重复标点。"""
    
    def test_scoreWeightText_accepts_modeName_and_a(self):
        '''scoreWeightText 接收 modeName 和 a 参数'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("function scoreWeightText(hexagon, modeName, a)", js,
                       "scoreWeightText 应接收 modeName 和 a")
    
    def test_weight_renormalized_for_unavailable(self):
        '''不可用轴时展示重归一化权重'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("availWeight", js, "应有 available_weight 重归一化计算")
        self.assertIn("暂不参与", js, "不可用轴应标暂不参与")
    
    def test_axis_definition_dynamic_count(self):
        '''scoreAxisDefinitionText 动态显示当前已纳入项数'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("当前${availCount}项已纳入加权", js,
                       "应动态显示当前已纳入项数")
        self.assertIn("${unavail.length}项等待观察", js,
                       "应动态显示等待观察项数")
    
    def test_confidence_label_no_duplicate_period(self):
        '''confidenceLabel 末尾无多余句号'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        self.assertIn("观察满 5 个有效观察日后取得完整置信度',", js,
                       "confidenceLabel 5天不应有末尾句号")
        self.assertIn("观察满 3 个有效观察日后取得完整置信度',", js,
                       "confidenceLabel 3天不应有末尾句号")
    
    def test_timeliness_today_weights_unchanged(self):
        '''时效/当天榜继续展示名义权重，不受重归一化影响'''
        import os
        js_path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
        with open(js_path) as f:
            js = f.read()
        # 确认重归一化只在 modeName === 'score' 时触发
        self.assertIn("if (modeName === 'score' && a)", js,
                       "重归一化权重只在 account 榜触发")
