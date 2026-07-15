from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class WechatSourceError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "source_unavailable", status: int | None = None, payload: dict[str, Any] | None = None) -> None:
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
    payload: dict[str, Any]
    status: int


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
    "keyword_read_deltas.json": "keyword_deltas",
}
ARTICLE_LIST_RE = re.compile(r"^####\s+文章列表\s*$", re.MULTILINE)
MAX_MARKDOWN_SCAN_BYTES = 1024 * 1024
MAX_MARKDOWN_SCAN_LINES = 10000


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
    OPTIONAL_FILES = {"keyword_read_deltas": "normalized/keyword_read_deltas.json"}

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.base_url = str(settings.wechat_source_url).rstrip("/")
        self.root = Path(settings.wechat_source_root)
        self.timeout = float(settings.wechat_source_timeout_seconds)

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
        return result

    def manifest_id(self, manifest: dict[str, dict[str, Any]]) -> str:
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _request_response(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> RemoteResponse:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw, status = response.read(), int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try: body_value = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError): body_value = {"raw": raw.decode("utf-8", "replace")}
            raise WechatSourceError(f"旧微信搜一搜返回 HTTP {exc.code}", kind="remote_http", status=exc.code, payload=body_value if isinstance(body_value, dict) else {}) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WechatSourceError(f"旧微信搜一搜服务不可用：{exc}") from exc
        try: value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatSourceError("旧微信搜一搜服务返回了无效 JSON", kind="invalid_source_payload", status=status) from exc
        if not isinstance(value, dict):
            raise WechatSourceError("旧微信搜一搜服务返回结构不是对象", kind="invalid_source_payload", status=status)
        return RemoteResponse(value, status)

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

    def read_markdown(self, relative: str) -> str:
        """只读读取旧系统正文，禁止绝对路径、越界路径和软链接逃逸。"""
        if not relative or Path(str(relative)).is_absolute():
            raise WechatSourceError("正文路径必须是旧系统根目录下的相对路径", kind="path_not_allowed", status=403)
        try:
            resolved = (self.root / str(relative)).resolve()
            resolved.relative_to(self.root.resolve())
            if not resolved.exists():
                raise FileNotFoundError(str(resolved))
            if not resolved.is_file():
                raise FileNotFoundError(str(resolved))
            return resolved.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise WechatSourceError(f"微信正文不存在：{relative}", kind="content_not_found", status=404) from exc
        except ValueError as exc:
            raise WechatSourceError(f"微信正文路径越界：{relative}", kind="path_not_allowed", status=403) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise WechatSourceError(f"读取微信正文失败：{relative}: {exc}", kind="content_read_failed") from exc

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
