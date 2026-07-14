from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.repositories.keyword_registry_repo import effective_refresh_interval_hours
from app.services.keyword_refresh_policy_service import recommend_frequency
from app.services.refresh_service import (
    _calculate_adaptive_batch_limit,
    get_incremental_keywords,
)
from app.services import scheduler_service
from app.services.scheduler_service import (
    _advance_fixed_clock,
    _check_active_batch,
    _summarize_auto_policy,
)


class KeywordRefreshScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data/config").mkdir(parents=True)
        (self.root / "data/state").mkdir(parents=True)
        (self.root / "normalized").mkdir(parents=True)
        (self.root / "data/config/keyword_refresh_policy.json").write_text(
            json.dumps(
                {
                    "daily_keyword_budget": 500,
                    "max_keywords_per_batch": 250,
                    "failure_retry_hours": 12,
                    "minimum_observation_days": 15,
                    "analysis_window_days": 30,
                    "turnover_thresholds": {
                        "daily_min": 0.3,
                        "three_day_min": 0.15,
                        "seven_day_min": 0.05,
                    },
                    "rank_movement_weight": 0.6,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.root / "normalized/snapshots.json").write_text("[]", encoding="utf-8")
        (self.root / "normalized/ranking_hits.json").write_text("[]", encoding="utf-8")
        self.db_path = self.root / "data/state/app.db"
        self.repo = KeywordRegistryRepository(self.db_path)
        self.group_id = self.repo.create_group("测试组")["group_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_policy_is_not_overwritten_and_result_state_is_saved(self) -> None:
        auto_item = self.repo.create_keyword(self.group_id, "自动测试词")
        manual_item = self.repo.create_keyword(self.group_id, "人工测试词")
        self.repo.set_refresh_policy(
            manual_item["keyword_id"],
            refresh_frequency_days=7,
            source="manual",
        )
        changed = self.repo.apply_auto_refresh_policies(
            {
                auto_item["keyword_id"]: {
                    "refresh_frequency_days": 15,
                    "refresh_policy_reason": "自动：稳定",
                },
                manual_item["keyword_id"]: {
                    "refresh_frequency_days": 1,
                    "refresh_policy_reason": "自动：高变化",
                },
            }
        )
        self.assertEqual(changed, 1)
        self.assertEqual(self.repo.get(auto_item["keyword_id"])["refresh_frequency_days"], 15)
        self.assertEqual(self.repo.get(manual_item["keyword_id"])["refresh_frequency_days"], 7)
        restored = self.repo.set_refresh_policy(
            manual_item["keyword_id"],
            source="auto",
        )
        self.assertEqual(restored["refresh_frequency_days"], 1)
        self.assertEqual(restored["refresh_frequency_source"], "auto")

        self.repo.record_refresh_results(
            succeeded_keyword_ids=[auto_item["keyword_id"]],
            failed_keyword_ids=[manual_item["keyword_id"]],
            refreshed_at="2026-07-11T10:00:00",
        )
        auto_after = self.repo.get(auto_item["keyword_id"])
        manual_after = self.repo.get(manual_item["keyword_id"])
        self.assertEqual(auto_after["last_refresh_status"], "success")
        self.assertEqual(auto_after["last_refresh_at"], "2026-07-11T10:00:00")
        self.assertEqual(manual_after["last_refresh_status"], "failed")
        self.assertIsNone(manual_after["last_refresh_at"])

    def test_policy_thresholds_are_transparent(self) -> None:
        self.assertEqual(
            recommend_frequency({"observation_days": 14})["refresh_frequency_days"],
            1,
        )
        self.assertEqual(
            recommend_frequency(
                {"observation_days": 15, "article_turnover_rate": 0.35}
            )["refresh_frequency_days"],
            1,
        )
        self.assertEqual(
            recommend_frequency(
                {"observation_days": 15, "article_turnover_rate": 0.20}
            )["refresh_frequency_days"],
            3,
        )
        self.assertEqual(
            recommend_frequency(
                {"observation_days": 15, "article_turnover_rate": 0.10}
            )["refresh_frequency_days"],
            7,
        )
        self.assertEqual(
            recommend_frequency(
                {"observation_days": 15, "article_turnover_rate": 0.01}
            )["refresh_frequency_days"],
            15,
        )

    def test_scheduler_respects_daily_and_batch_budget(self) -> None:
        for text in ("测试词A", "测试词B", "测试词C"):
            self.repo.create_keyword(self.group_id, text)

        with (
            patch.object(Config, "PROJECT_ROOT", self.root),
            patch.object(Config, "NORMALIZED_DIR", self.root / "normalized"),
            patch.object(Config, "SQLITE_PATH", self.db_path),
            patch(
                "app.services.refresh_service.LEDGER_PATH",
                self.root / "data/state/keyword_refresh_ledger.json",
            ),
        ):
            plan = get_incremental_keywords(
                daily_keyword_budget=2,
                max_keywords_per_batch=2,
                now=datetime(2026, 7, 11, 10, 0, 0),
            )
        self.assertEqual(plan["due_count"], 3)
        self.assertEqual(len(plan["keywords"]), 2)
        self.assertEqual(plan["budget"]["remaining_count"], 2)

    def test_observing_auto_keyword_uses_three_hour_slice_and_repo_schedule(self) -> None:
        item = self.repo.create_keyword(self.group_id, "观察期测试词")
        self.repo.set_discovery_lifecycle(
            item["keyword_id"],
            lifecycle_stage="observing",
            observation_started_at="2026-07-11T10:00:00",
            observation_deadline_at="2026-07-26T10:00:00",
        )
        self.repo.record_refresh_results(
            succeeded_keyword_ids=[item["keyword_id"]],
            failed_keyword_ids=[],
            refreshed_at="2026-07-11T10:00:00",
        )
        observed = self.repo.get(item["keyword_id"])
        self.assertEqual(effective_refresh_interval_hours(observed), 3)
        self.assertEqual(observed["effective_refresh_interval_hours"], 3)
        self.assertEqual(observed["next_refresh_at"], "2026-07-11T13:00:00")

    def test_scheduler_catalog_preserves_observing_three_hour_cadence(self) -> None:
        item = self.repo.create_keyword(self.group_id, "调度观察词")
        self.repo.set_discovery_lifecycle(
            item["keyword_id"],
            lifecycle_stage="observing",
            observation_started_at="2026-07-11T10:00:00",
            observation_deadline_at="2026-07-26T10:00:00",
        )
        self.repo.record_refresh_results(
            succeeded_keyword_ids=[item["keyword_id"]],
            failed_keyword_ids=[],
            refreshed_at="2026-07-11T10:00:00",
        )
        with (
            patch.object(Config, "PROJECT_ROOT", self.root),
            patch.object(Config, "NORMALIZED_DIR", self.root / "normalized"),
            patch.object(Config, "SQLITE_PATH", self.db_path),
            patch(
                "app.services.refresh_service.LEDGER_PATH",
                self.root / "data/state/keyword_refresh_ledger.json",
            ),
            patch(
                "app.services.refresh_service.BATCH_RUNS_ROOT",
                self.root / "data/runs",
            ),
        ):
            early = get_incremental_keywords(
                daily_keyword_budget=10,
                max_keywords_per_batch=10,
                now=datetime(2026, 7, 11, 12, 59, 0),
            )
            due = get_incremental_keywords(
                daily_keyword_budget=10,
                max_keywords_per_batch=10,
                now=datetime(2026, 7, 11, 13, 0, 0),
            )
        self.assertNotIn(item["keyword_id"], {x["keyword_id"] for x in early["keywords"]})
        self.assertIn(item["keyword_id"], {x["keyword_id"] for x in due["keywords"]})

    def test_refresh_events_keep_each_keywords_actual_completion_time(self) -> None:
        first = self.repo.create_keyword(self.group_id, "先完成")
        second = self.repo.create_keyword(self.group_id, "后完成")
        result = self.repo.record_refresh_events(
            succeeded_at_by_id={
                first["keyword_id"]: "2026-07-11T10:02:00",
                second["keyword_id"]: "2026-07-11T12:58:00",
            },
            failed_keyword_ids=[],
            refreshed_at="2026-07-11T13:00:00",
        )
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(
            self.repo.get(first["keyword_id"])["last_refresh_at"],
            "2026-07-11T10:02:00",
        )
        self.assertEqual(
            self.repo.get(second["keyword_id"])["last_refresh_at"],
            "2026-07-11T12:58:00",
        )

    def test_adaptive_batch_limit_targets_recent_rpa_throughput(self) -> None:
        capacity = _calculate_adaptive_batch_limit(
            250,
            [90, 100, 110],
            target_runtime_minutes=180,
            minimum_batch_size=20,
        )
        self.assertEqual(capacity["median_keyword_seconds"], 100)
        self.assertEqual(capacity["effective_limit"], 108)
        self.assertEqual(capacity["sample_count"], 3)

    def test_manual_observing_keyword_keeps_one_day_cadence_and_enters_scheduler(self) -> None:
        item = self.repo.create_keyword(self.group_id, "人工周期测试词")
        self.repo.set_refresh_policy(
            item["keyword_id"],
            refresh_frequency_days=1,
            source="manual",
        )
        self.repo.set_discovery_lifecycle(
            item["keyword_id"],
            lifecycle_stage="observing",
            observation_started_at="2026-07-11T10:00:00",
            observation_deadline_at="2026-07-26T10:00:00",
        )
        self.repo.record_refresh_results(
            succeeded_keyword_ids=[item["keyword_id"]],
            failed_keyword_ids=[],
            refreshed_at="2026-07-11T10:00:00",
        )
        with (
            patch.object(Config, "PROJECT_ROOT", self.root),
            patch.object(Config, "NORMALIZED_DIR", self.root / "normalized"),
            patch.object(Config, "SQLITE_PATH", self.db_path),
            patch(
                "app.services.refresh_service.LEDGER_PATH",
                self.root / "data/state/keyword_refresh_ledger.json",
            ),
        ):
            plan = get_incremental_keywords(
                daily_keyword_budget=10,
                max_keywords_per_batch=10,
                now=datetime(2026, 7, 11, 10, 0, 0),
            )
            due_plan = get_incremental_keywords(
                daily_keyword_budget=10,
                max_keywords_per_batch=10,
                now=datetime(2026, 7, 12, 10, 0, 0),
            )
        self.assertNotIn(item["keyword_id"], {x["keyword_id"] for x in plan["keywords"]})
        self.assertIn(item["keyword_id"], {x["keyword_id"] for x in due_plan["keywords"]})
        self.assertEqual(
            effective_refresh_interval_hours(self.repo.get(item["keyword_id"])),
            24,
        )
        self.assertEqual(
            self.repo.get(item["keyword_id"])["effective_refresh_interval_hours"],
            24,
        )

    def test_established_auto_one_day_exposes_24_hour_effective_interval(self) -> None:
        item = self.repo.create_keyword(self.group_id, "稳定期测试词")
        self.repo.record_refresh_results(
            succeeded_keyword_ids=[item["keyword_id"]],
            failed_keyword_ids=[],
            refreshed_at="2026-07-11T10:00:00",
        )
        established = self.repo.get(item["keyword_id"])
        self.assertEqual(established["lifecycle_stage"], "established")
        self.assertEqual(established["refresh_frequency_days"], 1)
        self.assertEqual(established["effective_refresh_interval_hours"], 24)

    def test_scheduler_advances_from_fixed_slot_and_skips_missed_slots(self) -> None:
        next_slot = _advance_fixed_clock(
            datetime(2026, 7, 13, 9, 0, 0),
            now=datetime(2026, 7, 13, 16, 10, 0),
            interval_hours=3,
        )
        self.assertEqual(next_slot, datetime(2026, 7, 13, 18, 0, 0))

    def test_scheduler_status_keeps_only_auto_policy_summary(self) -> None:
        summary = _summarize_auto_policy(
            {
                "updated_count": 7,
                "total_auto_keywords": 321,
                "distribution": {"1": 245, "3": 59},
                "metrics": {"kw_001": {"observation_days": 30}},
            }
        )
        self.assertEqual(
            summary,
            {
                "updated_count": 7,
                "total_auto_keywords": 321,
                "distribution": {"1": 245, "3": 59},
            },
        )

    def test_scheduler_startup_recovers_active_batch_without_self_http_request(self) -> None:
        with patch(
            "app.services.scheduler_service.get_active_batch_status",
            return_value={"batch_id": "web_20260711_161333", "is_active": True},
        ):
            self.assertEqual(_check_active_batch(), "web_20260711_161333")

    def test_scheduler_runs_discovery_after_regular_cycle(self) -> None:
        discovery_first = {
            "launch": {"launched": True, "batch_id": "discovery_001"},
            "summary": {"probes": {"queued": 1}, "candidates": {}},
        }
        discovery_reconciled = {
            "launch": {"launched": False, "reason": "auto_launch_disabled"},
            "summary": {"probes": {"replaced": 1}, "candidates": {"discovered": 1}},
        }
        with (
            patch(
                "app.services.scheduler_service.get_incremental_keywords",
                return_value={
                    "keywords": [],
                    "due_count": 0,
                    "deferred_due_count": 0,
                    "budget": {},
                    "auto_policy": {},
                },
            ),
            patch(
                "app.services.scheduler_service._wait_for_batch",
                return_value=True,
            ) as wait_for_batch,
            patch(
                "app.services.keyword_discovery_service.run_discovery_cycle",
                side_effect=[discovery_first, discovery_reconciled],
            ) as discovery_cycle,
        ):
            scheduler_service._do_refresh()
        self.assertEqual(discovery_cycle.call_count, 2)
        self.assertTrue(discovery_cycle.call_args_list[0].kwargs["auto_launch"])
        self.assertFalse(discovery_cycle.call_args_list[1].kwargs["auto_launch"])
        wait_for_batch.assert_called_once_with(
            "discovery_001",
            scheduler_service._state["base_url"],
        )
        self.assertEqual(
            scheduler_service._state["last_discovery"],
            discovery_reconciled,
        )


if __name__ == "__main__":
    unittest.main()
