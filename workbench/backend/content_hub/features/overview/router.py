from __future__ import annotations

from fastapi import APIRouter, Request

from content_hub.db.connection import connect
from content_hub.features.overview.repository import OverviewRepository
from content_hub.features.overview.service import OverviewService

router = APIRouter(prefix="/api/v1/overview", tags=["overview"])


@router.get("")
def get_overview(request: Request) -> dict[str, object]:
    with connect(request.app.state.settings, readonly=True) as connection:
        service = OverviewService(OverviewRepository(connection))
        return {"ok": True, "data": service.get_overview()}
