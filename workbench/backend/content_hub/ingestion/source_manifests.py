"""共享的原始证据 manifest 写入与安全 source_ref 约定。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import PurePosixPath
from typing import Any, Iterable


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_id_for(system_key: str, manifest: Any, entries: Iterable[dict[str, Any]]) -> str:
    payload = {
        "system_key": system_key,
        "manifest": manifest,
        "entries": sorted(
            [
                {
                    "relative_path": str(item.get("relative_path") or ""),
                    "content_hash": item.get("content_hash"),
                    "size_bytes": item.get("size_bytes"),
                }
                for item in entries
            ],
            key=lambda item: item["relative_path"],
        ),
    }
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    return f"sm_{system_key}_{digest[:32]}"


def manifest_ref(system_key: str, manifest_id: str, relative_path: str | None = None) -> str:
    """返回不含本机路径的稳定引用；relative_path 只允许 manifest 内的 POSIX 相对路径。"""
    if not manifest_id or "/" in manifest_id or "\\" in manifest_id or manifest_id.startswith("."):
        raise ValueError("manifest_id 不安全")
    ref = f"manifest://{system_key}/{manifest_id}"
    if relative_path:
        path = PurePosixPath(str(relative_path).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("manifest relative_path 不安全")
        ref += "/" + path.as_posix()
    return ref


def write_manifest(
    con: sqlite3.Connection,
    *,
    manifest_id: str,
    system_key: str,
    source_kind: str,
    root_fingerprint: str,
    entries: Iterable[dict[str, Any]],
    captured_at: str,
    payload: dict[str, Any] | None = None,
) -> None:
    normalized = []
    for item in entries:
        relative_path = str(item.get("relative_path") or "").replace("\\", "/").lstrip("/")
        if not relative_path or ".." in PurePosixPath(relative_path).parts:
            continue
        normalized.append(
            {
                "relative_path": relative_path,
                "content_hash": item.get("content_hash"),
                "size_bytes": item.get("size_bytes"),
                "observed_at": item.get("observed_at") or captured_at,
                "payload": item.get("payload") or {},
            }
        )
    con.execute(
        """
        INSERT INTO source_manifests(
            manifest_id, system_key, source_kind, root_fingerprint,
            manifest_hash, entry_count, captured_at, immutable, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(manifest_id) DO NOTHING
        """,
        (
            manifest_id,
            system_key,
            source_kind,
            root_fingerprint,
            manifest_id,
            len(normalized),
            captured_at,
            _json(payload or {}),
        ),
    )
    for item in normalized:
        con.execute(
            """
            INSERT INTO source_manifest_entries(
                manifest_id, relative_path, content_hash, size_bytes,
                observed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(manifest_id, relative_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                size_bytes=excluded.size_bytes,
                observed_at=excluded.observed_at,
                payload_json=excluded.payload_json
            """,
            (
                manifest_id,
                item["relative_path"],
                item["content_hash"],
                item["size_bytes"],
                item["observed_at"],
                _json(item["payload"]),
            ),
        )
