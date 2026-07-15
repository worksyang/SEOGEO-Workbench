"""WritingMoney 服务：安全的配置状态、演示 Provider 与任务恢复。

dev-plan §5.6 协议：
- 项目 / 批次全部落到 production_jobs 表；
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

    def create_mother_forge(
        self,
        *,
        topic: str,
        purpose: str,
        urls: Iterable[str] = (),
        recommended_mothers: Iterable[str] = (),
        operator: str = "user",
    ) -> WritingJob:
        payload = {
            "topic": topic,
            "purpose": purpose,
            "urls": list(urls),
            "recommended_mothers": list(recommended_mothers),
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
        self._audit.record(
            action="writing.create",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details=self._safe_provider_details(mode="mother_forge"),
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
    ) -> WritingJob:
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
        self._audit.record(
            action="writing.create",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details=self._safe_provider_details(mode="batch_production"),
        )
        return WritingJob(
            job_id=job_id,
            job_type="batch_production",
            topic=topic,
            status="queued",
            payload=payload,
        )

    def run(self, job_id: str, *, operator: str = "user") -> dict[str, Any]:
        job = self._fetch(job_id)
        if not job:
            raise FileNotFoundError(f"未找到生产任务：{job_id}")
        if not self._jobs.claim(job_id, operator):
            return {"status": "skipped", "reason": "任务已被认领或不在 queued 状态"}
        if self._provider is None:
            details = self._safe_provider_details(
                mode=job.job_type,
                reason_code="writing.provider_unconfigured",
            )
            self._jobs.complete(job_id, status="blocked")
            self._record_event(job_id, "blocked", details)
            self._audit.record(
                action="writing.run",
                subject_type="writing_job",
                subject_id=job_id,
                actor_id=operator,
                outcome="blocked",
                details=details,
            )
            return {
                "status": "blocked",
                "job_id": job_id,
                "blocked": True,
                "demo": False,
                **details,
            }
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
            return {"status": "demo_only", "job_id": job_id, "blocked": False, **details, **written}
        except Exception as exc:  # noqa: BLE001
            self._jobs.complete(job_id, status="failed")
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
            return {"status": "failed", "job_id": job_id, **details}

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
        self._update_payload(
            job.job_id,
            {"batch_state": state, "keywords": [item.get("keyword", "") for item in state.get("keywords", [])]},
            event_type="writing.batch_queue_completed",
            operator="system",
        )
        return {"outputs": outputs, "count": len(outputs), "demo_only": True}

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._jobs.list_recent(limit=limit)
        items: list[dict[str, Any]] = []
        for row in rows:
            detail = self._jobs.detail(row["job_id"]) or {}
            payload = detail.get("payload") if isinstance(detail.get("payload"), dict) else {}
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
        detail = self._jobs.detail(job_id)
        return scrub_public_payload(detail, asset_root=self._markdown.root) if detail else None

    def mutate(
        self,
        job_id: str,
        *,
        action: str,
        value: dict[str, Any],
        operator: str = "user",
    ) -> dict[str, Any]:
        """持久化旧 WritingMoney 页面上的局部写操作。

        该接口不调用任何真实 Provider，也不把“收到 URL”包装成“已抓取”。
        解析器未配置时，URL 只进入 received 状态并明确显示等待解析。
        """
        job = self._fetch(job_id)
        if not job:
            raise FileNotFoundError(f"未找到生产任务：{job_id}")
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
        self._audit.record(
            action=f"writing.{action}",
            subject_type="writing_job",
            subject_id=job_id,
            actor_id=operator,
            outcome="succeeded",
            details={"job_type": job.job_type, "payload_keys": sorted(payload.keys())},
        )
        return self.detail(job_id) or {}

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
        row = self._conn.execute(
            "SELECT * FROM production_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        return WritingJob.from_row(row)

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


def _deterministic_id(*parts: str) -> str:
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"cnt_{digest}"
