from __future__ import annotations

import requests
from flask import Blueprint, Response, current_app, jsonify, make_response, request

from app.services.aidso_keyword_heat_service import (
    AidsoHeatError,
    AidsoLoginRequiredError,
    AidsoProfileBusyError,
    DEFAULT_BROWSER_CHANNEL,
    resolve_aidso_keyword_heat,
)
from app.services.account_alias_service import load_account_aliases
from app.services.article_cover_service import fetch_cover_image_bytes, resolve_article_covers_payload
from app.services.article_hit_detail_service import resolve_article_hit_detail
from app.services.agent_projection_service import (
    load_agent_artifact,
    load_agent_evidence,
    load_metric_dictionary,
)
from app.services.keyword_manage_service import (
    create_keyword_group,
    create_managed_keyword,
    delete_keyword_group,
    delete_managed_keyword,
    list_batch_refresh_keywords,
    list_keyword_groups,
    set_managed_keyword_auto_archive_lock,
    set_managed_keyword_commercial_value,
    set_managed_keyword_refresh_policy,
    update_keyword_group,
    update_managed_keyword,
)
from app.services.refresh_service import (
    BatchAlreadyRunningError,
    cancel_batch,
    get_active_batch_status,
    get_batch_status,
    get_job_status,
    get_single_refresh_status,
    list_batch_history,
    start_batch_refresh,
    start_single_refresh,
)
from app.services import scheduler_service
from app.services.article_list_service import (
    list_accounts_with_article_count,
    list_articles,
)
from app.services.monitor_service import (
    resolve_article_markdown,
    set_keyword_bucket,
    set_keyword_note,
    set_keyword_pin,
    set_keyword_topic,
)
from app.services.monitor_fast_service import get_fast_store
from app.services.penalty_signal_service import load_penalty_signals
from app.repositories.keyword_discovery_repo import KeywordDiscoveryRepository


bp = Blueprint("api", __name__, url_prefix="/api")


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _accepts_gzip() -> bool:
    """Return whether gzip is an acceptable response representation."""
    return request.accept_encodings["gzip"] > 0


def _etag_matches(etag: str) -> bool:
    """Handle both normal and weak If-None-Match validators."""
    candidates = {
        item.strip()
        for item in request.headers.get("If-None-Match", "").split(",")
        if item.strip()
    }
    return "*" in candidates or etag in candidates


def _cached_json_response(raw: bytes, compressed: bytes, etag: str) -> Response:
    """Serve pre-serialized JSON with representation negotiation and validators."""
    headers = {
        "ETag": etag,
        "Vary": "Accept-Encoding",
        "Cache-Control": "no-cache, must-revalidate",
    }
    if _etag_matches(etag):
        return make_response("", 304, headers)

    use_gzip = _accepts_gzip()
    body = compressed if use_gzip else raw
    response = Response(body, status=200, content_type="application/json; charset=utf-8")
    response.headers.update(headers)
    if use_gzip:
        response.headers["Content-Encoding"] = "gzip"
    return response


@bp.get("/monitor-data")
def monitor_data():
    try:
        return _cached_json_response(*get_fast_store().get_full())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/monitor-data/bootstrap")
def monitor_data_bootstrap():
    try:
        return _cached_json_response(*get_fast_store().get_bootstrap())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/monitor-data/keyword/<keyword_id>")
def monitor_data_keyword(keyword_id: str):
    try:
        payload = get_fast_store().get_keyword(keyword_id)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    if payload is None:
        return jsonify({"error": f"keyword not found: {keyword_id}"}), 404
    return _cached_json_response(*payload)


@bp.get("/monitor-data/account/<account_id>")
def monitor_data_account(account_id: str):
    try:
        payload = get_fast_store().get_account(account_id)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    if payload is None:
        return jsonify({"error": f"account not found: {account_id}"}), 404
    return _cached_json_response(*payload)


@bp.get("/agent/manifest")
def agent_manifest():
    try:
        return jsonify(load_agent_artifact(current_app.config["PROJECT_ROOT"], "manifest.json"))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/agent/daily-brief")
def agent_daily_brief():
    try:
        return jsonify(load_agent_artifact(current_app.config["PROJECT_ROOT"], "daily_brief.json"))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/agent/metric-dictionary")
def agent_metric_dictionary():
    try:
        return jsonify(load_metric_dictionary(current_app.config["PROJECT_ROOT"]))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/agent/evidence/<evidence_id>")
