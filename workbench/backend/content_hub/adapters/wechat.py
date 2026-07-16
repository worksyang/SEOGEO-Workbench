from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import sqlite3
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from content_hub.services.contract_diff import HTTPMetadata


class WechatSourceError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "source_unavailable", status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.payload = payload or {}


@dataclass(frozen=True, slots=True)
class SourceResult:
    payload: dict[str, Any]
    source: str
    status: str
    checked_at: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteResponse:
    payload: Any
    status: int
    metadata: HTTPMetadata = HTTPMetadata()


_ROW = {"type": "object"}
_SOURCE_SCHEMAS: dict[str, dict[str, Any]] = {
    "monitor": {
        "type": "object",
        "required": ["keywords"],
        "properties": {
            "keywords": {"type": "array", "items": {"type": "object", "required": ["keyword_id", "keyword"]}},
            "accounts": {"type": "array", "items": {"type": "object"}},
        },
    },
    "accounts": {"type": "array", "items": {"type": "object", "required": ["account_id"], "properties": {"account_id": {"type": "string"}}}},
    "articles": {"type": "array", "items": {"type": "object", "required": ["article_id"], "properties": {"article_id": {"type": "string"}}}},
    "snapshots": {"type": "array", "items": {"type": "object", "required": ["snapshot_id", "captured_at"], "properties": {"snapshot_id": {"type": "string"}, "captured_at": {"type": ["string", "null"]}}}},
    "hits": {"type": "array", "items": {"type": "object", "required": ["snapshot_id", "rank"], "properties": {"snapshot_id": {"type": "string"}, "rank": {"type": "integer"}}}},
    "terms": {"type": "array", "items": {"type": "object", "required": ["snapshot_id", "term_type", "position", "term_text"], "properties": {"snapshot_id": {"type": "string"}, "term_type": {"type": "string"}, "position": {"type": "integer"}, "term_text": {"type": "string"}}}},
    "observations": {"type": "array", "items": {"type": "object", "required": ["observation_id", "article_id", "observed_at"], "properties": {"observation_id": {"type": "string"}, "article_id": {"type": "string"}, "observed_at": {"type": ["string", "null"]}}}},
    "keyword_deltas": {"type": "array", "items": {"type": "object", "required": ["keyword_id", "keyword", "window_end", "status", "daily_read_delta_points"], "properties": {"keyword_id": {"type": "string"}, "keyword": {"type": "string"}, "window_end": {"type": "string"}, "status": {"enum": ["ok", "insufficient_data"]}, "daily_read_delta_points": {"type": "array"}}}},
    "registry": {"type": "object", "additionalProperties": {"type": "object"}},
    # 冻结产物的指标元数据包含 generated_at 字符串和统计对象，
    # 不是 snapshot_registry 的动态字典；两者不能共用 schema。
    "article_metric_meta": {
        "type": "object",
        "required": ["generated_at", "source_files", "outputs"],
        "properties": {
            "generated_at": {"type": "string"},
            "source_files": {"type": "object"},
            "outputs": {"type": "object"},
            "observation_stats": {"type": "object"},
            "keyword_delta_stats": {"type": "object"},
        },
    },
    "runtime_object": {"type": "object"},
}
SCHEMA_BY_FILENAME = {
    "monitor-data.json": "monitor",
    "accounts.json": "accounts",
    "articles.json": "articles",
    "snapshots.json": "snapshots",
    "snapshot_registry.json": "registry",
    "ranking_hits.json": "hits",
    "snapshot_terms.json": "terms",
    "article_metric_observations.json": "observations",
    "article_metric_observations_meta.json": "article_metric_meta",
    "scheduler.json": "runtime_object",
    "keyword_refresh_ledger.json": "runtime_object",
    "keyword_read_deltas.json": "keyword_deltas",
}
ARTICLE_LIST_RE = re.compile(r"^####\s+文章列表\s*$", re.MULTILINE)
MAX_MARKDOWN_SCAN_BYTES = 1024 * 1024
MAX_MARKDOWN_SCAN_LINES = 10000

