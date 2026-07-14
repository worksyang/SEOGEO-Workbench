#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核 2026-07-11 新增的 170 个关键词，并迁移为正式词/探针/归档词。

口径：
1. 只使用新增前（2026-07-05 至 2026-07-10）的成功 primary 快照；
2. 关联词是正式词证据，下拉词只作辅助，不单独晋级；
3. 跨至少 2 天的精确关联词可正式保留；单日但跨至少 3 个父词、出现至少
   4 次的突发新词也可正式保留；
4. 已有更准确正式词的重复表达直接归档，其余证据不足的词转入探针池；
5. 从未抓取过的词绝不处理。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_discovery_repo import probe_id_for


DB_PATH = ROOT / "data/state/app.db"
CANDIDATE_PATH = ROOT / "data/keyword_lists/2026-07-11_上涨候选词150.json"
WINDOW_START = "2026-07-05"
WINDOW_END = "2026-07-10"
OBSERVATION_DAYS = 15

# 已经有更准确、且具有关联词证据的正式表达时，不再浪费探针额度。
ARCHIVE_REPLACEMENTS = {
    "aia环宇盈活是什么产品": "AIA环宇盈活是什么产品（大小写重复）",
    "香港保险的信托功能": "香港保险信托功能",
    "香港保险类信托功能": "香港保险信托功能",
    "友邦环宇盈活介绍": "友邦环宇盈活产品介绍",
    "保诚信守明天回本期": "信守明天回本期",
    "永明星河尊享2提取密码": "星河尊享2提取密码",
    "周大福 匠心传承": "周大福保险匠心传承",
    "港险分红实现率": "港险的分红实现率",
}


def normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复核并迁移 2026-07-11 新增170词")
    parser.add_argument("--apply", action="store_true", help="写入生产 SQLite；默认仅预览")
    parser.add_argument(
        "--output-json",
        default=str(ROOT / "临时产物/260712_前天新增170词复核.json"),
    )
    parser.add_argument(
        "--output-md",
        default=str(ROOT / "临时产物/260712_前天新增170词复核.md"),
    )
    return parser.parse_args()


def load_target_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    manual_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM keyword_registry
            WHERE substr(created_at, 1, 16) = '2026-07-11T01:40'
            ORDER BY keyword_order, keyword_text
            """
        ).fetchall()
    ]
    source = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    candidate_texts = [
        str(item.get("keyword_text") or "").strip()
        for item in source.get("candidates", [])
        if str(item.get("keyword_text") or "").strip()
    ]
    placeholders = ",".join("?" for _ in candidate_texts)
    imported_by_text = {
        str(row["keyword_text"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM keyword_registry
            WHERE keyword_text IN ({placeholders})
            """,
            candidate_texts,
        ).fetchall()
    }
    if len(manual_rows) != 20:
        raise RuntimeError(f"预期凌晨人工预测词20个，实际{len(manual_rows)}个")
    if len(candidate_texts) != 150:
        raise RuntimeError(f"预期上涨候选词150个，实际{len(candidate_texts)}个")
    missing = [text for text in candidate_texts if text not in imported_by_text]
    if missing:
        raise RuntimeError(f"数据库缺少上涨候选词：{missing}")

    source_by_text = {
        str(item["keyword_text"]): str(item.get("source") or "term_rise")
        for item in source["candidates"]
    }
    rows: list[dict[str, Any]] = []
    for row in manual_rows:
        rows.append({**row, "review_source": "manual_prediction"})
    for text in candidate_texts:
        rows.append({**imported_by_text[text], "review_source": source_by_text[text]})
    if len(rows) != 170 or len({row["keyword_id"] for row in rows}) != 170:
        raise RuntimeError("170词范围不唯一，停止处理")
    unobserved = [row["keyword_text"] for row in rows if int(row["snapshot_count"] or 0) <= 0]
    if unobserved:
        raise RuntimeError(f"发现从未跑过的词，按保护规则停止：{unobserved}")
    return rows


