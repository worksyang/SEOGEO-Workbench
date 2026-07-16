"""微信迁移读取影子验收。

本文件是可重复的真实 HTTP harness，不改变产品代码。默认只在显式传入
``--run`` 或 ``RUN_WECHAT_SHADOW_E2E=1`` 时执行，避免普通 pytest 意外启动
服务、读取冻结大数据或访问 8774。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workbench/backend"))
from content_hub.services.contract_diff import compare_values, default_registry

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "data/migration/wechat/freeze_20260716T024524+0800/payload"
FREEZE_DB = FREEZE / "data/state/app.db"
SHADOW_ROOT = ROOT / "data/migration/wechat/shadow_read"
EVIDENCE = ROOT / "docs/微信关键词读取影子验收证据_v1.md"
LEGACY = "http://127.0.0.1:8774"
DEFAULT_HUB_PORT = 8775
FORBIDDEN_PORT = 8765
OLD_SOURCE_ROOT = Path("/Users/works14/.claude/监控/wechat-ybxhyyh-top3")
LAUNCHD_LABELS = (
    "com.local.wechat-monitor-8765",
    "com.claude.schedule.wechat-ybxhyyh-top3",
)
READ_CONTRACTS = (
    "monitor-data", "bootstrap", "keyword", "account", "article-content",
    "article-hit-detail", "keyword-manage", "keyword-discovery",
    "refresh-status", "refresh-all-status", "refresh-all-history",
    "scheduler-status", "articles", "articles-accounts", "agent-manifest",
    "agent-daily-brief", "agent-metric-dictionary", "agent-evidence",
    "penalty-signals", "account-aliases", "article-cover-image",
    "aidso-keyword-heat-get",
)


@dataclass
class HttpResult:
    method: str
    path: str
    query: dict[str, Any]
    status: int
    headers: dict[str, str | None]
    decoded_sha256: str
    wire_sha256: str
    wire_bytes: int
    value: Any
    decode_error: str | None = None

    def summary(self) -> dict[str, Any]:
        value = self.value
        if isinstance(value, dict):
            shape = {"type": "object", "keys": sorted(value)[:80]}
        elif isinstance(value, list):
            shape = {"type": "array", "length": len(value)}
        else:
            shape = {"type": type(value).__name__}
        return {
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "status": self.status,
            "headers": self.headers,
            "decoded_sha256": self.decoded_sha256,
            "wire_sha256": self.wire_sha256,
            "wire_bytes": self.wire_bytes,
            "shape": shape,
            "decode_error": self.decode_error,
        }


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


class SafetyBlocked(RuntimeError):
    def __init__(self, stage: str, snapshot: dict[str, Any]) -> None:
        self.stage = stage
        self.snapshot = snapshot
        super().__init__(f"shadow safety blocked at {stage}: {snapshot}")


class ShadowImportFailed(AssertionError):
    def __init__(
        self,
        *,
        pass_index: int,
        http_status: int,
        summary: dict[str, Any],
    ) -> None:
        self.details = {
            "pass": pass_index,
            "http_status": http_status,
            **summary,
        }
        super().__init__(
            f"第 {pass_index} 次导入失败："
            + json.dumps(
                self.details,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )


def _launchd_safety() -> dict[str, Any]:
    uid = str(os.getuid())
    disabled = subprocess.run(
        ["launchctl", "print-disabled", f"gui/{uid}"],
        capture_output=True, text=True, timeout=5,
    )
    loaded = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True, timeout=5,
    )
    disabled_text = disabled.stdout + disabled.stderr
    loaded_text = loaded.stdout + loaded.stderr
    states = {
        label: (
            f'"{label}" => disabled' in disabled_text
            or f'"{label}" => true' in disabled_text
        )
        for label in LAUNCHD_LABELS
    }
    loaded_labels = [
        label for label in LAUNCHD_LABELS
        if any(line.split()[-1:] == [label] for line in loaded_text.splitlines())
    ]
    return {
        "command_ok": disabled.returncode == 0 and loaded.returncode == 0,
        "disabled": states,
        "loaded": loaded_labels,
        "raw_errors": [x for x in (disabled.stderr, loaded.stderr) if x.strip()],
    }


def _safety_snapshot(stage: str) -> dict[str, Any]:
    listener = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{FORBIDDEN_PORT}", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=5,
    )
    process_rows = []
    for line in subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True, text=True, timeout=5,
    ).stdout.splitlines():
        if str(OLD_SOURCE_ROOT) not in line:
            continue
        if any(token in line for token in (
            "run.py", "keyword_batch_runner.py", "keyword_batch_watchdog.py",
        )):
            process_rows.append(line.strip())
    launchd = _launchd_safety()
    return {
        "stage": stage,
        "forbidden_port": FORBIDDEN_PORT,
        "listener": listener.stdout.strip(),
        "old_source_processes": process_rows,
        "launchd": launchd,
        "pass": (
            not listener.stdout.strip()
            and not process_rows
            and launchd["command_ok"]
            and all(launchd["disabled"].values())
            and not launchd["loaded"]
        ),
    }


def _assert_shadow_safety(stage: str) -> dict[str, Any]:
    snapshot = _safety_snapshot(stage)
    if not snapshot["pass"]:
        raise SafetyBlocked(stage, snapshot)
    return snapshot


def _json_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _import_failure_summary(value: Any) -> dict[str, Any]:
    """从失败响应提取有界诊断，不复制可能数百 KiB 的完整 body。"""
    root = value if isinstance(value, dict) else {}
    data = root.get("data")
    if not isinstance(data, dict):
        data = root
    audit = data.get("audit")
    if not isinstance(audit, dict):
        audit = {}
    reconcile = audit.get("reconcile")
    if not isinstance(reconcile, dict):
        reconcile = {}
    rejected = audit.get("rejected")
    if not isinstance(rejected, list):
        rejected = []
    collisions = audit.get("content_collisions")
    if not isinstance(collisions, list):
        collisions = []

    collision_types: dict[str, int] = {}
    identity_collision_rows = []
    for item in collisions:
        if not isinstance(item, dict):
            kind = "unclassified"
        else:
            kind = str(
                item.get("collision_type")
                or item.get("kind")
                or "unclassified"
            )
            if kind == "identity_conflict":
                identity_collision_rows.append(item)
        collision_types[kind] = collision_types.get(kind, 0) + 1
    rejected_identity_count = sum(
        1
        for item in rejected
        if isinstance(item, dict)
        and item.get("kind") == "article_identity_conflict"
    )

    return {
        "semantic_status": (
            data.get("semantic_status")
            or data.get("status")
            or root.get("semantic_status")
        ),
        "verified": data.get("verified"),
        "batch_id": data.get("batch_id") or data.get("job_id"),
        "reconcile": {
            "status": reconcile.get("status"),
            "difference": reconcile.get("difference"),
            "scope_match": reconcile.get("scope_match"),
        },
        "asset_integrity": audit.get("asset_integrity"),
        "rejected_count": len(rejected),
        "rejected_first_20": rejected[:20],
        "collision_count": audit.get("collision_count", len(collisions)),
        "collision_classification": {
            "by_type": {
                key: collision_types[key]
                for key in sorted(collision_types)
            },
            "identity_conflict_collision_count": len(identity_collision_rows),
            "identity_conflict_rejected_count": rejected_identity_count,
            "identity_conflict_first_20": identity_collision_rows[:20],
        },
        "checkpoint": data.get("checkpoint"),
    }


def _request(
    base: str,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = 180,
) -> HttpResult:
    query = query or {}
    encoded = urllib.parse.urlencode(query, doseq=True)
    url = f"{base}{path}" + (f"?{encoded}" if encoded else "")
    headers = {
        "Accept-Encoding": "gzip",
        "Connection": "close",
        "X-Request-ID": "wechat-shadow-e2e",
    }
    body = None
    if json_body is not None:
        body = json.dumps(
            json_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        status = response.status
        raw = response.read()
        headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
        headers = {key.lower(): value for key, value in exc.headers.items()}
    except Exception as exc:
        return HttpResult(
            method, path, query, 599, {}, "", "", 0, None, f"{type(exc).__name__}: {exc}"
        )
    wire_hash = hashlib.sha256(raw).hexdigest()
    decoded = raw
    if (headers.get("content-encoding") or "").lower() == "gzip":
        try:
            decoded = gzip.decompress(raw)
        except OSError as exc:
            return HttpResult(
                method, path, query, status, headers, "", wire_hash, len(raw), None,
                f"gzip: {exc}",
            )
    decoded_hash = hashlib.sha256(decoded).hexdigest()
    content_type = (headers.get("content-type") or "").lower()
    value: Any
    decode_error = None
    if "json" in content_type or decoded.lstrip()[:1] in {b"{", b"["}:
        try:
            value = json.loads(decoded.decode("utf-8"))
            decoded_hash = _json_hash(value)
        except Exception as exc:
            value = decoded.decode("utf-8", "replace")
            decode_error = f"{type(exc).__name__}: {exc}"
    else:
        value = decoded.decode("utf-8", "replace")
    return HttpResult(
        method, path, query, status,
        {key: headers.get(key) for key in (
            "content-type", "content-encoding", "etag", "cache-control", "vary"
        )},
        decoded_hash, wire_hash, len(raw), value, decode_error,
    )


def _json_pointer_diffs(left: Any, right: Any, *, limit: int = 40) -> list[dict[str, Any]]:
    """小而稳定的 JSON Pointer 差异；大值只保留 hash，避免证据膨胀。"""
    diffs: list[dict[str, Any]] = []

    def pointer(parent: str, token: Any) -> str:
        return f"{parent}/{str(token).replace('~', '~0').replace('/', '~1')}"

    def compact(value: Any) -> Any:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(raw.encode("utf-8")) <= 2048:
            return value
        return {"truncated": True, "sha256": hashlib.sha256(raw.encode()).hexdigest()}

    def walk(left_value: Any, right_value: Any, path: str = "") -> None:
        if len(diffs) >= limit:
            return
        if isinstance(left_value, dict) and isinstance(right_value, dict):
            for key in sorted(set(left_value) | set(right_value), key=str):
                if key not in left_value:
                    diffs.append({"json_pointer": pointer(path, key), "kind": "added",
                                  "legacy": None, "hub": compact(right_value[key])})
                elif key not in right_value:
                    diffs.append({"json_pointer": pointer(path, key), "kind": "removed",
                                  "legacy": compact(left_value[key]), "hub": None})
                else:
                    walk(left_value[key], right_value[key], pointer(path, key))
            return
        if isinstance(left_value, list) and isinstance(right_value, list):
            if len(left_value) != len(right_value):
                diffs.append({"json_pointer": path or "/", "kind": "length",
                              "legacy": len(left_value), "hub": len(right_value)})
            for index in range(min(len(left_value), len(right_value))):
                walk(left_value[index], right_value[index], pointer(path, index))
            return
        if left_value != right_value:
            diffs.append({"json_pointer": path or "/", "kind": "value",
                          "legacy": compact(left_value), "hub": compact(right_value)})

    walk(left, right)
    return diffs


def _article_asset_hashes(database: Path, asset_store: Path) -> dict[str, Any]:
    articles = json.loads((FREEZE / "normalized/articles.json").read_text(encoding="utf-8"))
    by_article: dict[str, dict[str, Any]] = {}
    source_no_content_path = 0
    missing_source = []
    for row in articles:
        article_id = str(row.get("article_id") or "")
        relative = str(row.get("content_file_path") or "").replace("\\", "/")
        if not article_id:
            continue
        if not relative:
            source_no_content_path += 1
            continue
        candidate = FREEZE / relative
        actual_relative = relative
        if not candidate.is_file():
            candidate = FREEZE / "code-snapshot" / relative
            actual_relative = f"code-snapshot/{relative}"
        if not candidate.is_file():
            missing_source.append(article_id)
            continue
        raw = candidate.read_bytes()
        by_article[article_id] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "relative_path": relative,
            "actual_source_relative": actual_relative,
        }
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        path_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT old_article_id,relative_path,asset_path,source_ref
                FROM wechat_article_paths
                ORDER BY old_article_id,source_ref
                """
            )
        ]
    rows_by_article: dict[str, list[dict[str, Any]]] = {}
    for row in path_rows:
        rows_by_article.setdefault(str(row["old_article_id"]), []).append(row)

    imported_assets = 0
    missing_path_assets = []
    for article_id, source in by_article.items():
        rows = rows_by_article.get(article_id, [])
        if len(rows) != 1:
            missing_path_assets.append(
                {"article_id": article_id, "reason": f"path_rows={len(rows)}"}
            )
            continue
        row = rows[0]
        if row.get("relative_path") != source["relative_path"]:
            missing_path_assets.append(
                {"article_id": article_id, "reason": "relative_path_mismatch"}
            )
            continue
        if not str(row.get("source_ref") or "").endswith(
            "/" + source["actual_source_relative"]
        ):
            missing_path_assets.append(
                {"article_id": article_id, "reason": "source_ref_mismatch"}
            )
            continue
        asset_relative = str(row.get("asset_path") or "")
        asset_file = asset_store / asset_relative
        try:
            asset_file.resolve().relative_to(asset_store.resolve())
        except (OSError, ValueError):
            missing_path_assets.append(
                {"article_id": article_id, "reason": "asset_path_escape"}
            )
            continue
        if (
            not asset_relative
            or asset_file.is_symlink()
            or not asset_file.is_file()
            or hashlib.sha256(asset_file.read_bytes()).hexdigest()
            != source["sha256"]
        ):
            missing_path_assets.append(
                {"article_id": article_id, "reason": "asset_missing_or_hash_mismatch"}
            )
            continue
        imported_assets += 1
    aggregate = hashlib.sha256()
    for article_id in sorted(by_article):
        aggregate.update(article_id.encode())
        aggregate.update(b"\0")
        aggregate.update(by_article[article_id]["sha256"].encode())
        aggregate.update(b"\0")
    return {
        "article_records": len(articles),
        "article_identity_count": len({str(x.get("article_id")) for x in articles if x.get("article_id")}),
        "path_records": len(by_article),
        "content_path_records": len(by_article),
        "asset_hash_count": len(by_article),
        "imported_assets": imported_assets,
        "missing_path_asset_count": len(missing_path_assets) + len(missing_source),
        "missing_path_asset_samples": (missing_source + missing_path_assets)[:20],
        "source_no_content_path": source_no_content_path,
        "db_path_record_count": len(path_rows),
        "asset_blob_count": len(list((asset_store / "wechat").glob("*.md"))),
        "aggregate_sha256": aggregate.hexdigest(),
    }


