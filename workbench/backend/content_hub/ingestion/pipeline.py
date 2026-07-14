"""统一内容工作台 · 摄取层 / 适配器执行管道。

把适配器输出（RawBatch）与六类事实契约整合，集中失败记录、批次进度与 checkpoint 更新。
所有写入都走 Hub 的 Repository，因此满足 dev-plan §4.7 的单写锁 + WAL 协议。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..db.writer_lock import writer_lock
from ..domain.ids import generate_ulid_like
from ..domain.models import (
    CommentEvent,
    CommentRecord,
    ContentRecord,
    DiscoveryRecord,
    GeoAnswer,
    GeoSourceRelation,
    IdentifierRecord,
    IngestionBatch,
    MetricObservation,
    SearchHit,
    SearchSnapshot,
)
from ..domain.taxonomy import normalize_entity_list, normalize_intent_list
from ..validation.timestamps import utc_now_iso
from .identity_resolver import IdentityResolver, ResolverContext


@dataclass(slots=True)
class RawBatch:
    adapter_key: str
    source_scope: str
    contents: list[ContentRecord] = field(default_factory=list)
    identifiers: list[IdentifierRecord] = field(default_factory=list)
    discoveries: list[DiscoveryRecord] = field(default_factory=list)
    snapshots: list[tuple[SearchSnapshot, list[SearchHit]]] = field(default_factory=list)
    metric_observations: list[MetricObservation] = field(default_factory=list)
    comments: list[tuple[CommentRecord, list[CommentEvent]]] = field(default_factory=list)
    geo_answers: list[tuple[GeoAnswer, list[GeoSourceRelation]]] = field(default_factory=list)
    errors: list[Mapping[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PipelineResult:
    batch_id: str
    records_seen: int
    records_written: int
    records_failed: int
    errors: list[Mapping[str, Any]]


class IngestionPipeline:
    """统一 ingest 入口，所有适配器必须经它把事实写入 Hub。"""

    def __init__(self, connection: sqlite3.Connection, lock_path):
        self._conn = connection
        self._lock_path = lock_path

    def run(self, raw: RawBatch) -> PipelineResult:
        records_seen = (
            len(raw.contents)
            + len(raw.identifiers)
            + len(raw.discoveries)
            + sum(len(hits) for _, hits in raw.snapshots)
            + len(raw.metric_observations)
            + sum(len(events) for _, events in raw.comments)
            + sum(len(rels) for _, rels in raw.geo_answers)
        )
        batch = IngestionBatch.new(adapter_key=raw.adapter_key, source_scope=raw.source_scope)
        self._write_batch_start(batch, records_seen)
        errors: list[Mapping[str, Any]] = list(raw.errors)
        written = 0
        resolver = IdentityResolver(self._conn)
        try:
            with writer_lock(self._lock_path):
                # 已有 SQLite 隐式事务时显式先提交，避免 BEGIN IMMEDIATE 嵌套
                self._conn.commit()
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    written += self._ingest_contents(raw.contents, resolver, errors)
                    written += self._ingest_identifiers(raw.identifiers, errors)
                    written += self._ingest_discoveries(raw.discoveries, errors)
                    written += self._ingest_snapshots(raw.snapshots, errors)
                    written += self._ingest_metrics(raw.metric_observations, errors)
                    written += self._ingest_comments(raw.comments, errors)
                    written += self._ingest_geo(raw.geo_answers, errors)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
        except Exception as exc:
            errors.append({"scope": "pipeline", "error": str(exc)})
            self._mark_batch_status(batch.batch_id, "failed", errors)
            return PipelineResult(
                batch_id=batch.batch_id,
                records_seen=records_seen,
                records_written=0,
                records_failed=records_seen,
                errors=errors,
            )
        records_failed = len(errors)
        status = "succeeded" if records_failed == 0 else "partial_failed"
        self._mark_batch_status(batch.batch_id, status, errors, written=written, seen=records_seen)
        return PipelineResult(
            batch_id=batch.batch_id,
            records_seen=records_seen,
            records_written=written,
            records_failed=records_failed,
            errors=errors,
        )
    # ── 各种 ingest 内部实现 ─────────────────────────────

    def _ingest_contents(
        self,
        contents: Iterable[ContentRecord],
        resolver: IdentityResolver,
        errors: list,
    ) -> int:
        written = 0
        for record in contents:
            try:
                ctx = ResolverContext(
                    title=record.title,
                    author_name=record.author_name,
                    published_at=record.published_at,
                    canonical_url=record.canonical_url,
                )
                decision = resolver.resolve(ctx)
                entities = normalize_entity_list(record.entities)
                intents = normalize_intent_list(record.intents)
                if not record.content_id:
                    record.content_id = decision.content_id
                self._conn.execute(
                    """
                    INSERT INTO contents(
                        content_id, content_type, title, canonical_url, creator_id,
                        author_name, published_at, first_seen_at, updated_at,
                        md_path, file_hash, content_hash, domain,
                        entities_json, intents_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(content_id) DO UPDATE SET
                        title=COALESCE(excluded.title, contents.title),
                        canonical_url=COALESCE(excluded.canonical_url, contents.canonical_url),
                        creator_id=COALESCE(excluded.creator_id, contents.creator_id),
                        author_name=COALESCE(excluded.author_name, contents.author_name),
                        published_at=COALESCE(excluded.published_at, contents.published_at),
                        updated_at=excluded.updated_at,
                        md_path=COALESCE(excluded.md_path, contents.md_path),
                        file_hash=COALESCE(excluded.file_hash, contents.file_hash),
                        content_hash=COALESCE(excluded.content_hash, contents.content_hash),
                        domain=COALESCE(excluded.domain, contents.domain),
                        entities_json=excluded.entities_json,
                        intents_json=excluded.intents_json,
                        payload_json=excluded.payload_json
                    """,
                    (
                        record.content_id,
                        record.content_type,
                        record.title or None,
                        record.canonical_url or None,
                        record.creator_id or None,
                        record.author_name or None,
                        record.published_at or None,
                        record.first_seen_at or utc_now_iso(),
                        record.updated_at or record.first_seen_at or utc_now_iso(),
                        record.md_path or None,
                        record.file_hash or None,
                        record.content_hash or None,
                        record.domain or None,
                        json.dumps(entities, ensure_ascii=False),
                        json.dumps(intents, ensure_ascii=False),
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                resolver.record_evidence(decision)
                written += 1
            except Exception as exc:
                errors.append({"scope": "content", "content_id": record.content_id, "error": str(exc)})
        return written

    def _ingest_identifiers(
        self,
        identifiers: Iterable[IdentifierRecord],
        errors: list,
    ) -> int:
        written = 0
        for record in identifiers:
            try:
                self._conn.execute(
                    """
                    INSERT INTO content_identifiers(namespace, external_id, content_id, first_seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(namespace, external_id) DO UPDATE SET
                        content_id=excluded.content_id
                    """,
                    (
                        record.namespace,
                        record.external_id,
                        record.content_id,
                        record.first_seen_at or utc_now_iso(),
                    ),
                )
                written += 1
            except Exception as exc:
                errors.append({"scope": "identifier", "error": str(exc)})
        return written

    def _ingest_discoveries(
        self,
        discoveries: Iterable[DiscoveryRecord],
        errors: list,
    ) -> int:
        written = 0
        for record in discoveries:
            try:
                self._conn.execute(
                    """
                    INSERT INTO content_discoveries(
                        discovery_id, content_id, discovery_system, discovery_channel,
                        discovered_at, snapshot_id, source_ref, payload_json
                    ) VALUES (?, ?, ?, ?, ?, COALESCE(?, 'no-snapshot'), ?, ?)
                    ON CONFLICT(content_id, discovery_system, discovery_channel, COALESCE(snapshot_id, 'no-snapshot'))
                    DO UPDATE SET payload_json=excluded.payload_json
                    """,
                    (
                        record.discovery_id,
                        record.content_id,
                        record.discovery_system,
                        record.discovery_channel,
                        record.discovered_at,
                        record.snapshot_id or "",
                        record.source_ref or "",
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                written += 1
            except Exception as exc:
                errors.append({"scope": "discovery", "error": str(exc)})
        return written

    def _ingest_snapshots(self, snapshots, errors: list) -> int:
        written = 0
        for snapshot, hits in snapshots:
            try:
                self._conn.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id, platform, keyword, keyword_id, captured_at,
                        trigger_type, result_count, features_json, source_ref, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, keyword, captured_at) DO UPDATE SET
                        result_count=excluded.result_count,
                        features_json=excluded.features_json,
                        source_ref=excluded.source_ref,
                        payload_json=excluded.payload_json
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.platform,
                        snapshot.keyword,
                        snapshot.keyword_id or "",
                        snapshot.captured_at,
                        snapshot.trigger_type,
                        snapshot.result_count,
                        json.dumps(dict(snapshot.features), ensure_ascii=False),
                        snapshot.source_ref or "",
                        json.dumps(dict(snapshot.payload), ensure_ascii=False),
                    ),
                )
                for hit in hits:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO search_hits(
                                hit_id, snapshot_id, rank, content_id, title_raw,
                                url_raw, creator_name_raw, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(snapshot_id, rank) DO UPDATE SET
                                content_id=excluded.content_id,
                                title_raw=excluded.title_raw,
                                url_raw=excluded.url_raw,
                                creator_name_raw=excluded.creator_name_raw,
                                payload_json=excluded.payload_json
                            """,
                            (
                                hit.hit_id or generate_ulid_like("hit"),
                                snapshot.snapshot_id,
                                hit.rank,
                                hit.content_id or None,
                                hit.title_raw or "",
                                hit.url_raw or "",
                                hit.creator_name_raw or "",
                                json.dumps(dict(hit.payload), ensure_ascii=False),
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        errors.append({"scope": "hit", "error": str(exc)})
            except Exception as exc:
                errors.append({"scope": "snapshot", "error": str(exc)})
        return written

    def _ingest_metrics(
        self,
        observations: Iterable[MetricObservation],
        errors: list,
    ) -> int:
        written = 0
        for record in observations:
            try:
                self._conn.execute(
                    """
                    INSERT INTO metric_observations(
                        observation_id, subject_type, subject_id, metric_key,
                        observed_at, numeric_value, text_value, snapshot_id,
                        source_ref, confidence, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'no-snapshot'), ?, ?, ?)
                    ON CONFLICT(subject_type, subject_id, metric_key, observed_at, COALESCE(snapshot_id, 'no-snapshot'))
                    DO UPDATE SET
                        numeric_value=excluded.numeric_value,
                        text_value=excluded.text_value,
                        source_ref=excluded.source_ref,
                        confidence=excluded.confidence,
                        payload_json=excluded.payload_json
                    """,
                    (
                        record.observation_id,
                        record.subject_type,
                        record.subject_id,
                        record.metric_key,
                        record.observed_at,
                        record.numeric_value,
                        record.text_value,
                        record.snapshot_id or "",
                        record.source_ref or "",
                        record.confidence,
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                written += 1
            except Exception as exc:
                errors.append({"scope": "metric", "error": str(exc)})
        return written

    def _ingest_comments(self, comments, errors: list) -> int:
        written = 0
        for record, events in comments:
            try:
                self._conn.execute(
                    """
                    INSERT INTO comments(
                        comment_id, content_id, platform, external_id, parent_comment_id,
                        author_name, text_raw, first_seen_at, last_seen_at,
                        current_visibility, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, external_id) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        current_visibility=excluded.current_visibility,
                        payload_json=excluded.payload_json
                    """,
                    (
                        record.comment_id,
                        record.content_id,
                        record.platform,
                        record.external_id,
                        record.parent_comment_id or "",
                        record.author_name or "",
                        record.text_raw or "",
                        record.first_seen_at or utc_now_iso(),
                        record.last_seen_at or record.first_seen_at or utc_now_iso(),
                        record.current_visibility,
                        json.dumps(dict(record.payload), ensure_ascii=False),
                    ),
                )
                for event in events:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO comment_events(
                                event_id, comment_id, observed_at, event_type,
                                previous_state, current_state, source_ref, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event.event_id or generate_ulid_like("cev"),
                                record.comment_id,
                                event.observed_at,
                                event.event_type,
                                event.previous_state or "",
                                event.current_state or "",
                                event.source_ref or "",
                                json.dumps(dict(event.payload), ensure_ascii=False),
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        errors.append({"scope": "comment_event", "error": str(exc)})
                written += 1
            except Exception as exc:
                errors.append({"scope": "comment", "error": str(exc)})
        return written

    def _ingest_geo(self, geo_answers, errors: list) -> int:
        written = 0
        for answer, relations in geo_answers:
            try:
                self._conn.execute(
                    """
                    INSERT INTO geo_answers(
                        answer_id, content_id, app, mode, question_raw,
                        captured_at, answer_hash, tools_json, recommended_json,
                        source_ref, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(answer_id) DO UPDATE SET
                        mode=excluded.mode,
                        tools_json=excluded.tools_json,
                        recommended_json=excluded.recommended_json,
                        payload_json=excluded.payload_json
                    """,
                    (
                        answer.answer_id,
                        answer.content_id,
                        answer.app,
                        answer.mode or "",
                        answer.question_raw,
                        answer.captured_at,
                        answer.answer_hash or "",
                        json.dumps(list(answer.tools), ensure_ascii=False),
                        json.dumps(list(answer.recommended), ensure_ascii=False),
                        answer.source_ref or "",
                        json.dumps(dict(answer.payload), ensure_ascii=False),
                    ),
                )
                for relation in relations:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO geo_source_relations(
                                relation_id, answer_id, source_content_id,
                                relation_type, position, anchor_text, url_raw, payload_json
                            ) VALUES (?, ?, ?, ?, COALESCE(?, -1), ?, ?, ?)
                            ON CONFLICT(answer_id, relation_type, COALESCE(position, -1), url_raw)
                            DO UPDATE SET
                                source_content_id=COALESCE(excluded.source_content_id, geo_source_relations.source_content_id),
                                anchor_text=excluded.anchor_text,
                                payload_json=excluded.payload_json
                            """,
                            (
                                relation.relation_id or generate_ulid_like("rel"),
                                answer.answer_id,
                                relation.source_content_id or None,
                                relation.relation_type,
                                relation.position if relation.position is not None else None,
                                relation.anchor_text or "",
                                relation.url_raw or "",
                                json.dumps(dict(relation.payload), ensure_ascii=False),
                            ),
                        )
                        written += 1
                    except Exception as exc:
                        errors.append({"scope": "geo_relation", "error": str(exc)})
                written += 1
            except Exception as exc:
                errors.append({"scope": "geo_answer", "answer_id": answer.answer_id, "error": str(exc)})
        return written

    # ── batch 状态记录 ─────────────────────────────────

    def _write_batch_start(self, batch: IngestionBatch, records_seen: int) -> None:
        self._conn.execute(
            """
            INSERT INTO ingestion_batches(
                batch_id, adapter_key, source_scope, status,
                started_at, records_seen, error_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', '{}')
            """,
            (
                batch.batch_id,
                batch.adapter_key,
                batch.source_scope,
                "running",
                utc_now_iso(),
                records_seen,
            ),
        )

    def _mark_batch_status(
        self,
        batch_id: str,
        status: str,
        errors: list,
        written: int = 0,
        seen: int = 0,
    ) -> None:
        self._conn.execute(
            """
            UPDATE ingestion_batches
            SET status=?, finished_at=?, records_written=?, records_seen=?,
                records_failed=?, error_json=?, updated_at=?
            WHERE batch_id=?
            """,
            (
                status,
                utc_now_iso(),
                written,
                seen,
                len(errors),
                json.dumps(errors, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
                batch_id,
            ),
        )
