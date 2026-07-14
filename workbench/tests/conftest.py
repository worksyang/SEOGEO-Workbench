from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from content_hub.config import Settings
from content_hub.db.migrations import migrate


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    configured = replace(
        base.with_database(tmp_path / "hub.sqlite"),
        frontend_dist=tmp_path / "frontend-dist",
    )
    migrate(configured)
    return configured
