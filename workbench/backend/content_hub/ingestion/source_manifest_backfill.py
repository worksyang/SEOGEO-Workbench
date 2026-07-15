"""把历史 Hub 记录接回真实来源 manifest，不复制、不生成原始文件。"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from content_hub.ingestion.source_manifests import manifest_id_for, manifest_ref, write_manifest


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(path: Path) -> tuple[str | None, int | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, None
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
    except OSError:
        return None, None


def _file_entry(root: Path, relative_path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    relative_path = str(relative_path).replace("\\", "/").lstrip("/")
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return {"relative_path": relative_path, "payload": {"exists": False, "reason": "outside_root"}}
    content_hash, size = _sha256(path)
    return {
        "relative_path": relative_path,
        "content_hash": content_hash,
        "size_bytes": size,
        "payload": {**(payload or {}), "exists": content_hash is not None},
    }


def _manifest(
    con,
    *,
    settings,
    system_key: str,
    source_kind: str,
    root: Path,
    relative_paths: set[str],
    captured_at: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    entries = [_file_entry(root, item) for item in sorted(relative_paths) if item]
    manifest_id = manifest_id_for(system_key, {"source_kind": source_kind, "root_label": root.name}, entries)
    write_manifest(
        con,
        manifest_id=manifest_id,
        system_key=system_key,
        source_kind=source_kind,
        root_fingerprint=hashlib.sha256(f"{system_key}:{root.name}".encode()).hexdigest(),
        entries=entries,
        captured_at=captured_at,
        payload=payload,
    )
    refs = {item["relative_path"]: manifest_ref(system_key, manifest_id, item["relative_path"]) for item in entries}
    return manifest_id, refs


def _audit(con, details: dict[str, Any], now: str) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO audit_log(
            audit_id, occurred_at, actor_type, action, subject_type,
            outcome, details_json
        ) VALUES (?, ?, 'migration', 'source_manifest.backfill',
                  'source_manifest', 'succeeded', ?)
        """,
        ("audit_migration_0007_source_manifest_backfill", now, json.dumps(details, ensure_ascii=False, sort_keys=True)),
    )


