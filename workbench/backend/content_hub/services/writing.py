"""WritingMoney 服务：安全的配置状态、演示 Provider 与任务恢复。

v3.3 协议：
- 项目 / 批次 / 素材 / 草稿落到 wm_* 运行表；
- production_jobs 仅保留旧业务岛屿兼容任务壳和恢复事件，不能再作为领域事实源；
- 素材必用 / 参考 / 不用 = payload_json 内部分；
- 默认不配置任何真实 Provider；FakeProvider 只能由调用方显式注入并始终标记为 demo_only。
- 输出 Markdown 写到 asset_store/generated，并登记为 content。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..domain.ids import generate_ulid_like
from ..ingestion.markdown_store import MarkdownStore
from ..validation.timestamps import utc_now_iso
from .audit import AuditService
from .jobs import JobsService
from .safety import public_asset_ref, scrub_public_payload


@dataclass(slots=True)
class WritingJob:
    job_id: str
    job_type: str  # mother_forge | batch_production
    topic: str
    status: str
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict) -> "WritingJob":
        if isinstance(row, sqlite3.Row):
            data = {k: row[k] for k in row.keys()}
        else:
            data = dict(row)
        payload = json.loads(data.pop("payload_json") or "{}")
        return cls(
            job_id=data["job_id"],
            job_type=data["job_type"],
            topic=str(payload.get("topic") or ""),
            status=data["status"],
            payload=payload,
        )


class FakeProvider:
    """用于测试的 AI 提供方；不依赖任何真实 LLM。"""

    def __init__(self, latency_ms: int = 80):
        self.latency_ms = latency_ms
        self.kind = "fake"
        self.status = "demo_only"

    def generate(
        self,
        prompt: str,
        *,
        hook: str,
        mother_articles: list[str],
        keywords: list[str],
    ) -> str:
        time.sleep(min(self.latency_ms, 80) / 1000.0)
        joined_mothers = "\n\n".join(f"- {article}" for article in mother_articles) or "- 暂无母文章"
        joined_keywords = "、".join(keywords) or "（未指定关键词）"
        return (
            f"# {prompt}\n\n"
            f"> 自动生成 · 仅作演示用 · 钩子：{hook}\n\n"
            f"## 钩子与定位\n\n本篇围绕「{joined_keywords}」切入，"
            f"承接下列母文章事实：\n\n{joined_mothers}\n\n"
            f"## 主体内容\n\n1. 行业现状与读者基础\n2. 必须理解的产品边界\n"
            f"3. 评估与判断建议\n4. 风险与替代方案\n"
        )


class WritingService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        markdown_store: MarkdownStore,
        provider: FakeProvider | None = None,
        provider_kind: str = "unconfigured",
        provider_status: str = "unconfigured",
    ):
        self._conn = connection
        self._markdown = markdown_store
        self._provider = provider
        self._provider_kind = getattr(provider, "kind", provider_kind) if provider else provider_kind
        self._provider_status = getattr(provider, "status", provider_status) if provider else provider_status
        self._jobs = JobsService(connection)
        self._audit = AuditService(connection)

    def _record_write_receipt(
        self,
        *,
        operation: str,
        job_id: str,
        operator: str,
        source_ref: str,
        manifest: dict[str, Any],
        legacy_status: str = "not_written",
        hub_status: str = "succeeded",
        reconcile_status: str = "matched",
    ) -> None:
        """记录真实 Hub 写入回执，并明确本模块不反向双写 legacy UI。"""
        manifest_payload = {
            "module": "writing",
            "operation": operation,
            "job_id": job_id,
            "source_ref": source_ref,
            **manifest,
        }
        manifest_hash = hashlib.sha256(
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        manifest_id = f"sm_writing_{manifest_hash[:24]}"
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO source_manifests(
                manifest_id, system_key, source_kind, root_fingerprint,
                manifest_hash, entry_count, captured_at, immutable, payload_json
            ) VALUES (?, 'writing-money', 'runtime-write', 'writing-runtime', ?, ?, ?, 1, ?)
            """,
            (
                manifest_id,
                manifest_hash,
                len(manifest_payload.get("runtime_tables") or []),
                now,
                json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO source_manifest_entries(
                manifest_id, relative_path, content_hash, size_bytes, observed_at, payload_json
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                manifest_id,
                source_ref[:800],
                manifest_hash,
                now,
                json.dumps({"source_ref": source_ref, "operator": operator}, ensure_ascii=False),
            ),
        )
        idempotency_key = f"{operation}:{job_id}:{manifest_hash}"
        command_id = _runtime_deterministic_id("cmd", "writing", idempotency_key)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO command_runs(
                command_id, module_key, command_type, idempotency_key, actor_id,
                status, input_json, output_json, created_at, updated_at
            ) VALUES (?, 'writing', ?, ?, ?, 'succeeded', ?, ?, ?, ?)
            """,
            (
                command_id,
                operation,
                idempotency_key,
                operator,
                json.dumps({"source_ref": source_ref, "manifest_id": manifest_id}, ensure_ascii=False),
                json.dumps({"hub_status": hub_status}, ensure_ascii=False),
                now,
                now,
            ),
        )
        details = {
            "operation": operation,
            "operator": operator,
            "legacy_dual_write": False,
            "legacy_status": legacy_status,
            "hub_status": hub_status,
            "source_ref": source_ref,
            "manifest_id": manifest_id,
            "manifest": manifest_payload,
        }
        self._conn.execute(
            """
            INSERT INTO dual_write_receipts(
                receipt_id, module_key, command_id, idempotency_key,
                legacy_status, hub_status, reconcile_status, details_json, created_at
            ) VALUES (?, 'writing', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module_key, idempotency_key) DO UPDATE SET
                details_json=excluded.details_json,
                hub_status=excluded.hub_status,
                reconcile_status=excluded.reconcile_status
            """,
            (
                _runtime_deterministic_id("dwr", "writing", idempotency_key),
                command_id,
                idempotency_key,
                legacy_status,
                hub_status,
                reconcile_status,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def create_mother_forge(
        self,
        *,
        topic: str,
        purpose: str,
        urls: Iterable[str] = (),
        recommended_mothers: Iterable[str] = (),
        operator: str = "user",
        idempotency_key: str = "",
    ) -> WritingJob:
        existing = self._idempotent_job(idempotency_key)
        if existing:
            return existing
        url_list = list(urls)
        recommended_list = list(recommended_mothers)
        payload = {
            "topic": topic,
            "purpose": purpose,
            "urls": url_list,
            "recommended_mothers": recommended_list,
            "mode": "mother_forge",
            "stage": "decision",
            "materials": [],
            "url_materials": [],
            "templates": [],
            "plan": {
                "titleDirection": "等待真实写作决策回执",
                "core": "等待素材、模板与方案确认。",
                "outline": ["读取已持久化任务输入", "等待真实 Provider 或人工确认", "通过审计后进入下一阶段"],
                "close": "真实生成完成后再进入发布链路。",
            },
        }
        job_id = self._jobs.create(
            job_type="mother_forge",
            payload=payload,
        )
        self._create_runtime_project(job_id, topic=topic, purpose=purpose, operator=operator)
        self._record_write_receipt(
            operation="create_mother_forge",
            job_id=job_id,
            operator=operator,
            source_ref=f"writing/job/{job_id}",
            manifest={"runtime_tables": ["wm_projects", "wm_project_events"]},
        )
        self._audit.record(
            action="writing.create",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details=self._safe_provider_details(mode="mother_forge"),
        )
        self._record_command(
            idempotency_key=idempotency_key,
            command_type="create_mother_forge",
            actor=operator,
            status="succeeded",
            input_data={"topic": topic, "purpose": purpose, "urls": url_list, "recommended_mothers": recommended_list},
            output_data={"job_id": job_id},
        )
        return WritingJob(job_id=job_id, job_type="mother_forge", topic=topic, status="queued", payload=payload)

    def create_batch(
        self,
        *,
        topic: str,
        source: str,
        requirements: dict[str, Any],
        keywords: Iterable[str],
        target_article_count: int,
        matched_articles: Iterable[str] = (),
        operator: str = "user",
        idempotency_key: str = "",
    ) -> WritingJob:
        existing = self._idempotent_job(idempotency_key)
        if existing:
            return existing
        keyword_list = list(keywords)
        matched_list = list(matched_articles)
        payload = {
            "topic": topic,
            "source": source,
            "requirements": requirements,
            "keywords": keyword_list,
            "target_article_count": target_article_count,
            "matched_articles": matched_list,
            "mode": "batch_production",
            "stage": "batch-config",
            "batch_state": {
                "name": topic,
                "source": source,
                "brief": str(requirements.get("brief") or ""),
                "output_dir": str(requirements.get("output_dir") or ""),
                "publish_handoff": False,
                "stage": "batch-config",
                "keywords": [
                    {
                        "id": f"kw-{index}",
                        "keyword": keyword,
                        "purpose": "",
                        "count": 1,
                        "recommendedCount": 1,
                        "signal": "medium",
                        "signalReason": "Hub 任务输入",
                        "hookId": "hook-plan",
                        "motherMatches": [],
                        "readiness": "needs-mother",
                    }
                    for index, keyword in enumerate(keyword_list)
                ],
                "queue": [],
            },
        }
        job_id = self._jobs.create(
            job_type="batch_production",
            payload=payload,
        )
        self._create_runtime_batch(
            job_id,
            topic=topic,
            source=source,
            requirements=requirements,
            state=payload["batch_state"],
            operator=operator,
        )
        self._record_write_receipt(
            operation="create_batch",
            job_id=job_id,
            operator=operator,
            source_ref=f"writing/job/{job_id}",
            manifest={"runtime_tables": ["wm_batches", "wm_batch_keywords", "wm_batch_queue_items"]},
        )
        self._audit.record(
            action="writing.create",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details=self._safe_provider_details(mode="batch_production"),
        )
        self._record_command(
            idempotency_key=idempotency_key,
            command_type="create_batch",
            actor=operator,
            status="succeeded",
            input_data={"topic": topic, "source": source, "requirements": requirements, "keywords": keyword_list, "target_article_count": target_article_count},
            output_data={"job_id": job_id},
        )
        return WritingJob(
            job_id=job_id,
            job_type="batch_production",
            topic=topic,
            status="queued",
            payload=payload,
        )

    def run(self, job_id: str, *, operator: str = "user", idempotency_key: str = "") -> dict[str, Any]:
        job = self._fetch(job_id)
        if not job:
            raise FileNotFoundError(f"未找到生产任务：{job_id}")
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._jobs.claim(job_id, operator):
            result = {"status": "skipped", "reason": "任务已被认领或不在 queued 状态", "job_id": job_id}
            self._record_command(
                idempotency_key=idempotency_key,
                command_type="run",
                actor=operator,
                status="succeeded",
                input_data={"job_id": job_id},
                output_data=result,
            )
            return result
        if self._provider is None:
            details = self._safe_provider_details(
                mode=job.job_type,
                reason_code="writing.provider_unconfigured",
            )
            self._jobs.complete(job_id, status="blocked")
            self._sync_runtime_status(job_id, "blocked")
            self._record_write_receipt(
                operation="run_blocked",
                job_id=job_id,
                operator=operator,
                source_ref=f"writing/job/{job_id}",
                manifest={
                    "runtime_tables": ["wm_projects", "wm_batches"],
                    "reason_code": "writing.provider_unconfigured",
                },
                hub_status="blocked",
                reconcile_status="blocked",
            )
            self._record_event(job_id, "blocked", details)
            self._audit.record(
                action="writing.run",
                subject_type="writing_job",
                subject_id=job_id,
                actor_id=operator,
                outcome="blocked",
                details=details,
            )
            result = {
                "status": "blocked",
                "job_id": job_id,
                "blocked": True,
                "demo": False,
                **details,
            }
            self._record_command(
                idempotency_key=idempotency_key,
                command_type="run",
                actor=operator,
                status="blocked",
                input_data={"job_id": job_id},
                output_data=result,
            )
            return result
        try:
            if job.job_type == "mother_forge":
                written = self._run_mother_forge(job)
            elif job.job_type == "batch_production":
                written = self._run_batch(job)
            else:
                raise ValueError(f"不支持的 job_type={job.job_type}")
            self._update_payload(
                job.job_id,
                {
                    "stage": "done",
                    "outputs": written.get("outputs") or [written.get("asset_ref")] if written else [],
                    "output_content_id": written.get("content_id", ""),
                },
                event_type="writing.output_persisted",
                operator=operator,
            )
            self._jobs.complete(job_id, output_content_id=written.get("content_id", ""), status="blocked")
            self._sync_runtime_status(job_id, "completed")
            self._record_write_receipt(
                operation="run_demo_only",
                job_id=job_id,
                operator=operator,
                source_ref=f"writing/job/{job_id}",
                manifest={
                    "runtime_tables": ["wm_drafts", "wm_projects", "wm_batches"],
                    "outputs": written.get("outputs") or [written.get("asset_ref")],
                },
                hub_status="blocked",
                reconcile_status="blocked",
            )
            details = self._safe_provider_details(
                mode=job.job_type,
                reason_code="writing.demo_provider",
            )
            details["demo"] = True
            self._record_event(job_id, "demo_only", details)
            self._audit.record(
                action="writing.run",
                subject_type="writing_job",
                subject_id=job_id,
                actor_id=operator,
                outcome="blocked",
                details=details,
            )
            result = {"status": "demo_only", "job_id": job_id, "blocked": False, **details, **written}
            self._record_command(
                idempotency_key=idempotency_key,
                command_type="run",
                actor=operator,
                status="blocked",
                input_data={"job_id": job_id},
                output_data=result,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            self._jobs.complete(job_id, status="failed")
            self._sync_runtime_status(job_id, "failed")
            self._record_write_receipt(
                operation="run_failed",
                job_id=job_id,
                operator=operator,
                source_ref=f"writing/job/{job_id}",
                manifest={
                    "runtime_tables": ["wm_projects", "wm_batches"],
                    "error_type": exc.__class__.__name__,
                },
                hub_status="failed",
                reconcile_status="blocked",
            )
            details = self._safe_provider_details(
                mode=job.job_type,
                reason_code="writing.provider_error",
            )
            self._record_event(job_id, "failed", details)
            self._audit.record(
                action="writing.run",
                subject_type="writing_job",
                subject_id=job_id,
                actor_id=operator,
                outcome="failed",
                details=details,
            )
            result = {"status": "failed", "job_id": job_id, **details}
            self._record_command(
                idempotency_key=idempotency_key,
                command_type="run",
                actor=operator,
                status="failed",
                input_data={"job_id": job_id},
                output_data=result,
            )
            return result

    def _run_mother_forge(self, job: WritingJob) -> dict[str, Any]:
        body = self._provider.generate(
            prompt=job.payload.get("topic", ""),
            hook="母文章铸造",
            mother_articles=job.payload.get("recommended_mothers", []),
            keywords=[],
        )
        title = job.topic or "未命名母文章"
        result = self._markdown.write(
            bucket="generated",
            content_id=_deterministic_id("mother", title, job.job_id),
            content_type="demo_mother_article",
            title=title,
            author="WritingMoney",
            body=body,
            published_at=utc_now_iso(),
            extra_frontmatter={"mode": "mother_forge", "purpose": job.payload.get("purpose", ""), "demo_only": True},
        )
        asset_ref = self._asset_ref(result.md_path)
        self._record_content(
            asset_ref,
            title,
            "demo_mother_article",
            result.file_hash,
            result.content_hash,
        )
        content_id = self._last_content_id(asset_ref)
        self._record_runtime_draft(
            job,
            title=title,
            artifact_ref=asset_ref,
            content_id=content_id,
            status="draft",
        )
        self._record_event(job.job_id, "mother_forge.written", {"asset_ref": asset_ref, "content_id": content_id})
        return {
            "asset_ref": asset_ref,
            "md_path": asset_ref,
            "content_id": content_id,
            "demo_only": True,
        }

    def _run_batch(self, job: WritingJob) -> dict[str, Any]:
        outputs: list[str] = []
        keywords = job.payload.get("keywords") or []
        mothers = job.payload.get("matched_articles") or []
        state = self._batch_state(job.payload)
        queue = list(state.get("queue") or [])
        planned = len(queue) or sum(
            max(0, int(item.get("count") or 0))
            for item in state.get("keywords", [])
            if isinstance(item, dict)
        )
        # 批量页的每个关键词行都带有明确的 count；不能让创建任务时的
        # target_article_count 默认值 1 覆盖页面上已经确认的 0..N 计划，
        # 否则会出现“计划 2 篇但只写出 1 篇”的假完成队列。
        target = max(1, planned or int(job.payload.get("target_article_count") or 0) or len(keywords) or 1)
        for index in range(target):
            keyword = keywords[index % len(keywords)] if keywords else f"{job.topic}-{index + 1}"
            prompt = f"{job.topic} - {keyword}"
            body = self._provider.generate(
                prompt=prompt,
                hook=keyword,
                mother_articles=mothers,
                keywords=[keyword],
            )
            result = self._markdown.write(
                bucket="generated",
                content_id=_deterministic_id("batch", prompt, job.job_id, str(index)),
                content_type="demo_generated_article",
                title=prompt,
                author="WritingMoney",
                body=body,
                published_at=utc_now_iso(),
                extra_frontmatter={"mode": "batch", "keyword": keyword, "demo_only": True},
            )
            asset_ref = self._asset_ref(result.md_path)
            outputs.append(asset_ref)
            self._record_content(
                asset_ref,
                prompt,
                "demo_generated_article",
                result.file_hash,
                result.content_hash,
            )
            self._record_event(
                job.job_id,
                f"batch.article.{index + 1}",
                {"asset_ref": asset_ref, "content_id": self._last_content_id(asset_ref)},
            )
            self._record_runtime_draft(
                job,
                title=prompt,
                artifact_ref=asset_ref,
                content_id=self._last_content_id(asset_ref),
                status="draft",
                ordinal=index,
            )
        if not queue:
            for index, keyword in enumerate(keywords or [job.topic]):
                queue.append(
                    {
                        "id": f"{job.job_id}-kw-{index}",
                        "keywordId": f"kw-{index}",
                        "title": f"【{keyword}】第1篇",
                        "status": "done",
                        "outputFile": outputs[index] if index < len(outputs) else "",
                    }
                )
        else:
            for index, item in enumerate(queue):
                item["status"] = "done"
                if index < len(outputs):
                    item["outputFile"] = outputs[index]
        state["queue"] = queue
        state["stage"] = "batch-done"
        state["status"] = "done"
        updated_payload = dict(job.payload)
        updated_payload.update(
            {
                "batch_state": state,
                "keywords": [item.get("keyword", "") for item in state.get("keywords", [])],
            }
        )
        self._update_payload(
            job.job_id,
            {
                "batch_state": state,
                "keywords": [item.get("keyword", "") for item in state.get("keywords", [])],
            },
            event_type="writing.batch_queue_completed",
            operator="system",
        )
        self._sync_runtime_from_payload(
            job,
            updated_payload,
            operator="system",
            event_type="writing.batch_queue_completed",
        )
        return {"outputs": outputs, "count": len(outputs), "demo_only": True}

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._runtime_job_rows(limit=limit)
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = self._runtime_payload(row["job_id"], row.get("job_type"))
            items.append(
                {
                    "job_id": row["job_id"],
                    "job_type": row["job_type"],
                    "status": row["status"],
                    "provider_kind": self._provider_kind,
                    "provider_status": self._provider_status,
                    "demo": self._provider is not None,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "scheduled_at": row["scheduled_at"],
                    "payload": payload,
                }
            )
        return items

    def detail(self, job_id: str) -> dict[str, Any] | None:
        runtime = self._runtime_job_row(job_id)
        legacy = self._jobs.detail(job_id)
        if not runtime and not legacy:
            return None
        if not runtime:
            return scrub_public_payload(legacy, asset_root=self._markdown.root)
        if not legacy:
            legacy = {"job_id": job_id, "events": [], "payload": {}}
        detail = dict(legacy)
        detail["job_type"] = runtime["job_type"]
        detail["status"] = runtime["status"]
        detail["payload"] = self._runtime_payload(job_id, runtime["job_type"])
        detail["wm_project_id"] = runtime.get("wm_project_id")
        detail["wm_batch_id"] = runtime.get("wm_batch_id")
        return scrub_public_payload(detail, asset_root=self._markdown.root)

    def mutate(
        self,
        job_id: str,
        *,
        action: str,
        value: dict[str, Any],
        operator: str = "user",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """持久化旧 WritingMoney 页面上的局部写操作。

        该接口不调用任何真实 Provider，也不把“收到 URL”包装成“已抓取”。
        解析器未配置时，URL 只进入 received 状态并明确显示等待解析。
        """
        job = self._fetch(job_id)
        if not job:
            raise FileNotFoundError(f"未找到生产任务：{job_id}")
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        payload = dict(job.payload)
        if action == "purpose":
            purpose = str(value.get("purpose") or "").strip()
            if not purpose:
                raise ValueError("写作目的不能为空")
            payload["purpose"] = purpose
        elif action == "add_url":
            raw_url = str(value.get("url") or "").strip()
            parsed = urlparse(raw_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("URL 格式不合法")
            materials = list(payload.get("url_materials") or [])
            material_id = f"url_{generate_ulid_like('mat')}"
            note = str(value.get("note") or "").strip()
            materials.append(
                {
                    "id": material_id,
                    "type": "url",
                    "title": note[:80] or "URL 临时素材",
                    "url": raw_url,
                    "path": f"Hub 临时素材/{job_id}/{material_id}",
                    "reason": "URL 已收录；当前未配置正文/OCR 解析器，未将其误报为已解析。",
                    "points": [],
                    "visuals": [],
                    "usage": "reference",
                    "parseStatus": "received",
                    "addedAt": utc_now_iso(),
                }
            )
            payload["url_materials"] = materials
        elif action == "material_usage":
            material_id = str(value.get("material_id") or "")
            usage = str(value.get("usage") or "")
            if usage not in {"must", "reference", "skip"}:
                raise ValueError("素材使用状态不合法")
            if not self._set_material_usage(payload, material_id, usage):
                raise ValueError("素材不存在")
        elif action == "template_select":
            template_id = str(value.get("template_id") or "")
            templates = list(payload.get("templates") or [])
            found = False
            for template in templates:
                if isinstance(template, dict):
                    template["selected"] = str(template.get("id") or "") == template_id
                    found = found or template["selected"]
            if templates and not found:
                raise ValueError("模板不存在")
            payload["selected_template_id"] = template_id
            payload["templates"] = templates
        elif action in {"stage", "confirm_decision", "confirm_plan"}:
            stage = str(value.get("stage") or ("package" if action != "stage" else "decision"))
            if stage not in {"decision", "package", "done", "batch-config", "batch-done"}:
                raise ValueError("阶段不合法")
            payload["stage"] = stage
            if action != "stage":
                payload["decision_confirmed_at"] = utc_now_iso()
        elif action == "batch_state":
            if job.job_type != "batch_production":
                raise ValueError("只有批量成稿任务支持批次状态")
            state = value.get("state")
            if not isinstance(state, dict):
                raise ValueError("批次状态必须是对象")
            payload["batch_state"] = self._safe_batch_state(state)
            payload["keywords"] = [
                str(item.get("keyword") or "")
                for item in payload["batch_state"].get("keywords", [])
                if isinstance(item, dict)
            ]
            payload["output_dir"] = str(payload["batch_state"].get("output_dir") or "")
            payload["stage"] = str(payload["batch_state"].get("stage") or "batch-config")
        elif action == "batch_keyword_edit":
            if job.job_type != "batch_production":
                raise ValueError("只有批量成稿任务支持关键词编辑")
            state = self._batch_state(payload)
            keyword_id = str(value.get("keyword_id") or "")
            keyword = str(value.get("keyword") or "").strip()
            purpose = str(value.get("purpose") or "").strip()
            if not keyword:
                raise ValueError("关键词不能为空")
            found = False
            for item in state.get("keywords", []):
                if isinstance(item, dict) and str(item.get("id")) == keyword_id:
                    item["keyword"] = keyword[:200]
                    item["purpose"] = purpose[:4000]
                    item["motherMatches"] = []
                    item["readiness"] = "needs-mother"
                    found = True
            if not found:
                raise ValueError("关键词不存在")
            state["stage"] = "batch-config"
            state["queue"] = []
            payload["batch_state"] = self._safe_batch_state(state)
            payload["keywords"] = [str(item.get("keyword") or "") for item in state["keywords"]]
            payload["stage"] = "batch-config"
        elif action == "batch_mother_edit":
            if job.job_type != "batch_production":
                raise ValueError("只有批量成稿任务支持母文章匹配")
            state = self._batch_state(payload)
            keyword_id = str(value.get("keyword_id") or "")
            mother_ids = [str(item) for item in (value.get("mother_ids") or [])]
            found = False
            for item in state.get("keywords", []):
                if isinstance(item, dict) and str(item.get("id")) == keyword_id:
                    item["motherMatches"] = [
                        {"motherId": mother_id, "role": "手动选择", "confidence": 0.8}
                        for mother_id in mother_ids
                    ]
                    item["readiness"] = "ready" if mother_ids else "needs-mother"
                    found = True
            if not found:
                raise ValueError("关键词不存在")
            state["stage"] = "batch-config"
            state["queue"] = []
            payload["batch_state"] = self._safe_batch_state(state)
            payload["stage"] = "batch-config"
        elif action == "batch_confirm_queue":
            if job.job_type != "batch_production":
                raise ValueError("只有批量成稿任务支持成稿队列")
            state = self._batch_state(payload)
            queue: list[dict[str, Any]] = []
            for keyword in state.get("keywords", []):
                if not isinstance(keyword, dict):
                    continue
                count = max(0, int(keyword.get("count") or 0))
                if keyword.get("readiness") != "ready" or count <= 0:
                    continue
                for index in range(count):
                    queue.append(
                        {
                            "id": f"{keyword.get('id')}-{index}",
                            "keywordId": keyword.get("id"),
                            "title": f"【{keyword.get('keyword') or '未命名关键词'}】第{index + 1}篇",
                            "status": "waiting",
                            "outputFile": "",
                        }
                    )
            state["queue"] = queue
            state["stage"] = "batch-done"
            payload["batch_state"] = self._safe_batch_state(state)
            payload["stage"] = "batch-done"
        elif action == "batch_queue_item":
            state = self._batch_state(payload)
            item_id = str(value.get("item_id") or "")
            status = str(value.get("status") or "")
            if status not in {"waiting", "running", "done", "rework"}:
                raise ValueError("队列状态不合法")
            matched = False
            for item in state.get("queue", []):
                if isinstance(item, dict) and str(item.get("id")) == item_id:
                    item["status"] = status
                    matched = True
            if not matched:
                raise ValueError("队列项不存在")
            payload["batch_state"] = self._safe_batch_state(state)
        elif action == "publish_handoff":
            state = self._batch_state(payload)
            state["publish_handoff"] = bool(value.get("enabled", True))
            payload["batch_state"] = self._safe_batch_state(state)
        else:
            raise ValueError(f"不支持的写作变更：{action}")

        self._update_payload(job_id, payload, event_type=f"writing.{action}", operator=operator)
        self._sync_runtime_from_payload(job, payload, operator=operator, event_type=f"writing.{action}")
        self._record_write_receipt(
            operation=f"mutate_{action}",
            job_id=job_id,
            operator=operator,
            source_ref=f"writing/job/{job_id}/{action}",
            manifest={
                "runtime_tables": (
                    ["wm_projects", "wm_materials", "wm_project_materials"]
                    if job.job_type == "mother_forge"
                    else ["wm_batches", "wm_batch_keywords", "wm_batch_mother_links", "wm_batch_queue_items"]
                ),
                "action": action,
            },
        )
        self._audit.record(
            action=f"writing.{action}",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details={"job_type": job.job_type, "payload_keys": sorted(payload.keys())},
        )
        result = self.detail(job_id) or {}
        self._record_command(
            idempotency_key=idempotency_key,
            command_type=f"mutate_{action}",
            actor=operator,
            status="succeeded",
            input_data={"job_id": job_id, "action": action, "value": value},
            output_data=result,
        )
        return result

    def _update_payload(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        event_type: str,
        operator: str,
    ) -> dict[str, Any]:
        job = self._fetch(job_id)
        if not job:
            raise FileNotFoundError(f"未找到生产任务：{job_id}")
        payload = dict(job.payload)
        payload.update(patch)
        return self._jobs.update_payload(
            job_id,
            payload,
            event_type=event_type,
            actor=operator,
        )

    def _record_command(
        self,
        *,
        idempotency_key: str,
        command_type: str,
        actor: str,
        status: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
    ) -> None:
        """记录用户命令；没有 key 的历史调用仍可运行，但不会伪造幂等键。"""
        key = (idempotency_key or "").strip()[:200]
        if not key:
            return
        now = utc_now_iso()
        command_id = _runtime_deterministic_id("cmd", "writing-user", key)
        self._conn.execute(
            """
            INSERT INTO command_runs(
                command_id, module_key, command_type, idempotency_key, actor_id,
                status, input_json, output_json, created_at, updated_at
            ) VALUES (?, 'writing', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module_key, idempotency_key) DO UPDATE SET
                status=excluded.status, output_json=excluded.output_json,
                updated_at=excluded.updated_at
            """,
            (
                command_id,
                command_type,
                key,
                actor,
                status,
                json.dumps(input_data, ensure_ascii=False, sort_keys=True),
                json.dumps(output_data, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )

    def _idempotent_output(self, idempotency_key: str) -> dict[str, Any] | None:
        key = (idempotency_key or "").strip()[:200]
        if not key:
            return None
        row = self._conn.execute(
            "SELECT output_json FROM command_runs WHERE module_key='writing' AND idempotency_key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["output_json"] or "{}")
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) and value else None

    def _idempotent_job(self, idempotency_key: str) -> WritingJob | None:
        output = self._idempotent_output(idempotency_key)
        job_id = str(output.get("job_id") or "") if output else ""
        return self._fetch(job_id) if job_id else None

    def list_projects(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT wm_project_id, title, purpose, stage, status, workspace_ref,
                   created_by, created_at, updated_at, legacy_job_id
            FROM wm_projects ORDER BY updated_at DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM wm_projects WHERE wm_project_id=?", (project_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["materials"] = self._runtime_materials(project_id)
        result["events"] = [
            dict(event)
            for event in self._conn.execute(
                "SELECT * FROM wm_project_events WHERE wm_project_id=? ORDER BY occurred_at",
                (project_id,),
            ).fetchall()
        ]
        return result

    def update_project(
        self, project_id: str, *, patch: dict[str, Any], operator: str, idempotency_key: str = ""
    ) -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        row = self._conn.execute("SELECT * FROM wm_projects WHERE wm_project_id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        title = str(patch.get("title", row["title"])).strip()[:300]
        purpose = str(patch.get("purpose", row["purpose"])).strip()[:4000]
        stage = str(patch.get("stage", row["stage"]))
        if not title:
            raise ValueError("项目标题不能为空")
        if stage not in {"decision", "materials", "template", "plan", "package", "draft", "completed", "archived"}:
            raise ValueError("项目阶段不合法")
        now = utc_now_iso()
        self._conn.execute(
            "UPDATE wm_projects SET title=?, purpose=?, stage=?, updated_at=? WHERE wm_project_id=?",
            (title, purpose, stage, now, project_id),
        )
        self._runtime_event(project_id, "project.updated", operator, {"fields": sorted(patch)})
        result = self.project(project_id) or {}
        self._audit.record(action="writing.project.update", subject_type="wm_project", subject_id=project_id, actor_id=operator, details={"fields": sorted(patch)})
        self._record_command(idempotency_key=idempotency_key, command_type="project_update", actor=operator, status="succeeded", input_data={"project_id": project_id, "patch": patch}, output_data=result)
        return result

    def delete_project(self, project_id: str, *, operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._conn.execute("SELECT 1 FROM wm_projects WHERE wm_project_id=?", (project_id,)).fetchone():
            raise KeyError(project_id)
        self._conn.execute("UPDATE wm_projects SET status='archived', stage='archived', updated_at=? WHERE wm_project_id=?", (utc_now_iso(), project_id))
        self._runtime_event(project_id, "project.archived", operator, {})
        result = {"wm_project_id": project_id, "status": "archived"}
        self._audit.record(action="writing.project.delete", subject_type="wm_project", subject_id=project_id, actor_id=operator, details=result)
        self._record_command(idempotency_key=idempotency_key, command_type="project_delete", actor=operator, status="succeeded", input_data={"project_id": project_id}, output_data=result)
        return result

    def list_batches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM wm_batches ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [self._batch_public(row) for row in rows]

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM wm_batches WHERE wm_batch_id=?", (batch_id,)).fetchone()
        return self._batch_public(row) if row else None

    def update_batch(self, batch_id: str, *, patch: dict[str, Any], operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        row = self._conn.execute("SELECT * FROM wm_batches WHERE wm_batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError(batch_id)
        title = str(patch.get("title", row["title"])).strip()[:300]
        source = str(patch.get("source", row["source"])).strip()[:120]
        requirements = patch.get("requirements")
        if requirements is None:
            requirements = json.loads(row["requirements_json"] or "{}")
        if not isinstance(requirements, dict):
            raise ValueError("requirements 必须是对象")
        self._conn.execute(
            "UPDATE wm_batches SET title=?, source=?, requirements_json=?, updated_at=? WHERE wm_batch_id=?",
            (title, source, json.dumps(requirements, ensure_ascii=False, sort_keys=True), utc_now_iso(), batch_id),
        )
        result = self.batch(batch_id) or {}
        self._audit.record(action="writing.batch.update", subject_type="wm_batch", subject_id=batch_id, actor_id=operator, details={"fields": sorted(patch)})
        self._record_command(idempotency_key=idempotency_key, command_type="batch_update", actor=operator, status="succeeded", input_data={"batch_id": batch_id, "patch": patch}, output_data=result)
        return result

    def delete_batch(self, batch_id: str, *, operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._conn.execute("SELECT 1 FROM wm_batches WHERE wm_batch_id=?", (batch_id,)).fetchone():
            raise KeyError(batch_id)
        self._conn.execute("UPDATE wm_batches SET status='archived', updated_at=? WHERE wm_batch_id=?", (utc_now_iso(), batch_id))
        result = {"wm_batch_id": batch_id, "status": "archived"}
        self._audit.record(action="writing.batch.delete", subject_type="wm_batch", subject_id=batch_id, actor_id=operator, details=result)
        self._record_command(idempotency_key=idempotency_key, command_type="batch_delete", actor=operator, status="succeeded", input_data={"batch_id": batch_id}, output_data=result)
        return result

    def list_keywords(self, batch_id: str) -> list[dict[str, Any]]:
        return self._runtime_keywords(batch_id, include_ids=True)

    def create_keyword(self, batch_id: str, *, data: dict[str, Any], operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._conn.execute("SELECT 1 FROM wm_batches WHERE wm_batch_id=?", (batch_id,)).fetchone():
            raise KeyError(batch_id)
        keyword = str(data.get("keyword") or "").strip()[:200]
        if not keyword:
            raise ValueError("关键词不能为空")
        count = max(1, min(100, int(data.get("target_article_count", data.get("count", 1)))))
        readiness = str(data.get("readiness_status") or "pending")
        if readiness not in {"pending", "ready", "blocked"}:
            raise ValueError("关键词就绪状态不合法")
        keyword_id = generate_ulid_like("wmk")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO wm_batch_keywords(
                wm_batch_keyword_id, wm_batch_id, keyword_text, purpose,
                target_article_count, readiness_status, ordinal, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                keyword_id, batch_id, keyword, str(data.get("purpose") or "")[:4000],
                count, readiness,
                int(data.get("ordinal") or 0),
                json.dumps({"created_via": "writing-api"}, ensure_ascii=False),
            ),
        )
        self._conn.execute("UPDATE wm_batches SET updated_at=? WHERE wm_batch_id=?", (now, batch_id))
        output = dict(self._conn.execute("SELECT * FROM wm_batch_keywords WHERE wm_batch_keyword_id=?", (keyword_id,)).fetchone())
        self._audit.record(action="writing.keyword.create", subject_type="wm_batch_keyword", subject_id=keyword_id, actor_id=operator, details={"batch_id": batch_id})
        self._record_command(idempotency_key=idempotency_key, command_type="keyword_create", actor=operator, status="succeeded", input_data={"batch_id": batch_id, "data": data}, output_data=output)
        return output

    def update_keyword(self, keyword_id: str, *, patch: dict[str, Any], operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        row = self._conn.execute("SELECT * FROM wm_batch_keywords WHERE wm_batch_keyword_id=?", (keyword_id,)).fetchone()
        if not row:
            raise KeyError(keyword_id)
        keyword = str(patch.get("keyword", row["keyword_text"])).strip()[:200]
        purpose = str(patch.get("purpose", row["purpose"])).strip()[:4000]
        count = max(1, min(100, int(patch.get("target_article_count", row["target_article_count"]))))
        readiness = str(patch.get("readiness_status", row["readiness_status"]))
        if not keyword or readiness not in {"pending", "ready", "blocked"}:
            raise ValueError("关键词或就绪状态不合法")
        self._conn.execute(
            "UPDATE wm_batch_keywords SET keyword_text=?, purpose=?, target_article_count=?, readiness_status=? WHERE wm_batch_keyword_id=?",
            (keyword, purpose, count, readiness, keyword_id),
        )
        result = self._conn.execute("SELECT * FROM wm_batch_keywords WHERE wm_batch_keyword_id=?", (keyword_id,)).fetchone()
        output = dict(result)
        self._audit.record(action="writing.keyword.update", subject_type="wm_batch_keyword", subject_id=keyword_id, actor_id=operator, details={"fields": sorted(patch)})
        self._record_command(idempotency_key=idempotency_key, command_type="keyword_update", actor=operator, status="succeeded", input_data={"keyword_id": keyword_id, "patch": patch}, output_data=output)
        return output

    def delete_keyword(self, keyword_id: str, *, operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._conn.execute("SELECT 1 FROM wm_batch_keywords WHERE wm_batch_keyword_id=?", (keyword_id,)).fetchone():
            raise KeyError(keyword_id)
        self._conn.execute("DELETE FROM wm_batch_keywords WHERE wm_batch_keyword_id=?", (keyword_id,))
        result = {"wm_batch_keyword_id": keyword_id, "deleted": True}
        self._audit.record(action="writing.keyword.delete", subject_type="wm_batch_keyword", subject_id=keyword_id, actor_id=operator, details=result)
        self._record_command(idempotency_key=idempotency_key, command_type="keyword_delete", actor=operator, status="succeeded", input_data={"keyword_id": keyword_id}, output_data=result)
        return result

    def list_drafts(self, *, project_id: str = "", batch_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses, args = [], []
        if project_id:
            clauses.append("wm_project_id=?"); args.append(project_id)
        if batch_id:
            clauses.append("wm_batch_id=?"); args.append(batch_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM wm_drafts {where} ORDER BY updated_at DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
        return [dict(row) for row in rows]

    def create_draft(self, *, data: dict[str, Any], operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        project_id = str(data.get("wm_project_id") or "") or None
        batch_id = str(data.get("wm_batch_id") or "") or None
        keyword_id = str(data.get("wm_batch_keyword_id") or "") or None
        if not project_id and not batch_id:
            raise ValueError("草稿必须关联项目或批次")
        if project_id and not self._conn.execute("SELECT 1 FROM wm_projects WHERE wm_project_id=?", (project_id,)).fetchone():
            raise KeyError(project_id)
        if batch_id and not self._conn.execute("SELECT 1 FROM wm_batches WHERE wm_batch_id=?", (batch_id,)).fetchone():
            raise KeyError(batch_id)
        draft_id = generate_ulid_like("wmd")
        now = utc_now_iso()
        title = str(data.get("title") or "未命名草稿").strip()[:400]
        status = str(data.get("status") or "queued")
        if status not in {"queued", "draft", "review", "rework"}:
            raise ValueError("新草稿状态不合法")
        self._conn.execute(
            """
            INSERT INTO wm_drafts(
                wm_draft_id, wm_project_id, wm_batch_id, wm_batch_keyword_id,
                status, title, input_hash, provider_kind, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id, project_id, batch_id, keyword_id, status, title,
                hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                self._provider_kind, now, now,
            ),
        )
        output = self.draft(draft_id) or {}
        self._audit.record(action="writing.draft.create", subject_type="wm_draft", subject_id=draft_id, actor_id=operator, details={"project_id": project_id, "batch_id": batch_id})
        self._record_command(idempotency_key=idempotency_key, command_type="draft_create", actor=operator, status="succeeded", input_data=data, output_data=output)
        return output

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM wm_drafts WHERE wm_draft_id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def update_draft(self, draft_id: str, *, patch: dict[str, Any], operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        row = self._conn.execute("SELECT * FROM wm_drafts WHERE wm_draft_id=?", (draft_id,)).fetchone()
        if not row:
            raise KeyError(draft_id)
        status = str(patch.get("status", row["status"]))
        if status not in {"queued", "running", "draft", "review", "rework", "ready_for_publish", "published", "failed", "cancelled"}:
            raise ValueError("草稿状态不合法")
        title = str(patch.get("title", row["title"] or "")).strip()[:400]
        self._conn.execute("UPDATE wm_drafts SET status=?, title=?, updated_at=? WHERE wm_draft_id=?", (status, title, utc_now_iso(), draft_id))
        output = self.draft(draft_id) or {}
        self._audit.record(action="writing.draft.update", subject_type="wm_draft", subject_id=draft_id, actor_id=operator, details={"fields": sorted(patch)})
        self._record_command(idempotency_key=idempotency_key, command_type="draft_update", actor=operator, status="succeeded", input_data={"draft_id": draft_id, "patch": patch}, output_data=output)
        return output

    def delete_draft(self, draft_id: str, *, operator: str, idempotency_key: str = "") -> dict[str, Any]:
        prior = self._idempotent_output(idempotency_key)
        if prior is not None:
            return prior
        if not self._conn.execute("SELECT 1 FROM wm_drafts WHERE wm_draft_id=?", (draft_id,)).fetchone():
            raise KeyError(draft_id)
        self._conn.execute("UPDATE wm_drafts SET status='cancelled', updated_at=? WHERE wm_draft_id=?", (utc_now_iso(), draft_id))
        output = {"wm_draft_id": draft_id, "status": "cancelled"}
        self._audit.record(action="writing.draft.delete", subject_type="wm_draft", subject_id=draft_id, actor_id=operator, details=output)
        self._record_command(idempotency_key=idempotency_key, command_type="draft_delete", actor=operator, status="succeeded", input_data={"draft_id": draft_id}, output_data=output)
        return output

    def _batch_public(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if not row:
            return {}
        result = dict(row)
        result["requirements"] = json.loads(result.pop("requirements_json") or "{}")
        result["keywords"] = self.list_keywords(result["wm_batch_id"])
        result["drafts"] = self.list_drafts(batch_id=result["wm_batch_id"])
        result["queue"] = [dict(item) for item in self._conn.execute(
            "SELECT * FROM wm_batch_queue_items WHERE wm_batch_id=? ORDER BY ordinal",
            (result["wm_batch_id"],),
        ).fetchall()]
        return result

    def _create_runtime_project(
        self,
        job_id: str,
        *,
        topic: str,
        purpose: str,
        operator: str,
    ) -> str:
        project_id = generate_ulid_like("wmp")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO wm_projects(
                wm_project_id, title, purpose, stage, status, workspace_ref,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'decision', 'active', ?, ?, ?, ?)
            """,
            (
                project_id,
                topic[:300] or "未命名母文章",
                purpose[:4000],
                f"writing/projects/{project_id}",
                operator,
                now,
                now,
            ),
        )
        self._conn.execute(
            "UPDATE production_jobs SET wm_project_id=? WHERE job_id=?",
            (project_id, job_id),
        )
        self._conn.execute(
            "UPDATE wm_projects SET legacy_job_id=? WHERE wm_project_id=?",
            (job_id, project_id),
        )
        self._runtime_event(project_id, "project.created", operator, {"job_id": job_id})
        return project_id

    def _create_runtime_batch(
        self,
        job_id: str,
        *,
        topic: str,
        source: str,
        requirements: dict[str, Any],
        state: dict[str, Any],
        operator: str,
    ) -> str:
        batch_id = generate_ulid_like("wmb")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO wm_batches(
                wm_batch_id, title, source, status, requirements_json, workspace_ref,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                topic[:300] or "未命名批次",
                source[:120] or "manual",
                json.dumps(requirements, ensure_ascii=False, sort_keys=True),
                f"writing/batches/{batch_id}",
                operator,
                now,
                now,
            ),
        )
        self._conn.execute(
            "UPDATE production_jobs SET wm_batch_id=? WHERE job_id=?",
            (batch_id, job_id),
        )
        self._conn.execute(
            "UPDATE wm_batches SET legacy_job_id=? WHERE wm_batch_id=?",
            (job_id, batch_id),
        )
        self._sync_batch_keywords(batch_id, state)
        self._sync_batch_queue(batch_id, state)
        return batch_id

    def _runtime_ids(self, job_id: str) -> tuple[str | None, str | None]:
        row = self._conn.execute(
            """
            SELECT
                (SELECT wm_project_id FROM wm_projects WHERE legacy_job_id=? LIMIT 1) AS wm_project_id,
                (SELECT wm_batch_id FROM wm_batches WHERE legacy_job_id=? LIMIT 1) AS wm_batch_id
            """,
            (job_id, job_id),
        ).fetchone()
        if row and (row["wm_project_id"] or row["wm_batch_id"]):
            return row["wm_project_id"], row["wm_batch_id"]
        row = self._conn.execute(
            "SELECT wm_project_id, wm_batch_id FROM production_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            return None, None
        return row["wm_project_id"], row["wm_batch_id"]

    def _runtime_event(
        self,
        project_id: str,
        event_type: str,
        actor: str,
        details: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO wm_project_events(
                wm_project_event_id, wm_project_id, event_type, actor_id, details_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                generate_ulid_like("wme"),
                project_id,
                event_type,
                actor,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
            ),
        )

    def _sync_runtime_from_payload(
        self,
        job: WritingJob,
        payload: dict[str, Any],
        *,
        operator: str,
        event_type: str,
    ) -> None:
        """把旧 UI 兼容快照投影到 v3.3 运行表，领域查询不再依赖 payload_json。"""
        project_id, batch_id = self._runtime_ids(job.job_id)
        now = utc_now_iso()
        if project_id:
            stage = str(payload.get("stage") or "decision")
            normalized_stage = stage if stage in {
                "decision", "materials", "template", "plan", "package", "draft", "completed", "archived"
            } else "decision"
            self._conn.execute(
                """
                UPDATE wm_projects SET title=?, purpose=?, stage=?, updated_at=? WHERE wm_project_id=?
                """,
                (
                    str(payload.get("topic") or job.topic)[:300],
                    str(payload.get("purpose") or "")[:4000],
                    normalized_stage,
                    now,
                    project_id,
                ),
            )
            self._sync_project_materials(project_id, payload, operator)
            self._runtime_event(project_id, event_type, operator, {"job_id": job.job_id})
        if batch_id:
            state = self._batch_state(payload)
            status = "ready" if state.get("queue") else "draft"
            self._conn.execute(
                """
                UPDATE wm_batches
                SET title=?, source=?, status=?, requirements_json=?, updated_at=?
                WHERE wm_batch_id=?
                """,
                (
                    str(state.get("name") or job.topic)[:300],
                    str(state.get("source") or "manual")[:120],
                    status,
                    json.dumps({"brief": str(state.get("brief") or "")[:4000]}, ensure_ascii=False),
                    now,
                    batch_id,
                ),
            )
            self._sync_batch_keywords(batch_id, state)
            self._sync_batch_queue(batch_id, state)

    def _sync_project_materials(
        self,
        project_id: str,
        payload: dict[str, Any],
        operator: str,
    ) -> None:
        for raw in [*list(payload.get("materials") or []), *list(payload.get("url_materials") or [])]:
            if not isinstance(raw, dict):
                continue
            material_id = str(raw.get("id") or generate_ulid_like("wmm"))[:160]
            kind = "url" if str(raw.get("type") or "") == "url" else "manual"
            usage = {"must": "required", "reference": "reference", "skip": "excluded"}.get(
                str(raw.get("usage") or "reference"), "reference"
            )
            now = utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO wm_materials(
                    wm_material_id, material_kind, title, source_ref, url, parse_status,
                    body_ref, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wm_material_id) DO UPDATE SET
                    title=excluded.title, source_ref=excluded.source_ref, url=excluded.url,
                    parse_status=excluded.parse_status, body_ref=excluded.body_ref,
                    metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
                """,
                (
                    material_id, kind, str(raw.get("title") or "")[:300],
                    str(raw.get("path") or "")[:800] or None,
                    str(raw.get("url") or "")[:2000] or None,
                    "parsed" if raw.get("parseStatus") == "parsed" else "received",
                    str(raw.get("path") or "")[:800] or None,
                    json.dumps({"legacy_job_material": True}, ensure_ascii=False),
                    now, now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO wm_project_materials(
                    wm_project_id, wm_material_id, usage_state, selected_by, selected_at, note
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wm_project_id, wm_material_id) DO UPDATE SET
                    usage_state=excluded.usage_state, selected_by=excluded.selected_by,
                    selected_at=excluded.selected_at, note=excluded.note
                """,
                (
                    project_id, material_id, usage, operator, now,
                    str(raw.get("reason") or "")[:4000],
                ),
            )

    def _sync_batch_keywords(self, batch_id: str, state: dict[str, Any]) -> None:
        """保留稳定的批次关键词 ID，并把母文章选择投影为显式关系。"""
        # 旧页面允许把同一词以不同临时行 ID 重放回来。运行表要求
        # (batch, keyword_text) 唯一，因此每次按最新快照重建关系并折叠重复词。
        self._conn.execute(
            """
            DELETE FROM wm_batch_mother_links
            WHERE wm_batch_keyword_id IN (
                SELECT wm_batch_keyword_id FROM wm_batch_keywords WHERE wm_batch_id=?
            )
            """,
            (batch_id,),
        )
        self._conn.execute("DELETE FROM wm_batch_keywords WHERE wm_batch_id=?", (batch_id,))
        seen_keywords: set[str] = set()
        for ordinal, raw in enumerate(state.get("keywords") or []):
            if not isinstance(raw, dict):
                continue
            legacy_id = str(raw.get("id") or f"kw-{ordinal}")
            keyword_text = str(raw.get("keyword") or "")[:200]
            if not keyword_text or keyword_text in seen_keywords:
                continue
            seen_keywords.add(keyword_text)
            keyword_id = _runtime_deterministic_id("wmk", batch_id, legacy_id)
            now = utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO wm_batch_keywords(
                    wm_batch_keyword_id, wm_batch_id, keyword_text, purpose,
                    target_article_count, readiness_status, ordinal, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wm_batch_keyword_id) DO UPDATE SET
                    keyword_text=excluded.keyword_text, purpose=excluded.purpose,
                    target_article_count=excluded.target_article_count,
                    readiness_status=excluded.readiness_status, ordinal=excluded.ordinal,
                    metadata_json=excluded.metadata_json
                """,
                (
                    keyword_id, batch_id, keyword_text,
                    str(raw.get("purpose") or "")[:4000],
                    max(1, min(100, int(raw.get("count") or 1))),
                    "ready" if raw.get("readiness") == "ready" else "pending",
                    ordinal,
                    json.dumps({"legacy_id": legacy_id, "updated_at": now}, ensure_ascii=False),
                ),
            )
            self._conn.execute(
                "DELETE FROM wm_batch_mother_links WHERE wm_batch_keyword_id=?",
                (keyword_id,),
            )
            for match in raw.get("motherMatches") or []:
                if not isinstance(match, dict):
                    continue
                content_id = str(match.get("motherId") or "")
                exists = self._conn.execute(
                    "SELECT 1 FROM contents WHERE content_id=?",
                    (content_id,),
                ).fetchone()
                if not exists:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO wm_batch_mother_links(
                        wm_batch_keyword_id, content_id, relation_type, confidence, reason, created_at
                    ) VALUES (?, ?, 'selected', ?, ?, ?)
                    """,
                    (
                        keyword_id, content_id, match.get("confidence"),
                        str(match.get("role") or "")[:1000], now,
                    ),
                )

    def _sync_batch_queue(self, batch_id: str, state: dict[str, Any]) -> None:
        self._conn.execute("DELETE FROM wm_batch_queue_items WHERE wm_batch_id=?", (batch_id,))
        keyword_ids: dict[str, str] = {}
        for row in self._conn.execute(
            "SELECT wm_batch_keyword_id, keyword_text, metadata_json FROM wm_batch_keywords WHERE wm_batch_id=?",
            (batch_id,),
        ).fetchall():
            keyword_ids[row["keyword_text"]] = row["wm_batch_keyword_id"]
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("legacy_id"):
                keyword_ids[str(metadata["legacy_id"])] = row["wm_batch_keyword_id"]
        now = utc_now_iso()
        allowed = {"waiting", "running", "done", "rework", "failed", "cancelled"}
        for ordinal, raw in enumerate(state.get("queue") or []):
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "waiting")
            queue_item_id = _runtime_deterministic_id("wmq", batch_id, str(raw.get("id") or ordinal))
            keyword_id = keyword_ids.get(str(raw.get("keywordId") or ""))
            self._conn.execute(
                """
                INSERT INTO wm_batch_queue_items(
                    wm_batch_queue_item_id, wm_batch_id, wm_batch_keyword_id, ordinal,
                    title, status, output_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_item_id,
                    batch_id,
                    keyword_id,
                    ordinal,
                    str(raw.get("title") or "")[:400],
                    status if status in allowed else "waiting",
                    str(raw.get("outputFile") or "")[:800],
                    now,
                    now,
                ),
            )
            draft_id = _runtime_deterministic_id("wmd", batch_id, str(raw.get("id") or ordinal))
            self._conn.execute(
                """
                INSERT INTO wm_drafts(
                    wm_draft_id, wm_batch_id, wm_batch_keyword_id, status, title,
                    input_hash, provider_kind, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wm_draft_id) DO UPDATE SET
                    status=excluded.status, title=excluded.title, updated_at=excluded.updated_at
                """,
                (
                    draft_id,
                    batch_id,
                    keyword_id,
                    {"waiting": "queued", "running": "running", "done": "draft"}.get(status, "queued"),
                    str(raw.get("title") or "")[:400],
                    hashlib.sha256(f"{batch_id}:{ordinal}".encode()).hexdigest(),
                    self._provider_kind,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE wm_batch_queue_items SET wm_draft_id=? WHERE wm_batch_queue_item_id=?",
                (draft_id, queue_item_id),
            )

    def _record_runtime_draft(
        self,
        job: WritingJob,
        *,
        title: str,
        artifact_ref: str,
        content_id: str,
        status: str,
        ordinal: int = 0,
    ) -> None:
        project_id, batch_id = self._runtime_ids(job.job_id)
        if not project_id and not batch_id:
            return
        draft_id = _runtime_deterministic_id("wmd", job.job_id, str(ordinal), artifact_ref)
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO wm_drafts(
                wm_draft_id, wm_project_id, wm_batch_id, content_id, status, title,
                artifact_ref, input_hash, output_hash, provider_kind, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wm_draft_id) DO UPDATE SET
                status=excluded.status, title=excluded.title, artifact_ref=excluded.artifact_ref,
                content_id=excluded.content_id, output_hash=excluded.output_hash, updated_at=excluded.updated_at
            """,
            (
                draft_id, project_id, batch_id, content_id or None, status,
                title[:400], artifact_ref, hashlib.sha256(job.job_id.encode()).hexdigest(),
                hashlib.sha256(artifact_ref.encode()).hexdigest(), self._provider_kind, now, now,
            ),
        )

    @staticmethod
    def _set_material_usage(payload: dict[str, Any], material_id: str, usage: str) -> bool:
        found = False
        for key in ("materials", "url_materials"):
            for material in payload.get(key) or []:
                if isinstance(material, dict) and str(material.get("id") or "") == material_id:
                    material["usage"] = usage
                    found = True
        return found

    @staticmethod
    def _batch_state(payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("batch_state")
        if isinstance(raw, dict):
            return raw
        keywords = []
        for index, keyword in enumerate(payload.get("keywords") or []):
            keywords.append(
                {
                    "id": f"kw-{index}",
                    "keyword": str(keyword),
                    "purpose": "",
                    "count": 1,
                    "recommendedCount": 1,
                    "signal": "medium",
                    "signalReason": "Hub 任务输入",
                    "hookId": "hook-plan",
                    "motherMatches": [],
                    "readiness": "needs-mother",
                }
            )
        return {
            "name": str(payload.get("topic") or ""),
            "source": str(payload.get("source") or "Hub"),
            "brief": str((payload.get("requirements") or {}).get("brief") or ""),
            "output_dir": str(payload.get("output_dir") or ""),
            "publish_handoff": False,
            "stage": "batch-config",
            "status": "pending",
            "keywords": keywords,
            "queue": [],
        }

    @staticmethod
    def _safe_batch_state(state: dict[str, Any]) -> dict[str, Any]:
        """只保留旧页面可编辑的批次字段，避免任意 JSON 进入任务快照。"""
        keywords: list[dict[str, Any]] = []
        for index, raw in enumerate(state.get("keywords") or []):
            if not isinstance(raw, dict):
                continue
            keywords.append(
                {
                    "id": str(raw.get("id") or f"kw-{index}"),
                    "keyword": str(raw.get("keyword") or "")[:200],
                    "purpose": str(raw.get("purpose") or "")[:4000],
                    "signal": str(raw.get("signal") or "medium")[:40],
                    "signalReason": str(raw.get("signalReason") or "")[:400],
                    "count": max(0, min(99, int(raw.get("count") or 0))),
                    "recommendedCount": max(0, min(99, int(raw.get("recommendedCount") or 0))),
                    "hookId": str(raw.get("hookId") or "")[:80],
                    "motherMatches": [
                        {
                            "motherId": str(match.get("motherId") or ""),
                            "role": str(match.get("role") or "")[:200],
                            "confidence": match.get("confidence"),
                        }
                        for match in (raw.get("motherMatches") or [])
                        if isinstance(match, dict)
                    ],
                    "readiness": "ready" if raw.get("readiness") == "ready" else "needs-mother",
                }
            )
        return {
            "name": str(state.get("name") or "")[:200],
            "source": str(state.get("source") or "")[:120],
            "brief": str(state.get("brief") or "")[:4000],
            "output_dir": str(state.get("output_dir") or "")[:500],
            "publish_handoff": bool(state.get("publish_handoff", False)),
            "stage": str(state.get("stage") or "batch-config"),
            "status": str(state.get("status") or "pending"),
            "keywords": keywords,
            "queue": [
                {
                    "id": str(item.get("id") or "")[:120],
                    "keywordId": str(item.get("keywordId") or "")[:120],
                    "title": str(item.get("title") or "")[:400],
                    "status": str(item.get("status") or "waiting"),
                    "outputFile": str(item.get("outputFile") or "")[:500],
                }
                for item in (state.get("queue") or [])
                if isinstance(item, dict)
            ],
        }

    def _asset_ref(self, path: str) -> str:
        ref = public_asset_ref(path, self._markdown.root)
        if not ref:
            raise ValueError("生成的 Markdown 路径不在受控 asset_store 内")
        return ref

    def _fetch(self, job_id: str) -> WritingJob | None:
        runtime = self._runtime_job_row(job_id)
        if runtime:
            return WritingJob(
                job_id=job_id,
                job_type=runtime["job_type"],
                topic=runtime["topic"],
                status=runtime["status"],
                payload=self._runtime_payload(job_id, runtime["job_type"]),
            )
        row = self._conn.execute(
            "SELECT * FROM production_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        return WritingJob.from_row(row)

    def _runtime_job_row(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT wm_project_id, NULL AS wm_batch_id, title AS topic, 'mother_forge' AS job_type,
                   CASE status
                       WHEN 'active' THEN 'queued'
                       WHEN 'completed' THEN 'blocked'
                       ELSE status
                   END AS status
            FROM wm_projects WHERE legacy_job_id=?
            UNION ALL
            SELECT NULL, wm_batch_id, title, 'batch_production',
                   CASE status
                       WHEN 'draft' THEN 'queued'
                       WHEN 'ready' THEN 'queued'
                       WHEN 'completed' THEN 'blocked'
                       ELSE status
                   END
            FROM wm_batches WHERE legacy_job_id=?
            LIMIT 1
            """,
            (job_id, job_id),
        ).fetchone()
        return dict(row) if row else None

    def _runtime_job_rows(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM (
                SELECT p.legacy_job_id AS job_id, p.wm_project_id, NULL AS wm_batch_id,
                       p.title AS topic, 'mother_forge' AS job_type,
                       CASE p.status WHEN 'active' THEN 'queued' WHEN 'completed' THEN 'blocked' ELSE p.status END AS status,
                       p.created_at, p.updated_at, NULL AS scheduled_at
                FROM wm_projects p
                WHERE p.legacy_job_id IS NOT NULL
                UNION ALL
                SELECT b.legacy_job_id, NULL, b.wm_batch_id, b.title, 'batch_production',
                       CASE b.status WHEN 'draft' THEN 'queued' WHEN 'ready' THEN 'queued'
                            WHEN 'completed' THEN 'blocked' ELSE b.status END,
                       b.created_at, b.updated_at, NULL
                FROM wm_batches b
                WHERE b.legacy_job_id IS NOT NULL
            ) ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if len(rows) < limit:
            legacy = self._conn.execute(
                """
                SELECT job_id, job_type, status, created_at, updated_at, scheduled_at
                FROM production_jobs
                WHERE job_type IN ('mother_forge', 'batch_production')
                  AND job_id NOT IN (SELECT legacy_job_id FROM wm_projects WHERE legacy_job_id IS NOT NULL)
                  AND job_id NOT IN (SELECT legacy_job_id FROM wm_batches WHERE legacy_job_id IS NOT NULL)
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit - len(rows),),
            ).fetchall()
            rows.extend(legacy)
        return [dict(row) for row in rows]

    def _runtime_payload(self, job_id: str, job_type: str | None) -> dict[str, Any]:
        legacy_row = self._conn.execute(
            "SELECT payload_json FROM production_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        payload = json.loads(legacy_row["payload_json"] or "{}") if legacy_row else {}
        if job_type == "mother_forge":
            row = self._conn.execute(
                """
                SELECT wm_project_id, title, purpose, stage, status, workspace_ref
                FROM wm_projects WHERE legacy_job_id=?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return payload
            payload.update(
                {
                    "topic": row["title"],
                    "purpose": row["purpose"],
                    "stage": row["stage"],
                    "runtime": {
                        "wm_project_id": row["wm_project_id"],
                        "status": row["status"],
                        "workspace_ref": row["workspace_ref"],
                    },
                    "materials": self._runtime_materials(row["wm_project_id"]),
                }
            )
        elif job_type == "batch_production":
            row = self._conn.execute(
                """
                SELECT wm_batch_id, title, source, status, requirements_json, workspace_ref
                FROM wm_batches WHERE legacy_job_id=?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return payload
            requirements = json.loads(row["requirements_json"] or "{}")
            keywords = self._runtime_keywords(row["wm_batch_id"])
            queue = [
                dict(item)
                for item in self._conn.execute(
                    """
                    SELECT wm_batch_queue_item_id AS id, title, status,
                           output_ref AS outputFile
                    FROM wm_batch_queue_items
                    WHERE wm_batch_id=? ORDER BY ordinal
                    """,
                    (row["wm_batch_id"],),
                ).fetchall()
            ]
            state = self._batch_state(payload)
            state.update(
                {
                    "name": row["title"],
                    "source": row["source"],
                    "brief": requirements.get("brief", ""),
                    "keywords": keywords,
                    "queue": queue,
                    "status": row["status"],
                }
            )
            payload.update(
                {
                    "topic": row["title"],
                    "source": row["source"],
                    "requirements": requirements,
                    "batch_state": state,
                    "keywords": [item["keyword"] for item in keywords],
                    "runtime": {
                        "wm_batch_id": row["wm_batch_id"],
                        "status": row["status"],
                        "workspace_ref": row["workspace_ref"],
                    },
                }
            )
        return payload

    def _runtime_materials(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT m.wm_material_id AS id, m.material_kind AS type, m.title,
                   m.source_ref AS path, m.url, m.parse_status AS parseStatus,
                   pm.usage_state AS usage, pm.note AS reason
            FROM wm_project_materials pm
            JOIN wm_materials m ON m.wm_material_id=pm.wm_material_id
            WHERE pm.wm_project_id=? ORDER BY m.created_at
            """,
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _runtime_keywords(self, batch_id: str, *, include_ids: bool = False) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT wm_batch_keyword_id AS id, keyword_text AS keyword, purpose,
                   wm_batch_keyword_id,
                   target_article_count AS count, readiness_status AS readiness
            FROM wm_batch_keywords WHERE wm_batch_id=? ORDER BY ordinal
            """,
            (batch_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["readiness"] = "ready" if item["readiness"] == "ready" else "needs-mother"
            item["motherMatches"] = []
            if not include_ids:
                item.pop("wm_batch_keyword_id", None)
            result.append(item)
        return result

    def _sync_runtime_status(self, job_id: str, status: str) -> None:
        project_id, batch_id = self._runtime_ids(job_id)
        if project_id:
            mapped = "completed" if status == "completed" else "blocked" if status == "blocked" else "active"
            self._conn.execute(
                "UPDATE wm_projects SET status=?, updated_at=? WHERE wm_project_id=?",
                (mapped, utc_now_iso(), project_id),
            )
        if batch_id:
            mapped = (
                "completed" if status == "completed"
                else "blocked" if status == "blocked"
                else "running" if status == "running"
                else "draft"
            )
            self._conn.execute(
                "UPDATE wm_batches SET status=?, updated_at=? WHERE wm_batch_id=?",
                (mapped, utc_now_iso(), batch_id),
            )

    def _record_content(
        self,
        md_path: str,
        title: str,
        content_type: str,
        file_hash: str,
        content_hash: str,
    ) -> None:
        content_id = generate_ulid_like("cnt")
        self._conn.execute(
            """
            INSERT INTO contents(
                content_id, content_type, title, canonical_url,
                first_seen_at, updated_at, md_path, file_hash, content_hash
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(content_id) DO UPDATE SET file_hash=excluded.file_hash
            """,
            (
                content_id,
                content_type,
                title,
                utc_now_iso(),
                utc_now_iso(),
                md_path,
                file_hash,
                content_hash,
            ),
        )

    def _record_event(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO job_events(event_id, job_id, occurred_at, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                generate_ulid_like("evt"),
                job_id,
                utc_now_iso(),
                event,
                None,
                json.dumps(
                    scrub_public_payload(payload, asset_root=self._markdown.root),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

    def _safe_provider_details(self, *, mode: str, reason_code: str = "writing.job_created") -> dict[str, Any]:
        return {
            "provider_kind": self._provider_kind,
            "provider_status": self._provider_status,
            "reason_code": reason_code,
            "mode": mode,
            "demo": self._provider is not None,
        }

    def _last_content_id(self, md_path: str) -> str:
        row = self._conn.execute(
            "SELECT content_id FROM contents WHERE md_path=? ORDER BY updated_at DESC LIMIT 1",
            (md_path,),
        ).fetchone()
        return row["content_id"] if row else ""


def backfill_writing_runtime(
    connection: sqlite3.Connection,
    *,
    asset_root: Path,
) -> int:
    """为 v3.3 前创建的兼容任务补齐 wm_* 外键和运行记录。

    该过程幂等：只处理还没有 runtime 外键的任务；旧 payload 只作为一次性迁移
    输入，后续领域状态由 wm_* 表承载。
    """
    service = WritingService(
        connection=connection,
        markdown_store=MarkdownStore(asset_root),
        provider=None,
    )
    rows = connection.execute(
        """
        SELECT * FROM production_jobs
        WHERE job_type IN ('mother_forge', 'batch_production')
          AND ((job_type='mother_forge' AND wm_project_id IS NULL)
            OR (job_type='batch_production' AND wm_batch_id IS NULL))
        ORDER BY created_at, job_id
        """
    ).fetchall()
    for row in rows:
        job = WritingJob.from_row(row)
        if job.job_type == "mother_forge":
            service._create_runtime_project(
                job.job_id,
                topic=job.topic,
                purpose=str(job.payload.get("purpose") or ""),
                operator="v33-runtime-backfill",
            )
        else:
            state = service._batch_state(job.payload)
            service._create_runtime_batch(
                job.job_id,
                topic=job.topic,
                source=str(job.payload.get("source") or state.get("source") or "legacy"),
                requirements=job.payload.get("requirements")
                if isinstance(job.payload.get("requirements"), dict)
                else {"brief": str(state.get("brief") or "")},
                state=state,
                operator="v33-runtime-backfill",
            )
        service._sync_runtime_from_payload(
            job,
            job.payload,
            operator="v33-runtime-backfill",
            event_type="writing.runtime_backfilled",
        )
        service._record_write_receipt(
            operation="runtime_backfill",
            job_id=job.job_id,
            operator="v33-runtime-backfill",
            source_ref=f"writing/job/{job.job_id}/runtime-backfill",
            manifest={
                "runtime_tables": (
                    ["wm_projects", "wm_project_events"]
                    if job.job_type == "mother_forge"
                    else ["wm_batches", "wm_batch_keywords", "wm_batch_queue_items"]
                ),
            },
        )
    return len(rows)


def _deterministic_id(*parts: str) -> str:
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"cnt_{digest}"


def _runtime_deterministic_id(prefix: str, *parts: str) -> str:
    """稳定映射旧 UI 临时行 ID 到 v3.3 运行表主键。"""
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"
