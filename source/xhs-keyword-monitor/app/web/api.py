"""web/api.py — 所有 API 路由。

覆盖：
- /api/monitor-data
- /api/articles / /api/articles/accounts
- /api/keyword-manage (list/groups/keywords CRUD)
- /api/keywords/<id>/{pin,unpin,topic,note,bucket,refresh}
- /api/refresh-status/<job_id>
- /api/refresh-all (POST/GET status/history/cancel)
- /api/scheduler/{status,config,trigger}
- /api/article-hit-detail
- /api/article-content
- /api/article-covers / /api/article-cover-image
- /api/account-aliases
- /api/penalty-signals
"""
from __future__ import annotations

import requests
from flask import Blueprint, Response, current_app, jsonify, request

from app.services.account_alias_service import load_account_aliases
from app.services.article_cover_service import fetch_cover_image_bytes, resolve_article_covers_payload
from app.services.article_hit_detail_service import resolve_article_hit_detail
from app.services.article_list_service import (
    list_accounts_with_article_count,
    list_articles,
)
from app.services.keyword_manage_service import (
    create_keyword_group,
    create_managed_keyword,
    delete_keyword_group,
    delete_managed_keyword,
    list_batch_refresh_keywords,
    list_keyword_groups,
    save_batch_default_selection,
    update_keyword_group,
    update_managed_keyword,
)
from app.services.monitor_service import (
    load_monitor_payload,
    resolve_article_markdown,
    set_keyword_bucket,
    set_keyword_note,
    set_keyword_pin,
    set_keyword_topic,
)
from app.services.penalty_signal_service import load_penalty_signals
from app.services.monitor_fast_service import get_fast_store
from flask import Response as FlaskResponse
from flask import make_response
from app.ingest.tikhub.detail_service import get_note_detail as _tk_get_note_detail, get_creator_detail as _tk_get_creator_detail
from app.services.refresh_service import (
    BatchAlreadyRunningError,
    cancel_batch,
    get_active_batch_status,
    get_batch_status,
    get_job_status,
    list_batch_history,
    resume_batch,
    start_batch_refresh,
    start_single_refresh,
)
from app.services import scheduler_service


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



# ── 高性能分片数据端点 ──────────────────────────────────
@bp.get("/monitor-data/bootstrap")
def monitor_data_bootstrap():
    """轻量 bootstrap：metadata + keyword 摘要 + account 摘要。
    支持 ETag/If-None-Match → 304，Vary: Accept-Encoding，gzip。
    """
    try:
        store = get_fast_store()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    json_bytes, gz_bytes, etag = store.get_bootstrap()

    # ETag 条件请求
    if_none_match = request.headers.get("If-None-Match", "")
    if if_none_match and (if_none_match == etag or if_none_match == f'W/"{etag.strip(chr(34))}"'):
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        resp.headers["Vary"] = "Accept-Encoding"
        return resp

    accept_encoding = request.headers.get("Accept-Encoding", "")
    if "gzip" in accept_encoding:
        resp = FlaskResponse(gz_bytes, mimetype="application/json")
        resp.headers["Content-Encoding"] = "gzip"
    else:
        resp = FlaskResponse(json_bytes, mimetype="application/json")

    resp.headers["ETag"] = etag
    resp.headers["Vary"] = "Accept-Encoding"
    # 允许每天数据更新后立即失效，不设 max-age
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@bp.get("/monitor-data/keyword/<keyword_id>")
def monitor_data_keyword(keyword_id: str):
    """返回单个完整 keyword 对象，支持 ETag/304。"""
    try:
        store = get_fast_store()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    body, etag = store.get_keyword(keyword_id)
    if body is None:
        return jsonify({"error": "keyword not found", "keyword_id": keyword_id}), 404

    if_none_match = request.headers.get("If-None-Match", "")
    if if_none_match and (if_none_match == etag or if_none_match == f'W/"{etag.strip(chr(34))}"'):
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        return resp

    resp = FlaskResponse(body, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@bp.get("/monitor-data/account/<account_id>")
def monitor_data_account(account_id: str):
    """返回单个完整 account 对象，支持 ETag/304。"""
    try:
        store = get_fast_store()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    body, etag = store.get_account(account_id)
    if body is None:
        return jsonify({"error": "account not found", "account_id": account_id}), 404

    if_none_match = request.headers.get("If-None-Match", "")
    if if_none_match and (if_none_match == etag or if_none_match == f'W/"{etag.strip(chr(34))}"'):
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        return resp

    resp = FlaskResponse(body, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ── 监控数据 ──────────────────────────────────────────
@bp.get("/monitor-data")
def monitor_data():
    try:
        return jsonify(load_monitor_payload())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc), "hint": "请先通过 TikHub 刷新关键词或运行 scripts/import_tikhub.py 生成数据"}), 404


# ── 文章 List ──────────────────────────────────────────
@bp.get("/articles")
def api_articles():
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
                page=page, page_size=page_size, sort=sort, time_range=time_range,
                min_hits=min_hits, account=account, search=search,
            )
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/articles/accounts")
def api_articles_accounts():
    try:
        return jsonify({"accounts": list_accounts_with_article_count()})
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


# ── 关键词控制层 ────────────────────────────────────────
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
    bucket = payload.get("keyword_bucket")
    if bucket is not None:
        bucket = str(bucket).strip()
    return jsonify(set_keyword_bucket(keyword_id, keyword_text, bucket))


