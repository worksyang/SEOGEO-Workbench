from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _normalized(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def _stable_id(prefix: str, *parts: Any, length: int = 16) -> str:
    payload = "||".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:length]}"


def probe_id_for(
    *,
    brief_id: str,
    source_article_id: str,
    probe_text: str,
) -> str:
    return _stable_id("probe", brief_id, source_article_id, _normalized(probe_text))


def candidate_id_for(candidate_text: str) -> str:
    return _stable_id("cand", _normalized(candidate_text))


class KeywordDiscoveryRepository:
    """Persistent candidate state machine, separate from active keyword facts."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS keyword_discovery_probes (
                    probe_id TEXT PRIMARY KEY,
                    probe_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    probe_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    source_brief_id TEXT NOT NULL DEFAULT '',
                    source_article_id TEXT NOT NULL DEFAULT '',
                    source_title TEXT NOT NULL DEFAULT '',
                    source_quote TEXT NOT NULL DEFAULT '',
                    warming_facts_json TEXT NOT NULL DEFAULT '[]',
                    proposed_at TEXT NOT NULL,
                    queued_at TEXT,
                    searched_at TEXT,
                    search_batch_id TEXT,
                    replacement_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                    archived_at TEXT,
                    archive_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_probes_status
                    ON keyword_discovery_probes(status, proposed_at);
                CREATE INDEX IF NOT EXISTS idx_discovery_probes_batch
                    ON keyword_discovery_probes(search_batch_id);

                CREATE TABLE IF NOT EXISTS keyword_discovery_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    candidate_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    first_related_at TEXT,
                    source_probe_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_article_ids_json TEXT NOT NULL DEFAULT '[]',
                    related_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    related_parent_probe_count INTEGER NOT NULL DEFAULT 0,
                    related_date_count INTEGER NOT NULL DEFAULT 0,
                    suggestion_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    best_related_position INTEGER,
                    historical_related_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    historical_related_parent_count INTEGER NOT NULL DEFAULT 0,
                    historical_related_date_count INTEGER NOT NULL DEFAULT 0,
                    historical_suggestion_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    validation_batch_id TEXT,
                    validation_queued_at TEXT,
                    validated_at TEXT,
                    validation_round_count INTEGER NOT NULL DEFAULT 0,
                    source_article_best_rank INTEGER,
                    validation_result_count INTEGER NOT NULL DEFAULT 0,
                    validation_score REAL NOT NULL DEFAULT 0,
                    business_value_score INTEGER NOT NULL DEFAULT 5,
                    business_value_reason TEXT NOT NULL DEFAULT '',
                    observation_started_at TEXT,
                    observation_deadline_at TEXT,
                    promoted_keyword_id TEXT,
                    rejected_at TEXT,
                    rejection_reason TEXT,
                    archived_at TEXT,
                    archive_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_candidates_status
                    ON keyword_discovery_candidates(status, first_seen_at);
                CREATE INDEX IF NOT EXISTS idx_discovery_candidates_batch
                    ON keyword_discovery_candidates(validation_batch_id);

                CREATE TABLE IF NOT EXISTS keyword_discovery_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    probe_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    evidence_date TEXT NOT NULL,
                    term_type TEXT NOT NULL,
                    position INTEGER,
                    source_article_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES keyword_discovery_candidates(candidate_id),
                    FOREIGN KEY(probe_id) REFERENCES keyword_discovery_probes(probe_id)
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_evidence_candidate
                    ON keyword_discovery_evidence(candidate_id, evidence_date);
                CREATE INDEX IF NOT EXISTS idx_discovery_evidence_probe
                    ON keyword_discovery_evidence(probe_id, evidence_date);
                """
            )
            self._ensure_column(
                conn,
                "keyword_discovery_candidates",
                "historical_related_occurrence_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                "keyword_discovery_candidates",
                "historical_related_parent_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                "keyword_discovery_candidates",
                "historical_related_date_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                "keyword_discovery_candidates",
                "historical_suggestion_occurrence_count",
                "INTEGER NOT NULL DEFAULT 0",
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _decode_json(value: Any, default: Any) -> Any:
        try:
            parsed = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return parsed

    @classmethod
    def _probe_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "warming_facts": cls._decode_json(row["warming_facts_json"], []),
            "replacement_candidate_ids": cls._decode_json(
                row["replacement_candidate_ids_json"],
                [],
            ),
        }

    @classmethod
    def _candidate_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "source_probe_ids": cls._decode_json(row["source_probe_ids_json"], []),
            "source_article_ids": cls._decode_json(row["source_article_ids_json"], []),
        }

    def upsert_probe(
        self,
        *,
        brief_id: str,
        source_article_id: str,
        source_title: str,
        probe_text: str,
        probe_type: str,
        source_quote: str,
        warming_facts: list[Any],
        proposed_at: str,
    ) -> dict[str, Any]:
        text = str(probe_text or "").strip()
        if not text:
            raise ValueError("probe text is required")
        probe_id = probe_id_for(
            brief_id=brief_id,
            source_article_id=source_article_id,
            probe_text=text,
        )
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keyword_discovery_probes (
                    probe_id, probe_text, normalized_text, probe_type, status,
                    source_brief_id, source_article_id, source_title, source_quote,
                    warming_facts_json, proposed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(probe_id) DO UPDATE SET
                    source_title = excluded.source_title,
                    source_quote = excluded.source_quote,
                    warming_facts_json = excluded.warming_facts_json,
                    updated_at = excluded.updated_at
                """,
                (
                    probe_id,
                    text,
                    _normalized(text),
                    str(probe_type or "").strip(),
                    str(brief_id or "").strip(),
                    str(source_article_id or "").strip(),
                    str(source_title or "").strip(),
                    str(source_quote or "").strip(),
                    json.dumps(warming_facts or [], ensure_ascii=False),
                    str(proposed_at or now),
                    now,
                    now,
                ),
            )
        return self.get_probe(probe_id) or {}

    def get_probe(self, probe_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keyword_discovery_probes WHERE probe_id = ?",
                (probe_id,),
            ).fetchone()
        return self._probe_row(row) if row else None

    def list_probes(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        statuses = [str(item) for item in (statuses or []) if str(item)]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(statuses)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM keyword_discovery_probes
                {where}
                ORDER BY proposed_at, probe_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._probe_row(row) for row in rows]

    def mark_probes_queued(
        self,
        probe_ids: list[str],
        *,
        batch_id: str,
        queued_at: str,
    ) -> int:
        if not probe_ids:
            return 0
        updated = 0
        with self._connect() as conn:
            for probe_id in probe_ids:
                result = conn.execute(
                    """
                    UPDATE keyword_discovery_probes
                    SET status = 'queued', search_batch_id = ?, queued_at = ?, updated_at = ?
                    WHERE probe_id = ? AND status = 'proposed'
                    """,
                    (batch_id, queued_at, queued_at, probe_id),
                )
                updated += int(result.rowcount or 0)
        return updated

    def mark_probe_searched(self, probe_id: str, *, searched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_probes
                SET status = 'searched', searched_at = ?, updated_at = ?
                WHERE probe_id = ? AND status IN ('queued', 'proposed')
                """,
                (searched_at, searched_at, probe_id),
            )

    def archive_probe_as_replaced(
        self,
        probe_id: str,
        *,
        candidate_ids: list[str],
        archived_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_probes
                SET status = 'replaced',
                    replacement_candidate_ids_json = ?,
                    archived_at = ?,
                    archive_reason = '关联词中已找到更适配的真实候选词',
                    updated_at = ?
                WHERE probe_id = ?
                """,
                (
                    json.dumps(sorted(set(candidate_ids)), ensure_ascii=False),
                    archived_at,
                    archived_at,
                    probe_id,
                ),
            )

    def reject_probe(self, probe_id: str, *, reason: str, archived_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_probes
                SET status = 'rejected', archived_at = ?, archive_reason = ?, updated_at = ?
                WHERE probe_id = ?
                """,
                (archived_at, str(reason or "").strip(), archived_at, probe_id),
            )

    def upsert_candidate_evidence(
        self,
        *,
        candidate_text: str,
        probe_id: str,
        snapshot_id: str,
        evidence_date: str,
        term_type: str,
        position: int | None,
        source_article_id: str,
        observed_at: str,
        business_value_score: int,
        business_value_reason: str,
    ) -> dict[str, Any]:
        text = str(candidate_text or "").strip()
        if not text:
            raise ValueError("candidate text is required")
        candidate_id = candidate_id_for(text)
        evidence_id = _stable_id(
            "evidence",
            candidate_id,
            probe_id,
            snapshot_id,
            term_type,
            position,
        )
        now = self._now()
        with self._connect() as conn:
            probe = conn.execute(
                "SELECT source_article_id FROM keyword_discovery_probes WHERE probe_id = ?",
                (probe_id,),
            ).fetchone()
            if not probe:
                raise FileNotFoundError(f"probe not found: {probe_id}")
            existing = conn.execute(
                """
                SELECT source_probe_ids_json, source_article_ids_json
                FROM keyword_discovery_candidates WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            probe_ids = set(self._decode_json(existing["source_probe_ids_json"], [])) if existing else set()
            article_ids = (
                set(self._decode_json(existing["source_article_ids_json"], []))
                if existing
                else set()
            )
            probe_ids.add(probe_id)
            if source_article_id:
                article_ids.add(source_article_id)
            conn.execute(
                """
                INSERT INTO keyword_discovery_candidates (
                    candidate_id, candidate_text, normalized_text, status,
                    first_seen_at, last_seen_at, first_related_at,
                    source_probe_ids_json, source_article_ids_json,
                    business_value_score, business_value_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    candidate_text = excluded.candidate_text,
                    status = CASE
                        WHEN keyword_discovery_candidates.status = 'suggestion_only'
                         AND excluded.status = 'discovered'
                        THEN 'discovered'
                        ELSE keyword_discovery_candidates.status
                    END,
                    last_seen_at = excluded.last_seen_at,
                    first_related_at = COALESCE(
                        keyword_discovery_candidates.first_related_at,
                        excluded.first_related_at
                    ),
                    source_probe_ids_json = excluded.source_probe_ids_json,
                    source_article_ids_json = excluded.source_article_ids_json,
                    business_value_score = excluded.business_value_score,
                    business_value_reason = excluded.business_value_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    candidate_id,
                    text,
                    _normalized(text),
                    "discovered" if term_type == "related" else "suggestion_only",
                    observed_at,
                    observed_at,
                    observed_at if term_type == "related" else None,
                    json.dumps(sorted(probe_ids), ensure_ascii=False),
                    json.dumps(sorted(article_ids), ensure_ascii=False),
                    int(business_value_score),
                    str(business_value_reason or ""),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO keyword_discovery_evidence (
                    evidence_id, candidate_id, probe_id, snapshot_id,
                    evidence_date, term_type, position, source_article_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    candidate_id,
                    probe_id,
                    snapshot_id,
                    evidence_date,
                    term_type,
                    position,
                    source_article_id,
                    now,
                ),
            )
        self.refresh_candidate_aggregates(candidate_id)
        return self.get_candidate(candidate_id) or {}

    def refresh_candidate_aggregates(self, candidate_id: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN term_type = 'related' THEN 1 ELSE 0 END) AS related_count,
                    COUNT(DISTINCT CASE WHEN term_type = 'related' THEN probe_id END) AS parent_count,
                    COUNT(DISTINCT CASE WHEN term_type = 'related' THEN evidence_date END) AS date_count,
                    SUM(CASE WHEN term_type = 'suggestion' THEN 1 ELSE 0 END) AS suggestion_count,
                    MIN(CASE WHEN term_type = 'related' THEN position END) AS best_position
                FROM keyword_discovery_evidence
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET related_occurrence_count = ?,
                    related_parent_probe_count = ?,
                    related_date_count = ?,
                    suggestion_occurrence_count = ?,
                    best_related_position = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    int(row["related_count"] or 0),
                    int(row["parent_count"] or 0),
                    int(row["date_count"] or 0),
                    int(row["suggestion_count"] or 0),
                    row["best_position"],
                    self._now(),
                    candidate_id,
                ),
            )

    def set_candidate_historical_evidence(
        self,
        candidate_id: str,
        *,
        related_occurrences: int,
        related_parents: int,
        related_dates: int,
        suggestion_occurrences: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET historical_related_occurrence_count = ?,
                    historical_related_parent_count = ?,
                    historical_related_date_count = ?,
                    historical_suggestion_occurrence_count = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    max(0, int(related_occurrences)),
                    max(0, int(related_parents)),
                    max(0, int(related_dates)),
                    max(0, int(suggestion_occurrences)),
                    self._now(),
                    candidate_id,
                ),
            )

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keyword_discovery_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return self._candidate_row(row) if row else None

    def get_candidate_by_text(self, candidate_text: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keyword_discovery_candidates
                WHERE normalized_text = ?
                """,
                (_normalized(candidate_text),),
            ).fetchone()
        return self._candidate_row(row) if row else None

    def list_candidates(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        statuses = [str(item) for item in (statuses or []) if str(item)]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(statuses)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM keyword_discovery_candidates
                {where}
                ORDER BY validation_score DESC, related_parent_probe_count DESC,
                         related_occurrence_count DESC, first_seen_at
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def queue_candidate_validation(
        self,
        candidate_ids: list[str],
        *,
        batch_id: str,
        queued_at: str,
    ) -> int:
        updated = 0
        with self._connect() as conn:
            for candidate_id in candidate_ids:
                result = conn.execute(
                    """
                    UPDATE keyword_discovery_candidates
                    SET status = 'validation_queued',
                        validation_batch_id = ?,
                        validation_queued_at = ?,
                        updated_at = ?
                    WHERE candidate_id = ?
                      AND status IN ('discovered', 'validation_failed')
                    """,
                    (batch_id, queued_at, queued_at, candidate_id),
                )
                updated += int(result.rowcount or 0)
        return updated

    def mark_candidate_validated(
        self,
        candidate_id: str,
        *,
        validated_at: str,
        source_article_best_rank: int | None,
        validation_result_count: int,
        validation_score: float,
        accepted: bool,
        rejection_reason: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET status = ?,
                    validated_at = ?,
                    validation_round_count = validation_round_count + 1,
                    source_article_best_rank = ?,
                    validation_result_count = ?,
                    validation_score = ?,
                    rejected_at = CASE WHEN ? THEN NULL ELSE ? END,
                    rejection_reason = CASE WHEN ? THEN NULL ELSE ? END,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    "validated" if accepted else "rejected",
                    validated_at,
                    source_article_best_rank,
                    int(validation_result_count),
                    float(validation_score),
                    1 if accepted else 0,
                    validated_at,
                    1 if accepted else 0,
                    str(rejection_reason or "").strip(),
                    validated_at,
                    candidate_id,
                ),
            )

    def mark_candidate_observing(
        self,
        candidate_id: str,
        *,
        keyword_id: str,
        observation_started_at: str,
        observation_deadline_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET status = 'observing',
                    promoted_keyword_id = ?,
                    observation_started_at = ?,
                    observation_deadline_at = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    keyword_id,
                    observation_started_at,
                    observation_deadline_at,
                    observation_started_at,
                    candidate_id,
                ),
            )

    def mark_candidate_matured(self, candidate_id: str, *, matured_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET status = 'matured', updated_at = ?
                WHERE candidate_id = ? AND status = 'observing'
                """,
                (matured_at, candidate_id),
            )

    def archive_candidate(
        self,
        candidate_id: str,
        *,
        archived_at: str,
        reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE keyword_discovery_candidates
                SET status = 'archived',
                    archived_at = ?,
                    archive_reason = ?,
                    updated_at = ?
                WHERE candidate_id = ?
                """,
                (archived_at, str(reason or "").strip(), archived_at, candidate_id),
            )

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            probe_rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM keyword_discovery_probes GROUP BY status"
            ).fetchall()
            candidate_rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM keyword_discovery_candidates GROUP BY status"
            ).fetchall()
        return {
            "probes": {row["status"]: int(row["total"]) for row in probe_rows},
            "candidates": {row["status"]: int(row["total"]) for row in candidate_rows},
        }
