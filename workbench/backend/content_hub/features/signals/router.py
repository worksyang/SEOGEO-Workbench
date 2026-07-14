"""Signals 路由：信号查询 + 触发检测。
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from content_hub.db.connection import connect
from content_hub.services.signals import SignalsService

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])


@router.get("")
def list_signals(
    request: Request,
    signal_date: str | None = None,
    signal_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    with connect(request.app.state.settings, readonly=True) as connection:
        svc = SignalsService(connection)
        items = svc.list_signals(signal_date=signal_date, signal_type=signal_type, limit=limit)
        summary: dict[str, int] = {}
        for item in items:
            summary[item["signal_type"]] = summary.get(item["signal_type"], 0) + 1
        return {"ok": True, "data": {"items": items, "summary": summary, "total": len(items)}}


@router.post("/detect")
def detect(request: Request) -> dict:
    with connect(request.app.state.settings, readonly=False) as connection:
        svc = SignalsService(connection)
        return {"ok": True, "data": svc.detect_all()}