def build_term_stats() -> dict[str, dict[str, Any]]:
    snapshots = json.loads((ROOT / "normalized/snapshots.json").read_text(encoding="utf-8"))
    terms = json.loads((ROOT / "normalized/snapshot_terms.json").read_text(encoding="utf-8"))
    selected = {
        str(item.get("snapshot_id") or ""): item
        for item in snapshots
        if item.get("is_primary")
        and str(item.get("status") or "success") == "success"
        and WINDOW_START <= str(item.get("snapshot_date") or "") <= WINDOW_END
    }
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "related_occurrences": 0,
            "related_dates": set(),
            "related_parents": set(),
            "suggestion_occurrences": 0,
            "suggestion_dates": set(),
            "suggestion_parents": set(),
            "best_related_position": None,
        }
    )
    for term in terms:
        snapshot = selected.get(str(term.get("snapshot_id") or ""))
        if not snapshot:
            continue
        key = normalize(term.get("term_text"))
        if not key:
            continue
        record = stats[key]
        term_type = str(term.get("term_type") or "")
        date = str(snapshot.get("snapshot_date") or "")
        parent = str(snapshot.get("keyword_id") or "")
        if term_type == "related":
            record["related_occurrences"] += 1
            record["related_dates"].add(date)
            record["related_parents"].add(parent)
            position = int(term.get("position") or 99)
            previous = record["best_related_position"]
            record["best_related_position"] = (
                position if previous is None else min(previous, position)
            )
        elif term_type == "suggestion":
            record["suggestion_occurrences"] += 1
            record["suggestion_dates"].add(date)
            record["suggestion_parents"].add(parent)
    return stats


