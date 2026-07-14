from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / ".web_console"
DATABASE_PATH = Path(os.getenv("MPGUI_DB_PATH", DATA_DIR / "app.db"))
DEFAULT_CLASSIFIER_PLATFORM = os.getenv("MPGUI_CLASSIFIER_PLATFORM", "chatnp_gemini")
DEFAULT_CLASSIFIER_MODEL = os.getenv("MPGUI_CLASSIFIER_MODEL", "gemini-3-flash-preview")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


DEFAULT_SETTINGS: Dict[str, object] = {
    "werss_base_url": os.getenv("MPGUI_WERSS_BASE_URL", "http://192.168.31.89:8001"),
    "username": os.getenv("MPGUI_WERSS_USERNAME", "admin"),
    "password": os.getenv("MPGUI_WERSS_PASSWORD", "admin@123"),
    "probe_keyword": os.getenv("MPGUI_PROBE_KEYWORD", "大湾通一峰火燎源"),
    "output_dir": os.getenv(
        "MPGUI_OUTPUT_DIR",
        "/Users/works14/Documents/output_md",
    ),
    "rejected_csv_file": os.getenv(
        "MPGUI_REJECTED_CSV_FILE",
        str(PROJECT_DIR / "rejected_articles.csv"),
    ),
    "days_to_fetch": _int_env("MPGUI_DAYS_TO_FETCH", 15),
    "refresh_wait_seconds": _int_env("MPGUI_REFRESH_WAIT_SECONDS", 10),
    "start_page": 0,
    "end_page": 20,
    "classifier_platform": DEFAULT_CLASSIFIER_PLATFORM,
    "classifier_model": DEFAULT_CLASSIFIER_MODEL,
}

DEFAULT_RUN_MP_IDS = {"MP_WXS_3921283819"}


def cors_origins() -> List[str]:
    raw = os.getenv(
        "MPGUI_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
    )
    return [item.strip() for item in raw.split(",") if item.strip()]
