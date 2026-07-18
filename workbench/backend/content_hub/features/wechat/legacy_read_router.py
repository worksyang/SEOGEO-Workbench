from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.repositories.wechat_legacy import WechatLegacyRepository
from content_hub.services.migration import MigrationResolver
from content_hub.services.wechat_refresh import BatchAlreadyRunningError, InvalidKeywordIDsError, WechatRefreshService
from content_hub.services.contract_diff import HTTPMetadata, PayloadWithHTTPMetadata

router = APIRouter(tags=["wechat-legacy-read"])
from content_hub.features.wechat.router import legacy_state_router
router.include_router(legacy_state_router)


def _repo(request: Request) -> WechatLegacyRepository:
    return WechatLegacyRepository(request.app.state.settings)


def _adapter(request: Request) -> WechatAdapter:
    return WechatAdapter(request.app.state.settings)


def _refresh_service(request: Request) -> WechatRefreshService:
    # Provider 只允许由测试/受控运行态显式注入；默认 disabled，不能因为旧代理可用
    # 就偷偷调用 8765、Aidso 或浏览器。
    return WechatRefreshService(
        request.app.state.settings,
        provider=getattr(request.app.state, "wechat_refresh_provider", None),
        actor_id=request.headers.get("X-Actor-ID", "user"),
    )


def _idempotency(
    request: Request,
    payload: dict[str, Any],
    *,
    operation: str,
    subject: str = "",
) -> str:
    explicit = (
        payload.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
    )
    if explicit:
        return str(explicit).strip()
    raise ValidationAppError("刷新写请求必须提供非空 Idempotency-Key。")


def _response_metadata(value: PayloadWithHTTPMetadata | Any) -> HTTPMetadata:
    if isinstance(value, PayloadWithHTTPMetadata):
        return value.metadata
    return HTTPMetadata(status_code=200, content_type="application/json")


def _legacy_error(exc: Exception) -> Response:
    if isinstance(exc, NotFoundError):
        if "微信刷新任务" in exc.message:
            message = "job not found"
        elif "微信刷新批次" in exc.message:
            message = "batch not found"
        else:
            message = exc.message
        return _reference_error(404, {"error": message})
    if isinstance(exc, ConflictError):
        body: dict[str, Any] = {"error": str(exc)}
        if isinstance(exc, BatchAlreadyRunningError):
            body["batch"] = exc.state
        return _reference_error(409, body)
    if isinstance(exc, InvalidKeywordIDsError):
        return _reference_error(
            400,
            {
                "error": "keyword_ids contains invalid items",
                "invalid_keyword_ids": exc.invalid_keyword_ids,
            },
        )
    if isinstance(exc, ValidationAppError):
        return _reference_error(400, {"error": exc.message})
    return _reference_error(500, {"error": str(exc)})


