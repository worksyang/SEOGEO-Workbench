from __future__ import annotations

from content_hub.features.overview.repository import OverviewRepository


class OverviewService:
    def __init__(self, repository: OverviewRepository) -> None:
        self.repository = repository

    def get_overview(self) -> dict[str, object]:
        counts = self.repository.counts()
        return {
            "counts": counts,
            "systems": self.repository.systems(),
            "data_state": "empty" if not counts["contents"] else "ready",
        }