def classify(row: dict[str, Any], stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    text = str(row["keyword_text"])
    evidence = stats[normalize(text)]
    related_occurrences = int(evidence["related_occurrences"])
    related_dates = len(evidence["related_dates"])
    related_parents = len(evidence["related_parents"] - {""})
    suggestion_occurrences = int(evidence["suggestion_occurrences"])
    suggestion_dates = len(evidence["suggestion_dates"])
    suggestion_parents = len(evidence["suggestion_parents"] - {""})

    replacement = ARCHIVE_REPLACEMENTS.get(text)
    if replacement:
        decision = "archive"
        reason = f"已有更准确正式词：{replacement}"
    elif (
        (related_occurrences >= 2 and related_dates >= 2)
        or (related_occurrences >= 4 and related_parents >= 3)
    ):
        decision = "formal"
        if related_dates >= 2:
            reason = f"精确关联词跨{related_dates}天出现{related_occurrences}次"
        else:
            reason = f"突发精确关联词单日跨{related_parents}个父词出现{related_occurrences}次"
    else:
        decision = "probe"
        if related_occurrences:
            reason = (
                f"精确关联词证据偏弱：{related_occurrences}次/"
                f"{related_dates}天/{related_parents}父词"
            )
        elif suggestion_occurrences:
            reason = f"只见下拉词{suggestion_occurrences}次，不能直接当真实搜索词"
        else:
            reason = "新增前未见精确关联词或下拉词证据"

    return {
        "keyword_id": row["keyword_id"],
        "keyword_text": text,
        "review_source": row["review_source"],
        "current_status": row["status"],
        "snapshot_count": int(row["snapshot_count"] or 0),
        "decision": decision,
        "reason": reason,
        "replacement": replacement,
        "related_occurrences": related_occurrences,
        "related_dates": related_dates,
        "related_parents": related_parents,
        "suggestion_occurrences": suggestion_occurrences,
        "suggestion_dates": suggestion_dates,
        "suggestion_parents": suggestion_parents,
        "best_related_position": evidence["best_related_position"],
        "created_at": row["created_at"],
    }


def apply_decisions(conn: sqlite3.Connection, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    applied = {"formal": 0, "probe": 0, "archive": 0, "probe_rows_created": 0}
    for item in decisions:
        decision = item["decision"]
        keyword_id = item["keyword_id"]
        text = item["keyword_text"]
        if decision == "formal":
            started = datetime.fromisoformat(str(item["created_at"]))
            deadline = started + timedelta(days=OBSERVATION_DAYS)
            conn.execute(
                """
                UPDATE keyword_registry
                SET status = 'active',
                    archived_at = NULL,
                    lifecycle_stage = 'observing',
                    observation_started_at = ?,
                    observation_deadline_at = ?,
                    refresh_frequency_days = 1,
                    refresh_frequency_source = 'auto',
                    refresh_policy_reason = ?,
                    archive_reason_code = NULL,
                    archive_reason_detail = NULL,
                    updated_at = ?
                WHERE keyword_id = ?
                """,
                (
                    started.isoformat(timespec="seconds"),
                    deadline.isoformat(timespec="seconds"),
                    f"新增关联词{OBSERVATION_DAYS}天每日观察；{item['reason']}",
                    now_iso,
                    keyword_id,
                ),
            )
            applied["formal"] += 1
            continue

        reason_code = (
            "duplicate_or_replaced"
            if decision == "archive"
            else "prediction_requires_probe"
        )
        conn.execute(
            """
            UPDATE keyword_registry
            SET status = 'archived',
                archived_at = COALESCE(archived_at, ?),
                lifecycle_stage = 'archived',
                is_pinned = 0,
                pin_order = NULL,
                archive_reason_code = ?,
                archive_reason_detail = ?,
                updated_at = ?
            WHERE keyword_id = ?
            """,
            (now_iso, reason_code, item["reason"], now_iso, keyword_id),
        )
        applied[decision] += 1
        if decision != "probe":
            continue

        brief_id = "20260711_added_170_recheck"
        probe_id = probe_id_for(
            brief_id=brief_id,
            source_article_id="",
            probe_text=text,
        )
        facts = [
            {
                "fact_type": "historical_term_evidence",
                "window": f"{WINDOW_START}~{WINDOW_END}",
                "related_occurrences": item["related_occurrences"],
                "related_dates": item["related_dates"],
                "related_parents": item["related_parents"],
                "suggestion_occurrences": item["suggestion_occurrences"],
                "suggestion_dates": item["suggestion_dates"],
                "suggestion_parents": item["suggestion_parents"],
            }
        ]
        conn.execute(
            """
            INSERT INTO keyword_discovery_probes (
                probe_id, probe_text, normalized_text, probe_type, status,
                source_brief_id, source_article_id, source_title, source_quote,
                warming_facts_json, proposed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'proposed', ?, '', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(probe_id) DO UPDATE SET
                probe_type = excluded.probe_type,
                source_title = excluded.source_title,
                source_quote = excluded.source_quote,
                warming_facts_json = excluded.warming_facts_json,
                status = CASE
                    WHEN keyword_discovery_probes.status IN ('replaced', 'archived')
                    THEN keyword_discovery_probes.status
                    ELSE 'proposed'
                END,
                updated_at = excluded.updated_at
            """,
            (
                probe_id,
                text,
                normalize(text).replace(" ", ""),
                "forecast_revalidation",
                brief_id,
                "2026-07-11新增170词复核",
                item["reason"],
                json.dumps(facts, ensure_ascii=False),
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        applied["probe_rows_created"] += 1
    return applied


def write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = payload["summary"]
    lines = [
        "# 2026-07-11新增170词复核",
        "",
        f"- 证据窗口：{WINDOW_START} 至 {WINDOW_END}，仅成功 primary 快照。",
        "- 关联词可作为正式词证据；下拉词只作辅助，不能单独晋级。",
        f"- 结论：正式词 **{summary['formal']}**，转探针 **{summary['probe']}**，直接归档 **{summary['archive']}**。",
        f"- 未跑过词：**{summary['unobserved']}**（必须为0）。",
        "",
    ]
    for decision, title in (
        ("formal", "正式关联词"),
        ("probe", "转为探针"),
        ("archive", "直接归档"),
    ):
        lines += [
            f"## {title}",
            "",
            "| 关键词 | 关联次数/天/父词 | 下拉次数 | 结论依据 |",
            "|---|---:|---:|---|",
        ]
        for item in payload["decisions"]:
            if item["decision"] != decision:
                continue
            lines.append(
                f"| {item['keyword_text']} | {item['related_occurrences']}/"
                f"{item['related_dates']}/{item['related_parents']} | "
                f"{item['suggestion_occurrences']} | {item['reason']} |"
            )
        lines.append("")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_target_rows(conn)
    stats = build_term_stats()
    decisions = [classify(row, stats) for row in rows]
    summary = {
        "total": len(decisions),
        "formal": sum(item["decision"] == "formal" for item in decisions),
        "probe": sum(item["decision"] == "probe" for item in decisions),
        "archive": sum(item["decision"] == "archive" for item in decisions),
        "unobserved": sum(item["snapshot_count"] <= 0 for item in decisions),
    }
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "applied": False,
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "rules": {
            "formal": "关联词>=2次且跨>=2天；或单日关联词>=4次且跨>=3父词",
            "probe": "未达到正式词门槛，且没有明确替代词",
            "archive": "已有更准确正式词或规范化重复",
            "unobserved_protection": True,
        },
        "summary": summary,
        "decisions": decisions,
    }

    if args.apply:
        backup_dir = ROOT / "data/backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"app.db.before_170_keyword_reclass_{stamp}"
        shutil.copy2(DB_PATH, backup_path)
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            applied = apply_decisions(conn, decisions)
            conn.commit()
        payload["applied"] = True
        payload["applied_counts"] = applied
        payload["backup_path"] = str(backup_path)

    write_reports(
        payload,
        Path(args.output_json).expanduser().resolve(),
        Path(args.output_md).expanduser().resolve(),
    )
    print(json.dumps({**summary, "applied": payload["applied"]}, ensure_ascii=False))
    if payload.get("backup_path"):
        print(f"backup={payload['backup_path']}")
    print(f"json={Path(args.output_json).expanduser().resolve()}")
    print(f"md={Path(args.output_md).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
