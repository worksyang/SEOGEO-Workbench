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

from ..domain.ids import generate_ulid_like
from ..ingestion.markdown_store import MarkdownStore
from ..validation.timestamps import utc_now_iso
from .audit import AuditService
from .jobs import JobsService


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
        self._record_content(
            result.md_path,
            title,
            "demo_mother_article",
            result.file_hash,
            result.content_hash,
        )
        self._record_event(job.job_id, "mother_forge.written", {"md_path": result.md_path})
        return {
            "md_path": result.md_path,
            "content_id": self._last_content_id(result.md_path),
            "demo_only": True,
        }

    def _run_batch(self, job: WritingJob) -> dict[str, Any]:
        outputs: list[str] = []
        keywords = job.payload.get("keywords") or []
        mothers = job.payload.get("matched_articles") or []
        target = max(1, int(job.payload.get("target_article_count") or len(keywords) or 1))
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
            outputs.append(result.md_path)
            self._record_content(
                result.md_path,
                prompt,
                "demo_generated_article",
                result.file_hash,
                result.content_hash,
            )
            self._record_event(job.job_id, f"batch.article.{index + 1}", {"md_path": result.md_path})
        return {"outputs": outputs, "count": len(outputs), "demo_only": True}

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._jobs.list_recent(limit=limit)
        return [
            {
                "job_id": row["job_id"],
                "job_type": row["job_type"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "scheduled_at": row["scheduled_at"],
            }
            for row in rows
        ]

    def detail(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.detail(job_id)

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
                f"ev_{job_id}_{event}",
                job_id,
                utc_now_iso(),
                event,
                None,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