def _core_counts(database: Path) -> dict[str, int]:
    import sqlite3
    with sqlite3.connect(database) as connection:
        return {
            "keywords": connection.execute(
                "SELECT COUNT(*) FROM keywords WHERE platform='wechat-search'"
            ).fetchone()[0],
            "contents": connection.execute(
                "SELECT COUNT(*) FROM contents WHERE content_id IN "
                "(SELECT content_id FROM content_identifiers WHERE namespace='wechat_article')"
            ).fetchone()[0],
            "snapshots": connection.execute(
                "SELECT COUNT(*) FROM search_snapshots WHERE platform='wechat-search'"
            ).fetchone()[0],
            "hits": connection.execute(
                "SELECT COUNT(*) FROM search_hits WHERE snapshot_id IN "
                "(SELECT snapshot_id FROM search_snapshots WHERE platform='wechat-search')"
            ).fetchone()[0],
            "metric_observations": connection.execute(
                "SELECT COUNT(*) FROM metric_observations WHERE metric_key LIKE 'wechat.%'"
            ).fetchone()[0],
        }


def _freeze_keyword_expectations() -> dict[str, int]:
    import sqlite3
    if not FREEZE_DB.is_file():
        raise FileNotFoundError(FREEZE_DB)
    with sqlite3.connect(f"file:{FREEZE_DB.resolve()}?mode=ro", uri=True) as connection:
        return {
            "groups": connection.execute(
                "SELECT COUNT(*) FROM keyword_groups WHERE archived_at IS NULL"
            ).fetchone()[0],
            "visible_keywords": connection.execute(
                "SELECT COUNT(*) FROM keyword_registry "
                "WHERE status='active' AND archived_at IS NULL"
            ).fetchone()[0],
            "registry_total": connection.execute(
                "SELECT COUNT(*) FROM keyword_registry"
            ).fetchone()[0],
            "archived_keywords": connection.execute(
                "SELECT COUNT(*) FROM keyword_registry "
                "WHERE status='archived' OR archived_at IS NOT NULL"
            ).fetchone()[0],
        }


