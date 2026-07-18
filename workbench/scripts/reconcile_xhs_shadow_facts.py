from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from typing import Any

from content_hub.adapters.xhs_search_provider import normalize_search_notes_response
from content_hub.config import Settings
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.features.xhs.service import _id, _json, _merge_payload, _number, _time


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalized_hits(settings: Settings) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with connect(settings, readonly=True) as con:
        for response in con.execute(
            "SELECT response_id,payload_json FROM xhs_shadow_responses ORDER BY captured_at"
        ):
            converted = normalize_search_notes_response(_decode(response["payload_json"]))
            if not converted.get("envelope_valid"):
                continue
            snapshot = con.execute(
                """SELECT snapshot_id,keyword_id,source_ref
                   FROM search_snapshots
                   WHERE source_ref=? AND platform='xiaohongshu'
                   ORDER BY captured_at DESC LIMIT 1""",
                (f"xhs-shadow://{response['response_id']}",),
            ).fetchone()
            if not snapshot:
                continue
            hit_rows = con.execute(
                "SELECT hit_id,rank,content_id,payload_json FROM search_hits WHERE snapshot_id=?",
                (snapshot["snapshot_id"],),
            ).fetchall()
            by_identity: dict[str, dict[str, Any]] = {}
            for item in converted["hits"]:
                note_id = str(item.get("note_id") or "").strip()
                url = str(item.get("canonical_url") or "").strip()
                if note_id:
                    by_identity[f"id:{note_id}"] = item
                if url:
                    by_identity[f"url:{url}"] = item
            for hit in hit_rows:
                stored = _decode(hit["payload_json"])
                note_id = str(stored.get("note_id") or "").strip()
                item = by_identity.get(f"id:{note_id}") if note_id else None
                if item is None:
                    url = str(stored.get("canonical_url") or stored.get("url_raw") or "").strip()
                    item = by_identity.get(f"url:{url}") if url else None
                if item is None:
                    continue
                rows.append(
                    {
                        "snapshot_id": snapshot["snapshot_id"],
                        "keyword_id": snapshot["keyword_id"],
                        "response_id": response["response_id"],
                        "hit_id": hit["hit_id"],
                        "content_id": hit["content_id"],
                        "item": item,
                    }
                )
    return rows


