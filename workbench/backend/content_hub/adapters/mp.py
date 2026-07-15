from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class MpSourceError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, kind: str = "source_unavailable", payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.payload = _scrub(payload if isinstance(payload, dict) else {})


@dataclass(frozen=True, slots=True)
class RemoteResponse:
    payload: dict[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class BinaryResponse:
    content: bytes
    status: int
    content_type: str


SOURCE_TZ = ZoneInfo("Asia/Shanghai")
_ALLOWED_CATEGORIES = (
    "热门产品", "z产品对比", "z香港vs内地", "港险优惠", "美联储降息", "保司盘点",
    "什么是香港保险", "香港储蓄险", "z非热门产品", "其他", "新加坡保险",
)
_SECRET_KEYS = {"password", "passwd", "token", "access_token", "refresh_token", "api_key", "apikey", "secret", "username", "user_name"}
_EXCLUDED_DIR_NAMES = {"wiki", "wiki-viewer", "WritingMoney", "微信搜索结果", "排除流", "候选区", "output", "临时目录", "临时", "tmp", "temp"}
_DATE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_AUTHOR_RE = re.compile(r"^\s*公众号\s*[：:]\s*(.+?)\s*$")


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if str(k).lower() in _SECRET_KEYS else _scrub(v) for k, v in value.items() if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _normalise_body(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _utc_date_from_name(name: str) -> str | None:
    match = _DATE_RE.search(name)
    if not match:
        return None
    try:
        value = datetime.strptime(match.group(1), "%y%m%d").replace(tzinfo=SOURCE_TZ)
    except ValueError:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_datetime(value: str) -> str | None:
    raw = str(value or "").strip().replace("/", "-")
    if not raw:
        return None
    parsed = None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            pass
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                pass
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SOURCE_TZ)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _filename_facts(stem: str, metadata_names: list[str]) -> dict[str, Any]:
    match = re.match(r"^(?P<date>\d{6})_(?P<body>.+)$", stem)
    if not match:
        date_match = _DATE_RE.search(stem)
        return {"mp_name": None, "ingested_date_hint": date_match.group(1) if date_match else None, "warning": "ambiguous_filename_metadata"}
    body = match["body"]
    candidates = [name for name in metadata_names if name and (body == name or body.startswith(name + "_"))]
    if len(candidates) != 1:
        return {"mp_name": None, "ingested_date_hint": match["date"], "warning": "ambiguous_filename_mp_name"}
    return {"mp_name": candidates[0], "ingested_date_hint": match["date"], "filename_title": body[len(candidates[0]):].lstrip("_")}


def parse_markdown(path: Path, root: Path, category: str, *, metadata_names: list[str] | None = None) -> dict[str, Any]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            chunks.append(chunk)
    raw = b"".join(chunks)
    text = raw.decode("utf-8")
    normalised = _normalise_body(text)
    title = next((m.group(1).strip() for line in normalised.split("\n") if (m := _H1_RE.match(line))), None)
    author = next((m.group(1).strip() for line in reversed(normalised.split("\n")) if (m := _AUTHOR_RE.match(line))), None)
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    warnings: list[str] = []
    if title is None:
        warnings.append("missing_h1")
    if author is None:
        warnings.append("missing_author")
    filename = _filename_facts(path.stem, metadata_names or [])
    if filename.get("warning"):
        warnings.append(filename["warning"])
    mp_name = filename.get("mp_name")
    if mp_name and not author:
        author = mp_name
    file_mtime_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "relative_path": relative,
        "absolute_path": str(path),
        "category": category,
        "title": title,
        "author": author,
        "mp_name": mp_name or author,
        "mp_id": None,
        "published_at": None,
        "ingested_date_hint": filename.get("ingested_date_hint"),
        "file_hash": digest.hexdigest(),
        "content_hash": hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "file_mtime_at": file_mtime_at,
        "source_updated_at": file_mtime_at,
        "size": stat.st_size,
        "integrity_warnings": warnings,
        "canonical_url": None,
    }


def _trusted_url(value: str) -> str | None:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "mp.weixin.qq.com" or not parsed.path:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", parsed.query, ""))