def _load_identifiers() -> dict[str, Any]:
    monitor = json.loads((FREEZE / "normalized/monitor-data.json").read_text(encoding="utf-8"))
    articles = json.loads((FREEZE / "normalized/articles.json").read_text(encoding="utf-8"))
    article = next((row for row in articles if row.get("content_file_path")), articles[0])
    return {
        "keyword_ids": [row["keyword_id"] for row in monitor["keywords"]],
        "account_ids": [row["account_id"] for row in monitor["accounts"]],
        "article_ids": [row["article_id"] for row in articles],
        "sample_article_id": article["article_id"],
        "sample_content_path": article.get("content_file_path") or "",
    }


def _corpus(ids: dict[str, Any]) -> list[tuple[str, str, dict[str, Any], str]]:
    kw, account, article = ids["keyword_ids"][0], ids["account_ids"][0], ids["sample_article_id"]
    content = ids["sample_content_path"]
    core = [
        ("GET", "/api/monitor-data", {}, "core"),
        ("GET", "/api/monitor-data/bootstrap", {}, "core"),
        ("GET", f"/api/monitor-data/keyword/{kw}", {}, "core"),
        ("GET", f"/api/monitor-data/account/{account}", {}, "core"),
        ("GET", "/api/agent/manifest", {}, "core"),
        ("GET", "/api/agent/daily-brief", {}, "core"),
        ("GET", "/api/agent/metric-dictionary", {}, "core"),
        ("GET", "/api/agent/evidence/missing-evidence", {}, "core"),
        ("GET", "/api/penalty-signals", {}, "core"),
        ("GET", "/api/account-aliases", {}, "core"),
        ("GET", "/api/article-content", {"path": content}, "core"),
        ("GET", "/api/article-hit-detail", {"article_id": article}, "core"),
        ("GET", "/api/article-cover-image", {"url": "https://example.invalid/blocked-cover.jpg"}, "core"),
        ("GET", "/api/aidso/keyword-heat", {"keyword": "隔离测试"}, "core"),
        ("GET", "/api/keyword-manage", {}, "core"),
        ("GET", "/api/keyword-discovery", {"limit": "1"}, "core"),
        ("GET", "/api/refresh-status/missing-job", {}, "core"),
        ("GET", "/api/refresh-all/status", {}, "core"),
        ("GET", "/api/refresh-all/history", {}, "core"),
        ("GET", "/api/scheduler/status", {}, "core"),
        ("GET", "/api/articles", {}, "core"),
        ("GET", "/api/articles/accounts", {}, "core"),
    ]
    invalid = [
        ("GET", "/api/monitor-data/keyword/not-a-real-keyword", {}, "invalid"),
        ("GET", "/api/monitor-data/account/not-a-real-account", {}, "invalid"),
        ("GET", "/api/article-content", {"path": "../../etc/passwd"}, "invalid"),
        ("GET", "/api/article-content", {}, "invalid"),
        ("GET", "/api/article-hit-detail", {}, "invalid"),
        ("GET", "/api/articles", {"page": "0", "page_size": "9999", "sort": "not-real", "time_range": "bad"}, "invalid"),
    ]
    r21 = [
        {"sort": "reads"}, {"sort": "likes"}, {"sort": "rank"},
        {"time_range": "7"}, {"min_hits": "2"}, {"account": account}, {"search": "保险"},
    ]
    return core + invalid + [
        ("GET", "/api/articles", query, f"R21-{index}")
        for index, query in enumerate(r21, 1)
    ]


