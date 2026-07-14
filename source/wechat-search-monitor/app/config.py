from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    NORMALIZED_DIR = PROJECT_ROOT / "normalized"
    MONITOR_DATA_FILE = NORMALIZED_DIR / "monitor-data.json"
    KEYWORD_READ_DELTAS_FILE = NORMALIZED_DIR / "keyword_read_deltas.json"
    ARTICLE_METRIC_META_FILE = NORMALIZED_DIR / "article_metric_observations_meta.json"
    STATE_DIR = PROJECT_ROOT / "data" / "state"
    AGENT_DATA_DIR = PROJECT_ROOT / "data" / "agent"
    AGENT_METRIC_DICTIONARY_FILE = PROJECT_ROOT / "data" / "config" / "agent_metric_dictionary.json"
    SQLITE_PATH = STATE_DIR / "app.db"
    KEYWORD_BLOCKLIST_FILE = PROJECT_ROOT / "data" / "config" / "keyword_blocklist.json"
    AIDSO_PLAYWRIGHT_PROFILE_DIR = STATE_DIR / "aidso_playwright_profile"
    SECRET_KEY = "monitor-dev-secret"
    PORT: int = int(os.environ.get("PORT", "8765"))
    DISABLE_SCHEDULER: bool = _env_bool("ZK_MONITOR_DISABLE_SCHEDULER")
    AUTO_REFRESH_ENABLED: bool = False
    AUTO_REFRESH_INTERVAL_HOURS: float = 24.0
