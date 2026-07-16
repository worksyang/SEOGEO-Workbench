from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.services.contract_diff import HTTPMetadata, PayloadWithHTTPMetadata
from content_hub.services.migration import MigrationResolver

from content_hub.services.wechat_aux import (
    AidsoLoginRequired,
    AidsoProfileBusy,
    AuxCommandReplay,
    AuxEvidenceNotFound,
    AuxIdempotencyConflict,
    AuxNotFound,
    AuxUpstreamError,
    AuxValidation,
    WechatAuxService,
)

router = APIRouter(tags=["wechat-legacy-aux"])
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _service(request: Request) -> WechatAuxService:
    existing = getattr(request.app.state, "wechat_aux_service", None)
    if isinstance(existing, WechatAuxService):
        return existing
    return WechatAuxService(
        request.app.state.settings,
        aidso_provider=getattr(request.app.state, "wechat_aux_provider", None),
        image_provider=getattr(request.app.state, "wechat_image_provider", None),
    )


def _json_error(body: dict[str, Any], status_code: int) -> Response:
    raw = (
        json.dumps(
            body,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        + "\n"
    ).encode()
    return Response(
        raw,
        status_code=status_code,
        headers={"Content-Type": "application/json", **_NO_STORE_HEADERS},
    )


def _error(exc: Exception) -> Response:
    if isinstance(exc, AuxCommandReplay):
        return _json_error(exc.payload, exc.status_code)
    if isinstance(exc, AuxIdempotencyConflict):
        return _json_error({"error": str(exc)}, 409)
    if isinstance(exc, AuxValidation):
        return _json_error({"error": str(exc)}, 400)
    if isinstance(exc, AuxEvidenceNotFound):
        return _json_error({"error": f"evidence not found: {exc}"}, 404)
    if isinstance(exc, AuxNotFound):
        return _json_error({"error": "resource not found"}, 404)
    return _json_error({"error": "upstream provider failed"}, 502)


def _legacy_json(request: Request, path: str) -> PayloadWithHTTPMetadata:
    query = request.url.query
    target = path + (f"?{query}" if query else "")
    try:
        remote = WechatAdapter(request.app.state.settings)._request_response(
            target, allow_http_errors=True
        )
    except WechatSourceError as exc:
        raise AuxUpstreamError(str(exc)) from exc
    return PayloadWithHTTPMetadata(remote.payload, remote.metadata)


def _hub_evidence(request: Request, evidence_id: str) -> PayloadWithHTTPMetadata:
    try:
        return PayloadWithHTTPMetadata.from_value(_service(request).artifact("evidence", evidence_id))
    except AuxEvidenceNotFound:
        return PayloadWithHTTPMetadata(
            {"error": f"evidence not found: {evidence_id}"},
            HTTPMetadata(
                status_code=404,
                content_type="application/json",
                cache_control="no-store, no-cache, must-revalidate, max-age=0",
            ),
        )


_NO_STORE_METADATA = HTTPMetadata(
    status_code=200,
    content_type="application/json",
    cache_control="no-store, no-cache, must-revalidate, max-age=0",
)


def _resolve_read(
    request: Request,
    contract: str,
    legacy_path: str,
    hub,
    *,
    hub_metadata: HTTPMetadata | None = _NO_STORE_METADATA,
):
    result, _ = MigrationResolver(
        request.app.state.settings,
        module_key="wechat-search",
        contract_key=contract,
    ).read(
        request_fingerprint=f"wechat:{contract}:{request.url.query}",
        legacy=lambda: _legacy_json(request, legacy_path),
        hub=hub,
        hub_metadata=hub_metadata,
        preserve_response=True,
    )
    return result


def _render_read(result: PayloadWithHTTPMetadata | Any) -> Response:
    if isinstance(result, PayloadWithHTTPMetadata):
        metadata = result.metadata
        body = result.payload
    else:
        metadata = _NO_STORE_METADATA
        body = result
    headers: dict[str, str] = {}
    if metadata.cache_control:
        headers["Cache-Control"] = metadata.cache_control
        if metadata.cache_control.startswith("no-store"):
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"
    if metadata.content_type:
        headers["Content-Type"] = metadata.content_type
    if metadata.content_encoding:
        headers["Content-Encoding"] = metadata.content_encoding
    if metadata.etag:
        headers["ETag"] = metadata.etag
    if metadata.vary:
        headers["Vary"] = metadata.vary
    if isinstance(body, (bytes, bytearray, memoryview)):
        raw = bytes(body)
    else:
        raw = (
            json.dumps(
                body,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        ).encode()
    return Response(
        raw,
        status_code=metadata.status_code or 200,
        headers=headers,
    )


def _aux_read(request: Request, contract: str, legacy_path: str, hub) -> Response:
    try:
        return _render_read(_resolve_read(request, contract, legacy_path, hub))
    except Exception as exc:
        return _error(exc)


def _external_blocked(path: str) -> PayloadWithHTTPMetadata:
    return PayloadWithHTTPMetadata(
        {
            "code": "REFERENCE_EXTERNAL_BLOCKED",
            "error": "external side effect blocked",
            "kind": "external_blocked",
            "method": "GET",
            "path": path,
        },
        HTTPMetadata(
            status_code=409,
            content_type="application/json",
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
        ),
    )


@router.get("/api/agent/manifest")
def agent_manifest(request: Request) -> Any:
    return _aux_read(request, "agent-manifest", "/api/agent/manifest", lambda: _service(request).artifact("manifest"))


@router.get("/api/agent/daily-brief")
def agent_daily_brief(request: Request) -> Any:
    return _aux_read(request, "agent-daily-brief", "/api/agent/daily-brief", lambda: _service(request).artifact("daily_brief"))


@router.get("/api/agent/metric-dictionary")
def agent_metric_dictionary(request: Request) -> Any:
    return _aux_read(request, "agent-metric-dictionary", "/api/agent/metric-dictionary", lambda: _service(request).artifact("metric_dictionary"))


@router.get("/api/agent/evidence/{evidence_id:path}")
def agent_evidence(evidence_id: str, request: Request) -> Any:
    return _aux_read(request, "agent-evidence", f"/api/agent/evidence/{evidence_id}", lambda: _hub_evidence(request, evidence_id))


@router.get("/api/penalty-signals")
def penalty_signals(request: Request) -> Any:
    return _aux_read(request, "penalty-signals", "/api/penalty-signals", lambda: _service(request).artifact("penalty_signals"))


@router.get("/api/account-aliases")
def account_aliases(request: Request) -> Any:
    return _aux_read(request, "account-aliases", "/api/account-aliases", lambda: _service(request).artifact("account_aliases"))


@router.get("/api/article-cover-image")
def article_cover_image(request: Request, url: str = "") -> Any:
    try:
        response = _render_read(
            _resolve_read(
                request,
                "article-cover-image",
                "/api/article-cover-image",
                lambda: _external_blocked("/api/article-cover-image"),
                hub_metadata=None,
            )
        )
        return response
    except Exception as exc:
        return _error(exc)


@router.post("/api/article-covers")
async def article_covers(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        result = _service(request).article_covers(
            payload.get("articles", []) if isinstance(payload, dict) else [],
            idempotency_key=request.headers.get("Idempotency-Key")
            or str(payload.get("idempotency_key", "") if isinstance(payload, dict) else ""),
        )
        return JSONResponse(result)
    except Exception as exc:
        return _error(exc)


@router.api_route("/api/aidso/keyword-heat", methods=["GET", "POST"])
async def aidso_keyword_heat(request: Request) -> Any:
    payload: dict[str, Any] = {}
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload.update(body)
        except Exception:
            return JSONResponse(
                {"error": "invalid JSON body"},
                status_code=400,
                headers=_NO_STORE_HEADERS,
            )
    for key, value in request.query_params.items():
        payload.setdefault(key, value)
    try:
        if request.method == "GET":
            return _render_read(
                _resolve_read(
                    request,
                    "aidso-keyword-heat-get",
                    "/api/aidso/keyword-heat",
                    lambda: _external_blocked("/api/aidso/keyword-heat"),
                    hub_metadata=None,
                )
            )
        return _service(request).aidso_heat(
            payload,
            idempotency_key=request.headers.get("Idempotency-Key")
            or str(payload.get("idempotency_key") or ""),
            write=True,
        )
    except AidsoLoginRequired:
        return JSONResponse(
            {"error": "login required", "login_required": True},
            status_code=409,
            headers=_NO_STORE_HEADERS,
        )
    except AidsoProfileBusy:
        return JSONResponse(
            {"error": "profile busy", "profile_busy": True},
            status_code=409,
            headers=_NO_STORE_HEADERS,
        )
    except AuxCommandReplay as exc:
        return JSONResponse(
            exc.payload, status_code=exc.status_code, headers=_NO_STORE_HEADERS
        )
    except AuxIdempotencyConflict as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=409, headers=_NO_STORE_HEADERS
        )
    except AuxValidation as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=400, headers=_NO_STORE_HEADERS
        )
    except AuxUpstreamError:
        return JSONResponse(
            {"error": "provider failed"},
            status_code=502,
            headers=_NO_STORE_HEADERS,
        )
    except Exception as exc:
        return _error(exc)