# ── 关键词管理 (CRUD) ─────────────────────────────────
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
        return jsonify(update_keyword_group(
            group_id=group_id,
            label=str(label).strip() if label is not None else None,
            order=int(order) if order is not None else None,
        ))
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
        return jsonify(create_managed_keyword(group_id=group_id, keyword_text=keyword_text, note=note))
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
        return jsonify(update_managed_keyword(
            keyword_id=keyword_id,
            keyword_text=str(keyword_text).strip() if keyword_text is not None else None,
            note=str(note) if note is not None else None,
            group_id=str(group_id).strip() if group_id else None,
        ))
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


@bp.post("/keyword-manage/batch-default")
def api_save_batch_default():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    try:
        save_batch_default_selection(items)
        return jsonify({"saved": len(items)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── 刷新 ──────────────────────────────────────────────
@bp.post("/keywords/<keyword_id>/refresh")
def refresh_keyword(keyword_id: str):
    payload = request.get_json(silent=True) or {}
    keyword = str(payload.get("keyword", "")).strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400
    result = start_single_refresh(keyword)
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


@bp.post("/refresh-all/resume")
def refresh_all_resume():
    payload = request.get_json(silent=True) or {}
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        return jsonify({"error": "batch_id is required"}), 400
    try:
        state = resume_batch(batch_id)
    except BatchAlreadyRunningError as exc:
        return jsonify({"error": "batch already running", "batch": exc.state}), 409
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(state), 202


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
        kid = str(item or "").strip()
        if not kid or kid in seen_ids:
            continue
        seen_ids.add(kid)
        selected_ids.append(kid)
    unknown_ids = [kid for kid in selected_ids if kid not in candidate_map]
    if unknown_ids:
        return jsonify({"error": "keyword_ids contains invalid items", "invalid_keyword_ids": unknown_ids}), 400

    selected_items = [candidate_map[kid] for kid in selected_ids] if selected_ids else candidates
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


# ── 调度器 ──────────────────────────────────────────────
@bp.get("/scheduler/status")
def api_scheduler_status():
    return jsonify(scheduler_service.get_status())


@bp.post("/scheduler/config")
def api_scheduler_config():
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    interval_hours = payload.get("interval_hours")
    return jsonify(scheduler_service.update_config(
        enabled=bool(enabled) if enabled is not None else None,
        interval_hours=float(interval_hours) if interval_hours is not None else None,
    ))


@bp.post("/scheduler/trigger")
def api_scheduler_trigger():
    return jsonify(scheduler_service.trigger_now())


# ── 文章详情 / 抽屉 ────────────────────────────────────
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
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404




@bp.get("/note-detail")
def note_detail():
    """TikHub 懒加载笔记详情（首次进入抽屉时按需补全 desc / 工作 / IP / 原始链接）。

    Query: ?note_id=<id>&note_type=<normal|video>
    """
    from flask import request as _req
    note_id = _req.args.get("note_id", "").strip()
    note_type = _req.args.get("note_type", "normal").strip() or "normal"
    force = _req.args.get("force", "").lower() in ("1", "true", "yes")
    if not note_id:
        return jsonify({"error": "note_id is required"}), 400
    item = _tk_get_note_detail(note_id=note_id, note_type_hint=note_type, force=force)
    if item is None:
        return jsonify({"error": "TikHub detail unavailable", "note_id": note_id}), 404
    return jsonify(item)


@bp.get("/creator-detail")
def creator_detail():
    """TikHub 懒加载博主详情。Query: ?user_id=<id>&force=1."""
    from flask import request as _req
    user_id = _req.args.get("user_id", "").strip()
    force = _req.args.get("force", "").lower() in ("1", "true", "yes")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    item = _tk_get_creator_detail(user_id=user_id, force=force)
    if item is None:
        return jsonify({"error": "TikHub creator detail unavailable", "user_id": user_id}), 404
    return jsonify(item)



@bp.post("/article-covers")
def article_covers():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(resolve_article_covers_payload(
            normalized_dir=current_app.config["NORMALIZED_DIR"],
            raw_items=payload.get("articles", []),
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/article-cover-image")
def article_cover_image():
    """代理 XHS 公开封面图；上游失败时返回 1x1 透明 PNG 占位（不报错）。"""
    url = request.args.get("url", "").strip()
    # 1x1 transparent PNG fallback (避免 console.error 噪音)
    PNG_FALLBACK = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\r\nIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\r\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    try:
        body, content_type = fetch_cover_image_bytes(url)
        return Response(body, mimetype=content_type, headers={"Cache-Control": "public, max-age=86400"})
    except ValueError:
        # 非 XHS host / 无效 URL：返回 400 错误（前端会忽略）
        return jsonify({"error": "invalid XHS image url"}), 400
    except (requests.HTTPError, requests.RequestException):
        # 上游 CDN 5xx / 网络错误：返回 1x1 PNG 占位，console 不报红
        return Response(PNG_FALLBACK, mimetype="image/png", headers={"Cache-Control": "public, max-age=300"})
    except Exception:
        return Response(PNG_FALLBACK, mimetype="image/png", headers={"Cache-Control": "public, max-age=300"})


# ── 旁路服务 ──────────────────────────────────────────
@bp.get("/account-aliases")
def account_aliases():
    try:
        return jsonify(load_account_aliases())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.get("/penalty-signals")
def penalty_signals():
    try:
        return jsonify(load_penalty_signals())
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404

# ── 监控数据精简版（bootstrap，替代99MB全量） ─────────