# Runtime artifacts are intentionally not added to Git or copied into the
# canonical payload manifest.  Their sidecar is nevertheless trusted only
# when this version-controlled anchor recognizes the exact freeze and exact
# sidecar bytes.  New freezes must add a reviewed entry here before they can
# use a runtime-artifact supplement.
TRUSTED_RUNTIME_SUPPLEMENT_SEALS: dict[str, dict[str, str]] = {
    "freeze_20260716T024524+0800": {
        "filename": "freeze_20260716T024524+0800-runtime-artifacts.json",
        "sha256": "27cfe16f9970761cbbfc0fcdff8da14ce8ad58b05e25e527e26b0a64744d9bca",
    },
}


class WechatAdapter:
    """微信搜一搜只读适配器；缓存按源文件 manifest 自动失效，绝不写旧系统。"""

    _cache_lock = threading.RLock()
    _json_cache: dict[tuple[str, str], tuple[tuple[int, int, str], Any]] = {}

    FILES = {
        "monitor": "normalized/monitor-data.json",
        "accounts": "normalized/accounts.json",
        "articles": "normalized/articles.json",
        "snapshots": "normalized/snapshots.json",
        "registry": "normalized/snapshot_registry.json",
        "hits": "normalized/ranking_hits.json",
        "terms": "normalized/snapshot_terms.json",
        "observations": "normalized/article_metric_observations.json",
    }
    OPTIONAL_FILES = {
        "keyword_read_deltas": "normalized/keyword_read_deltas.json",
        "article_metric_meta": "normalized/article_metric_observations_meta.json",
    }

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.base_url = str(settings.wechat_source_url).rstrip("/")
        self.root = Path(settings.wechat_source_root)
        self.timeout = float(settings.wechat_source_timeout_seconds)

    def verify_freeze_seal(self) -> dict[str, Any]:
        """Verify the immutable freeze seal without ever rewriting it.

        The current freeze ships both ``file-manifest.jsonl`` (size + SHA-256)
        and ``MANIFEST.sha256``.  The JSONL file is the authoritative seal
        because it also carries the required byte size.  Temporary unit-test
        roots are intentionally outside ``freeze_*`` and remain unsealed.
        """
        root = self.root.resolve()
        freeze_root = root.parent if root.name == "payload" else root
        if not freeze_root.name.startswith("freeze_"):
            return {"status": "not_applicable", "root": str(root)}

        manifest_path = freeze_root / "file-manifest.jsonl"
        checksum_path = freeze_root / "MANIFEST.sha256"
        if not manifest_path.is_file() and not checksum_path.is_file():
            raise WechatSourceError(
                f"冻结包 seal manifest 不存在：{freeze_root}",
                kind="freeze_seal_missing",
                status=409,
            )

        expected: dict[str, dict[str, Any]] = {}
        manifest_entry_count = 0
        if manifest_path.is_file():
            try:
                for line_number, line in enumerate(
                    manifest_path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if not line.strip():
                        continue
                    manifest_entry_count += 1
                    item = json.loads(line)
                    raw_path = str(item.get("path") or "").replace("\\", "/")
                    if not raw_path.startswith("payload/"):
                        continue
                    relative = raw_path[len("payload/") :]
                    expected[relative] = {
                        "size": int(item["size"]),
                        "sha256": str(item["sha256"]),
                    }
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise WechatSourceError(
                    f"冻结包 seal manifest 无法读取：{manifest_path}",
                    kind="freeze_seal_invalid",
                    status=409,
                ) from exc
        else:
            # Compatibility fallback for older sealed packages.  Size is
            # still checked from the immutable checksum entry's observed
            # file, while the JSONL seal remains preferred when present.
            try:
                for line in checksum_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        manifest_entry_count += 1
                    digest, raw_path = line.split(maxsplit=1)
                    raw_path = raw_path.replace("\\", "/")
                    if raw_path.startswith("payload/"):
                        expected[raw_path[len("payload/") :]] = {
                            "size": None,
                            "sha256": digest,
                        }
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise WechatSourceError(
                    f"冻结包 checksum seal 无法读取：{checksum_path}",
                    kind="freeze_seal_invalid",
                    status=409,
                ) from exc
        if not expected:
            raise WechatSourceError(
                f"冻结包 seal 没有 payload 条目：{freeze_root}",
                kind="freeze_seal_invalid",
                status=409,
            )

        actual: dict[str, Path] = {}
        symlinks: list[str] = []
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in list(dirnames):
                path = directory_path / name
                if path.is_symlink():
                    symlinks.append(str(path.relative_to(root)).replace("\\", "/"))
                    dirnames.remove(name)
            for name in filenames:
                path = directory_path / name
                relative = str(path.relative_to(root)).replace("\\", "/")
                if path.is_symlink():
                    symlinks.append(relative)
                else:
                    actual[relative] = path
        expected_paths = set(expected)
        actual_paths = set(actual)
        missing = sorted(expected_paths - actual_paths)
        # Only the exact runtime artifacts recorded by the external
        # supplemental seal are exempted.  A newly-created or renamed pyc is
        # not accepted merely because it lives under __pycache__.
        supplemental_path = freeze_root.parent / f"{freeze_root.name}-runtime-artifacts.json"
        supplemental: dict[str, dict[str, Any]] = {}
        if supplemental_path.is_file():
            try:
                trust = TRUSTED_RUNTIME_SUPPLEMENT_SEALS.get(freeze_root.name)
                if trust is None or supplemental_path.name != trust["filename"]:
                    raise ValueError(
                        f"未知 freeze 的 runtime artifact supplement：{freeze_root.name}"
                    )
                supplemental_bytes = supplemental_path.read_bytes()
                supplemental_sha256 = hashlib.sha256(supplemental_bytes).hexdigest()
                if supplemental_sha256 != trust["sha256"]:
                    raise ValueError(
                        f"runtime artifact supplement SHA-256 不受信任：{supplemental_path.name}"
                    )
                payload = json.loads(supplemental_bytes.decode("utf-8"))
                for item in payload.get("artifacts", []):
                    relative = str(item["path"]).replace("\\", "/")
                    # Only pre-existing Python bytecode below an actual
                    # __pycache__ directory may be supplemented. Any other
                    # exemption is malformed and must fail closed.
                    if (
                        relative.startswith("/")
                        or ".." in Path(relative).parts
                        or not re.search(r"/__pycache__/[^/]+\.pyc$", f"/{relative}")
                    ):
                        raise ValueError(f"非法 runtime artifact 路径：{relative}")
                    supplemental[relative] = {
                        "size": int(item["size"]),
                        "sha256": str(item["sha256"]),
                    }
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise WechatSourceError(
                    f"冻结包 runtime artifact supplement 无法读取：{supplemental_path}",
                    kind="freeze_seal_invalid",
                    status=409,
                ) from exc
        runtime_artifacts = sorted(
            path for path in (actual_paths - expected_paths)
            if path in supplemental
        )
        unsealed_runtime = sorted(
            path for path in (actual_paths - expected_paths)
            if "/__pycache__/" in f"/{path}" and path.endswith(".pyc")
            and path not in supplemental
        )
        added = sorted((actual_paths - expected_paths) - set(runtime_artifacts))
        if unsealed_runtime:
            added = sorted(set(added) | set(unsealed_runtime))
        if symlinks or missing or added:
            raise WechatSourceError(
                "冻结包 seal 文件集合不一致",
                kind="freeze_seal_mismatch",
                status=409,
                payload={
                    "missing": missing[:50],
                    "added": added[:50],
                    "symlinks": sorted(symlinks)[:50],
                    "ignored_runtime_artifacts": runtime_artifacts[:50],
                    "missing_count": len(missing),
                    "added_count": len(added),
                    "symlink_count": len(symlinks),
                    "ignored_runtime_artifact_count": len(runtime_artifacts),
                },
            )
        mismatches: list[dict[str, Any]] = []
        ignored_ds_store: list[dict[str, Any]] = []
        for relative in sorted(expected_paths):
            path = actual[relative]
            item = expected[relative]
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if (
                (item["size"] is not None and stat.st_size != item["size"])
                or (
                    digest.hexdigest() != item["sha256"]
                    and Path(relative).name != ".DS_Store"
                )
            ):
                mismatches.append({
                    "path": relative,
                    "expected_size": item["size"],
                    "actual_size": stat.st_size,
                    "expected_sha256": item["sha256"],
                    "actual_sha256": digest.hexdigest(),
                })
            elif (
                Path(relative).name == ".DS_Store"
                and digest.hexdigest() != item["sha256"]
            ):
                ignored_ds_store.append({
                    "path": relative,
                    "size": stat.st_size,
                    "expected_sha256": item["sha256"],
                    "actual_sha256": digest.hexdigest(),
                    "reason": "sealed_non_fact_ds_store_mismatch",
                })
        ignored: list[dict[str, Any]] = []
        for relative in runtime_artifacts:
            path = actual[relative]
            item = supplemental[relative]
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.st_size != item["size"] or digest != item["sha256"]:
                mismatches.append({
                    "path": relative,
                    "expected_size": item["size"],
                    "actual_size": stat.st_size,
                    "expected_sha256": item["sha256"],
                    "actual_sha256": digest,
                    "reason": "runtime_artifact_supplement_mismatch",
                })
            else:
                ignored.append({
                    "path": relative,
                    "size": stat.st_size,
                    "sha256": digest,
                    "reason": "sealed_runtime_artifact",
                })
        if mismatches:
            raise WechatSourceError(
                "冻结包 seal 校验失败",
                kind="freeze_seal_mismatch",
                status=409,
                payload={"mismatches": mismatches[:50], "mismatch_count": len(mismatches)},
            )
        return {
            "status": "verified",
            "manifest": str(manifest_path if manifest_path.is_file() else checksum_path),
            "entry_count": manifest_entry_count,
            "payload_entry_count": len(expected),
            "manifest_entry_count": manifest_entry_count,
            "ignored_runtime_artifact_count": len(ignored),
            "ignored_runtime_artifacts": ignored[:50],
            "ignored_ds_store_mismatch_count": len(ignored_ds_store),
            "ignored_ds_store_mismatches": ignored_ds_store[:50],
        }

    def file_manifest(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, relative in {**self.FILES, **self.OPTIONAL_FILES}.items():
            path = self.root / relative
            if not path.is_file():
                if key in self.OPTIONAL_FILES:
                    continue
                raise WechatSourceError(f"本地微信事实文件不存在：{path}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            stat = path.stat()
            result[key] = {"path": relative, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}
        db_path = self.runtime_db_path()
        if db_path is not None:
            digest = hashlib.sha256()
            with db_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            stat = db_path.stat()
            result["runtime_db"] = {
                "path": str(db_path.relative_to(self.root)).replace("\\", "/"),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
        runtime_files = [
            self.root / "data/state/scheduler.json",
            self.root / "data/state/keyword_refresh_ledger.json",
        ]
        jobs_root = self.root / "data/refresh_jobs"
        if jobs_root.is_dir():
            runtime_files.extend(sorted(jobs_root.glob("*.json")))
        for path in runtime_files:
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[f"runtime:{path.relative_to(self.root)}"] = {
                "path": str(path.relative_to(self.root)).replace("\\", "/"),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": digest,
            }
        return result

    def runtime_db_path(self) -> Path | None:
        """Resolve a frozen app.db without assuming a particular freeze_id."""
        candidates = (
            self.root / "data/state/app.db",
            self.root / "state/app.db",
            self.root / "sqlite/app.db",
            self.root / "app.db",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def runtime_records(self) -> dict[str, list[dict[str, Any]]]:
        path = self.runtime_db_path()
        if path is None:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            names = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in (
                "keyword_groups", "keyword_registry",
                "keyword_discovery_candidates", "keyword_discovery_evidence",
                "keyword_discovery_probes",
            ):
                if table in names:
                    result[table] = [
                        dict(row) for row in con.execute(f"SELECT * FROM {table}")
                    ]
        for key, relative in (
            ("scheduler", "data/state/scheduler.json"),
            ("keyword_refresh_ledger", "data/state/keyword_refresh_ledger.json"),
        ):
            value = self.local_json(relative, required=False)
            if value is not None:
                result[key] = value if isinstance(value, list) else [value]
        jobs = []
        jobs_root = self.root / "data/refresh_jobs"
        if jobs_root.is_dir():
            for path in sorted(jobs_root.glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    value["_source_file"] = str(path.relative_to(self.root)).replace("\\", "/")
                    jobs.append(value)
        if jobs:
            result["refresh_jobs"] = jobs
        runs_root = self.root / "data/runs"
        runs = []
        if runs_root.is_dir():
            for path in sorted(runs_root.glob("*/state.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    value["_source_file"] = str(path.relative_to(self.root)).replace("\\", "/")
                    runs.append(value)
        if runs:
            result["batch_runs"] = runs
        return result

    def manifest_id(self, manifest: dict[str, dict[str, Any]]) -> str:
        stable = {
            key: {
                "path": value.get("path"),
                "size": value.get("size"),
                "sha256": value.get("sha256"),
            }
            for key, value in manifest.items()
        }
        raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _request_response(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        allow_http_errors: bool = False,
        allow_any_json: bool = False,
    ) -> RemoteResponse:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)

        def header_value(header_map: Any, name: str) -> str | None:
            """Read real urllib headers and small test doubles alike."""
            if header_map is None:
                return None
            getter = getattr(header_map, "get", None)
            if callable(getter):
                value = getter(name)
                if value is not None:
                    return str(value)
            try:
                value = header_map[name]
            except (KeyError, IndexError, TypeError, AttributeError):
                value = None
            if value is not None:
                return str(value)
            items = getattr(header_map, "items", None)
            if callable(items):
                for key, item in items():
                    if str(key).lower() == name.lower():
                        return str(item)
            if name.lower() == "content-type":
                content_type = getattr(header_map, "get_content_type", None)
                if callable(content_type):
                    return str(content_type())
            return None

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw, status = response.read(), int(response.status)
                metadata = HTTPMetadata(
                    status_code=status,
                    content_type=header_value(response.headers, "Content-Type"),
                    content_encoding=header_value(response.headers, "Content-Encoding"),
                    etag=header_value(response.headers, "ETag"),
                    cache_control=header_value(response.headers, "Cache-Control"),
                    vary=header_value(response.headers, "Vary"),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try: body_value = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError): body_value = {"raw": raw.decode("utf-8", "replace")}
            if allow_http_errors:
                headers = exc.headers
                content_encoding = header_value(headers, "Content-Encoding")
                if (content_encoding or "").lower() == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                        body_value = json.loads(raw.decode("utf-8")) if raw else {}
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        body_value = {"raw": raw.decode("utf-8", "replace")}
                return RemoteResponse(
                    body_value if (allow_any_json or isinstance(body_value, dict)) else {"raw": body_value},
                    int(exc.code),
                    HTTPMetadata(
                        status_code=int(exc.code),
                        content_type=header_value(headers, "Content-Type"),
                        content_encoding=content_encoding,
                        etag=header_value(headers, "ETag"),
                        cache_control=header_value(headers, "Cache-Control"),
                        vary=header_value(headers, "Vary"),
                    ),
                )
            raise WechatSourceError(
                f"旧微信搜一搜返回 HTTP {exc.code}",
                kind="remote_http",
                status=exc.code,
                payload=body_value if (allow_any_json or isinstance(body_value, dict)) else {},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WechatSourceError(f"旧微信搜一搜服务不可用：{exc}") from exc
        if metadata.content_encoding and metadata.content_encoding.lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError as exc:
                raise WechatSourceError("旧微信搜一搜服务返回了无效 gzip JSON", kind="invalid_source_payload", status=status) from exc
        try: value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatSourceError("旧微信搜一搜服务返回了无效 JSON", kind="invalid_source_payload", status=status) from exc
        if not allow_any_json and not isinstance(value, dict):
            raise WechatSourceError("旧微信搜一搜服务返回结构不是对象", kind="invalid_source_payload", status=status)
        return RemoteResponse(value, status, metadata)

    def _request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request_response(path, **kwargs).payload

    @staticmethod
    def _validate(value: Any, name: str) -> Any:
        key = SCHEMA_BY_FILENAME.get(Path(name).name)
        schema = _SOURCE_SCHEMAS.get(key, {"type": "array", "items": _ROW})
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
        if errors:
            error = errors[0]
            location = ".".join(str(item) for item in error.path) or "$"
            raise WechatSourceError(f"源 JSON Schema 校验失败：{name}:{location} {error.message}", kind="invalid_source_payload")
        return value

    def local_json(self, relative: str, *, required: bool = True) -> Any:
        path = self.root / relative
        try:
            path.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            raise WechatSourceError(f"微信源路径越界：{relative}", kind="path_not_allowed") from exc
        try:
            stat = path.stat()
        except FileNotFoundError:
            if required: raise WechatSourceError(f"本地微信事实文件不存在：{path}")
            return None
        except OSError as exc:
            raise WechatSourceError(f"读取本地微信事实文件失败：{path}: {exc}", kind="invalid_source_payload") from exc
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
        fingerprint = (stat.st_size, stat.st_mtime_ns, digest.hexdigest())
        cache_key = (str(self.root), relative)
        with self._cache_lock:
            cached = self._json_cache.get(cache_key)
            if cached and cached[0] == fingerprint:
                return cached[1]
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatSourceError(f"读取本地微信事实文件失败：{path}: {exc}", kind="invalid_source_payload") from exc
        value = self._validate(value, relative)
        with self._cache_lock:
            self._json_cache[cache_key] = (fingerprint, value)
        return value

    def read_markdown_with_source(self, relative: str) -> tuple[str, str]:
        """安全读取冻结正文，并返回实际命中的 root 相对路径。

        冻结包优先保留 ``root/<原 relative>``；仅当它不存在时，才允许读取
        ``root/code-snapshot/<原 relative>``。两条候选都拒绝绝对路径、父级
        穿越、非 Markdown 以及任一层软链接。
        """
        original = str(relative or "").replace("\\", "/")
        parts = original.split("/")
        if (
            not original
            or Path(original).is_absolute()
            or not original.lower().endswith(".md")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise WechatSourceError(
                "正文路径必须是旧系统根目录下的 .md 相对路径",
                kind="path_not_allowed",
                status=400,
            )
        try:
            root_resolved = self.root.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise WechatSourceError(
                f"微信正文根目录不可用：{self.root}",
                kind="content_read_failed",
            ) from exc

        for prefix in ((), ("code-snapshot",)):
            relative_parts = (*prefix, *parts)
            candidate = self.root.joinpath(*relative_parts)
            current = self.root
            for part in relative_parts:
                current = current / part
                if current.is_symlink():
                    raise WechatSourceError(
                        "正文路径包含软链接",
                        kind="path_not_allowed",
                        status=400,
                    )
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise WechatSourceError(
                    f"微信正文路径越界：{relative}",
                    kind="path_not_allowed",
                    status=400,
                ) from exc
            if not candidate.is_file():
                continue
            try:
                return (
                    candidate.read_text(encoding="utf-8"),
                    "/".join(relative_parts),
                )
            except (OSError, UnicodeDecodeError) as exc:
                raise WechatSourceError(
                    f"读取微信正文失败：{relative}: {exc}",
                    kind="content_read_failed",
                ) from exc
        raise WechatSourceError(
            f"微信正文不存在：{relative}",
            kind="content_not_found",
            status=404,
        )

    def read_markdown(self, relative: str) -> str:
        """保持旧调用契约，只返回正文文本。"""
        markdown, _ = self.read_markdown_with_source(relative)
        return markdown

    @classmethod
    def clear_cache_for_root(cls, root: Path | str) -> None:
        """释放指定旧源根目录的 JSON 缓存，不影响其他源实例。"""
        root_key = str(Path(root))
        with cls._cache_lock:
            for cache_key in [key for key in cls._json_cache if key[0] == root_key]:
                del cls._json_cache[cache_key]

    def remote_bootstrap(self) -> dict[str, Any]: return self._request("/api/monitor-data/bootstrap")
    def remote_keyword(self, keyword_id: str) -> dict[str, Any]: return self._request(f"/api/monitor-data/keyword/{urllib.parse.quote(keyword_id, safe='')}")
    def remote_refresh(self, keyword_id: str, keyword: str) -> RemoteResponse:
        return self._request_response(f"/api/keywords/{urllib.parse.quote(keyword_id, safe='')}/refresh", method="POST", payload={"keyword": keyword})
    def remote_article_content(self, path: str) -> dict[str, Any]:
        return self._request("/api/article-content?path=" + urllib.parse.quote(path, safe=""))

    def remote_article_content_response(self, path: str) -> RemoteResponse:
        """读取正文时保留旧接口的真实 HTTP status/headers。"""
        return self._request_response(
            "/api/article-content?path=" + urllib.parse.quote(path, safe=""),
            allow_http_errors=True,
        )
    def remote_hit_detail(self, article_id: str, url: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"article_id": article_id, "url": url})
        return self._request("/api/article-hit-detail?" + query)

    def remote_refresh_status(self, job_id: str) -> dict[str, Any]:
        return self._request("/api/refresh-status/" + urllib.parse.quote(job_id, safe=""))

    def bootstrap(self) -> SourceResult:
        try: return SourceResult(self.remote_bootstrap(), "legacy_http", "healthy")
        except WechatSourceError as exc:
            try: return SourceResult(self.local_json(self.FILES["monitor"]), "legacy_normalized", "degraded", error=str(exc))
            except WechatSourceError: raise exc

    def all_records(self) -> dict[str, Any]:
        loaded = {key: self.local_json(path) for key, path in self.FILES.items()}
        loaded["keyword_read_deltas"] = self.local_json(self.OPTIONAL_FILES["keyword_read_deltas"], required=False) or []
        loaded["article_metric_meta"] = self.local_json(self.OPTIONAL_FILES["article_metric_meta"], required=False) or {}
        loaded["runtime"] = self.runtime_records()
        if isinstance(loaded["monitor"], dict): loaded["keywords"] = loaded["monitor"].get("keywords", [])
        else: loaded["keywords"] = loaded["monitor"]
        return loaded

    def detail_records(self) -> dict[str, Any]:
        return {
            key: self.local_json(self.FILES[key])
            for key in ("snapshots", "hits", "articles", "terms", "observations")
        }

    def import_records(self, *, limit: int | None = None, max_consistency_attempts: int = 3) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        records = None
        manifest = None
        for attempt in range(1, max_consistency_attempts + 1):
            before = self.file_manifest()
            candidate = self.all_records()
            after = self.file_manifest()
            if before == after:
                manifest, records = before, candidate
                break
        if records is None or manifest is None:
            raise WechatSourceError(
                f"源文件在读取期间持续变化，拒绝导入（尝试 {max_consistency_attempts} 次）",
                kind="source_changed_during_read",
            )
        records["_root"] = str(self.root)
        full_records = {key: value for key, value in records.items()}
        full_audit = self._registry_audit(full_records)
        if limit is not None:
            snapshots = sorted(records["snapshots"], key=lambda x: str(x.get("captured_at") or ""), reverse=True)[:limit]
            snapshot_ids = {x.get("snapshot_id") for x in snapshots}
            hit_rows = [x for x in records["hits"] if x.get("snapshot_id") in snapshot_ids]
            article_ids = {x.get("article_id") for x in hit_rows if x.get("article_id")}
            records["snapshots"] = snapshots
            records["hits"] = hit_rows
            records["terms"] = [x for x in records["terms"] if x.get("snapshot_id") in snapshot_ids]
            records["articles"] = [x for x in records["articles"] if x.get("article_id") in article_ids]
            records["observations"] = [x for x in records["observations"] if x.get("article_id") in article_ids]
            account_ids = {x.get("account_id") for x in records["articles"] if x.get("account_id")}
            records["accounts"] = [x for x in records["accounts"] if x.get("account_id") in account_ids]
            keyword_ids = {x.get("keyword_id") for x in snapshots if x.get("keyword_id")}
            records["keywords"] = [x for x in records["keywords"] if x.get("keyword_id") in keyword_ids]
        selection_audit = {**full_audit, "selection_snapshot_count": len(records["snapshots"]), "selection_snapshot_ids": sorted(x.get("snapshot_id") for x in records["snapshots"])}
        return records, manifest, selection_audit

    @staticmethod
    def _registry_audit(records: dict[str, Any]) -> dict[str, Any]:
        root = Path(records.get("_root", ".")) if isinstance(records.get("_root"), str) else None
        normalized = {str(x.get("source_file_path")).replace("\\", "/").lstrip("./") for x in records["snapshots"] if x.get("source_file_path")}
        registry = records["registry"] if isinstance(records["registry"], dict) else {}
        registry_paths: set[str] = set()
        for value in registry:
            path = Path(str(value))
            if root and path.is_absolute():
                try: path = path.relative_to(root)
                except ValueError: pass
            registry_paths.add(str(path).replace("\\", "/").lstrip("./"))
        orphan = sorted(registry_paths - normalized)
        missing = sorted(normalized - registry_paths)
        missing_headings = []
        scan_limited = []
        for path in normalized:
            candidate = (root / path) if root else None
            if candidate and candidate.is_file():
                try:
                    scanned_bytes = 0
                    scanned_lines = 0
                    found = False
                    with candidate.open("rb") as handle:
                        for raw_line in handle:
                            scanned_bytes += len(raw_line)
                            scanned_lines += 1
                            if ARTICLE_LIST_RE.match(raw_line.decode("utf-8", errors="replace").strip()):
                                found = True
                                break
                            if scanned_bytes >= MAX_MARKDOWN_SCAN_BYTES or scanned_lines >= MAX_MARKDOWN_SCAN_LINES:
                                scan_limited.append(path)
                                break
                    if not found and path not in scan_limited:
                        missing_headings.append(path)
                except OSError:
                    missing_headings.append(path)
        return {"registry_count": len(registry_paths), "normalized_snapshot_count": len(normalized), "orphan_count": len(orphan), "orphan_samples": orphan[:10], "missing_normalized_count": len(missing), "missing_normalized_samples": missing[:10], "unparsed_count": len(orphan), "markdown_missing_article_list_count": len(missing_headings), "markdown_missing_article_list_samples": missing_headings[:10], "scan_limited_count": len(scan_limited), "scan_limited_samples": scan_limited[:10]}