def reconcile(settings: Settings, *, apply: bool) -> dict[str, int | bool]:
    rows = _normalized_hits(settings)
    if not apply:
        return {
            "apply": False,
            "shadow_hits": len(rows),
            "rows_with_creator": sum(bool(item["item"].get("creator_id")) for item in rows),
            "rows_with_metrics": sum(
                any(item["item"].get(key) is not None for key in (
                    "liked_count",
                    "collected_count",
                    "comment_count",
                    "shared_count",
                ))
                for item in rows
            ),
        }

    now = _now()
    updated_contents = 0
    created_creators = 0
    written_metrics = 0
    written_discoveries = 0
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                for row in rows:
                    item = row["item"]
                    content_id = row["content_id"]
                    if not content_id:
                        continue
                    creator_external_id = str(item.get("creator_id") or "").strip() or None
                    creator_name = str(item.get("creator_name_raw") or "").strip() or None
                    creator_id = None
                    if creator_external_id:
                        existing_creator = con.execute(
                            "SELECT creator_id,payload_json FROM creators WHERE platform=? AND external_id=?",
                            ("xiaohongshu", creator_external_id),
                        ).fetchone()
                        creator_id = str(existing_creator["creator_id"]) if existing_creator else _id(
                            "xhs_creator", creator_external_id
                        )
                        old_payload = _decode(existing_creator["payload_json"]) if existing_creator else {}
                        creator_payload = _merge_payload(
                            old_payload,
                            {
                                "source": "xhs-shadow-reconcile",
                                "external_id": creator_external_id,
                                "name": creator_name,
                            },
                        )
                        con.execute(
                            """INSERT INTO creators(
                                creator_id,canonical_name,platform,external_id,
                                first_seen_at,updated_at,payload_json
                            ) VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(creator_id) DO UPDATE SET
                                canonical_name=COALESCE(excluded.canonical_name,creators.canonical_name),
                                updated_at=excluded.updated_at,
                                payload_json=excluded.payload_json""",
                            (
                                creator_id,
                                creator_name,
                                "xiaohongshu",
                                creator_external_id,
                                now,
                                now,
                                _json(creator_payload),
                            ),
                        )
                        if not existing_creator:
                            created_creators += 1

                    content = con.execute(
                        "SELECT title,creator_id,author_name,published_at,payload_json FROM contents WHERE content_id=?",
                        (content_id,),
                    ).fetchone()
                    if not content:
                        continue
                    old_content_payload = _decode(content["payload_json"])
                    merged_payload = _merge_payload(
                        old_content_payload,
                        {
                            "shadow": True,
                            "source": "xhs-shadow-reconcile",
                            "keyword_id": row["keyword_id"],
                            "note_id": item.get("note_id"),
                            "raw": item,
                        },
                    )
                    title = str(item.get("title_raw") or "").strip() or content["title"]
                    published_at = _time(item.get("published_at")) or content["published_at"]
                    con.execute(
                        """UPDATE contents
                           SET title=?,creator_id=COALESCE(?,creator_id),
                               author_name=COALESCE(?,author_name),
                               published_at=?,updated_at=?,payload_json=?
                           WHERE content_id=?""",
                        (
                            title,
                            creator_id,
                            creator_name,
                            published_at,
                            now,
                            _json(merged_payload),
                            content_id,
                        ),
                    )
                    updated_contents += 1
                    con.execute(
                        """INSERT OR IGNORE INTO content_discoveries(
                            discovery_id,content_id,discovery_system,discovery_channel,
                            discovered_at,snapshot_id,source_ref,payload_json
                        ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            _id("xhs_shadow_reconcile_discovery", f"{content_id}:{row['snapshot_id']}"),
                            content_id,
                            "xhs-search",
                            "keyword-rank",
                            now,
                            row["snapshot_id"],
                            f"xhs-shadow://{row['response_id']}",
                            _json({"keyword_id": row["keyword_id"], "hit_id": row["hit_id"]}),
                        ),
                    )
                    written_discoveries += 1
                    for field, metric_key, display_name in (
                        ("liked_count", "xhs.note.like", "小红书点赞"),
                        ("collected_count", "xhs.note.collect", "小红书收藏"),
                        ("comment_count", "xhs.note.comment", "小红书评论"),
                        ("shared_count", "xhs.note.share", "小红书分享"),
                    ):
                        value = _number(item.get(field))
                        if value is None:
                            continue
                        con.execute(
                            """INSERT OR IGNORE INTO metric_definitions(
                                metric_key,platform,subject_type,display_name,
                                value_type,unit,accumulation_mode,description
                            ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                metric_key,
                                "xiaohongshu",
                                "content",
                                display_name,
                                "number",
                                "count",
                                "gauge",
                                "小红书搜索级影子刷新观测",
                            ),
                        )
                        before = con.total_changes
                        con.execute(
                            """INSERT OR IGNORE INTO metric_observations(
                                observation_id,subject_type,subject_id,metric_key,
                                observed_at,numeric_value,snapshot_id,source_ref,payload_json
                            ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (
                                _id(
                                    "xhs_shadow_reconcile_observation",
                                    f"{row['snapshot_id']}:{content_id}:{metric_key}",
                                ),
                                "content",
                                content_id,
                                metric_key,
                                now,
                                value,
                                row["snapshot_id"],
                                f"xhs-shadow://{row['response_id']}",
                                _json({"keyword_id": row["keyword_id"], "note_id": item.get("note_id")}),
                            ),
                        )
                        if con.total_changes > before:
                            written_metrics += 1
    return {
        "apply": True,
        "shadow_hits": len(rows),
        "updated_contents": updated_contents,
        "created_creators": created_creators,
        "written_metrics": written_metrics,
        "written_discoveries": written_discoveries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="修复已落库的小红书搜索级影子事实投影")
    parser.add_argument("--apply", action="store_true", help="执行本地 Hub 修复；默认只输出预估")
    args = parser.parse_args()
    settings = Settings.load()
    print(json.dumps(reconcile(settings, apply=args.apply), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
