"""统一内容工作台 · 摄取层 / 身份解析器。

实现 v3.2 §十一与 dev-plan §4.3：
- 优先级：稳定外部 ID → 规范化 URL → 账号+文章 ID → 正文哈希 → 文本相似候选。
- 低置信结果写到 identity_merge_candidates，不直接合并。
- 合并进入 identity_merge_map，保留旧 ID。
- 同标题不同作者不自动合并。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..domain.ids import (
    canonicalize_url,
    content_id_from_canonical_url,
    content_id_from_text,
)
from ..validation.timestamps import utc_now_iso

AUTOMERGE_MIN_CONFIDENCE: float = 0.85


@dataclass(slots=True)
class ResolverDecision:
    content_id: str
    confidence: float
    method: str  # namespace | url | url_new | composite | hash | title_candidate | placeholder
    matched_namespace: str = ""
    matched_external_id: str = ""
    evidence: dict[str, str] = field(default_factory=dict)
    requires_human: bool = False


@dataclass(slots=True)
class ResolverContext:
    namespace: str = ""
    external_id: str = ""
    canonical_url: str = ""
    account_id: str = ""
    article_id: str = ""
    title: str = ""
    author_name: str = ""
    content_text: str = ""
    published_at: str = ""

    def normalized_url(self) -> str:
        return canonicalize_url(self.canonical_url) if self.canonical_url else ""


class IdentityResolver:
    """在 SQLite 中按优先级匹配身份；缺失证据时返回 requires_human=True。"""

    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def resolve(self, ctx: ResolverContext) -> ResolverDecision:
        # 1. namespace + external_id
        if ctx.namespace and ctx.external_id:
            row = self._conn.execute(
                "SELECT content_id FROM content_identifiers WHERE namespace=? AND external_id=?",
                (ctx.namespace, ctx.external_id),
            ).fetchone()
            if row:
                return ResolverDecision(
                    content_id=row[0],
                    confidence=1.0,
                    method="namespace",
                    matched_namespace=ctx.namespace,
                    matched_external_id=ctx.external_id,
                    evidence={"namespace": ctx.namespace, "external_id": ctx.external_id},
                )
        # 2. canonical URL
        canonical = ctx.normalized_url()
        if canonical:
            derived = content_id_from_canonical_url(canonical)
            row = self._conn.execute(
                "SELECT content_id FROM contents WHERE canonical_url=?",
                (canonical,),
            ).fetchone()
            if row:
                return ResolverDecision(
                    content_id=row[0],
                    confidence=0.95,
                    method="url",
                    matched_namespace=ctx.namespace or "url",
                    matched_external_id=canonical,
                    evidence={"canonical_url": canonical},
                )
            return ResolverDecision(
                content_id=derived,
                confidence=0.9,
                method="url_new",
                matched_namespace=ctx.namespace or "url",
                matched_external_id=canonical,
                evidence={"canonical_url": canonical},
            )
        # 3. composite: account + article
        if ctx.account_id and ctx.article_id:
            composite_key = f"{ctx.account_id}::{ctx.article_id}"
            row = self._conn.execute(
                "SELECT content_id FROM content_identifiers WHERE external_id=?",
                (composite_key,),
            ).fetchone()
            if row:
                return ResolverDecision(
                    content_id=row[0],
                    confidence=0.92,
                    method="composite",
                    matched_namespace=ctx.namespace or "composite",
                    matched_external_id=composite_key,
                    evidence={"account_id": ctx.account_id, "article_id": ctx.article_id},
                )
        # 4. hash: 全文完全相同
        if ctx.content_text:
            digest = hashlib.sha256(ctx.content_text.encode("utf-8")).hexdigest()
            row = self._conn.execute(
                "SELECT content_id FROM contents WHERE content_hash=?",
                (digest,),
            ).fetchone()
            if row:
                return ResolverDecision(
                    content_id=row[0],
                    confidence=0.88,
                    method="hash",
                    evidence={"content_hash": digest},
                )
        # 5. 候选：仅标题+作者完全相同，requires_human
        title = (ctx.title or "").strip()
        author = (ctx.author_name or "").strip()
        if title and author:
            row = self._conn.execute(
                "SELECT content_id, author_name FROM contents WHERE title=?",
                (title,),
            ).fetchone()
            if row and (row[1] or "").strip() == author:
                return ResolverDecision(
                    content_id=row[0],
                    confidence=0.8,
                    method="title_candidate",
                    evidence={"title": title, "author_name": author},
                    requires_human=True,
                )
        # 缺证据：使用 content_id_from_text
        text_seed = f"{ctx.title or ''}\n{ctx.author_name or ''}\n{ctx.published_at or ''}".strip()
        fallback = content_id_from_text("placeholder", text_seed)
        return ResolverDecision(
            content_id=fallback,
            confidence=0.5,
            method="placeholder",
            evidence={"placeholder": text_seed},
            requires_human=True,
        )

    def record_evidence(self, decision: ResolverDecision, ctx: ResolverContext | None = None) -> None:
        """把解析路径写入 identity_merge_candidates，未达阈值的等待人工处理。"""
        action = "auto" if decision.confidence >= AUTOMERGE_MIN_CONFIDENCE and not decision.requires_human else "candidate"
        self._conn.execute(
            """
            INSERT OR REPLACE INTO identity_merge_candidates(
                candidate_id, candidate_content_id, evidence_method, confidence,
                matched_namespace, matched_external_id, evidence_json, action,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{decision.content_id}::{decision.method}",
                decision.content_id,
                decision.method,
                decision.confidence,
                decision.matched_namespace or "",
                decision.matched_external_id or "",
                json.dumps(decision.evidence, ensure_ascii=False, sort_keys=True),
                action,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )

    def merge(self, old_id: str, new_id: str, operator: str, reason: str = "") -> str:
        if old_id == new_id:
            return new_id
        cur = self._conn.execute(
            "SELECT 1 FROM identity_merge_map WHERE old_content_id=? AND new_content_id=?",
            (old_id, new_id),
        ).fetchone()
        if cur:
            return new_id
        self._conn.execute(
            """
            INSERT INTO identity_merge_map(
                merge_id, old_content_id, new_content_id, operator, reason, merged_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"merge::{old_id}::{new_id}",
                old_id,
                new_id,
                operator,
                reason,
                utc_now_iso(),
            ),
        )
        rows = self._conn.execute(
            "SELECT namespace, external_id, first_seen_at FROM content_identifiers WHERE content_id=?",
            (old_id,),
        ).fetchall()
        for namespace, external_id, first_seen_at in rows:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO content_identifiers(namespace, external_id, content_id, first_seen_at) VALUES (?, ?, ?, ?)",
                    (namespace, external_id, new_id, first_seen_at),
                )
            except sqlite3.IntegrityError:
                pass
        return new_id

    def lookup_alias(self, old_id: str) -> str:
        row = self._conn.execute(
            "SELECT new_content_id FROM identity_merge_map WHERE old_content_id=? ORDER BY merged_at DESC LIMIT 1",
            (old_id,),
        ).fetchone()
        return row[0] if row else old_id