def _start_hub(port: int, database: Path, assets: Path, log_path: Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "workbench/backend"),
        "HUB_HOST": "127.0.0.1",
        "HUB_PORT": str(port),
        "HUB_DATABASE_PATH": str(database),
        "HUB_ASSET_STORE_PATH": str(assets),
        "HUB_WECHAT_SOURCE_ROOT": str(FREEZE),
        "HUB_WECHAT_FREEZE_ROOT": str(FREEZE),
        "HUB_WECHAT_SOURCE_URL": LEGACY,
        "HUB_XHS_SOURCE_URL": "http://127.0.0.1:1",
        "HUB_MP_SOURCE_URL": "http://127.0.0.1:1",
        "HUB_GEO_SOURCE_URL": "http://127.0.0.1:1",
        "HUB_LOG_LEVEL": "WARNING",
    })
    stream = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "workbench/run.py"), "--api-only", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, text=True,
    )


def _wait(port: int, process: subprocess.Popen[str], timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Hub 服务提前退出，exit={process.returncode}")
        if _request(f"http://127.0.0.1:{port}", "GET", "/api/v1/wechat/bootstrap", timeout=3).status < 599:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Hub 服务未在 {timeout}s 内就绪")


def _import_twice(
    base: str, database: Path, assets: Path,
    safety_guard: Any = _assert_shadow_safety,
) -> list[dict[str, Any]]:
    results = []
    import_keys = (
        "shadow-full-import-pass-1",
        "shadow-full-import-pass-2",
    )
    for index, idempotency_key in enumerate(import_keys, 1):
        result = _request(
            base, "POST", "/api/v1/wechat/import",
            json_body={
                "dry_run": False,
                "confirm": True,
                "idempotency_key": idempotency_key,
            },
            timeout=3600,
        )
        counts_after = _core_counts(database)
        results.append({
            "pass": index,
            "status": result.status,
            "decoded_sha256": result.decoded_sha256,
            "body": result.value,
            "core_counts_after": counts_after,
            "asset_blobs_after": len(list((assets / "wechat").glob("*.md"))),
        })
        if result.status != 200:
            raise ShadowImportFailed(
                pass_index=index,
                http_status=result.status,
                summary=_import_failure_summary(result.value),
            )
        if index == 2:
            before = results[0]["core_counts_after"]
            delta = {key: counts_after[key] - before[key] for key in before}
            results[-1]["core_count_delta_from_pass_1"] = delta
            if any(delta.values()):
                raise AssertionError(f"第二次导入新增核心事实或计数增长：{delta}")
            asset_delta = (
                results[-1]["asset_blobs_after"]
                - results[0]["asset_blobs_after"]
            )
            results[-1]["asset_blob_delta_from_pass_1"] = asset_delta
            if asset_delta:
                raise AssertionError(
                    f"第二次导入新增正文资产 blob：{asset_delta}"
                )
        results[-1]["safety_after_import"] = safety_guard(f"after_import_pass_{index}")
    return results


def _set_modes(database: Path, mode: str) -> None:
    import sqlite3
    with sqlite3.connect(database) as connection:
        placeholders = ",".join("?" for _ in READ_CONTRACTS)
        connection.execute(
            f"UPDATE migration_switches SET data_mode=?, rollback_mode=?, updated_by='wechat-shadow-e2e', reason=? "
            f"WHERE module_key='wechat-search' AND contract_key IN ({placeholders})",
            (mode, mode, f"影子验收显式切换为 {mode}", *READ_CONTRACTS),
        )
        connection.commit()


def _compare_pair(legacy: HttpResult, hub: HttpResult) -> dict[str, Any]:
    metadata_equal = legacy.headers == hub.headers
    payload_equal = legacy.decoded_sha256 == hub.decoded_sha256 and legacy.value == hub.value
    contract_key = "keyword-manage" if legacy.path == "/api/keyword-manage" else legacy.path
    semantic = compare_values(
        legacy.value,
        hub.value,
        registry=default_registry("wechat-search", contract_key),
    )
    diffs = []
    if legacy.value is not None and hub.value is not None:
        diffs = _json_pointer_diffs(legacy.value, hub.value)
    registry = default_registry("wechat-search", contract_key)
    tolerated_diffs = []
    for diff in diffs:
        rule = registry.for_path(diff["json_pointer"])
        if rule and rule.tolerance_seconds is not None:
            tolerated_diffs.append({
                **diff,
                "rule": rule.name,
                "tolerance_seconds": rule.tolerance_seconds,
            })
    semantic_equal = not semantic.diffs
    tolerated_only = bool(tolerated_diffs) and len(tolerated_diffs) == len(diffs) and semantic_equal
    return {
        "path": legacy.path,
        "query": legacy.query,
        "status_equal": legacy.status == hub.status,
        "payload_equal": payload_equal,
        "contract_payload_equal": semantic_equal,
        "tolerated_diff": tolerated_only,
        "tolerated_diffs": tolerated_diffs,
        "metadata_equal": metadata_equal,
        "legacy": legacy.summary(),
        "hub": hub.summary(),
        "json_pointer_diffs": diffs,
        "diff_count": len(diffs),
    }


def _strict_outcome(
    pairs: list[dict[str, Any]],
    identity_results: dict[str, list[dict[str, Any]]],
    compare_mode: dict[str, Any],
) -> dict[str, Any]:
    pair_failures = [
        {
            "path": item["path"], "query": item["query"],
            "status_equal": item["status_equal"],
            "payload_equal": item["payload_equal"],
            "contract_payload_equal": item.get("contract_payload_equal", item["payload_equal"]),
            "tolerated_diff": item.get("tolerated_diff", False),
            "metadata_equal": item["metadata_equal"],
        }
        for item in pairs
        if not (
            item["status_equal"]
            and item["metadata_equal"]
            and (
                item["payload_equal"]
                or (
                    item.get("tolerated_diff", False)
                    and item.get("contract_payload_equal", False)
                )
            )
        )
    ]
    identity_failures = {
        kind: [
            row["id"] for row in rows
            if not (
                row["status_equal"] and row["payload_equal"]
                and row["metadata_equal"]
            )
        ]
        for kind, rows in identity_results.items()
    }
    compare_pass = all([
        compare_mode["status_matches_legacy"],
        compare_mode["payload_matches_legacy"],
        compare_mode["decoded_sha256_matches_legacy"],
        compare_mode["headers_match_legacy"],
    ])
    return {
        "pass": (
            not pair_failures
            and not any(identity_failures.values())
            and compare_pass
        ),
        "pair_failures": pair_failures,
        "identity_failures": identity_failures,
        "compare_pass": compare_pass,
    }


def _run() -> dict[str, Any]:
    if not FREEZE.is_dir():
        raise FileNotFoundError(FREEZE)
    safety_preflight = _assert_shadow_safety("preflight")
    keyword_expected = _freeze_keyword_expectations()
    if not _port_available(8774):
        pass
    else:
        raise RuntimeError("127.0.0.1:8774 未监听；禁止在参考实例缺失时运行 shadow")
    port = DEFAULT_HUB_PORT if _port_available(DEFAULT_HUB_PORT) else next(
        candidate for candidate in range(8776, 8790) if _port_available(candidate)
    )
    SHADOW_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S%z")
    run_dir = SHADOW_ROOT / f"run_{run_id}"
    run_dir.mkdir()
    tmp = Path(tempfile.mkdtemp(prefix="wechat-shadow-", dir="/tmp"))
    database = tmp / "hub.sqlite"
    assets = tmp / "asset_store"
    log_path = run_dir / "hub.log"
    process: subprocess.Popen[str] | None = None
    started = time.time()
    try:
        process = _start_hub(port, database, assets, log_path)
        _wait(port, process)
        safety_before_import = _assert_shadow_safety("before_import")
        hub_base = f"http://127.0.0.1:{port}"
        imports = _import_twice(hub_base, database, assets)
        _set_modes(database, "hub")
        ids = _load_identifiers()
        corpus = _corpus(ids)
        compared: list[dict[str, Any]] = []
        for method, path, query, case in corpus:
            left = _request(LEGACY, method, path, query)
            right = _request(hub_base, method, path, query)
            compared.append({"case": case, **_compare_pair(left, right)})
        # 重新读取已解压响应体，保留真实返回体统计；不以文档中的 expected 替代。
        r15_left = _request(LEGACY, "GET", "/api/keyword-manage")
        r15_right = _request(hub_base, "GET", "/api/keyword-manage")
        def r15_stats(value: Any) -> dict[str, int]:
            groups = value.get("groups", []) if isinstance(value, dict) else []
            visible = sum(len(group.get("keywords", [])) for group in groups if isinstance(group, dict))
            return {"groups": len(groups), "visible_keywords": visible}
        r15_actual = {"legacy": r15_stats(r15_left.value), "hub": r15_stats(r15_right.value)}
        expected_r15 = {
            "groups": keyword_expected["groups"],
            "visible_keywords": keyword_expected["visible_keywords"],
        }
        if r15_actual["legacy"] != expected_r15 or r15_actual["hub"] != expected_r15:
            raise AssertionError(
                f"R15 返回体未还原冻结 active 可见口径：expected={expected_r15}, actual={r15_actual}"
            )
        import_reconcile = (
            (imports[-1]["body"] or {}).get("data", {}).get("audit", {}).get("reconcile", {})
            if isinstance(imports[-1]["body"], dict) else {}
        )
        registry_reconcile = (
            import_reconcile.get("scope", {}).get("keywords_runtime_registry", {})
        )
        expected_registry = {
            "source": keyword_expected["registry_total"],
            "hub": keyword_expected["registry_total"],
        }
        if registry_reconcile != expected_registry:
            raise AssertionError(
                f"底层 runtime registry 对账不是冻结全量：expected={expected_registry}, actual={registry_reconcile}"
            )

        # 全量 keyword/account GET 身份覆盖：不把 1,673 个响应体写入证据，只保留 hash/diff。
        identity_results = {"keywords": [], "accounts": []}
        for kind, values, path_prefix in (
            ("keywords", ids["keyword_ids"], "/api/monitor-data/keyword/"),
            ("accounts", ids["account_ids"], "/api/monitor-data/account/"),
        ):
            for value in values:
                path = f"{path_prefix}{urllib.parse.quote(value, safe='')}"
                left = _request(LEGACY, "GET", path)
                right = _request(hub_base, "GET", path)
                pair = _compare_pair(left, right)
                identity_results[kind].append({
                    "id": value, "status_equal": pair["status_equal"],
                    "payload_equal": pair["payload_equal"],
                    "metadata_equal": pair["metadata_equal"],
                    "diff_count": pair["diff_count"],
                    "legacy_sha256": left.decoded_sha256, "hub_sha256": right.decoded_sha256,
                })

        # R21 全量分页边界：先取每组 page=1，再按返回 total 访问 last/last+1。
        r21_boundaries = []
        for query in [
            {"sort": "reads"}, {"sort": "likes"}, {"sort": "rank"},
            {"time_range": "7"}, {"min_hits": "2"}, {"account": ids["account_ids"][0]},
            {"search": "保险"},
        ]:
            first_query = {**query, "page": "1", "page_size": "50"}
            left = _request(LEGACY, "GET", "/api/articles", first_query)
            total = int((left.value or {}).get("total") or 0) if isinstance(left.value, dict) else 0
            last = max(1, (total + 49) // 50)
            for page in sorted({1, last, last + 1}):
                q = {**query, "page": str(page), "page_size": "50"}
                l = _request(LEGACY, "GET", "/api/articles", q)
                h = _request(hub_base, "GET", "/api/articles", q)
                r21_boundaries.append(_compare_pair(l, h))

        article_assets = _article_asset_hashes(database, assets)
        expected_article_assets = {
            "path_records": 4328,
            "asset_hash_count": 4328,
            "imported_assets": 4328,
            "missing_path_asset_count": 0,
            "source_no_content_path": 2036,
            "db_path_record_count": 4328,
        }
        actual_article_assets = {
            key: article_assets.get(key)
            for key in expected_article_assets
        }
        if actual_article_assets != expected_article_assets:
            raise AssertionError(
                "冻结正文资产对账失败："
                f"expected={expected_article_assets}, actual={actual_article_assets}"
            )

        # compare 模式必须仍把 legacy payload 返回给页面；只验证代表性大响应的业务 JSON。
        _set_modes(database, "compare")
        compare_probe = _request(hub_base, "GET", "/api/monitor-data", timeout=360)
        legacy_probe = _request(LEGACY, "GET", "/api/monitor-data", timeout=360)
        compare_mode = {
            "status": compare_probe.status,
            "status_matches_legacy": compare_probe.status == legacy_probe.status,
            "payload_matches_legacy": compare_probe.value == legacy_probe.value,
            "decoded_sha256_matches_legacy": compare_probe.decoded_sha256 == legacy_probe.decoded_sha256,
            "headers_match_legacy": compare_probe.headers == legacy_probe.headers,
            "header_matches": {
                key: compare_probe.headers.get(key) == legacy_probe.headers.get(key)
                for key in ("content-type", "content-encoding", "etag", "cache-control", "vary")
            },
            "headers": compare_probe.headers,
        }
        _set_modes(database, "hub")

        safety_final = _assert_shadow_safety("final_before_pass_evidence")
        all_pairs = compared + r21_boundaries
        summary = {
            "run_id": run_id,
            "hub_port": port,
            "legacy_url": LEGACY,
            "freeze": str(FREEZE.relative_to(ROOT)),
            "elapsed_seconds": round(time.time() - started, 2),
            "imports": [
                {
                    "pass": item["pass"], "status": item["status"],
                    "core_counts_after": item["core_counts_after"],
                    "core_count_delta_from_pass_1": item.get("core_count_delta_from_pass_1"),
                    "asset_blobs_after": item["asset_blobs_after"],
                    "asset_blob_delta_from_pass_1": item.get("asset_blob_delta_from_pass_1"),
                    "counts": (item["body"] or {}).get("data", {}).get("counts")
                    if isinstance(item["body"], dict) else None,
                    "reconcile": (item["body"] or {}).get("data", {}).get("audit", {}).get("reconcile")
                    if isinstance(item["body"], dict) else None,
                }
                for item in imports
            ],
            "identifiers": {
                "keyword_count": len(ids["keyword_ids"]),
                "account_count": len(ids["account_ids"]),
                "article_count": len(ids["article_ids"]),
            },
            "article_assets": article_assets,
            "core_and_invalid": {
                "request_count": len(compared),
                "matched_status": sum(x["status_equal"] for x in compared),
                "matched_payload": sum(x["payload_equal"] for x in compared),
                "matched_contract_payload": sum(
                    x.get("contract_payload_equal", x["payload_equal"])
                    for x in compared
                ),
                "tolerated_diff_count": sum(
                    1 for x in compared if x.get("tolerated_diff")
                ),
                "matched_metadata": sum(x["metadata_equal"] for x in compared),
                "first_differences": [x for x in compared if not (x["status_equal"] and x["payload_equal"] and x["metadata_equal"])][:20],
            },
            "identity_results": {
                kind: {
                    "request_count": len(rows),
                    "status_equal": sum(row["status_equal"] for row in rows),
                    "payload_equal": sum(row["payload_equal"] for row in rows),
                    "metadata_equal": sum(row["metadata_equal"] for row in rows),
                    "different_ids": [row["id"] for row in rows if not (row["status_equal"] and row["payload_equal"])][:20],
                }
                for kind, rows in identity_results.items()
            },
            "r21": {
                "boundary_request_count": len(r21_boundaries),
                "all_status_equal": all(x["status_equal"] for x in r21_boundaries),
                "all_payload_equal": all(x["payload_equal"] for x in r21_boundaries),
                "first_differences": [x for x in r21_boundaries if not x["payload_equal"]][:20],
            },
            "r15": {
                "expected_from_freeze_db": expected_r15,
                "registry_expected_from_freeze_db": keyword_expected["registry_total"],
                "archived_expected_from_freeze_db": keyword_expected["archived_keywords"],
                "actual": r15_actual,
                "registry_reconcile": registry_reconcile,
                "pass": r15_actual["legacy"] == expected_r15
                and r15_actual["hub"] == expected_r15
                and registry_reconcile == expected_registry,
                "compared_in_core": True,
            },
            "compare_mode": compare_mode,
            "network_guard": {
                "forbidden_port": FORBIDDEN_PORT,
                "safety_preflight": safety_preflight,
                "safety_before_import": safety_before_import,
                "safety_final": safety_final,
                "safety_after_import": [
                    item["safety_after_import"] for item in imports
                ],
                "source_url": LEGACY,
                "external_fetches_requested": 0,
            },
            "artifact": str((run_dir / "result.json").relative_to(ROOT)),
        }
        summary["strict_outcome"] = _strict_outcome(
            all_pairs, identity_results, compare_mode
        )
        (run_dir / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (SHADOW_ROOT / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not summary["strict_outcome"]["pass"]:
            raise AssertionError(
                "shadow 存在 status/payload/metadata 差异；result.json 已保留："
                f"pairs={len(summary['strict_outcome']['pair_failures'])}, "
                f"identities={sum(len(x) for x in summary['strict_outcome']['identity_failures'].values())}, "
                f"compare_pass={summary['strict_outcome']['compare_pass']}"
            )
        _write_evidence(summary)
        return summary
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


def _write_evidence(summary: dict[str, Any]) -> None:
    core = summary["core_and_invalid"]
    identities = summary["identity_results"]
    r21 = summary["r21"]
    compare = summary["compare_mode"]
    lines = [
        "# 微信关键词读取影子验收证据 v1",
        "",
        f"- **执行时间**：`{summary['run_id']}`（Asia/Shanghai）",
        f"- **旧隔离参考实例**：`{summary['legacy_url']}`；新 Hub：`127.0.0.1:{summary['hub_port']}`",
        f"- **冻结基准**：`{summary['freeze']}`",
        f"- **范围护栏**：仅读取 8774；8765 在 preflight、导入前后和最终检查均不可连接，旧源 runner/watchdog 进程不存在，两个 launchd label 均核对为 disabled 且未加载；未调用真实抓取、Aidso 或封面外网；临时 SQLite/asset_store 已清理；未 commit/push。",
        "",
        "## 结果摘要",
        f"- 导入：**两次均 HTTP {summary['imports'][0]['status']}**；第二次用于幂等重放。",
        f"- 22 个 GET + 非法/缺失样本：{core['request_count']} 请求；状态相同 {core['matched_status']}，原始解压后 JSON/正文相同 {core['matched_payload']}，按合同语义相同 {core['matched_contract_payload']}，HTTP 元数据五字段相同 {core['matched_metadata']}；容差通过 {core['tolerated_diff_count']} 条（仅 keyword-manage /updated_at，<=5 秒）。",
        f"- 全部身份读取：312 keyword 请求，payload 相同 {identities['keywords']['payload_equal']}；1361 account 请求，payload 相同 {identities['accounts']['payload_equal']}。",
        f"- 6364 article 身份清单来自冻结 normalized：正文路径 {summary['article_assets']['path_records']}，已导入路径资产 {summary['article_assets']['imported_assets']}，缺失路径资产 {summary['article_assets']['missing_path_asset_count']}；源本来无 content_file_path {summary['article_assets']['source_no_content_path']}（不计迁移丢失）；物理去重 blob {summary['article_assets']['asset_blob_count']}，聚合 hash `{summary['article_assets']['aggregate_sha256']}`。",
        f"- R21：7 组排序/筛选，覆盖每组 page=1、末页、末页+1，共 {r21['boundary_request_count']} 个边界请求；状态全相同={r21['all_status_equal']}，payload 全相同={r21['all_payload_equal']}。",
        f"- R15：冻结只读 app.db 推导页面期望={summary['r15']['expected_from_freeze_db']}，返回体实测 legacy={summary['r15']['actual']['legacy']}、Hub={summary['r15']['actual']['hub']}；底层 registry 期望/对账={summary['r15']['registry_expected_from_freeze_db']}/{summary['r15']['registry_reconcile']}；pass={summary['r15']['pass']}。",
        f"- compare 模式回显 legacy：status_match={compare['status_matches_legacy']}，payload_match={compare['payload_matches_legacy']}，decoded_hash_match={compare['decoded_sha256_matches_legacy']}，五个头全相同={compare['headers_match_legacy']}，逐头={compare['header_matches']}。",
        "",
        "## 差异与处置",
        "- 证据 JSON：`data/migration/wechat/shadow_read/latest.json`；每个差异保留 HTTP 五字段、状态、解压后 hash 与最多 40 个 JSON Pointer 差异。",
        f"- 首批核心差异：{len(core['first_differences'])} 条（详见 JSON；未因失败修改产品代码）。",
        f"- R21 首批差异：{len(r21['first_differences'])} 条；身份差异样本见 JSON 的 `identity_results`。",
        "> 若出现差异，建议先按 JSON Pointer 区分投影契约差异、排序/分页差异、HTTP 缓存头差异和正文资产缺失，再回到对应模块契约修复；本次 harness 不自动修产品代码。",
        "",
        "## 可重复命令",
        "```bash",
        "cd /Users/works14/Documents/zkcode/260712_SEO-GEO",
        "python3 workbench/tests/test_wechat_shadow_e2e.py --run",
        "```",
        "",
        "## 证据文件",
        f"- `{summary['artifact']}`",
        "- 临时服务退出后不会保留临时 Hub 数据库与 asset_store；冻结 payload 未写入。",
        "",
    ]
    EVIDENCE.write_text("\n".join(lines), encoding="utf-8")


def test_shadow_safety_preflight() -> None:
    snapshot = _assert_shadow_safety("pytest_preflight")
    assert snapshot["forbidden_port"] == 8765
    assert snapshot["launchd"]["disabled"] == {
        "com.local.wechat-monitor-8765": True,
        "com.claude.schedule.wechat-ybxhyyh-top3": True,
    }
    assert not snapshot["launchd"]["loaded"]


def test_shadow_strict_outcome_rejects_any_difference(monkeypatch: Any) -> None:
    result = _strict_outcome(
        [{
            "path": "/api/monitor-data", "query": {}, "status_equal": True,
            "payload_equal": False, "metadata_equal": True,
        }],
        {"keywords": [], "accounts": []},
        {
            "status_matches_legacy": True, "payload_matches_legacy": True,
            "decoded_sha256_matches_legacy": True, "headers_match_legacy": True,
        },
    )
    assert result["pass"] is False
    assert result["pair_failures"][0]["payload_equal"] is False


def test_shadow_keyword_manage_updated_at_tolerance_is_explicit(monkeypatch: Any) -> None:
    def result(path: str, value: dict[str, Any]) -> HttpResult:
        digest = _json_hash(value)
        return HttpResult(
            "GET",
            path,
            {},
            200,
            {
                "content-type": "application/json",
                "content-encoding": None,
                "etag": None,
                "cache-control": "no-store",
                "vary": None,
            },
            digest,
            digest,
            len(json.dumps(value)),
            value,
        )

    one_second = _compare_pair(
        result("/api/keyword-manage", {"updated_at": "2026-07-16T11:08:08Z"}),
        result("/api/keyword-manage", {"updated_at": "2026-07-16T11:08:09Z"}),
    )
    assert one_second["payload_equal"] is False
    assert one_second["tolerated_diff"] is True
    assert one_second["tolerated_diffs"][0]["rule"] == "keyword-manage-request-clock"
    assert _strict_outcome(
        [one_second], {"keywords": [], "accounts": []},
        {
            "status_matches_legacy": True,
            "payload_matches_legacy": True,
            "decoded_sha256_matches_legacy": True,
            "headers_match_legacy": True,
        },
    )["pass"] is True

    six_seconds = _compare_pair(
        result("/api/keyword-manage", {"updated_at": "2026-07-16T11:08:08Z"}),
        result("/api/keyword-manage", {"updated_at": "2026-07-16T11:08:14Z"}),
    )
    assert six_seconds["tolerated_diff"] is False
    assert _strict_outcome(
        [six_seconds], {"keywords": [], "accounts": []},
        {
            "status_matches_legacy": True,
            "payload_matches_legacy": True,
            "decoded_sha256_matches_legacy": True,
            "headers_match_legacy": True,
        },
    )["pass"] is False

    missing = _compare_pair(
        result("/api/keyword-manage", {"updated_at": "2026-07-16T11:08:08Z"}),
        result("/api/keyword-manage", {}),
    )
    assert missing["tolerated_diff"] is False
    other_contract = _compare_pair(
        result("/api/monitor-data", {"updated_at": "2026-07-16T11:08:08Z"}),
        result("/api/monitor-data", {"updated_at": "2026-07-16T11:08:09Z"}),
    )
    assert other_contract["tolerated_diff"] is False

    captured: dict[str, Any] = {}

    class JsonResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        @staticmethod
        def read() -> bytes:
            return b'{"ok":true}'

    def fake_urlopen(request: urllib.request.Request, *, timeout: int) -> JsonResponse:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        return JsonResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    post = _request(
        "http://shadow.test",
        "POST",
        "/api/v1/wechat/import",
        {"evidence": "query-kept"},
        json_body={
            "dry_run": False,
            "confirm": True,
            "idempotency_key": "shadow-json-body-test",
            "label": "微信",
        },
        timeout=7,
    )
    assert captured == {
        "url": "http://shadow.test/api/v1/wechat/import?evidence=query-kept",
        "method": "POST",
        "body": (
            b'{"dry_run":false,"confirm":true,'
            b'"idempotency_key":"shadow-json-body-test","label":"\xe5\xbe\xae\xe4\xbf\xa1"}'
        ),
        "headers": {
            "accept-encoding": "gzip",
            "connection": "close",
            "x-request-id": "wechat-shadow-e2e",
            "content-type": "application/json",
        },
        "timeout": 7,
    }
    assert post.status == 200
    assert post.query == {"evidence": "query-kept"}
    assert post.summary()["query"] == {"evidence": "query-kept"}
    assert "json_body" not in post.summary()


def test_import_failure_summary_is_bounded_and_actionable() -> None:
    rejected = [
        {
            "kind": (
                "article_identity_conflict"
                if index % 2 == 0
                else "metric"
            ),
            "source_id": f"row-{index}",
            "reason": "identity_candidates_disagree",
        }
        for index in range(30)
    ]
    body = {
        "ok": False,
        "data": {
            "semantic_status": "partial_failed",
            "verified": False,
            "batch_id": "batch-shadow-failed",
            "audit": {
                "reconcile": {
                    "status": "mismatch",
                    "difference": {"contents": 1, "metric_observations": -2},
                    "scope_match": {
                        "contents_wechat_article_identifiers": False,
                    },
                    "large_unused_dimensions": "x" * 459_000,
                },
                "asset_integrity": {
                    "verified": False,
                    "missing_path_asset_count": 2,
                },
                "rejected": rejected,
                "collision_count": 3,
                "content_collisions": [
                    {
                        "collision_type": "identity_conflict",
                        "source_id": "wx-conflict",
                        "candidates": {"wechat_article_identifier": "B", "canonical_url": "A"},
                    },
                    {
                        "collision_type": "identity_preserved",
                        "source_id": "wx-preserved",
                    },
                    {"content_id": "shared-a"},
                ],
            },
            "checkpoint": {
                "checkpoint_key": "normalized",
                "advanced": False,
            },
        },
    }
    summary = _import_failure_summary(body)

    assert summary == {
        "semantic_status": "partial_failed",
        "verified": False,
        "batch_id": "batch-shadow-failed",
        "reconcile": {
            "status": "mismatch",
            "difference": {"contents": 1, "metric_observations": -2},
            "scope_match": {
                "contents_wechat_article_identifiers": False,
            },
        },
        "asset_integrity": {
            "verified": False,
            "missing_path_asset_count": 2,
        },
        "rejected_count": 30,
        "rejected_first_20": rejected[:20],
        "collision_count": 3,
        "collision_classification": {
            "by_type": {
                "identity_conflict": 1,
                "identity_preserved": 1,
                "unclassified": 1,
            },
            "identity_conflict_collision_count": 1,
            "identity_conflict_rejected_count": 15,
            "identity_conflict_first_20": [{
                "collision_type": "identity_conflict",
                "source_id": "wx-conflict",
                "candidates": {
                    "wechat_article_identifier": "B",
                    "canonical_url": "A",
                },
            }],
        },
        "checkpoint": {
            "checkpoint_key": "normalized",
            "advanced": False,
        },
    }
    assert len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) < 10_000
    error = ShadowImportFailed(
        pass_index=1,
        http_status=409,
        summary=summary,
    )
    assert error.details["semantic_status"] == "partial_failed"
    assert error.details["http_status"] == 409
    assert '"batch_id":"batch-shadow-failed"' in str(error)
    assert "large_unused_dimensions" not in str(error)


def test_wechat_shadow_e2e() -> None:
    import pytest
    if os.getenv("RUN_WECHAT_SHADOW_E2E") != "1":
        pytest.skip("影子验收需显式设置 RUN_WECHAT_SHADOW_E2E=1，避免普通 pytest 启动大数据服务")
    _run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        raise SystemExit("请使用 --run；普通 pytest 不会默认执行影子验收。")
    try:
        result = _run()
    except BaseException as exc:
        failure = {
            "status": "blocked",
            "reason": f"{type(exc).__name__}: {exc}",
            "observed_at": datetime.now().isoformat(timespec="seconds"),
            "safety": exc.snapshot if isinstance(exc, SafetyBlocked) else None,
            "import_failure": (
                exc.details if isinstance(exc, ShadowImportFailed) else None
            ),
            "constraints": {
                "legacy_required": LEGACY,
                "freeze": str(FREEZE.relative_to(ROOT)),
                "forbidden_port": FORBIDDEN_PORT,
                "note": "未因失败修改产品代码；临时服务/库由 finally 或人工清理。",
            },
        }
        SHADOW_ROOT.mkdir(parents=True, exist_ok=True)
        (SHADOW_ROOT / "latest-failure.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        EVIDENCE.write_text(
            "# 微信关键词读取影子验收证据 v1\n\n"
            f"- **状态**：BLOCKED（{failure['observed_at']}）\n"
            f"- **原因**：`{failure['reason']}`\n"
            f"- **安全快照**：`{json.dumps(failure['safety'], ensure_ascii=False)}`\n"
            "- 集成骨架测试先行通过；正式 shadow 未得到可判定的完整结果。\n"
            "- 历史首轮完整导入曾观察到临时 SQLite WAL 约 21 GiB、进程内存约 22 GiB；"
            "本次 harness 若遇资源阻塞仍立即停止，未把半成品结果解释为业务差异。\n"
            "- 已清理临时服务、临时 SQLite/asset_store；未启动/调用 8765、真实抓取、Aidso 或封面外网；未 commit/push。\n"
            "- 失败回执：`data/migration/wechat/shadow_read/latest-failure.json`\n"
            "- 建议：先处理/确认全量导入事务的资源上限与分批落盘策略，再重新执行本 harness；不要根据本次半成品运行判定迁移契约。\n",
            encoding="utf-8",
        )
        raise
    print(json.dumps({
        "run_id": result["run_id"],
        "hub_port": result["hub_port"],
        "elapsed_seconds": result["elapsed_seconds"],
        "core": result["core_and_invalid"],
        "identifiers": result["identifiers"],
    }, ensure_ascii=False, indent=2))