def _attach_metadata(record: dict[str, Any], rows: list[dict[str, Any]]) -> set[str]:
    title = _match_text(record.get("title"))
    mp_name = _match_text(record.get("mp_name"))
    title_candidates = [row for row in rows if row.get("_title_norm") == title]
    candidates = [row for row in title_candidates if mp_name and row.get("_mp_name_norm") == mp_name]
    method = "title+filename_mp" if len(candidates) == 1 else None
    if len(candidates) != 1:
        candidates = title_candidates
        method = "title_only" if len(candidates) == 1 else None
    identities = {str(row["_identity"]) for row in candidates}
    if len(candidates) == 1:
        row = candidates[0]
        record["canonical_url"] = _trusted_url(row.get("url", ""))
        record["mp_id"] = row.get("mp_id") or None
        record["mp_name"] = row.get("mp_name") or record.get("mp_name")
        record["author"] = record.get("author") or row.get("mp_name")
        record["integrity_warnings"] = [warning for warning in record["integrity_warnings"] if warning != "missing_author"]
        record["published_at"] = _source_datetime(row.get("publish_time") or row.get("publish_date", ""))
        record["source_updated_at"] = max(record.get("source_updated_at", ""), row.get("_source_mtime_at") or record.get("source_updated_at", ""))
        record["metadata_source"] = {"path": row.get("_source_path"), "row": row.get("_source_row")}
        record["metadata_match_status"] = "matched"
        record["metadata_match_method"] = method
        record["metadata_match_confidence"] = 1.0 if method == "title+filename_mp" else 0.9
        record["_metadata_identity"] = row["_identity"]
        if not record["canonical_url"]:
            record["integrity_warnings"].append("untrusted_metadata_url")
    elif len(candidates) > 1:
        record["integrity_warnings"].append("ambiguous_metadata_match")
        record["metadata_match_status"] = "ambiguous"
        record["metadata_match_method"] = "title+filename_mp" if mp_name else "title_only"
        record["metadata_match_confidence"] = 0.0
    elif title and mp_name:
        record["integrity_warnings"].append("metadata_not_found")
        record["metadata_match_status"] = "unmatched"
        record["metadata_match_method"] = "title+filename_mp" if mp_name else "title_only"
        record["metadata_match_confidence"] = 0.0
    else:
        record["metadata_match_status"] = "unmatched"
        record["metadata_match_method"] = None
        record["metadata_match_confidence"] = 0.0
    return identities if record.get("metadata_match_status") == "matched" else set()


