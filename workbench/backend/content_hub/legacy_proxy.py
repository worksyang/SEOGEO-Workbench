from __future__ import annotations

import json
import os
import posixpath
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from content_hub.db.connection import connect
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import AppError
from content_hub.services.wiki import WikiService
from content_hub.validation.timestamps import utc_now_iso


# 只代理两个原版业务岛屿已经使用的接口；禁止把工作台变成任意 URL 代理。
_ALLOWED_EXACT = frozenset({
    "monitor-data",
    "monitor-data/bootstrap",
    "creator-detail",
    "keyword-manage",
    "article-content",
    "article-covers",
    "article-hit-detail",
    "keyword-turnover",
    "article-cover-image",
    "note-detail",
    "articles",
    "account-aliases",
    "penalty-signals",
    "refresh-all",
    "keyword-discovery",
    "settings",
    "accounts",
    "categories",
    "jobs",
    "data",
    "import-json",
})
_ALLOWED_PATTERNS = (
    re.compile(r"^monitor-data/(?:keyword|account)/[^/]+$"),
    re.compile(r"^keywords/[^/]+/refresh$"),
    re.compile(r"^keywords/[^/]+/(?:pin|unpin|topic|note|bucket)$"),
    re.compile(r"^refresh-status/[^/]+$"),
    re.compile(r"^accounts/[^/]+$"),
    re.compile(r"^categories/[^/]+$"),
    re.compile(r"^jobs/[^/]+$"),
    re.compile(r"^runtime/[^/]+$"),
    re.compile(r"^auth/[^/]+$"),
    re.compile(r"^ai/[^/]+$"),
    re.compile(r"^agent/(?:manifest|daily-brief|metric-dictionary|evidence/[^/]+)$"),
    re.compile(r"^aidso/keyword-heat$"),
)


def _allowed(path: str) -> bool:
    normalized = path.lstrip("/")
    return normalized in _ALLOWED_EXACT or any(
        pattern.fullmatch(normalized) for pattern in _ALLOWED_PATTERNS
    )


def legacy_referer_kind(referer: str) -> str | None:
    """只接受工作台同源 Referer，避免外域伪造路径触发业务岛屿分流。"""
    raw = str(referer or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.port != 8799:
        return None
    path = parsed.path or ""
    if not path.startswith("/legacy/"):
        return None
    for kind in ("xhs", "mp", "geo", "wechat"):
        if path.startswith(f"/legacy/{kind}/"):
            return kind
    return None


def _upstream_url(base_url: str, path: str, query: str) -> str:
    encoded_path = urllib.parse.quote(
        path.lstrip("/"),
        safe="/:%@-._~!$&'()*+,;=",
    )
    url = f"{base_url.rstrip('/')}/api/{encoded_path}"
    return f"{url}?{query}" if query else url


_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "secret",
    "api_key",
    "apikey",
    "authorization",
}