def _reference_error(
    status_code: int,
    body: dict[str, Any] | str,
    *,
    content_type: str = "application/json",
    cache_control: str | None = "no-store, no-cache, must-revalidate, max-age=0",
) -> Response:
    if isinstance(body, dict):
        raw = (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    else:
        raw = body.encode()
    headers = {"Content-Type": content_type}
    if cache_control:
        headers["Cache-Control"] = cache_control
        if cache_control.startswith("no-store"):
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"
    return Response(raw, status_code=status_code, headers=headers)


_REFERENCE_500_HTML = (
    "<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n"
    "<h1>Internal Server Error</h1>\n<p>The server encountered an internal error "
    "and was unable to complete your request. Either the server is overloaded or "
    "there is an error in the application.</p>\n"
)


def _json_response(request: Request, payload: PayloadWithHTTPMetadata | Any) -> Response:
    metadata = _response_metadata(payload)
    body = payload.payload if isinstance(payload, PayloadWithHTTPMetadata) else payload
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    varies_encoding = any(
        item.strip().lower() == "accept-encoding"
        for item in (metadata.vary or "").split(",")
    )
    # R01–R04 use pre-serialized JSON without a trailing newline. Flask
    # ``jsonify`` powers the remaining JSON endpoints and appends one.
    if not varies_encoding and (metadata.content_type or "").lower().startswith(
        "application/json"
    ):
        raw += b"\n"
    etag = metadata.etag
    if etag == "__AUTO__":
        etag = 'W/"' + hashlib.md5(raw).hexdigest() + '"'
    candidates = {x.strip() for x in request.headers.get("if-none-match", "").split(",") if x.strip()}
    if etag and ("*" in candidates or etag in candidates):
        headers = {"ETag": etag}
        if metadata.vary:
            headers["Vary"] = metadata.vary
        if metadata.cache_control:
            headers["Cache-Control"] = metadata.cache_control
            if metadata.cache_control.startswith("no-store"):
                headers["Pragma"] = "no-cache"
                headers["Expires"] = "0"
        return Response(status_code=304, headers=headers)
    headers: dict[str, str] = {}
    if etag:
        headers["ETag"] = etag
    if metadata.vary:
        headers["Vary"] = metadata.vary
    if metadata.cache_control:
        headers["Cache-Control"] = metadata.cache_control
        if metadata.cache_control.startswith("no-store"):
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"
    if metadata.content_type:
        headers["Content-Type"] = metadata.content_type
    # The reference service negotiates gzip only for the four core contracts,
    # identified by ``Vary: Accept-Encoding``.  Do not gzip the R05–R22
    # no-store JSON responses, and do not emit Content-Encoding when the
    # caller did not accept it.
    accepted_encodings: dict[str, float] = {}
    for token in request.headers.get("accept-encoding", "").split(","):
        parts = [item.strip() for item in token.split(";") if item.strip()]
        if not parts:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        accepted_encodings[parts[0].lower()] = quality
    accepts_gzip = accepted_encodings.get(
        "gzip", accepted_encodings.get("*", 0.0)
    ) > 0
    encoding = metadata.content_encoding
    if varies_encoding and accepts_gzip and (
        not encoding or encoding.lower() == "gzip"
    ):
        headers["Content-Encoding"] = "gzip"
        return Response(
            gzip.compress(raw, compresslevel=6),
            status_code=metadata.status_code or 200,
            headers=headers,
        )
    return Response(raw, status_code=metadata.status_code or 200, headers=headers)


def _mode_read(request: Request, contract: str, fingerprint: str, legacy, hub):
    core = contract in {"monitor-data", "bootstrap", "keyword", "account"}
    result, _ = MigrationResolver(
        request.app.state.settings, module_key="wechat-search", contract_key=contract
    ).read(
        request_fingerprint=fingerprint,
        legacy=legacy,
        hub=hub,
        hub_metadata=HTTPMetadata(
            status_code=200,
            content_type="application/json; charset=utf-8" if core else "application/json",
            etag="__AUTO__" if core else None,
            cache_control="no-cache, must-revalidate" if core else "no-store, no-cache, must-revalidate, max-age=0",
            vary="Accept-Encoding" if core else None,
        ),
        preserve_response=True,
    )
    return result


def _remote(
    request: Request, path: str, *, allow_any_json: bool = False
) -> PayloadWithHTTPMetadata:
    try:
        remote = _adapter(request)._request_response(
            path, allow_http_errors=True, allow_any_json=allow_any_json
        )
        return PayloadWithHTTPMetadata(remote.payload, remote.metadata)
    except WechatSourceError as exc:
        raise ConflictError(str(exc)) from exc


def _remote_query(request: Request, path: str) -> PayloadWithHTTPMetadata:
    query = request.url.query
    return _remote(request, path + (f"?{query}" if query else ""))


def _not_found_payload(message: str) -> PayloadWithHTTPMetadata:
    return PayloadWithHTTPMetadata(
        {"error": message},
        HTTPMetadata(
            status_code=404,
            content_type="application/json",
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
        ),
    )


def _segment(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


@router.get("/api/monitor-data")
def monitor_data(request: Request) -> Response:
    return _json_response(request, _mode_read(request, "monitor-data", "wechat:monitor-data", lambda: _remote_query(request, "/api/monitor-data"), lambda: _repo(request).full()))


@router.get("/api/monitor-data/bootstrap")
async def monitor_bootstrap(request: Request) -> Response:
    # 同一路径也被旧小红书页使用；保持既有 referer 分流，不截获非微信请求。
    if "/legacy/xhs/" in request.headers.get("referer", ""):
        from content_hub.legacy_proxy import proxy_legacy_wechat_api
        return await proxy_legacy_wechat_api("monitor-data/bootstrap", request)
    return _json_response(request, _mode_read(request, "bootstrap", "wechat:bootstrap", lambda: _remote_query(request, "/api/monitor-data/bootstrap"), lambda: _repo(request).bootstrap()))


@router.get("/api/monitor-data/keyword/{keyword_id}")
def monitor_keyword(keyword_id: str, request: Request) -> Response:
    try:
        return _json_response(request, _mode_read(request, "keyword", f"wechat:keyword:{keyword_id}", lambda: _remote(request, f"/api/monitor-data/keyword/{_segment(keyword_id)}"), lambda: _repo(request).keyword(keyword_id)))
    except NotFoundError:
        return _reference_error(404, {"error": f"keyword not found: {keyword_id}"}, cache_control=None)


@router.get("/api/monitor-data/account/{account_id}")
def monitor_account(account_id: str, request: Request) -> Response:
    try:
        return _json_response(request, _mode_read(request, "account", f"wechat:account:{account_id}", lambda: _remote(request, f"/api/monitor-data/account/{_segment(account_id)}"), lambda: _repo(request).account(account_id)))
    except NotFoundError:
        return _reference_error(404, {"error": f"account not found: {account_id}"}, cache_control=None)


@router.get("/api/article-content")
def article_content(request: Request, path: str = "") -> Response:
    if not path:
        return _reference_error(404, {"error": "empty content path"})
    if path.startswith("/") or not path.lower().endswith(".md") or ".." in path.replace("\\", "/").split("/"):
        return _reference_error(400, {"error": "path escapes project root"})

    def legacy():
        try:
            remote = _adapter(request).remote_article_content_response(path)
            return PayloadWithHTTPMetadata(remote.payload, remote.metadata)
        except WechatSourceError as exc:
            if exc.status == 404:
                raise NotFoundError("微信正文", path) from exc
            raise ConflictError(str(exc)) from exc

    def hub():
        record = _repo(request).article_content(path)
        if not record.get("relative_path") or not record.get("asset_path"):
            raise NotFoundError("微信正文", path)
        try:
            body = _repo(request).asset_content(record)
        except NotFoundError:
            raise
        return {"path": path, "markdown": body}

    return _json_response(request, _mode_read(request, "article-content", f"wechat:article-content:{path}", legacy, hub))


@router.get("/api/article-hit-detail")
def article_hit_detail(request: Request, article_id: str = "", url: str = "") -> Response:
    if not article_id and not url:
        return _reference_error(404, {"error": "article not found"})
    return _json_response(request, _mode_read(
        request, "article-hit-detail", f"wechat:article-hit-detail:{article_id}:{url}",
        lambda: _remote(request, "/api/article-hit-detail?" + _query(article_id=article_id, url=url)),
        lambda: _repo(request).hit_detail(article_id, url),
    ))


def _query(**values: str) -> str:
    from urllib.parse import urlencode
    return urlencode({key: value for key, value in values.items() if value})


@router.get("/api/keyword-manage")
def keyword_manage(request: Request) -> Response:
    return _json_response(request, _mode_read(request, "keyword-manage", "wechat:keyword-manage", lambda: _remote(request, "/api/keyword-manage"), lambda: _repo(request).keyword_manage()))


@router.get("/api/keyword-discovery")
def keyword_discovery(request: Request) -> Response:
    # 旧 API 接受重复参数；不能用 get() 丢掉同名值。
    probe_statuses = request.query_params.getlist("probe_status")
    candidate_statuses = request.query_params.getlist("candidate_status")
    try:
        raw_limit = int(request.query_params.get("limit", "100"))
    except (TypeError, ValueError):
        raw_limit = 100
    effective_limit = max(1, min(500, raw_limit))
    fingerprint = f"wechat:keyword-discovery:{probe_statuses}:{candidate_statuses}:{effective_limit}"
    return _json_response(request, _mode_read(request, "keyword-discovery", fingerprint, lambda: _remote_query(request, "/api/keyword-discovery"), lambda: _repo(request).discovery(probe_statuses, effective_limit, candidate_statuses)))


@router.get("/api/refresh-status/{job_id}")
def refresh_status(job_id: str, request: Request) -> Response:
    def hub():
        value = _repo(request).runtime(job_id, subtype="single_job")
        if value is None:
            return _not_found_payload("job not found")
        return value
    try:
        return _json_response(request, _mode_read(request, "refresh-status", f"wechat:refresh-status:{job_id}", lambda: _remote(request, f"/api/refresh-status/{_segment(job_id)}"), hub))
    except NotFoundError:
        return _reference_error(404, {"error": "job not found"})


@router.get("/api/refresh-all/status")
def refresh_all_status(request: Request, batch_id: str | None = None) -> Response:
    def hub():
        if not batch_id:
            return _repo(request).active_batch_runtime() or {"status": "idle", "is_active": False}
        value = _repo(request).runtime(batch_id, subtype="batch")
        if value is None:
            raise NotFoundError("微信刷新批次", batch_id)
        return value
    try:
        return _json_response(request, _mode_read(request, "refresh-all-status", f"wechat:refresh-all-status:{batch_id}", lambda: _remote(request, "/api/refresh-all/status?" + _query(batch_id=batch_id or "")), hub))
    except NotFoundError:
        return _reference_error(404, {"error": "batch not found"})


@router.get("/api/refresh-all/history")
def refresh_all_history(request: Request) -> Response:
    return _json_response(request, _mode_read(request, "refresh-all-history", "wechat:refresh-all-history", lambda: _remote(request, "/api/refresh-all/history", allow_any_json=True), lambda: _repo(request).runtime_history()))


@router.get("/api/scheduler/status")
def scheduler_status(request: Request) -> Response:
    return _json_response(request, _mode_read(request, "scheduler-status", "wechat:scheduler-status", lambda: _remote(request, "/api/scheduler/status"), lambda: _repo(request).scheduler_runtime()))


@router.post("/api/keywords/{keyword_id}/refresh")
def refresh_keyword_write(keyword_id: str, request: Request, body: dict[str, Any] | None = None) -> Response:
    payload = body or {}
    if not str(payload.get("keyword", "")).strip():
        return Response(json.dumps({"error": "keyword is required"}, ensure_ascii=False).encode(), status_code=400, media_type="application/json")
    try:
        key = _idempotency(request, payload, operation="keywords-refresh", subject=keyword_id)
        # 旧页面传 keyword；Hub 以 keyword_id 为事实源，keyword 只作为兼容校验输入。
        result = _refresh_service(request).refresh_one(
            keyword_id=keyword_id,
            request_keyword=str(payload.get("keyword", "")).strip(),
            key=key,
            request_id=request.headers.get("X-Request-ID"),
            confirm=payload.get("confirm", True) is not False,
        )
        if result.get("blocked"):
            result = {**result, "status": "failed", "hub_status": "blocked"}
            status = 409
        elif result.get("status") == "failed":
            result = {**result, "status": "rejected"}
            status = 409
        elif result.get("status") == "queued":
            status = 202
        else:
            # 旧接口只返回启动回执；最终 succeeded/failed 由 R17 读取。
            result = {
                "job_id": result.get("job_id"),
                "refresh_job_id": result.get("refresh_job_id"),
                "command_id": result.get("command_id"),
                "keyword_id": result.get("keyword_id"),
                "keyword": result.get("keyword"),
                "status": "running",
                "source": result.get("source", "hub"),
                "provider": result.get("provider"),
                "start_receipt": True,
            }
            status = 200
        return Response(json.dumps(result, ensure_ascii=False, default=str).encode(), status_code=status, media_type="application/json")
    except Exception as exc:
        return _legacy_error(exc)


@router.post("/api/refresh-all/cancel")
def refresh_all_cancel_write(request: Request, body: dict[str, Any] | None = None) -> Response:
    payload = body or {}
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        return Response(json.dumps({"error": "batch_id is required"}, ensure_ascii=False).encode(), status_code=400, media_type="application/json")
    try:
        result = _refresh_service(request).cancel_batch(
            batch_id=batch_id,
            key=_idempotency(request, payload, operation="refresh-all-cancel", subject=batch_id),
            request_id=request.headers.get("X-Request-ID"),
        )
        return Response(json.dumps(result, ensure_ascii=False, default=str).encode(), media_type="application/json")
    except Exception as exc:
        return _legacy_error(exc)


@router.post("/api/refresh-all")
def refresh_all_write(request: Request, body: dict[str, Any] | None = None) -> Response:
    payload = body or {}
    raw_ids = payload.get("keyword_ids", [])
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        return Response(json.dumps({"error": "keyword_ids must be a list"}, ensure_ascii=False).encode(), status_code=400, media_type="application/json")
    try:
        refresh_round = int(payload["refresh_round"]) if payload.get("refresh_round") is not None else None
    except (TypeError, ValueError):
        return Response(json.dumps({"error": f"invalid literal for int() with base 10: {payload.get('refresh_round')!r}"}, ensure_ascii=False).encode(), status_code=400, media_type="application/json")
    try:
        source = "scheduler" if request.headers.get("X-Scheduler", "").lower() in {"1", "true", "yes"} else "web_refresh_all"
        result = _refresh_service(request).refresh_batch(
            keyword_ids=raw_ids,
            key=_idempotency(request, payload, operation="refresh-all"),
            source=source,
            incremental=bool(payload.get("incremental")),
            refresh_round=refresh_round,
            request_id=request.headers.get("X-Request-ID"),
        )
        status = 409 if result.get("status") in {"failed", "completed_with_failures"} or result.get("hub_status") in {"failed", "partial_failed", "blocked"} else 202
        return Response(json.dumps(result, ensure_ascii=False, default=str).encode(), status_code=status, media_type="application/json")
    except Exception as exc:
        return _legacy_error(exc)


@router.post("/api/scheduler/config")
def scheduler_config_write(request: Request, body: dict[str, Any] | None = None) -> Response:
    try:
        config_payload = {
            key: value
            for key, value in (body or {}).items()
            if key != "idempotency_key"
        }
        result = _refresh_service(request).scheduler_config(
            payload=config_payload,
            key=_idempotency(request, body or {}, operation="scheduler-config"),
            request_id=request.headers.get("X-Request-ID"),
        )
        return Response(json.dumps(result, ensure_ascii=False, default=str).encode(), media_type="application/json")
    except Exception as exc:
        return _legacy_error(exc)


@router.post("/api/scheduler/trigger")
def scheduler_trigger_write(request: Request, body: dict[str, Any] | None = None) -> Response:
    try:
        result = _refresh_service(request).scheduler_trigger(
            key=_idempotency(request, body or {}, operation="scheduler-trigger"),
            request_id=request.headers.get("X-Request-ID"),
        )
        status = 409 if result.get("blocked") else 200
        return Response(json.dumps(result, ensure_ascii=False, default=str).encode(), status_code=status, media_type="application/json")
    except Exception as exc:
        return _legacy_error(exc)


@router.get("/api/articles")
def articles(request: Request) -> Response:
    def integer(name: str, default: int) -> int:
        raw = request.query_params.get(name, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            # The frozen Flask endpoint leaked its ValueError as a standard
            # 500 page.  Keep that negative contract rather than normalizing
            # malformed legacy query input to a successful Hub response.
            raise ValueError(f"invalid integer query parameter: {name}")

    page = max(1, integer("page", 1))
    page_size = integer("page_size", 50)
    if page_size < 1 or page_size > 200:
        page_size = 50
    sort = request.query_params.get("sort", "reads")
    if sort not in {"reads", "hitCount", "publishTime", "likes", "accountScore", "todayReads", "onRankDays"}:
        sort = "reads"
    try:
        time_range = integer("time_range", 15)
    except ValueError:
        return _reference_error(
            500,
            _REFERENCE_500_HTML,
            content_type="text/html; charset=utf-8",
        )
    min_hits = max(0, integer("min_hits", 0))
    account = request.query_params.get("account", "")
    search = request.query_params.get("search", "")
    fingerprint = f"wechat:articles:{page}:{page_size}:{sort}:{time_range}:{min_hits}:{account}:{search}"
    return _json_response(request, _mode_read(request, "articles", fingerprint, lambda: _remote_query(request, "/api/articles"), lambda: _repo(request).articles(page=page, page_size=page_size, sort=sort, time_range=time_range, min_hits=min_hits, account=account, search=search)))


@router.get("/api/articles/accounts")
def article_accounts(request: Request) -> Response:
    return _json_response(request, _mode_read(request, "articles-accounts", "wechat:articles-accounts", lambda: _remote(request, "/api/articles/accounts"), lambda: _repo(request).article_accounts()))
