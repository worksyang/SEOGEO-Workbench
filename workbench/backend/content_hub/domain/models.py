"""统一内容工作台 · 域层 / 内容模型。

v3.2 核心 14 张表的轻量 dataclass 形态，对应 Repository 入参与字典返回。
不替代 SQLite 行对象；只用于序列化 payload_json / facts / reading 接口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .ids import (
    canonicalize_url,
    content_id_from_canonical_url,
    content_id_from_text,
    generate_ulid_like,
    namespace_for,
)

# ── 内容侧 ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ContentRecord:
    """一篇文章 / 笔记 / Wiki / AI 回答统一建模。

    命名保持与 contents 表一致；payload_json / entities_json / intents_json
    用属性 dict 引用，业务代码使用时按 JSON Schema 校验。
    """

    content_id: str
    content_type: str
    title: str = ""
    canonical_url: str = ""
    creator_id: str = ""
    author_name: str = ""
    published_at: str = ""
    first_seen_at: str = ""
    updated_at: str = ""
    md_path: str = ""
    file_hash: str = ""
    content_hash: str = ""
    domain: str = ""
    entities: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_url(
cls,
        *,
        url: str,
        content_type: str,
        title: str = "",
        author_name: str = "",
        published_at: str = "",
        creator_id: str = "",
        first_seen_at: str = "",
        updated_at: str = "",
        domain: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> "ContentRecord":
        canonical = canonicalize_url(url)
        return cls(
            content_id=content_id_from_canonical_url(canonical) if canonical else content_id_from_text("placeholder", title or url, content_type),
            content_type=content_type,
            title=title,
            canonical_url=canonical,
            creator_id=creator_id,
            author_name=author_name,
            published_at=published_at,
            first_seen_at=first_seen_at,
            updated_at=updated_at or first_seen_at,
            domain=domain,
            payload=payload or {},
        )


# ── 发现 / 标识 / 快照 ────────────────────────────────────────────────


@dataclass(slots=True)
class IdentifierRecord:
    namespace: str
    external_id: str
    content_id: str
    first_seen_at: str = ""


@dataclass(slots=True)
class DiscoveryRecord:
    """一次内容被发现的事实。

    snapshot_id 可空；空时通过表达式唯一索引 ``COALESCE(snapshot_id, 'no-snapshot')``
    保证幂等。
    """

    discovery_id: str
    content_id: str
    discovery_system: str
    discovery_channel: str
    discovered_at: str
    snapshot_id: str = ""
    source_ref: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        content_id: str,
        system: str,
        channel: str,
        discovered_at: str,
        snapshot_id: str = "",
        source_ref: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> "DiscoveryRecord":
        return cls(
            discovery_id=generate_ulid_like("dsc"),
            content_id=content_id,
            discovery_system=system,
            discovery_channel=channel,
            discovered_at=discovered_at,
            snapshot_id=snapshot_id,
            source_ref=source_ref,
            payload=payload or {},
        )


@dataclass(slots=True)
class SearchSnapshot:
    snapshot_id: str
    platform: str
    keyword: str
    captured_at: str
    keyword_id: str = ""
    trigger_type: str = "scheduled"
    result_count: int = 0
    source_ref: str = ""
    features: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        platform: str,
        keyword: str,
        captured_at: str,
        keyword_id: str = "",
        trigger_type: str = "scheduled",
        result_count: int = 0,
        source_ref: str = "",
        features: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "SearchSnapshot":
        return cls(
            snapshot_id=generate_ulid_like("snp"),
            platform=platform,
            keyword=keyword,
            captured_at=captured_at,
            keyword_id=keyword_id,
            trigger_type=trigger_type,
            result_count=result_count,
            source_ref=source_ref,
            features=features or {},
            payload=payload or {},
        )


@dataclass(slots=True)
class SearchHit:
    hit_id: str
    snapshot_id: str
    rank: int
    content_id: str = ""
    title_raw: str = ""
    url_raw: str = ""
    creator_name_raw: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


# ── 指标 / 评论 / GEO / 信号 / 生产任务 ────────────────────────────────


@dataclass(slots=True)
class MetricObservation:
    observation_id: str
    subject_type: str
    subject_id: str
    metric_key: str
    observed_at: str
    numeric_value: float | None = None
    text_value: str | None = None
    snapshot_id: str = ""
    source_ref: str = ""
    confidence: float | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommentRecord:
    comment_id: str
    content_id: str
    platform: str
    external_id: str
    parent_comment_id: str = ""
    author_name: str = ""
    text_raw: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    current_visibility: str = "visible"
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommentEvent:
    event_id: str
    comment_id: str
    observed_at: str
    event_type: str
    previous_state: str = ""
    current_state: str = ""
    source_ref: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GeoAnswer:
    answer_id: str
    content_id: str
    app: str
    question_raw: str
    captured_at: str
    mode: str = ""
    answer_hash: str = ""
    tools: list[Mapping[str, Any]] = field(default_factory=list)
    recommended: list[Mapping[str, Any]] = field(default_factory=list)
    source_ref: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        content_id: str,
        app: str,
        question_raw: str,
        captured_at: str,
        mode: str = "",
        answer_hash: str = "",
        tools: list[Mapping[str, Any]] | None = None,
        recommended: list[Mapping[str, Any]] | None = None,
        source_ref: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> "GeoAnswer":
        return cls(
            answer_id=generate_ulid_like("ans"),
            content_id=content_id,
            app=app,
            mode=mode,
            question_raw=question_raw,
            captured_at=captured_at,
            answer_hash=answer_hash,
            tools=list(tools or []),
            recommended=list(recommended or []),
            source_ref=source_ref,
            payload=payload or {},
        )


@dataclass(slots=True)
class GeoSourceRelation:
    relation_id: str
    answer_id: str
    relation_type: str
    source_content_id: str = ""
    position: int | None = None
    anchor_text: str = ""
    url_raw: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        answer_id: str,
        relation_type: str,
        source_content_id: str = "",
        position: int | None = None,
        anchor_text: str = "",
        url_raw: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> "GeoSourceRelation":
        return cls(
            relation_id=generate_ulid_like("rel"),
            answer_id=answer_id,
            source_content_id=source_content_id,
            relation_type=relation_type,
            position=position,
            anchor_text=anchor_text,
            url_raw=url_raw,
            payload=payload or {},
        )


@dataclass(slots=True)
class Signal:
    signal_id: str
    signal_type: str
    subject_type: str
    subject_id: str
    detected_at: str
    signal_date: str
    severity: float = 0.0
    value: float | None = None
    baseline_value: float | None = None
    model_version: str = "v3.2.0"
    status: str = "new"
    details: Mapping[str, Any] = field(default_factory=dict)
    consumed_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProductionJob:
    job_id: str
    job_type: str
    status: str
    created_at: str
    updated_at: str
    input_signal_ids: list[str] = field(default_factory=list)
    source_content_ids: list[str] = field(default_factory=list)
    output_content_id: str = ""
    scheduled_at: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


# ── 工程支持表 ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class IngestionBatch:
    batch_id: str
    adapter_key: str
    source_scope: str
    status: str  # queued | running | succeeded | partial_failed | failed | cancelled
    started_at: str = ""
    finished_at: str = ""
    records_seen: int = 0
    records_written: int = 0
    records_failed: int = 0
    source_ref: str = ""
    errors: list[Mapping[str, Any]] = field(default_factory=list)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, adapter_key: str, source_scope: str) -> "IngestionBatch":
        return cls(
            batch_id=generate_ulid_like("batch"),
            adapter_key=adapter_key,
            source_scope=source_scope,
            status="queued",
        )


@dataclass(slots=True)
class PublishAttempt:
    """发布尝试幂等记录。"""

    attempt_id: str
    account_id: str
    content_md_path: str
    idem_key: str
    status: str  # queued | running | succeeded | failed | cancelled
    started_at: str = ""
    finished_at: str = ""
    external_url: str = ""
    message_id: str = ""
    error: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        account_id: str,
        content_md_path: str,
        idem_key: str,
        details: Mapping[str, Any] | None = None,
    ) -> "PublishAttempt":
        return cls(
            attempt_id=generate_ulid_like("pub"),
            account_id=account_id,
            content_md_path=content_md_path,
            idem_key=idem_key,
            status="queued",
            details=details or {},
        )


# ── 工具函数 ──────────────────────────────────────────────────────────


def chunks(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """按 size 切片 yield 列表项。Repository 批量写入使用。"""
    if size <= 0:
        raise ValueError("size 必须 > 0")
    bucket: list[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def namespace_for_pair(system: str, kind: str) -> str:
    return namespace_for(system, kind)
