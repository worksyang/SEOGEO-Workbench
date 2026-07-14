"""WritingMoney 适配器：把 Fake Provider 落地的生产任务同步写入 Hub。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from ..ingestion.pipeline import IngestionPipeline, RawBatch
from ..validation.timestamps import utc_now_iso
from .base import AdapterStatus, AdapterTask

ADAPTER_KEY = "writing-fake"


def make_dummy_batch(*, count: int = 3) -> tuple[RawBatch, AdapterStatus]:
    raw = RawBatch(adapter_key=ADAPTER_KEY, source_scope="fake-provider")
    status = AdapterStatus(adapter_key=ADAPTER_KEY, source_scope="fake-provider")
    status.total = count
    now = utc_now_iso()
    from ..domain.ids import content_id_from_text
    from ..domain.models import ContentRecord, DiscoveryRecord, IdentifierRecord

    samples = [
        ("分红实现率判断框架（自检稿）", "demo-分红实现率"),
        ("香港储蓄险选择清单（自检稿）", "demo-香港储蓄险"),
        ("保司对比表（自检稿）", "demo-保司对比"),
    ]
    for index, (title, slug) in enumerate(samples[:count]):
        body = (
            f"# {title}\n\n"
            f"> 由 Fake Provider 在适配器中生成的演示稿，仅用于校验 ingest 路径与回放。\n\n"
            f"## 1. 关键词命中\n\n展示自动关键词识别与正文的对应关系。\n\n"
            f"## 2. 母文章复用\n\n- 友邦财富盈活评估\n- 保诚信守明天评估\n- 安盛盛利2评估\n\n"
            f"## 3. 风险与提示\n\n本文为成稿示例，正式发表前需要经过事实校对与敏感词二次检查。\n"
        )
        content_id = content_id_from_text("writing_demo", slug)
        content = ContentRecord(
            content_id=content_id,
            content_type="generated_article",
            title=title,
            canonical_url="",
            first_seen_at=now,
            updated_at=now,
            md_path="",
            payload={"adapter": "writing-fake", "slug": slug, "index": index},
        )
        raw.contents.append(content)
        raw.identifiers.append(
            IdentifierRecord(
                namespace="mother.frontmatter_asset_id",
                external_id=f"writing-fake::{slug}",
                content_id=content_id,
                first_seen_at=now,
            )
        )
        raw.discoveries.append(
            DiscoveryRecord.build(
                content_id=content_id,
                system="writing",
                channel="fake_provider",
                discovered_at=now,
                source_ref=f"writing-fake::{slug}",
                payload={"slug": slug},
            )
        )
        status.written += 1
    return raw, status


def run(connection: sqlite3.Connection, pipeline: IngestionPipeline, *, count: int = 3) -> AdapterTask:
    task = AdapterTask(
        adapter_key=ADAPTER_KEY,
        status=AdapterStatus(adapter_key=ADAPTER_KEY, source_scope="fake-provider"),
    )
    raw, status = make_dummy_batch(count=count)
    task.status = status
    result = pipeline.run(raw)
    task.records_written = result.records_written
    task.records_failed = result.records_failed
    return task
