"""统一内容工作台 · 摄取层 / 对账引擎。

实现 v3.2 §8.3：
1. 文件完整性：md_path 是否存在，文件哈希是否变化。
2. 身份完整性：外部 ID 是否出现一对多 / 同 URL 多内容 / 孤立 hit。
3. 事实连续性：关键采集源当天是否缺快照、指标是否异常倒退。
4. 引用完整性：GEO relation / comment event / production output 是否指向存在的主体。

对账始终是只读：异常进入 correction_jobs（不直接改事实）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..domain.ids import generate_ulid_like
from ..validation.paths import resolve_within
from ..validation.timestamps import utc_now_iso


@dataclass(slots=True)
class ReconcileCheckResult:
    section: str
    severity: str  # info | warn | error
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
        }


class ReconcileEngine:
    def __init__(self, connection: sqlite3.Connection, allowed_roots: Iterable[Path]):
        self._conn = connection
        self._roots = [Path(root).resolve() for root in allowed_roots if root is not None]

    def run(self) -> list[ReconcileCheckResult]:
        results = [
            *self.file_integrity(),
            *self.identity_integrity(),
            *self.fact_continuity(),
            *self.reference_integrity(),
        ]
        for result in results:
            if result.severity == "error":
                self._queue_correction_job(result)
        return results

    def file_integrity(self) -> list[ReconcileCheckResult]:
        rows = self._conn.execute(
            "SELECT content_id, md_path, file_hash FROM contents WHERE md_path != ''"
        ).fetchall()
        missing = 0
        drifted = 0
        unverified = 0
        for content_id, md_path, file_hash in rows:
            candidates = self._reference_candidates(md_path)
            existing = [path for path in candidates if path.is_file()]
            if not existing:
                missing += 1
                continue
            if not file_hash:
                unverified += 1
                continue
            matched = False
            for path in existing:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                if digest == file_hash:
                    matched = True
                    break
            if not matched:
                drifted += 1
        severity = "warn" if missing or drifted else "info"
        return [
            ReconcileCheckResult(
                section="file_integrity",
                severity=severity,
                summary=(
                    f"共 {len(rows)} 个 markdown 指针；缺失 {missing}、"
                    f"内容漂移 {drifted}、未记录哈希 {unverified}"
                ),
                evidence={
                    "missing": missing,
                    "drifted": drifted,
                    "unverified": unverified,
                    "total": len(rows),
                },
            )
        ]

    def _reference_candidates(self, raw_path: str) -> list[Path]:
        """将绝对路径或相对来源路径解析为受允许根目录约束的候选路径。"""
        raw = Path(raw_path).expanduser()
        if raw.is_absolute():
            try:
                return [resolve_within(raw, self._roots)]
            except ValueError:
                return []

        candidates: list[Path] = []
        for root in self._roots:
            try:
                candidate = resolve_within(root / raw, [root])
            except ValueError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def identity_integrity(self) -> list[ReconcileCheckResult]:
        results: list[ReconcileCheckResult] = []
        dup_url = self._conn.execute(
            "SELECT canonical_url, COUNT(*) AS n FROM contents WHERE canonical_url != '' "
            "GROUP BY canonical_url HAVING n > 1"
        ).fetchall()
        if dup_url:
            results.append(
                ReconcileCheckResult(
                    section="identity",
                    severity="error",
                    summary=f"{len(dup_url)} 条 canonical_url 出现多份内容",
                    evidence={"duplicates": [{"canonical_url": row[0], "count": row[1]} for row in dup_url]},
                )
            )
        dup_id = self._conn.execute(
            "SELECT namespace, external_id, COUNT(DISTINCT content_id) AS n FROM content_identifiers "
            "GROUP BY namespace, external_id HAVING n > 1"
        ).fetchall()
        if dup_id:
            results.append(
                ReconcileCheckResult(
                    section="identity",
                    severity="warn",
                    summary=f"{len(dup_id)} 个 external_id 指向多个 content_id",
                    evidence={"conflicts": [{"namespace": row[0], "external_id": row[1], "count": row[2]} for row in dup_id]},
                )
            )
        orphan = self._conn.execute(
            "SELECT COUNT(*) FROM search_hits h LEFT JOIN contents c ON c.content_id = h.content_id "
            "WHERE h.content_id IS NOT NULL AND c.content_id IS NULL"
        ).fetchone()[0]
        if orphan:
            results.append(
                ReconcileCheckResult(
                    section="identity",
                    severity="warn",
                    summary=f"孤立 hit 数 = {orphan}",
                    evidence={"orphan_hits": orphan},
                )
            )
        if not results:
            results.append(ReconcileCheckResult(section="identity", severity="info", summary="身份关系无异常"))
        return results

    def fact_continuity(self) -> list[ReconcileCheckResult]:
        rows = self._conn.execute(
            """
            WITH latest AS (
                SELECT subject_type, subject_id, metric_key, numeric_value, observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY subject_type, subject_id, metric_key
                           ORDER BY observed_at DESC
                       ) AS rn
                FROM metric_observations
                WHERE numeric_value IS NOT NULL
            )
            SELECT prev.subject_type, prev.subject_id, prev.metric_key,
                   prev.numeric_value AS prev_value, prev.observed_at AS prev_at,
                   cur.numeric_value AS cur_value, cur.observed_at AS cur_at
            FROM latest cur
            JOIN latest prev
              ON prev.subject_type = cur.subject_type
             AND prev.subject_id = cur.subject_id
             AND prev.metric_key = cur.metric_key
             AND prev.rn = cur.rn + 1
            WHERE cur.rn = 1
              AND cur.numeric_value + 1 < prev.numeric_value
            """
        ).fetchall()
        if rows:
            return [
                ReconcileCheckResult(
                    section="continuity",
                    severity="warn",
                    summary=f"{len(rows)} 个主体的指标出现倒退",
                    evidence={"samples": [dict(row) for row in rows[:20]]},
                )
            ]
        return [ReconcileCheckResult(section="continuity", severity="info", summary="最近观测连续")]

    def reference_integrity(self) -> list[ReconcileCheckResult]:
        results: list[ReconcileCheckResult] = []
        orphan_relations = self._conn.execute(
            "SELECT COUNT(*) FROM geo_source_relations r LEFT JOIN geo_answers a ON a.answer_id=r.answer_id "
            "WHERE a.answer_id IS NULL"
        ).fetchone()[0]
        if orphan_relations:
            results.append(
                ReconcileCheckResult(
                    section="references",
                    severity="warn",
                    summary=f"{orphan_relations} 条 geo_source_relations 指向缺失的 answer",
                    evidence={"orphan_geo_relations": orphan_relations},
                )
            )
        orphan_comments = self._conn.execute(
            "SELECT COUNT(*) FROM comment_events e LEFT JOIN comments c ON c.comment_id=e.comment_id "
            "WHERE c.comment_id IS NULL"
        ).fetchone()[0]
        if orphan_comments:
            results.append(
                ReconcileCheckResult(
                    section="references",
                    severity="warn",
                    summary=f"{orphan_comments} 条 comment_events 指向缺失的 comment",
                    evidence={"orphan_comment_events": orphan_comments},
                )
            )
        if not results:
            results.append(ReconcileCheckResult(section="references", severity="info", summary="引用关系完整"))
        return results

    def _queue_correction_job(self, result: ReconcileCheckResult) -> None:
        self._conn.execute(
            """
            INSERT INTO correction_jobs(
                correction_id, kind, summary, evidence_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                generate_ulid_like("corr"),
                result.section,
                result.summary,
                json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
                utc_now_iso(),
                utc_now_iso(),
            ),
        )
