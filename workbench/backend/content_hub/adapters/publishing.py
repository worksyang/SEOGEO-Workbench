"""发布中心适配器：从 publish_attempts + publish accounts 反查历史。
真实发布动作通过 PublishingService.publish() 单独触发。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from ..ingestion.pipeline import IngestionPipeline, RawBatch
from ..validation.timestamps import utc_now_iso
from .base import AdapterStatus, AdapterTask

ADAPTER_KEY = "publishing"


def make_attempts_batch(
    *,
    account_id: str,
    attempts: Iterable[dict],
) -> tuple[RawBatch, AdapterStatus]:
    raw = RawBatch(adapter_key=ADAPTER_KEY, source_scope=account_id)
    status = AdapterStatus(adapter_key=ADAPTER_KEY, source_scope=account_id)
    items = list(attempts)
    status.total = len(items)
    if not items:
        return raw, status
    from ..domain.ids import content_id_from_text
    from ..domain.models import ContentRecord, DiscoveryRecord, IdentifierRecord

    for attempt in items:
        title = attempt.get("title") or attempt.get("account_id", account_id)
        content_id = content_id_from_text("publish", account_id, attempt.get("idem_key", ""))
        content = ContentRecord(
            content_id=content_id,
            content_type="demo_publish_artifact",
            title=title,
            canonical_url="",
            first_seen_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            md_path="",
            payload={"account_id": account_id, "idem_key": attempt.get("idem_key", ""), "demo_only": True},
        )
        raw.contents.append(content)
        status.written += 1
    return raw, status


def run(connection: sqlite3.Connection, pipeline: IngestionPipeline, *, account_id: str = "") -> AdapterTask:
    task = AdapterTask(
        adapter_key=ADAPTER_KEY,
        status=AdapterStatus(adapter_key=ADAPTER_KEY, source_scope=account_id or "all-accounts"),
    )
    rows = connection.execute(
        "SELECT DISTINCT account_key FROM publish_attempts LIMIT 50"
    ).fetchall()
    total_seen = 0
    total_written = 0
    total_failed = 0
    for row in rows:
        acct = row["account_key"]
        if account_id and acct != account_id:
            continue
        attempts = [
            dict(item)
            for item in connection.execute(
                "SELECT attempt_id, idempotency_key, status FROM publish_attempts WHERE account_key=? LIMIT 200",
                (acct,),
            ).fetchall()
        ]
        attempts = [
            {"account_id": acct, "idem_key": item.get("idempotency_key", ""), "status": item.get("status")}
            for item in attempts
        ]
        raw, _ = make_attempts_batch(account_id=acct, attempts=attempts)
        task.status.total += len(attempts)
        result = pipeline.run(raw)
        total_seen += len(attempts)
        total_written += result.records_written
        total_failed += result.records_failed
    task.status.written = total_written
    task.records_written = total_written
    task.records_failed = total_failed
    return task