class MpAdapter:
    """公众号监控的 live 代理和只读 Markdown 发现器。"""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.base_url = str(settings.mp_source_url).rstrip("/")
        self.root = Path(settings.mp_source_root).expanduser().absolute()
        self.rejected_csv_path = Path(settings.mp_rejected_csv_path).expanduser().absolute()
        self.metadata_root = Path(settings.mp_metadata_root).expanduser().absolute()
        self.timeout = float(settings.mp_source_timeout_seconds)
        self.categories = tuple(sorted(set(settings.mp_categories or _ALLOWED_CATEGORIES) & set(_ALLOWED_CATEGORIES)))

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> RemoteResponse:
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
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {"message": raw.decode("utf-8", "replace")}
            kind = "upstream_auth_invalid" if exc.code in {401, 403} else "remote_http"
            raise MpSourceError(f"公众号监控上游返回 HTTP {exc.code}", status=exc.code, kind=kind, payload=parsed) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MpSourceError(f"公众号监控上游不可用：{exc}", kind="timeout" if isinstance(exc, TimeoutError) else "source_unavailable") from exc
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MpSourceError("公众号监控上游返回无效 JSON", status=status, kind="invalid_source_payload") from exc
        if not isinstance(parsed, dict):
            raise MpSourceError("公众号监控上游返回结构不是对象", status=status, kind="invalid_source_payload")
        return RemoteResponse(_scrub(parsed), status)

    def health(self) -> RemoteResponse: return self._request("/health")
    def runtime_overview(self) -> RemoteResponse: return self._request("/api/runtime/overview")
    def accounts(self) -> RemoteResponse: return self._request("/api/accounts")
    def categories_remote(self) -> RemoteResponse: return self._request("/api/categories")
    def jobs(self) -> RemoteResponse: return self._request("/api/jobs")
    def auth_check(self) -> RemoteResponse: return self._request("/api/auth/wechat/check", method="POST", payload={})
    def auth_qrcode(self) -> RemoteResponse: return self._request("/api/auth/wechat/qrcode", method="POST", payload={})
    def auth_qrcode_finish(self) -> RemoteResponse: return self._request("/api/auth/wechat/qrcode/finish", method="POST", payload={})
    def update_flags(self, mp_id: str, payload: dict[str, Any]) -> RemoteResponse: return self._request(f"/api/accounts/{urllib.parse.quote(mp_id, safe='')}/flags", method="PATCH", payload=payload)
    def create_job(self, payload: dict[str, Any]) -> RemoteResponse: return self._request("/api/jobs", method="POST", payload=payload)
    def job(self, job_id: str) -> RemoteResponse: return self._request(f"/api/jobs/{urllib.parse.quote(job_id, safe='')}")
    def cancel_job(self, job_id: str) -> RemoteResponse: return self._request(f"/api/jobs/{urllib.parse.quote(job_id, safe='')}/cancel", method="POST", payload={})

    def auth_qrcode_image(self, qr_id: str, *, max_bytes: int = 5 * 1024 * 1024) -> BinaryResponse:
        request = urllib.request.Request(
            self.base_url + f"/api/auth/wechat/qrcode/image/{urllib.parse.quote(qr_id, safe='')}",
            headers={"Accept": "image/png,image/*"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise MpSourceError("二维码图片超过大小限制", status=413, kind="payload_too_large")
                    chunks.append(chunk)
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip()
                if not content_type.lower().startswith("image/"):
                    raise MpSourceError("二维码上游返回了非图片 Content-Type", status=502, kind="invalid_source_payload")
                return BinaryResponse(b"".join(chunks), int(response.status), content_type)
        except urllib.error.HTTPError as exc:
            raise MpSourceError(f"公众号监控上游返回 HTTP {exc.code}", status=exc.code, kind="remote_http") from exc
        except MpSourceError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MpSourceError(f"公众号监控上游不可用：{exc}", kind="timeout" if isinstance(exc, TimeoutError) else "source_unavailable") from exc

    def _tree_manifest(self) -> list[dict[str, Any]]:
        if self.root.is_symlink() or not self.root.is_dir():
            raise MpSourceError(f"公众号正文根目录不可用或是 symlink：{self.root}", kind="invalid_source_root")
        result: list[dict[str, Any]] = []
        for category in sorted(self.categories):
            directory = self.root / category
            if not directory.is_dir() or directory.is_symlink():
                continue
            for current, dirs, files in os.walk(directory, topdown=True, followlinks=False):
                current_path = Path(current)
                dirs[:] = sorted(name for name in dirs if not (current_path / name).is_symlink() and not name.startswith((".", "_")) and name not in _EXCLUDED_DIR_NAMES)
                for filename in files:
                    path = current_path / filename
                    if path.is_symlink() or path.suffix.lower() != ".md":
                        continue
                    try:
                        stat = path.stat()
                        digest = hashlib.sha256()
                        with path.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                    except OSError:
                        continue
                    result.append({"path": path.relative_to(self.root).as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "file_hash": digest.hexdigest()})
        return result

    def _metadata_manifest(self) -> list[dict[str, Any]]:
        if not self.metadata_root.is_dir() or self.metadata_root.is_symlink():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(self.metadata_root.glob("*.csv")):
            if path.is_symlink() or not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            stat = path.stat()
            result.append({"path": path.relative_to(self.metadata_root).as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()})
        return result

    def _load_metadata(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw_rows: list[dict[str, Any]] = []
        if not self.metadata_root.is_dir() or self.metadata_root.is_symlink():
            return [], {"raw_rows": [], "unique_rows": []}
        for path in sorted(self.metadata_root.glob("*.csv")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    for index, row in enumerate(csv.DictReader(handle), start=2):
                        normalized = {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
                        normalized["_source_path"] = path.relative_to(self.metadata_root).as_posix()
                        normalized["_source_row"] = index
                        normalized["_source_mtime_ns"] = path.stat().st_mtime_ns
                        normalized["_source_mtime_at"] = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                        raw_url = normalized.get("url", "")
                        normalized["_invalid_url"] = bool(raw_url and _trusted_url(raw_url) is None)
                        raw_rows.append(normalized)
            except (OSError, UnicodeDecodeError, csv.Error):
                continue
        for row in raw_rows:
            row["_title_norm"] = _match_text(row.get("title"))
            row["_mp_name_norm"] = _match_text(row.get("mp_name"))
            canonical_url = _trusted_url(row.get("url", ""))
            stable_fields = {key: value for key, value in row.items() if not key.startswith("_")}
            stable_fallback = hashlib.sha256(
                json.dumps(stable_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            row["_identity"] = f"url:{canonical_url}" if canonical_url else f"id:{row.get('id') or stable_fallback}"
        unique: dict[str, dict[str, Any]] = {}
        for row in raw_rows:
            unique.setdefault(str(row["_identity"]), row)
        return list(unique.values()), {"raw_rows": raw_rows, "unique_rows": list(unique.values())}

    def _scan_once(self, *, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.root.is_symlink() or not self.root.is_dir():
            raise MpSourceError(f"公众号正文根目录不可用或是 symlink：{self.root}", kind="invalid_source_root")
        root = self.root.resolve()
        records: list[dict[str, Any]] = []
        metadata_rows, metadata_info = self._load_metadata()
        metadata_names = sorted({row.get("mp_name", "") for row in metadata_rows if row.get("mp_name")})
        matched_metadata: set[str] = set()
        skipped: list[dict[str, str]] = []
        for category in sorted(self.categories):
            directory = root / category
            if directory.is_symlink():
                skipped.append({"path": str(directory), "reason": "symlink"})
                continue
            if not directory.is_dir():
                continue
            for current, dirs, files in os.walk(directory, topdown=True, followlinks=False):
                current_path = Path(current)
                dirs[:] = sorted(name for name in dirs if not (current_path / name).is_symlink() and not name.startswith((".", "_")) and name not in _EXCLUDED_DIR_NAMES)
                for filename in sorted(files):
                    path = current_path / filename
                    if path.is_symlink() or path.suffix.lower() != ".md":
                        if path.is_symlink(): skipped.append({"path": str(path), "reason": "symlink"})
                        continue
                    try:
                        resolved = path.resolve(strict=True)
                        if root not in resolved.parents or resolved.is_symlink():
                            skipped.append({"path": str(path), "reason": "root_escape"})
                            continue
                        record = parse_markdown(resolved, root, category, metadata_names=metadata_names)
                        matched_metadata |= _attach_metadata(record, metadata_rows)
                        records.append(record)
                    except (OSError, UnicodeDecodeError, ValueError) as exc:
                        skipped.append({"path": str(path), "reason": type(exc).__name__})
                    if limit is not None and len(records) >= limit:
                        break
                if limit is not None and len(records) >= limit:
                    break
        rejected = _rejected_report(self.rejected_csv_path, root)
        csv_only = [row for row in metadata_rows if str(row["_identity"]) not in matched_metadata and not row.get("_invalid_url")]
        rejected_metadata = [
            {
                "source": row.get("_source_path"),
                "row": row.get("_source_row"),
                "title": row.get("title") or None,
                "reason": "invalid_url",
            }
            for row in metadata_rows
            if row.get("_invalid_url")
        ]
        manifest = {
            "root": str(root),
            "categories": list(self.categories),
            "metadata_root": str(self.metadata_root),
            "metadata_files": self._metadata_manifest(),
            "metadata_rows": csv_only,
            "reconcile": {
                "csv_rows_raw": len(metadata_info["raw_rows"]),
                "csv_unique": len(metadata_rows),
                "md_matched": sum(1 for row in records if row.get("metadata_match_status") == "matched"),
                "md_ambiguous": sum(1 for row in records if row.get("metadata_match_status") == "ambiguous"),
                "md_unmatched": sum(1 for row in records if row.get("metadata_match_status") == "unmatched"),
                "csv_only": len(csv_only),
            },
            "files": [{k: row[k] for k in ("relative_path", "file_hash", "content_hash", "mtime_ns", "size")} for row in records],
            "skipped": skipped[:100],
            "rejected": rejected_metadata[:100],
            "rejected_articles": rejected,
        }
        return records, manifest

    def scan(self, *, limit: int | None = None, max_consistency_attempts: int = 3) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        for _ in range(max_consistency_attempts):
            before = {"files": self._tree_manifest(), "metadata": self._metadata_manifest(), "rejected_articles": _rejected_manifest(self.rejected_csv_path, self.root)}
            records, manifest = self._scan_once(limit=limit)
            after = {"files": self._tree_manifest(), "metadata": self._metadata_manifest(), "rejected_articles": _rejected_manifest(self.rejected_csv_path, self.root)}
            if before == after:
                return records, manifest
        raise MpSourceError("公众号正文在读取期间发生变化，拒绝导入", kind="source_changed_during_read")


def _rejected_manifest(path: Path, root: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        if path.is_symlink() or not path.is_file():
            raise OSError("not a regular file")
    except OSError:
        return {"path": str(path), "size": None, "mtime_ns": None, "sha256": None, "rows": 0, "columns": [], "samples": []}
    digest = hashlib.sha256()
    rows = 0
    columns: list[str] = []
    samples: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = sorted(reader.fieldnames or [])
            for row in reader:
                rows += 1
                if len(samples) < 3:
                    samples.append(_scrub({key: str(value)[:200] for key, value in row.items()}))
    except (OSError, UnicodeDecodeError, csv.Error):
        return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest(), "rows": None, "columns": columns, "samples": []}
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest(), "rows": rows, "columns": columns, "samples": samples}


def _rejected_report(path: Path, root: Path) -> dict[str, Any]:
    report = _rejected_manifest(path, root)
    try:
        report["path"] = str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        report["path"] = str(path)
    return report