def _redact_json_payload(payload: bytes, content_type: str) -> bytes:
    """旧控制台 settings/AI 响应不得把凭据回显到 iframe。

    仅对可解析 JSON 做递归字段脱敏；HTML、图片和二进制响应保持原样。
    """
    if "json" not in (content_type or "").lower():
        return payload
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload

    def redact(item: Any):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _audit_xhs_legacy_write(settings: Any, *, method: str, path: str) -> None:
    """记录被白名单代理拦截的旧小红书副作用请求。"""
    with writer_lock(settings.lock_path):
        with connect(settings) as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, actor_type, actor_id, action,
                    subject_type, subject_id, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit_{uuid.uuid4().hex[:16]}",
                    utc_now_iso(),
                    "legacy_proxy",
                    "legacy-xhs",
                    "xhs.legacy_write_blocked",
                    "legacy_endpoint",
                    path,
                    "blocked",
                    json.dumps(
                        {
                            "method": method,
                            "path": path,
                            "upstream_called": False,
                            "reason_code": "xhs.legacy_write_blocked",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )


def _audit_legacy_write_blocked(
    settings: Any,
    *,
    legacy_system: str,
    method: str,
    path: str,
) -> None:
    """所有原版 iframe 写请求必须止于工作台，不能绕过新运行层。"""
    with writer_lock(settings.lock_path):
        with connect(settings) as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, actor_type, actor_id, action,
                    subject_type, subject_id, outcome, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit_{uuid.uuid4().hex[:16]}",
                    utc_now_iso(),
                    "legacy_proxy",
                    f"legacy-{legacy_system}",
                    f"{legacy_system}.legacy_write_blocked",
                    "legacy_endpoint",
                    path,
                    "blocked",
                    json.dumps(
                        {
                            "method": method,
                            "path": path,
                            "upstream_called": False,
                            "reason_code": "legacy_write_requires_hub_command",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )


def _wiki_root(settings: Any):
    candidates = [
        root for root in settings.wiki_allowed_roots
        if root != settings.asset_store_path and root.is_dir() and (root / "wiki").is_dir()
    ]
    return sorted(candidates, key=lambda item: str(item))[0] if candidates else None


def _wiki_safe_path(root, relative: str, *, suffix: str | None = None):
    if not isinstance(relative, str) or not relative:
        return None
    normalized = posixpath.normpath(relative.replace("\\", "/")).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        return None
    target = root / normalized
    if suffix and target.suffix.lower() != suffix.lower():
        return None
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve())
        stat = resolved.stat()
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or target.is_symlink():
        return None
    return resolved


def _wiki_is_hidden(name: str) -> bool:
    return name.startswith(".")


def _wiki_md_files(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if not _wiki_is_hidden(name)
            and not (Path(base).resolve() == root.resolve() and name == "wiki-viewer")
            and not (Path(base) / name).is_symlink()
        ]
        for name in files:
            if name.endswith(".md") and not _wiki_is_hidden(name):
                path = Path(base) / name
                if not path.is_symlink():
                    yield path


def _wiki_count_md(path: Path) -> int:
    return sum(1 for _ in _wiki_md_files(path))


def _wiki_list_dir(root, relative: str) -> dict[str, Any] | None:
    base = root if not relative else _wiki_safe_dir(root, relative)
    if base is None or not base.is_dir():
        return None
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, str]] = []
    for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
        if _wiki_is_hidden(entry.name) or entry.is_symlink():
            continue
        rel = entry.relative_to(root).as_posix()
        if entry.is_dir():
            if base == root and entry.name == "wiki-viewer":
                continue
            dirs.append({"name": entry.name, "path": rel, "count": _wiki_count_md(entry)})
        elif entry.is_file() and entry.suffix.lower() == ".md":
            files.append({"name": entry.name, "path": rel})
    return {"dirs": dirs, "files": files}


def _wiki_safe_dir(root, relative: str):
    if not isinstance(relative, str):
        return None
    normalized = posixpath.normpath(relative.replace("\\", "/")).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        return root
    target = root / normalized
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if target.is_symlink() or not resolved.is_dir():
        return None
    return resolved


def _wiki_search(root, query: str, limit: int = 200) -> list[str]:
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[str] = []
    for path in sorted(_wiki_md_files(root), key=lambda item: item.relative_to(root).as_posix().lower()):
        relative = path.relative_to(root).as_posix()
        if needle in relative.lower():
            hits.append(relative)
            if len(hits) >= limit:
                break
    return hits


_IMG_LINE_RE = re.compile(r"^!\[.*?\]\((.+)\)$")


def _url_core(url: str) -> str:
    value = str(url).split("#", 1)[0].split("?", 1)[0]
    return re.sub(r"/\d+$", "", value)


def _image_hits(content: str, core: str) -> list[int]:
    hits: list[int] = []
    for index, line in enumerate(content.splitlines()):
        match = _IMG_LINE_RE.match(line.strip())
        if match and _url_core(match.group(1)) == core:
            hits.append(index)
    return hits


