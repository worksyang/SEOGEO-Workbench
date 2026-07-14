from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class XhsSourceError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "source_unavailable", status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.kind, self.status = kind, status
        self.payload = scrub(payload if isinstance(payload, dict) else {})


@dataclass(frozen=True, slots=True)
class RemoteResponse:
    payload: dict[str, Any]
    status: int


FILES = {
    "keywords": "keywords.json",
    "snapshots": "snapshots.json",
    "snapshot_terms": "snapshot_terms.json",
    "ranking_hits": "ranking_hits.json",
    "articles": "articles.json",
    "accounts": "accounts.json",
    "note_metric_observations": "note_metric_observations.json",
}
REQUIRED = {
    "keywords": {"keyword_id", "keyword_text"},
    "snapshots": {"snapshot_id", "keyword_id", "captured_at"},
    "snapshot_terms": {"term_id", "snapshot_id", "term_type", "position", "term_text"},
    "ranking_hits": {"hit_id", "snapshot_id", "rank", "article_id"},
    "articles": {"article_id", "account_id", "title"},
    "accounts": {"account_id", "canonical_name"},
    "note_metric_observations": {"observation_id", "article_id", "snapshot_id", "captured_at"},
}
_SECRET_KEYS = {"token", "xsec_token", "access_token", "refresh_token", "api_key", "apikey", "secret", "password"}


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if str(k).lower() in _SECRET_KEYS else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class XhsAdapter:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.base_url = str(settings.xhs_source_url).rstrip("/")
        self.root = Path(settings.xhs_normalized_root).expanduser().resolve()
        self.settings_db_path = Path(settings.xhs_settings_db_path).expanduser().resolve()
        self.timeout = float(settings.xhs_source_timeout_seconds)

    def _check_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise XhsSourceError(f"小红书 normalized 根目录不可用或是 symlink：{self.root}", kind="invalid_source_root")
        if not self.root.is_absolute():
            raise XhsSourceError("小红书 normalized 根目录必须是绝对路径", kind="invalid_source_root")

    def _path(self, relative: str) -> Path:
        self._check_root()
        path = (self.root / relative).resolve()
        if self.root not in path.parents or path.is_symlink():
            raise XhsSourceError(f"小红书事实文件越过安全根目录：{relative}", kind="path_traversal")
        return path

    def file_manifest(self) -> dict[str, dict[str, Any]]:
        result = {}
        for key, relative in FILES.items():
            path = self._path(relative)
            if not path.is_file():
                raise XhsSourceError(f"小红书事实文件不存在：{path}", kind="missing_source_file")
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[key] = {"path": relative, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}
        result["settings_db"] = self._settings_manifest()
        return result

    def _settings_manifest(self) -> dict[str, Any]:
        if self.settings_db_path.exists() and (self.settings_db_path.is_symlink() or not self.settings_db_path.is_file()):
            raise XhsSourceError(f"小红书 settings DB 不是普通文件：{self.settings_db_path}", kind="settings_db_error")
        if not self.settings_db_path.exists():
            return {"path": str(self.settings_db_path), "missing": True}
        try:
            rows = self.read_settings()
            semantic = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return {"path": str(self.settings_db_path), "semantic_sha256": semantic, "row_count": len(rows)}
        except XhsSourceError:
            raise
        except OSError as exc:
            raise XhsSourceError(f"读取小红书 settings DB 失败：{exc}", kind="settings_db_error") from exc

    def read_settings(self) -> list[dict[str, Any]]:
        if self.settings_db_path.exists() and (self.settings_db_path.is_symlink() or not self.settings_db_path.is_file()):
            raise XhsSourceError(f"小红书 settings DB 不是普通文件：{self.settings_db_path}", kind="settings_db_error")
        if not self.settings_db_path.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self.settings_db_path}?mode=ro", uri=True)
            try:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='keyword_settings'").fetchone()
                if not exists:
                    return []
                con.row_factory = sqlite3.Row
                return [dict(row) for row in con.execute("SELECT * FROM keyword_settings ORDER BY keyword_id")]
            finally:
                con.close()
        except (OSError, sqlite3.Error) as exc:
            raise XhsSourceError(f"读取小红书 settings DB 失败：{exc}", kind="settings_db_error") from exc

    @staticmethod
    def manifest_id(manifest: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _validate(self, key: str, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise XhsSourceError(f"{key}.json 顶层必须是数组", kind="invalid_source_payload")
        schema = {"type": "array", "items": {"type": "object", "required": sorted(REQUIRED[key])}}
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
        if errors:
            error = errors[0]
            location = ".".join(str(item) for item in error.path) or "$"
            raise XhsSourceError(f"{key}.json Schema 校验失败：{location} {error.message}", kind="invalid_source_payload")
        return value

    def read_records(self, *, max_attempts: int = 3) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        for _ in range(max_attempts):
            before = self.file_manifest()
            loaded: dict[str, list[dict[str, Any]]] = {}
            try:
                for key, relative in FILES.items():
                    path = self._path(relative)
                    loaded[key] = self._validate(key, json.loads(path.read_text(encoding="utf-8")))
                loaded["_settings"] = self.read_settings()  # type: ignore[assignment]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise XhsSourceError(f"读取小红书事实文件失败：{exc}", kind="invalid_source_payload") from exc
            after = self.file_manifest()
            if before == after:
                loaded["_root"] = str(self.root)  # type: ignore[assignment]
                return loaded, before
        raise XhsSourceError("小红书源文件在读取期间持续变化，拒绝导入（尝试 3 次）", kind="source_changed_during_read")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        allowed_error_statuses: set[int] | None = None,
    ) -> RemoteResponse:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw, status = response.read(), int(response.status)
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise XhsSourceError(f"小红书 live 服务不可用：{exc}", kind="source_unavailable") from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise XhsSourceError("小红书 live 服务返回无效 JSON", kind="invalid_source_payload", status=status) from exc
        if not isinstance(value, dict):
            raise XhsSourceError("小红书 live 服务返回结构不是对象", kind="invalid_source_payload", status=status)
        if not 200 <= status < 300 and status not in (allowed_error_statuses or set()):
            raise XhsSourceError(
                f"小红书 live 服务返回 HTTP {status}",
                kind="remote_http",
                status=status,
                payload=value,
            )
        return RemoteResponse(scrub(value), status)

    def bootstrap(self) -> RemoteResponse:
        return self._request("/api/monitor-data/bootstrap")

    def keyword(self, keyword_id: str) -> RemoteResponse:
        return self._request("/api/monitor-data/keyword/" + urllib.parse.quote(keyword_id, safe=""))

    def refresh(self, keyword_id: str) -> RemoteResponse:
        return self._request(
            "/api/keywords/" + urllib.parse.quote(keyword_id, safe="") + "/refresh",
            method="POST",
            payload={"confirm": True},
            allowed_error_statuses={409},
        )

    def account(self, account_id: str) -> RemoteResponse:
        return self._request("/api/monitor-data/account/" + urllib.parse.quote(account_id, safe=""))

    def refresh_status(self, job_id: str) -> RemoteResponse:
        return self._request("/api/refresh-status/" + urllib.parse.quote(job_id, safe=""))