def backfill_existing_manifests(con, settings) -> dict[str, Any]:
    now = _now()
    result: dict[str, Any] = {"systems": {}, "updated_rows": 0, "missing_files": 0}

    # 微信搜索：source_ref 中的本地标签来自 search result Markdown；
    # normalized 文件作为同一不可再生批次的证据入口。
    wechat_root = Path(settings.wechat_source_root).expanduser()
    wechat_rows = con.execute(
        """
        SELECT source_ref FROM search_snapshots WHERE platform='wechat-search'
        UNION SELECT source_ref FROM metric_observations WHERE metric_key LIKE 'wechat.%'
        UNION SELECT source_ref FROM ingestion_batches WHERE adapter_key='wechat-search'
        """
    ).fetchall()
    wechat_paths = {
        str(row[0]).replace("\\", "/").lstrip("./")
        for row in wechat_rows
        if row[0]
        and not str(row[0]).startswith(("/", "http://", "https://", "manifest://"))
    }
    wechat_paths.update(
        {
            "normalized/monitor-data.json",
            "normalized/accounts.json",
            "normalized/articles.json",
            "normalized/snapshots.json",
            "normalized/snapshot_registry.json",
            "normalized/ranking_hits.json",
            "normalized/snapshot_terms.json",
            "normalized/article_metric_observations.json",
            "normalized/keyword_read_deltas.json",
        }
    )
    if wechat_rows:
        manifest_id, refs = _manifest(
            con, settings=settings, system_key="wechat-search", source_kind="normalized+markdown",
            root=wechat_root, relative_paths=wechat_paths, captured_at=now,
            payload={"migration": "0007", "source_root": "configured/wechat-search"},
        )
        count = 0
        for table, where, args in (
            ("search_snapshots", "platform='wechat-search' AND source_ref IS NOT NULL AND source_ref<>'' AND source_ref NOT LIKE 'http%' AND source_ref NOT LIKE 'manifest://%'", ()),
            ("metric_observations", "metric_key LIKE 'wechat.%' AND source_ref IS NOT NULL AND source_ref<>'' AND source_ref NOT LIKE 'http%' AND source_ref NOT LIKE 'manifest://%'", ()),
            ("ingestion_batches", "adapter_key='wechat-search' AND source_ref IS NOT NULL AND source_ref<>'' AND source_ref NOT LIKE 'manifest://%'", ()),
        ):
            for row in con.execute(f"SELECT rowid, source_ref FROM {table} WHERE {where}", args).fetchall():
                raw = str(row[1])
                rel = raw if raw in refs else ("normalized/" + Path(raw).name if raw.endswith(".json") else "")
                ref = refs.get(rel) if rel else None
                if ref is None:
                    ref = manifest_ref("wechat-search", manifest_id)
                con.execute(f"UPDATE {table} SET source_ref=? WHERE rowid=?", (ref, row[0]))
                count += 1
        result["systems"]["wechat-search"] = {"manifest_id": manifest_id, "updated_rows": count, "entry_count": len(refs)}
        result["updated_rows"] += count

    # 小红书：旧 metric source_ref=tikhub_xhs_search_notes 是来源标签，不是文件路径；
    # 将其收敛到真实 normalized note-metric 文件的 manifest 引用。
    xhs_root = Path(settings.xhs_normalized_root).expanduser()
    xhs_rows = con.execute(
        """
        SELECT source_ref FROM metric_observations WHERE metric_key LIKE 'xhs.%'
        UNION SELECT source_ref FROM ingestion_batches WHERE adapter_key='xiaohongshu'
        """
    ).fetchall()
    if xhs_rows:
        xhs_paths = {
            "keywords.json", "snapshots.json", "snapshot_terms.json", "ranking_hits.json",
            "articles.json", "accounts.json", "note_metric_observations.json",
        }
        manifest_id, refs = _manifest(
            con, settings=settings, system_key="xiaohongshu", source_kind="normalized",
            root=xhs_root, relative_paths=xhs_paths, captured_at=now,
            payload={"migration": "0007", "source_root": "configured/xiaohongshu-normalized"},
        )
        count = 0
        note_ref = refs.get("note_metric_observations.json", manifest_ref("xiaohongshu", manifest_id, "note_metric_observations.json"))
        for row in con.execute("SELECT rowid, source_ref FROM metric_observations WHERE metric_key LIKE 'xhs.%' AND source_ref IS NOT NULL AND source_ref<>'' AND source_ref NOT LIKE 'manifest://%'"):
            con.execute("UPDATE metric_observations SET source_ref=? WHERE rowid=?", (note_ref, row[0]))
            count += 1
        for row in con.execute("SELECT rowid, source_ref FROM ingestion_batches WHERE adapter_key='xiaohongshu' AND source_ref IS NOT NULL AND source_ref NOT LIKE 'manifest://%'"):
            con.execute("UPDATE ingestion_batches SET source_ref=? WHERE rowid=?", (manifest_ref("xiaohongshu", manifest_id), row[0]))
            count += 1
        result["systems"]["xiaohongshu"] = {"manifest_id": manifest_id, "updated_rows": count, "entry_count": len(refs)}
        result["updated_rows"] += count

    # Wiki：只登记当前允许根内实际存在的 Markdown；缺失文件保留 manifest entry，
    # 但绝不创建替代文件。
    wiki_rows = con.execute(
        """
        SELECT md_path FROM contents
        WHERE content_type IN ('mother_article', 'knowledge_article')
          AND md_path IS NOT NULL AND md_path<>''
        UNION SELECT source_ref FROM content_discoveries WHERE discovery_system='wiki'
        """
    ).fetchall()
    if wiki_rows:
        wiki_root = next(
            (Path(root).expanduser() for root in settings.wiki_allowed_roots if Path(root).expanduser().exists()),
            Path(settings.project_root) / "source",
        )
        wiki_paths = {str(row[0]) for row in wiki_rows if row[0] and not str(row[0]).startswith("manifest://")}
        manifest_id, refs = _manifest(
            con, settings=settings, system_key="wiki", source_kind="markdown",
            root=wiki_root, relative_paths=wiki_paths, captured_at=now,
            payload={"migration": "0007", "source_root": "configured/wiki-source"},
        )
        count = 0
        for row in con.execute("SELECT rowid, source_ref FROM content_discoveries WHERE discovery_system='wiki' AND source_ref IS NOT NULL AND source_ref<>'' AND source_ref NOT LIKE 'manifest://%'"):
            rel = str(row[1])
            con.execute("UPDATE content_discoveries SET source_ref=? WHERE rowid=?", (refs.get(rel, manifest_ref("wiki", manifest_id, rel)), row[0]))
            count += 1
        result["systems"]["wiki"] = {"manifest_id": manifest_id, "updated_rows": count, "entry_count": len(refs)}
        result["updated_rows"] += count

    _audit(con, result, now)
    return result
