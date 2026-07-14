from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts import refresh_failure_autofix as autofix
from scripts.keyword_batch_watchdog import should_autofix_terminal_state


def test_only_failure_or_remote_cancel_events_are_eligible():
    assert autofix.is_failure_event({"status": "completed_with_failures"}) == (
        True,
        "completed_with_failures",
    )
    assert autofix.is_failure_event(
        {"status": "cancelled", "cancel_reason": "对方电脑掉线"}
    ) == (True, "cancelled_remote_signal")
    assert autofix.is_failure_event(
        {"status": "cancelled", "cancel_reason": "用户主动取消"}
    ) == (False, "manual_cancel")
    assert autofix.is_failure_event({"status": "completed"}) == (
        False,
        "status_completed",
    )
    assert autofix.is_failure_event({"status": "running"}, "runner_not_alive") == (
        True,
        "runner_not_alive",
    )
    assert should_autofix_terminal_state({"status": "completed_with_failures"}) is True
    assert should_autofix_terminal_state(
        {"status": "cancelled", "cancel_reason": "对方电脑掉线"}
    ) is True
    assert should_autofix_terminal_state({"status": "completed"}) is False
    assert should_autofix_terminal_state(
        {"status": "cancelled", "cancel_reason": "用户主动取消"}
    ) is False


def test_dry_run_writes_event_evidence_without_calling_claude():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        batch_dir = root / "batch_001"
        batch_dir.mkdir()
        (batch_dir / "state.json").write_text(
            json.dumps(
                {
                    "batch_id": "batch_001",
                    "status": "completed_with_failures",
                    "total_keywords": 2,
                    "success_count": 1,
                    "failed_count": 1,
                    "server": "http://192.168.31.238:8000",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(Path(autofix.__file__).resolve()),
                "--batch-id",
                "batch_001",
                "--runs-root",
                str(root),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        run = json.loads((batch_dir / "autofix" / "run.json").read_text(encoding="utf-8"))
        event = json.loads((batch_dir / "autofix" / "event.json").read_text(encoding="utf-8"))
        assert run["status"] == "dry_run"
        assert event["trigger_reason"] == "completed_with_failures"
        assert (batch_dir / "autofix" / "context.md").exists()


def test_watchdog_starts_the_dry_run_autofix_process():
    with tempfile.TemporaryDirectory() as temp:
        runs_root = Path(temp)
        batch_dir = runs_root / "batch_002"
        batch_dir.mkdir()
        (batch_dir / "state.json").write_text(
            json.dumps(
                {
                    "batch_id": "batch_002",
                    "status": "completed_with_failures",
                    "total_keywords": 3,
                    "success_count": 2,
                    "failed_count": 1,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        env = {**os.environ, "WX_REFRESH_AUTOFIX_DRY_RUN": "1"}
        watchdog = Path(__file__).resolve().parent.parent / "scripts/keyword_batch_watchdog.py"
        result = subprocess.run(
            [
                sys.executable,
                str(watchdog),
                "--batch-id",
                "batch_002",
                "--runs-root",
                str(runs_root),
                "--interval",
                "1",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        run_path = batch_dir / "autofix" / "run.json"
        deadline = time.time() + 5
        while not run_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert run_path.exists()
        run = json.loads(run_path.read_text(encoding="utf-8"))
        assert run["status"] == "dry_run"
