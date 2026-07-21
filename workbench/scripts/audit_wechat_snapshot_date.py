#!/usr/bin/env python3
"""Read-only WeChat source-date reconciliation audit.

The public product uses the canonical UTC calendar date of ``captured_at``.
This avoids converting late UTC snapshots into a future Asia/Shanghai label
while still preserving the full timestamp for audit.
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import zlib
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/hub/content_hub.sqlite"),
    )
    parser.add_argument("--target-date", default="2026-07-18")
    return parser.parse_args()


def projection_payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("__compressed_json__") != "zlib+base64":
        return value
    try:
        decoded = json.loads(
            zlib.decompress(base64.b64decode(value["data"])).decode("utf-8")
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        zlib.error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def main() -> None:
    args = parse_args()
    target = str(args.target_date)
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    effective = connection.execute(
        """
        SELECT k.keyword_id,k.keyword,MAX(ss.captured_at) AS latest
        FROM keywords k
        JOIN search_keyword_settings s
          ON s.keyword_id=k.keyword_id
         AND s.system_key='wechat-search'
         AND s.platform='wechat-search'
        LEFT JOIN search_snapshots ss
          ON ss.keyword_id=k.keyword_id
         AND ss.platform='wechat-search'
        WHERE k.platform='wechat-search'
          AND k.status='active'
          AND s.archived_at IS NULL
        GROUP BY k.keyword_id
        ORDER BY latest,k.keyword_id
        """
    ).fetchall()
    latest_distribution = Counter(
        str(row["latest"] or "")[:10] or "missing" for row in effective
    )
    lagging = [
        {
            "keyword_id": row["keyword_id"],
            "keyword": row["keyword"],
            "latest": row["latest"],
        }
        for row in effective
        if str(row["latest"] or "")[:10] < target
    ]

    projection_ids = {
        str(row["subject_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT subject_id
            FROM wechat_legacy_projections
            WHERE projection_kind='keyword'
            """
        )
    }
    missing_projections = [
        row["keyword_id"] for row in effective if row["keyword_id"] not in projection_ids
    ]

    content_ids = {
        str(row["content_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT h.content_id
            FROM search_hits h
            JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
            JOIN search_keyword_settings ks
              ON ks.keyword_id=s.keyword_id
             AND ks.system_key='wechat-search'
             AND ks.platform='wechat-search'
            WHERE s.platform='wechat-search'
              AND substr(s.captured_at,1,10)=?
              AND ks.archived_at IS NULL
              AND h.content_id IS NOT NULL
            """,
            (target,),
        )
    }
    metric_keys = {
        "read": ("wechat.read_count", "wechat.article.read_count"),
        "like": ("wechat.like_count", "wechat.article.like_count"),
        "friends_follow": (
            "wechat.friends_follow_count",
            "wechat.article.friends_follow_count",
        ),
        "original": (
            "wechat.original_article_count",
            "wechat.article.original_article_count",
        ),
    }
    metric_coverage: dict[str, dict[str, int | float]] = {}
    for name, keys in metric_keys.items():
        placeholders = ",".join("?" for _ in keys)
        observed = {
            str(row["subject_id"])
            for row in connection.execute(
                f"""
                SELECT DISTINCT subject_id
                FROM metric_observations
                WHERE subject_type='content'
                  AND metric_key IN ({placeholders})
                """,
                keys,
            )
        }
        count = len(content_ids & observed)
        total = len(content_ids)
        metric_coverage[name] = {
            "covered": count,
            "total": total,
            "ratio": round(count / total, 4) if total else 0.0,
        }

    full_row = connection.execute(
        """
        SELECT payload_json
        FROM wechat_legacy_projections
        WHERE projection_kind='full' AND subject_id=''
        ORDER BY updated_at DESC,projection_id DESC
        LIMIT 1
        """
    ).fetchone()
    full_payload = projection_payload(full_row["payload_json"]) if full_row else {}
    latest_accounts = {
        str(account.get("account_id") or account.get("name") or index): account
        for index, account in enumerate(full_payload.get("accounts") or [])
        if isinstance(account, dict)
    }
    score_fields = ("score", "timeliness_score", "today_score")
    score_coverage = {
        field: sum(payload.get(field) is not None for payload in latest_accounts.values())
        for field in score_fields
    }

    result = {
        "target_source_date": target,
        "effective_keyword_count": len(effective),
        "latest_date_distribution": dict(sorted(latest_distribution.items())),
        "lagging_keyword_count": len(lagging),
        "lagging_keywords": lagging,
        "keyword_projection_count": len(projection_ids),
        "missing_keyword_projections": missing_projections,
        "target_date_unique_content_count": len(content_ids),
        "metric_coverage": metric_coverage,
        "account_projection_count": len(latest_accounts),
        "account_score_coverage": score_coverage,
        "pass": (
            not lagging
            and not missing_projections
            and all(value == len(latest_accounts) for value in score_coverage.values())
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
