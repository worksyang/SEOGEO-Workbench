from __future__ import annotations

import contextlib
import io
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from workflow_service import WorkflowOptions, run_full_workflow


class _LineLogWriter(io.TextIOBase):
    def __init__(self, on_line):
        self._on_line = on_line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._on_line(line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._on_line(self._buffer.rstrip())
        self._buffer = ""


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def create_job(
        self,
        runtime_settings: Dict[str, Any],
        selected_mp_ids: Set[str],
        refresh_before_run: bool,
        use_ai_filter: bool,
        days_to_fetch: int,
        start_page: Optional[int],
        end_page: Optional[int],
    ) -> Dict[str, Any]:
        with self._lock:
            active = [
                job for job in self._jobs.values()
                if job["status"] in {"queued", "running", "cancelling"}
            ]
            if active:
                raise RuntimeError(f"已有任务正在执行：{active[0]['id']}")

        job_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "progress": {
                "stage": "queued",
                "current": 0,
                "total": 0,
                "message": "任务已进入队列",
            },
            "request": {
                "refresh_before_run": refresh_before_run,
                "use_ai_filter": use_ai_filter,
                "days_to_fetch": days_to_fetch,
                "selected_mp_ids": sorted(selected_mp_ids),
                "start_page": start_page,
                "end_page": end_page,
            },
            "result": None,
            "error": None,
            "logs": ["任务已创建，等待后台执行。"],
        }
        stop_event = threading.Event()
        with self._lock:
            self._jobs[job_id] = job
            self._stop_events[job_id] = stop_event

        self._executor.submit(
            self._run_job,
            job_id,
            runtime_settings,
            selected_mp_ids,
            refresh_before_run,
            use_ai_filter,
            days_to_fetch,
            start_page,
            end_page,
            stop_event,
        )
        return self.get_job(job_id) or job

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            event = self._stop_events.get(job_id)
            if event is not None:
                event.set()
            if job["status"] in {"queued", "running"}:
                job["status"] = "cancelling"
                job["updated_at"] = datetime.now().isoformat(timespec="seconds")
                job["logs"].append("已收到停止请求，等待当前步骤安全退出。")
        return self.get_job(job_id) or job

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] {message}")
            job["logs"] = job["logs"][-500:]
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def _set_job(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(updates)
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def _set_progress(self, job_id: str, payload: Dict[str, Any]) -> None:
        progress = {
            "stage": payload.get("stage", "running"),
            "current": int(payload.get("current") or 0),
            "total": int(payload.get("total") or 0),
            "message": str(payload.get("message") or ""),
            "mp_name": payload.get("mp_name"),
        }
        self._set_job(job_id, progress=progress)
        if progress["message"]:
            self._append_log(job_id, progress["message"])

    def _run_job(
        self,
        job_id: str,
        runtime_settings: Dict[str, Any],
        selected_mp_ids: Set[str],
        refresh_before_run: bool,
        use_ai_filter: bool,
        days_to_fetch: int,
        start_page: Optional[int],
        end_page: Optional[int],
        stop_event: threading.Event,
    ) -> None:
        self._set_job(job_id, status="running")
        self._append_log(job_id, "后台任务开始执行。")
        options = WorkflowOptions(
            base_url=str(runtime_settings["werss_base_url"]),
            username=str(runtime_settings["username"]),
            password=str(runtime_settings["password"]),
            refresh_before_run=refresh_before_run,
            use_ai_filter=use_ai_filter,
            days_to_fetch=days_to_fetch,
            refresh_wait_seconds=int(runtime_settings["refresh_wait_seconds"]),
            selected_mp_ids=selected_mp_ids,
            output_dir=str(runtime_settings["output_dir"]),
            rejected_csv_file=str(runtime_settings["rejected_csv_file"]),
            start_page=start_page,
            end_page=end_page,
            classifier_platform=str(runtime_settings["classifier_platform"]),
            classifier_model=str(runtime_settings["classifier_model"]),
        )

        writer = _LineLogWriter(lambda line: self._append_log(job_id, line))
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                result = run_full_workflow(
                    options,
                    progress_callback=lambda payload: self._set_progress(job_id, payload),
                    stop_event=stop_event,
                )
            writer.flush()
            if stop_event.is_set() or result.get("stopped"):
                self._set_job(job_id, status="cancelled", result=result)
                self._append_log(job_id, "任务已停止。")
            else:
                self._set_job(job_id, status="success", result=result)
                self._append_log(job_id, "任务执行完成。")
        except Exception as exc:
            writer.flush()
            self._set_job(job_id, status="failed", error=str(exc))
            self._append_log(job_id, traceback.format_exc())
        finally:
            with self._lock:
                self._stop_events.pop(job_id, None)