def _load_wiki_ocr(root: Path) -> dict[str, Any]:
    db_path = root / "wiki-viewer" / "ocr-db.json"
    try:
        db_path.resolve().relative_to(root.resolve())
        return json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """原子更新 Wiki OCR 索引，避免删除图片时留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _delete_wiki_ocr_record(root: Path, core: str) -> bool:
    """按图片 core 清理 output_md/wiki-viewer/ocr-db.json。"""
    db_path = root / "wiki-viewer" / "ocr-db.json"
    data = _load_wiki_ocr(root)
    if not data:
        return False
    kept = {
        key: value
        for key, value in data.items()
        if _url_core(key) != core
    }
    if len(kept) == len(data):
        return False
    _atomic_json_write(db_path, kept)
    return True


def _delete_image_ranges(content: str, core: str) -> tuple[str, int]:
    lines = content.splitlines()
    ranges: list[tuple[int, int]] = []
    for start in _image_hits(content, core):
        end = start + 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end < len(lines) and re.match(r"^<!--\s*(插图建议|OCR内容)", lines[end].strip()):
            while end < len(lines) and "-->" not in lines[end]:
                end += 1
            if end < len(lines):
                end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end < len(lines) and lines[end].strip() == "---":
            end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        ranges.append((start, end))
    if not ranges:
        return content, 0
    doomed = {index for start, end in ranges for index in range(start, end)}
    result = "\n".join(line for index, line in enumerate(lines) if index not in doomed)
    return result, len(ranges)


async def proxy_legacy_wiki_api(
    path: str,
    request: Request,
) -> Response:
    """兼容原 Wiki UI 的 API；正文和索引都直接操作 output_md。"""
    settings = request.app.state.settings
    root = _wiki_root(settings)
    if root is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Wiki 数据根不可用"})

    body = await request.body()
    query = urllib.parse.parse_qs(str(request.url.query))
    if path == "list" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("path", [""])[0])
        data = _wiki_list_dir(root, relative)
        return JSONResponse(status_code=200 if data is not None else 404, content=data or {"error": "invalid dir"})

    if path == "search" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("q", [""])[0])
        return JSONResponse(content={"files": _wiki_search(root, relative)})

    if path == "file" and request.method == "GET":
        relative = urllib.parse.unquote(query.get("path", [""])[0])
        target = _wiki_safe_path(root, relative, suffix=".md")
        if target is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        try:
            return JSONResponse(content={"path": relative, "content": target.read_text(encoding="utf-8")})
        except (OSError, UnicodeDecodeError):
            return JSONResponse(status_code=422, content={"error": "file unreadable"})

    if path == "ocr" and request.method == "GET":
        url = urllib.parse.unquote(query.get("url", [""])[0]).strip()
        if not url:
            return JSONResponse(status_code=400, content={"error": "missing url"})
        record = _load_wiki_ocr(root).get(_url_core(url))
        return JSONResponse(content={"ok": bool(record), "ocr": (record or {}).get("ocr", ""), "source": (record or {}).get("source", "")})

    if path == "scan-image" and request.method == "GET":
        core = urllib.parse.unquote(query.get("core", [""])[0]).strip()
        if not core:
            return JSONResponse(status_code=400, content={"error": "missing core"})
        results: list[dict[str, Any]] = []
        total = 0
        for file_path in _wiki_md_files(root):
            try:
                matches = _image_hits(file_path.read_text(encoding="utf-8"), core)
            except (OSError, UnicodeDecodeError):
                continue
            if matches:
                results.append({"path": file_path.relative_to(root).as_posix(), "count": len(matches)})
                total += len(matches)
        return JSONResponse(content={"ok": True, "total": total, "files": results})

    if path == "image-index" and request.method == "GET":
        index: dict[str, dict[str, Any]] = {}
        for file_path in _wiki_md_files(root):
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            counts: dict[str, int] = {}
            for line in lines:
                match = _IMG_LINE_RE.match(line.strip())
                if match:
                    core = _url_core(match.group(1))
                    counts[core] = counts.get(core, 0) + 1
            relative = file_path.relative_to(root).as_posix()
            for core, count in counts.items():
                item = index.setdefault(core, {"total": 0, "files": []})
                item["total"] += count
                item["files"].append({"path": relative, "count": count})
        return JSONResponse(content={"ok": True, "index": index})

    if path == "save" and request.method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "bad json"})
        relative = payload.get("path", "")
        content = payload.get("content", "")
        if not isinstance(content, str):
            return JSONResponse(status_code=400, content={"error": "content must be string"})
        try:
            with writer_lock(settings.lock_path):
                result = WikiService(
                    connection=connect(settings, readonly=False),
                    asset_root=Path(settings.asset_store_path),
                    source_roots=settings.wiki_allowed_roots,
                    lock_path=Path(settings.lock_path),
                ).save_source_ref(relative, body=content, operator="legacy-wiki")
            return JSONResponse(content={"ok": True, "path": relative, "data": result})
        except (FileNotFoundError, ValueError, OSError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    if path == "bulk-delete-image" and request.method == "POST":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "bad json"})
        core = str(payload.get("core") or "").strip()
        if not core:
            return JSONResponse(status_code=400, content={"error": "missing core"})
        deleted_files = 0
        deleted_images = 0
        try:
            with writer_lock(settings.lock_path):
                service = WikiService(
                    connection=connect(settings, readonly=False),
                    asset_root=Path(settings.asset_store_path),
                    source_roots=settings.wiki_allowed_roots,
                    lock_path=Path(settings.lock_path),
                )
                for file_path in list(_wiki_md_files(root)):
                    try:
                        original = file_path.read_text(encoding="utf-8")
                        updated, count = _delete_image_ranges(original, core)
                    except (OSError, UnicodeDecodeError):
                        continue
                    if not count:
                        continue
                    service.save_source_ref(
                        file_path.relative_to(root).as_posix(),
                        body=updated,
                        operator="legacy-wiki",
                    )
                    deleted_files += 1
                    deleted_images += count
                ocr_db_updated = _delete_wiki_ocr_record(root, core)
            return JSONResponse(content={
                "ok": True,
                "deleted_files": deleted_files,
                "deleted_images": deleted_images,
                "ocr_db_updated": ocr_db_updated,
                "note": "Markdown 已直接写回 output_md，并同步更新 OCR 数据库与审计记录。",
            })
        except AppError as exc:
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.message})
        except (FileNotFoundError, ValueError, OSError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    return JSONResponse(status_code=404, content={"error": "unknown endpoint"})


async def proxy_legacy_wechat_api(
    path: str,
    request: Request,
) -> Response:
    """把原微信关键词页面的 API 原样接到旧服务，页面本身仍由工作台托管。"""
    if path in {
        "list",
        "search",
        "file",
        "ocr",
        "scan-image",
        "image-index",
        "save",
        "bulk-delete-image",
    }:
        return await proxy_legacy_wiki_api(path, request)
    body = await request.body()
    if not _allowed(path):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_ENDPOINT_NOT_ALLOWED",
                    "message": "该旧系统接口未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    referer_kind = legacy_referer_kind(request.headers.get("referer", ""))
    is_xhs = referer_kind == "xhs"
    is_mp = referer_kind == "mp"
    is_geo = referer_kind == "geo"
    if is_xhs:
        source_url = settings.xhs_source_url
        timeout_seconds = settings.xhs_source_timeout_seconds
    elif is_mp:
        source_url = settings.mp_source_url
        timeout_seconds = settings.mp_source_timeout_seconds
    elif is_geo:
        source_url = settings.geo_source_url
        timeout_seconds = 10.0
    else:
        source_url = settings.wechat_source_url
        timeout_seconds = settings.wechat_source_timeout_seconds
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        legacy_system = "xhs" if is_xhs else "mp" if is_mp else "geo" if is_geo else "wechat"
        _audit_legacy_write_blocked(
            settings, legacy_system=legacy_system, method=request.method, path=path
        )
        code = "LEGACY_XHS_WRITE_BLOCKED" if is_xhs else "LEGACY_WRITE_BLOCKED"
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "blocked": True,
                "upstream_called": False,
                "error": {
                    "code": code,
                    "message": "原版业务岛屿写操作已被工作台阻断；请经对应 Hub 命令接口执行，旧系统不会收到请求。",
                },
            },
        )
    target = _upstream_url(
        str(source_url),
        path,
        str(request.url.query),
    )
    headers = {"Accept": request.headers.get("accept", "application/json")}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    upstream_request = urllib.request.Request(
        target,
        data=body if body else None,
        headers=headers,
        method=request.method,
    )

    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            status = int(upstream.status)
            response_type = upstream.headers.get_content_type() or "application/json"
            if is_mp:
                payload = _redact_json_payload(payload, response_type)
            return Response(
                content=payload,
                status_code=status,
                media_type=response_type,
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        response_type = exc.headers.get_content_type() if exc.headers else "application/json"
        if is_mp:
            payload = _redact_json_payload(payload, response_type)
        return Response(
            content=payload,
            status_code=int(exc.code),
            media_type=response_type or "application/json",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统服务暂时不可用：{exc}",
                },
            },
        )


async def proxy_legacy_geo_page(path: str, request: Request) -> Response:
    """把 GEOProMax 原始服务端页面原样承载到工作台业务岛屿。"""
    if path not in {"", "index.html"}:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_GEO_PAGE_NOT_ALLOWED",
                    "message": "该 GEO 原始页面路径未登记到工作台。",
                },
            },
        )

    settings: Any = request.app.state.settings
    target = f"{str(settings.geo_source_url).rstrip('/')}/"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    upstream_request = urllib.request.Request(
        target,
        headers={"Accept": request.headers.get("accept", "text/html")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(upstream_request, timeout=10.0) as upstream:
            return Response(
                content=upstream.read(),
                status_code=int(upstream.status),
                media_type=upstream.headers.get_content_type() or "text/html",
            )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=int(exc.code),
            media_type=exc.headers.get_content_type() if exc.headers else "text/plain",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"GEO 原系统服务暂时不可用：{exc}",
                },
            },
        )


async def proxy_legacy_xhs_page(request: Request) -> Response:
    """承载小红书原版的辅助页面，保留原页面 DOM 与脚本入口。

    主监控页由静态镜像直接提供；两个从榜单跳转的辅助页没有扩展名，
    因而需要显式映射到镜像 HTML。页面内部仍使用原版 API 契约，
    请求 Referer 会继续落在 /legacy/xhs/，由同一白名单代理承接。
    """
    pages = {
        "article-hit-detail": "article_hit_detail.html",
        "keyword-turnover": "keyword_turnover.html",
    }
    page_key = request.url.path.rsplit("/", 1)[-1]
    filename = pages.get(page_key)
    if filename is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_XHS_PAGE_NOT_ALLOWED",
                    "message": "该小红书辅助页面未登记到工作台镜像。",
                },
            },
        )
    page = request.app.state.settings.workbench_root / "frontend/public/legacy/xhs" / filename
    if not page.is_file():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_XHS_PAGE_MISSING",
                    "message": "小红书辅助页面镜像文件不存在。",
                },
            },
        )
    return FileResponse(page, media_type="text/html")


async def proxy_legacy_static(path: str, request: Request) -> Response:
    """代理公众号旧控制台返回的有限静态资源，不提供通用文件代理。"""
    if path != "logo.svg":
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_STATIC_NOT_ALLOWED",
                    "message": "该旧系统静态资源未登记到工作台代理白名单。",
                },
            },
        )

    settings: Any = request.app.state.settings
    local_logo = settings.workbench_root / "frontend/public/legacy/mp/static/logo.svg"
    if local_logo.is_file():
        return FileResponse(local_logo, media_type="image/svg+xml")

    target = f"{str(settings.mp_source_url).rstrip('/')}/static/logo.svg"
    upstream_request = urllib.request.Request(
        target,
        headers={"Accept": request.headers.get("accept", "image/svg+xml")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            upstream_request,
            timeout=float(settings.mp_source_timeout_seconds),
        ) as upstream:
            payload = upstream.read()
            return Response(
                content=payload,
                status_code=int(upstream.status),
                media_type=upstream.headers.get_content_type() or "image/svg+xml",
            )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=int(exc.code),
            media_type=exc.headers.get_content_type() if exc.headers else "text/plain",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": {
                    "code": "LEGACY_UPSTREAM_UNAVAILABLE",
                    "message": f"原系统静态资源暂时不可用：{exc}",
                },
            },
        )
