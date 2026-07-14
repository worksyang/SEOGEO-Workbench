from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
someurl2md_dir = PROJECT_DIR / "SomeURL2MD"
if str(someurl2md_dir) not in sys.path:
    sys.path.insert(1, str(someurl2md_dir))

from werss_client import WeRSSClient
from workflow_service import (
    WorkflowOptions,
    build_client,
    get_runtime_overview,
    get_wechat_auth_status,
    list_ai_model_options,
    probe_ai_model,
)

from .config import DATABASE_PATH, DEFAULT_SETTINGS, cors_origins
from .jobs import JobManager
from .store import AppStore


class SettingsUpdate(BaseModel):
    werss_base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    probe_keyword: Optional[str] = None
    output_dir: Optional[str] = None
    rejected_csv_file: Optional[str] = None
    days_to_fetch: Optional[int] = Field(default=None, ge=1, le=365)
    refresh_wait_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    start_page: Optional[int] = Field(default=None, ge=0, le=999)
    end_page: Optional[int] = Field(default=None, ge=0, le=999)
    classifier_platform: Optional[str] = None
    classifier_model: Optional[str] = None


class AccountFlagsUpdate(BaseModel):
    monitor_enabled: Optional[bool] = None
    run_enabled: Optional[bool] = None
    category_name: Optional[str] = None


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class JobCreate(BaseModel):
    refresh_before_run: bool = True
    use_ai_filter: bool = True
    days_to_fetch: Optional[int] = Field(default=None, ge=1, le=365)
    selected_mp_ids: Optional[List[str]] = None
    start_page: Optional[int] = Field(default=None, ge=0, le=999)
    end_page: Optional[int] = Field(default=None, ge=0, le=999)


class ModelSettingsUpdate(BaseModel):
    platform: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)


class ModelProbeRequest(BaseModel):
    platform: Optional[str] = None
    model: Optional[str] = None