def agent_evidence(evidence_id: str):
    try:
        return jsonify(load_agent_evidence(current_app.config["PROJECT_ROOT"], evidence_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/penalty-signals")
def penalty_signals():
    try:
        return jsonify(load_penalty_signals())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/account-aliases")
def account_aliases():
    try:
        return jsonify(load_account_aliases())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/article-content")
def article_content():
    content_path = request.args.get("path", "").strip()
    try:
        return jsonify(resolve_article_markdown(content_path))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/article-hit-detail")
def article_hit_detail():
    article_id = request.args.get("article_id", "").strip()
    url = request.args.get("url", "").strip()
    try:
        return jsonify(resolve_article_hit_detail(article_id=article_id, url=url))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.post("/article-covers")
def article_covers():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            resolve_article_covers_payload(
                normalized_dir=current_app.config["NORMALIZED_DIR"],
                raw_items=payload.get("articles", []),
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/article-cover-image")
def article_cover_image():
    url = request.args.get("url", "").strip()
    try:
        body, content_type = fetch_cover_image_bytes(url)
        return Response(
            body,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.HTTPError as exc:
        return jsonify({"error": str(exc)}), 502
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@bp.post("/keywords/<keyword_id>/pin")
def pin_keyword(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword_text = str(payload.get("keyword", "")).strip()
    if not keyword_text:
        return jsonify({"error": "keyword is required"}), 400
    return jsonify(set_keyword_pin(keyword_id, keyword_text, True))


@bp.post("/keywords/<keyword_id>/unpin")
def unpin_keyword(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword_text = str(payload.get("keyword", "")).strip()
    if not keyword_text:
        return jsonify({"error": "keyword is required"}), 400
    return jsonify(set_keyword_pin(keyword_id, keyword_text, False))


@bp.post("/keywords/<keyword_id>/topic")
def update_keyword_topic(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword_text = str(payload.get("keyword", "")).strip()
    if not keyword_text:
        return jsonify({"error": "keyword is required"}), 400
    topic = payload.get("topic")
    if topic is not None:
        topic = str(topic).strip()
    return jsonify(set_keyword_topic(keyword_id, keyword_text, topic))


@bp.post("/keywords/<keyword_id>/note")
def update_keyword_note(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword_text = str(payload.get("keyword", "")).strip()
    if not keyword_text:
        return jsonify({"error": "keyword is required"}), 400
    note = str(payload.get("note", "")).strip()
    return jsonify(set_keyword_note(keyword_id, keyword_text, note))


@bp.post("/keywords/<keyword_id>/bucket")
def update_keyword_bucket(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword_text = str(payload.get("keyword", "")).strip()
    if not keyword_text:
        return jsonify({"error": "keyword is required"}), 400
    keyword_bucket = payload.get("keyword_bucket")
    if keyword_bucket is not None:
        keyword_bucket = str(keyword_bucket).strip()
    return jsonify(set_keyword_bucket(keyword_id, keyword_text, keyword_bucket))


@bp.route("/aidso/keyword-heat", methods=["GET", "POST"])
def aidso_keyword_heat():
    payload = request.get_json(silent=True) or {}
    keyword = str(payload.get("keyword") or request.args.get("keyword", "")).strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    profile_dir = str(
        payload.get("profile_dir")
        or request.args.get("profile_dir")
        or current_app.config["AIDSO_PLAYWRIGHT_PROFILE_DIR"]
    ).strip()
    headless = _coerce_bool(payload.get("headless", request.args.get("headless")), default=True)
    auto_login = _coerce_bool(payload.get("auto_login", request.args.get("auto_login")), default=False)
    wait_timeout_ms = _coerce_int(
        payload.get("wait_timeout_ms", request.args.get("wait_timeout_ms")),
        default=30_000,
    )
    login_wait_timeout_ms = _coerce_int(
        payload.get("login_wait_timeout_ms", request.args.get("login_wait_timeout_ms")),
        default=300_000,
    )
    no_channel = _coerce_bool(payload.get("no_channel", request.args.get("no_channel")), default=False)
    executable_path = _coerce_optional_str(
        payload.get("executable_path", request.args.get("executable_path"))
    )
    channel = None if no_channel else _coerce_optional_str(
        payload.get("channel", request.args.get("channel"))
    )
    if channel is None and not no_channel and executable_path is None:
        channel = DEFAULT_BROWSER_CHANNEL

    try:
        return jsonify(
            resolve_aidso_keyword_heat(
                keyword=keyword,
                profile_dir=profile_dir,
                headless=headless,
                wait_timeout_ms=wait_timeout_ms,
                auto_login=auto_login,
                login_wait_timeout_ms=login_wait_timeout_ms,
                channel=channel,
                executable_path=executable_path,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except AidsoLoginRequiredError as exc:
        return jsonify({"error": str(exc), "login_required": True}), 409
    except AidsoProfileBusyError as exc:
        return jsonify({"error": str(exc), "profile_busy": True}), 409
    except AidsoHeatError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.get("/keyword-manage")
def api_keyword_manage_list():
    return jsonify(list_keyword_groups())


@bp.post("/keyword-manage/groups")
def api_create_keyword_group():
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label", "")).strip()
    if not label:
        return jsonify({"error": "label is required"}), 400
    try:
        return jsonify(create_keyword_group(label=label))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.patch("/keyword-manage/groups/<group_id>")
def api_update_keyword_group(group_id: str):
    payload = request.get_json(silent=True) or {}
    label = payload.get("label")
    order = payload.get("order")
    try:
        return jsonify(
            update_keyword_group(
                group_id=group_id,
                label=str(label).strip() if label is not None else None,
                order=int(order) if order is not None else None,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.delete("/keyword-manage/groups/<group_id>")
def api_delete_keyword_group(group_id: str):
    try:
        return jsonify(delete_keyword_group(group_id=group_id))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/keyword-manage/keywords")
def api_create_keyword():
    payload = request.get_json(silent=True) or {}
    group_id = str(payload.get("group_id", "")).strip()
    keyword_text = str(payload.get("keyword_text", "")).strip()
    note = str(payload.get("note", "")).strip()
    if not group_id:
        return jsonify({"error": "group_id is required"}), 400
    if not keyword_text:
        return jsonify({"error": "keyword_text is required"}), 400
    try:
        return jsonify(
            create_managed_keyword(
                group_id=group_id,
                keyword_text=keyword_text,
                note=note,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.patch("/keyword-manage/keywords/<keyword_id>")
def api_update_keyword(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    note = payload.get("note")
    keyword_text = payload.get("keyword_text")
    group_id = payload.get("group_id")
    try:
        return jsonify(
            update_managed_keyword(
                keyword_id=keyword_id,
                keyword_text=str(keyword_text).strip() if keyword_text is not None else None,
                note=str(note) if note is not None else None,
                group_id=str(group_id).strip() if group_id else None,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.delete("/keyword-manage/keywords/<keyword_id>")
def api_delete_keyword(keyword_id: str):
    try:
        return jsonify(delete_managed_keyword(keyword_id=keyword_id))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.patch("/keyword-manage/keywords/<keyword_id>/refresh-policy")
def api_update_keyword_refresh_policy(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source") or "manual").strip().lower()
    days = payload.get("refresh_frequency_days")
    try:
        return jsonify(
            set_managed_keyword_refresh_policy(
                keyword_id,
                refresh_frequency_days=int(days) if days is not None else None,
                source=source,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.patch("/keyword-manage/keywords/<keyword_id>/commercial-value")
def api_update_keyword_commercial_value(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            set_managed_keyword_commercial_value(
                keyword_id,
                score=int(payload.get("score")),
                reason=str(payload.get("reason") or ""),
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.patch("/keyword-manage/keywords/<keyword_id>/auto-archive-lock")
def api_update_keyword_auto_archive_lock(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            set_managed_keyword_auto_archive_lock(
                keyword_id,
                locked=_coerce_bool(payload.get("locked"), False),
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/keyword-discovery")
def api_keyword_discovery():
    repo = KeywordDiscoveryRepository(current_app.config["SQLITE_PATH"])
    probe_status = request.args.getlist("probe_status")
    candidate_status = request.args.getlist("candidate_status")
    limit = max(1, min(500, _coerce_int(request.args.get("limit"), 100)))
    return jsonify({
        "summary": repo.summary(),
        "probes": repo.list_probes(statuses=probe_status or None, limit=limit),
        "candidates": repo.list_candidates(
            statuses=candidate_status or None,
            limit=limit,
        ),
    })


@bp.post("/keywords/<keyword_id>/refresh")
def refresh_keyword(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword = str(payload.get("keyword", "")).strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400
    result = start_single_refresh(keyword, keyword_id=keyword_id)
    status = result.get("status", "running")
    if status == "queued":
        return jsonify(result), 202
    if status == "rejected":
        return jsonify(result), 409
    return jsonify(result), 200


@bp.get("/refresh-status/<job_id>")
def refresh_status(job_id: str):
    state = get_job_status(job_id)
    if state is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(state)


@bp.get("/refresh-all/status")
def refresh_all_status():
    batch_id = str(request.args.get("batch_id", "")).strip()
    if batch_id:
        state = get_batch_status(batch_id)
        if state is None:
            return jsonify({"error": "batch not found"}), 404
        return jsonify(state)

    state = get_active_batch_status()
    if state is None:
        return jsonify({"status": "idle", "is_active": False})
    return jsonify(state)


@bp.get("/refresh-all/history")
def refresh_all_history():
    return jsonify(list_batch_history())


@bp.post("/refresh-all/cancel")
def refresh_all_cancel():
    payload = request.get_json(silent=True) or {}
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        return jsonify({"error": "batch_id is required"}), 400

    try:
        state = cancel_batch(batch_id)
    except FileNotFoundError:
        return jsonify({"error": "batch not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if state.get("is_finished"):
        return jsonify({
            "status": state.get("status") or "unknown",
            "message": "批次已结束",
            "batch": state,
        })

    return jsonify({
        "status": "cancelling",
        "message": "取消信号已发送，当前关键词跑完后停止",
        "batch": state,
    })


@bp.post("/refresh-all")
def refresh_all():
    payload = request.get_json(silent=True) or {}
    incremental = bool(payload.get("incremental"))
    refresh_round = payload.get("refresh_round")
    raw_ids = payload.get("keyword_ids", [])
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        return jsonify({"error": "keyword_ids must be a list"}), 400

    candidates = list_batch_refresh_keywords()
    if not candidates:
        return jsonify({"error": "no keywords found"}), 400

    candidate_map = {
        str(item.get("keyword_id") or "").strip(): item
        for item in candidates
        if str(item.get("keyword_id") or "").strip()
    }
    selected_ids = []
    seen_ids = set()
    for item in raw_ids:
        keyword_id = str(item or "").strip()
        if not keyword_id or keyword_id in seen_ids:
            continue
        seen_ids.add(keyword_id)
        selected_ids.append(keyword_id)

    unknown_ids = [keyword_id for keyword_id in selected_ids if keyword_id not in candidate_map]
    if unknown_ids:
        return jsonify({"error": "keyword_ids contains invalid items", "invalid_keyword_ids": unknown_ids}), 400

    selected_items = [candidate_map[keyword_id] for keyword_id in selected_ids] if selected_ids else candidates
    if incremental and not selected_ids:
        return jsonify({"error": "incremental refresh requires keyword_ids"}), 400
    if not selected_items:
        return jsonify({"error": "no selected keywords"}), 400

    try:
        is_scheduler = request.headers.get("X-Scheduler", "").lower() in ("1", "true", "yes")
        source = "scheduler" if is_scheduler else "web_refresh_all"
        state = start_batch_refresh(
            selected_items,
            source=source,
            refresh_round=int(refresh_round) if refresh_round is not None else None,
        )
    except BatchAlreadyRunningError as exc:
        return jsonify({"error": "batch already running", "batch": exc.state}), 409
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(state), 202


@bp.get("/scheduler/status")
def api_scheduler_status():
    return jsonify(scheduler_service.get_status())


@bp.post("/scheduler/config")
def api_scheduler_config():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    interval_hours = payload.get("interval_hours")
    daily_keyword_budget = payload.get("daily_keyword_budget")
    max_keywords_per_batch = payload.get("max_keywords_per_batch")
    return jsonify(
        scheduler_service.update_config(
            enabled=bool(enabled) if enabled is not None else None,
            interval_hours=float(interval_hours) if interval_hours is not None else None,
            daily_keyword_budget=(
                int(daily_keyword_budget)
                if daily_keyword_budget is not None
                else None
            ),
            max_keywords_per_batch=(
                int(max_keywords_per_batch)
                if max_keywords_per_batch is not None
                else None
            ),
        )
    )


@bp.post("/scheduler/trigger")
def api_scheduler_trigger():
    """立即触发一次批量刷新（忽略 enabled 状态，供测试用）。"""
    return jsonify(scheduler_service.trigger_now())


@bp.get("/articles")
def api_articles():
    """文章列表 — 服务端分页/排序/筛选。"""
    page = _coerce_int(request.args.get("page"), default=1)
    page_size = _coerce_int(request.args.get("page_size"), default=50)
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
    sort = str(request.args.get("sort") or "reads").strip()
    time_range = _coerce_int(request.args.get("time_range"), default=15)
    min_hits = _coerce_int(request.args.get("min_hits"), default=0)
    account = str(request.args.get("account") or "").strip()
    search = str(request.args.get("search") or "").strip()
    try:
        return jsonify(
            list_articles(
                page=page,
                page_size=page_size,
                sort=sort,
                time_range=time_range,
                min_hits=min_hits,
                account=account,
                search=search,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/articles/accounts")
def api_articles_accounts():
    """有文章的账号列表, 供下拉选择器使用。"""
    try:
        return jsonify({"accounts": list_accounts_with_article_count()})
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