def create_app() -> FastAPI:
    app = FastAPI(title="MP GUI Web Console", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    store = AppStore(DATABASE_PATH)
    jobs = JobManager()
    qr_cache: Dict[str, bytes] = {}

    def current_settings() -> Dict[str, Any]:
        return store.get_settings()

    def workflow_options(settings: Optional[Dict[str, Any]] = None) -> WorkflowOptions:
        data = settings or current_settings()
        return WorkflowOptions(
            base_url=str(data["werss_base_url"]),
            username=str(data["username"]),
            password=str(data["password"]),
            days_to_fetch=int(data["days_to_fetch"]),
            refresh_wait_seconds=int(data["refresh_wait_seconds"]),
            output_dir=str(data["output_dir"]),
            rejected_csv_file=str(data["rejected_csv_file"]),
            start_page=int(data["start_page"]) if data.get("start_page") is not None else None,
            end_page=int(data["end_page"]) if data.get("end_page") is not None else None,
            classifier_platform=str(data["classifier_platform"]),
            classifier_model=str(data["classifier_model"]),
        )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True, "service": "mpgui-web", "database": str(DATABASE_PATH)}

    @app.get("/api/settings")
    def get_settings() -> Dict[str, Any]:
        return current_settings()

    @app.put("/api/settings")
    def put_settings(payload: SettingsUpdate) -> Dict[str, Any]:
        updates = payload.dict(exclude_unset=True)
        return store.update_settings(updates)

    @app.get("/api/ai/models")
    def get_ai_models() -> Dict[str, Any]:
        settings = current_settings()
        options = list_ai_model_options(
            classifier_platform=str(settings["classifier_platform"]),
            classifier_model=str(settings["classifier_model"]),
        )
        return {
            **options,
            "classifier": {
                "platform": settings["classifier_platform"],
                "model": settings["classifier_model"],
            },
        }

    @app.put("/api/ai/models/classifier")
    def put_classifier_model(payload: ModelSettingsUpdate) -> Dict[str, Any]:
        options = list_ai_model_options()
        platforms = {item["key"]: item for item in options["platforms"]}
        if payload.platform not in platforms:
            raise HTTPException(status_code=400, detail=f"平台不存在：{payload.platform}")
        models = platforms[payload.platform].get("models", [])
        if models and payload.model not in models:
            raise HTTPException(status_code=400, detail=f"模型不在平台配置中：{payload.model}")

        settings = store.update_settings(
            {
                "classifier_platform": payload.platform,
                "classifier_model": payload.model,
            }
        )
        return {
            "platform": settings["classifier_platform"],
            "model": settings["classifier_model"],
        }

    @app.post("/api/ai/models/classifier/probe")
    def probe_classifier_model(payload: ModelProbeRequest) -> Dict[str, Any]:
        settings = current_settings()
        platform = payload.platform or str(settings["classifier_platform"])
        model = payload.model or str(settings["classifier_model"])
        try:
            return probe_ai_model(platform=platform, model=model, timeout_seconds=60)
        except Exception as exc:
            raise HTTPException(status_code=504, detail=f"模型 60 秒内未返回有效响应：{exc}") from exc

    @app.get("/api/runtime/overview")
    def runtime_overview() -> Dict[str, Any]:
        settings = current_settings()
        try:
            overview = get_runtime_overview(workflow_options(settings))
            overview["mps"] = store.merge_account_flags(overview.get("mps", []))
            overview["settings"] = {
                key: settings[key]
                for key in DEFAULT_SETTINGS.keys()
                if key != "password"
            }
            return overview
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"WeRSS 运行状态读取失败：{exc}") from exc

    @app.post("/api/auth/wechat/check")
    def check_wechat_auth() -> Dict[str, Any]:
        settings = current_settings()
        try:
            client = build_client(workflow_options(settings))
            return get_wechat_auth_status(client, keyword=str(settings["probe_keyword"]))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"微信登录状态强校验失败：{exc}") from exc

    @app.post("/api/auth/wechat/qrcode")
    def create_wechat_qrcode() -> Dict[str, Any]:
        settings = current_settings()
        try:
            client = build_client(workflow_options(settings))
            auth_status = get_wechat_auth_status(client, keyword=str(settings["probe_keyword"]))
            if auth_status.get("logged_in"):
                return {
                    "already_logged_in": True,
                    "auth_status": auth_status,
                    "image_url": None,
                    "raw": None,
                }

            qr = client.get_qr_code()
            image_bytes = client.wait_for_qr_code_image(qr["absolute_code_url"])
            qr_id = uuid.uuid4().hex
            qr_cache[qr_id] = image_bytes
            return {
                "already_logged_in": False,
                "auth_status": auth_status,
                "image_url": f"/api/auth/wechat/qrcode/image/{qr_id}",
                "raw": qr,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"二维码获取失败：{exc}") from exc

    @app.get("/api/auth/wechat/qrcode/image/{qr_id}")
    def get_wechat_qrcode_image(qr_id: str) -> Response:
        image = qr_cache.get(qr_id)
        if not image:
            raise HTTPException(status_code=404, detail="二维码图片不存在或已过期")
        return Response(content=image, media_type="image/png")

    @app.post("/api/auth/wechat/qrcode/finish")
    def finish_wechat_qrcode() -> Dict[str, Any]:
        try:
            settings = current_settings()
            client = build_client(workflow_options(settings))
            return {"ok": client.finish_qr_login()}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"扫码结束通知失败：{exc}") from exc

    @app.get("/api/accounts")
    def accounts() -> Dict[str, Any]:
        try:
            client = build_client(workflow_options())
            mps = client.get_all_mps()
            accounts = store.merge_account_flags(mps)
            return {
                "total": len(accounts),
                "accounts": accounts,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"公众号列表读取失败：{exc}") from exc

    @app.patch("/api/accounts/{mp_id}/flags")
    def update_account_flags(mp_id: str, payload: AccountFlagsUpdate) -> Dict[str, Any]:
        if (
            payload.monitor_enabled is None
            and payload.run_enabled is None
            and payload.category_name is None
        ):
            raise HTTPException(status_code=400, detail="没有需要更新的字段")
        try:
            return store.update_account_flags(
                mp_id=mp_id,
                monitor_enabled=payload.monitor_enabled,
                run_enabled=payload.run_enabled,
                category_name=payload.category_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/categories")
    def list_categories() -> Dict[str, Any]:
        categories = store.list_categories()
        return {"total": len(categories), "categories": categories}

    @app.post("/api/categories")
    def create_category(payload: CategoryCreate) -> Dict[str, Any]:
        try:
            return store.create_category(payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/categories/{name}")
    def delete_category(name: str) -> Dict[str, Any]:
        try:
            store.delete_category(name)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs")
    def list_jobs() -> Dict[str, Any]:
        return {"jobs": jobs.list_jobs()}

    @app.post("/api/jobs")
    def create_job(payload: JobCreate) -> Dict[str, Any]:
        settings = current_settings()
        selected_mp_ids: Set[str]
        if payload.selected_mp_ids is None:
            selected_mp_ids = store.selected_run_mp_ids()
        else:
            selected_mp_ids = {item for item in payload.selected_mp_ids if item}

        if not selected_mp_ids:
            raise HTTPException(status_code=400, detail="当前没有任何公众号被选中")

        try:
            return jobs.create_job(
                runtime_settings=settings,
                selected_mp_ids=selected_mp_ids,
                refresh_before_run=payload.refresh_before_run,
                use_ai_filter=payload.use_ai_filter,
                days_to_fetch=payload.days_to_fetch or int(settings["days_to_fetch"]),
                start_page=payload.start_page if payload.start_page is not None else int(settings["start_page"]),
                end_page=payload.end_page if payload.end_page is not None else int(settings["end_page"]),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> Dict[str, Any]:
        try:
            return jobs.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    return app


app = create_app()
