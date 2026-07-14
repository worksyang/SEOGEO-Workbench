from __future__ import annotations

import os
from pathlib import Path

# 把父目录 `.env` 视作候选：避免硬编码；这里只做追加，不覆盖已有变量
_PARENT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _PARENT_ENV.exists():
    try:
        for raw in _PARENT_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv_into_environ(env_path: Path) -> None:
    """轻量 .env 加载（不依赖 python-dotenv），优先级低于系统 env。"""
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        return


# 项目根目录 .env 自动加载
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv_into_environ(_PROJECT_ROOT / ".env")


class Config:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    # 数据源 provider：tikhub（默认）| redfox（仅作为历史审计保留）
    DATA_PROVIDER: str = os.environ.get("XHS_DATA_PROVIDER", "tikhub").strip().lower()

    # TikHub 配置
    TIKHUB_BASE_URL: str = os.environ.get("TIKHUB_BASE_URL", "https://api.tikhub.io").strip()
    TIKHUB_API_TOKEN: str = os.environ.get("TIKHUB_API_TOKEN", "").strip()
    TIKHUB_TIMEOUT: int = int(os.environ.get("TIKHUB_TIMEOUT", "60"))
    TIKHUB_INTER_REQUEST_DELAY: float = float(os.environ.get("TIKHUB_INTER_REQUEST_DELAY", "0.3"))
    TIKHUB_MAX_RETRIES: int = int(os.environ.get("TIKHUB_MAX_RETRIES", "3"))

    NORMALIZED_DIR = PROJECT_ROOT / "normalized"
    MONITOR_DATA_FILE = NORMALIZED_DIR / "monitor-data.json"
    # 当前 provider 的 raw 目录；老 raw/redfox/xhs 保留作审计，不混入
    RAW_DIR = PROJECT_ROOT / "data" / "raw" / DATA_PROVIDER / "xhs"
    RAW_DIR_LEGACY = PROJECT_ROOT / "data" / "raw" / "redfox" / "xhs"


    DATA_DIR = PROJECT_ROOT / "data"
    STATE_DIR = DATA_DIR / "state"
    AGENT_DATA_DIR = DATA_DIR / "agent"
    REFRESH_JOBS_DIR = DATA_DIR / "refresh_jobs"
    RUNS_DIR = DATA_DIR / "runs"
    TMP_DIR = DATA_DIR / "tmp"
    BATCH_TEMP_ROOT = TMP_DIR / "batch_refresh"

    AGENT_METRIC_DICTIONARY_FILE = DATA_DIR / "config" / "agent_metric_dictionary.json"
    SQLITE_PATH = STATE_DIR / "app.db"
    KEYWORDS_CONFIG_FILE = DATA_DIR / "config" / "keywords.json"
    KEYWORD_BLOCKLIST_FILE = DATA_DIR / "config" / "keyword_blocklist.json"

    REDFOX_BASE_URL = os.environ.get("REDFOX_BASE_URL", "https://redfox.hk").rstrip("/")
    REDFOX_API_KEY = os.environ.get("REDFOX_API_KEY", "").strip()
    REDFOX_TIMEOUT = int(os.environ.get("REDFOX_TIMEOUT", "60"))

    SECRET_KEY = "xhs-monitor-dev-secret"
    PORT: int = int(os.environ.get("PORT", "8766"))
    DISABLE_SCHEDULER: bool = _env_bool("ZK_MONITOR_DISABLE_SCHEDULER", default=True)
    AUTO_REFRESH_ENABLED: bool = _env_bool("AUTO_REFRESH_ENABLED", default=False)
    AUTO_REFRESH_INTERVAL_HOURS: float = float(os.environ.get("AUTO_REFRESH_INTERVAL_HOURS", "24.0"))

    PLATFORM = "小红书"
    PLATFORM_CODE = "xhs"
    SCORE_METHOD = "xhs_three_board_breakthrough_v1"

    DEFAULT_FETCH_OFFSETS = (0,)
    DEFAULT_FETCH_PAGE_SIZE = 20
    DEFAULT_FETCH_TIMEOUT = 60

def require_provider_token(provider: str) -> str:
    """返回 provider 所需的 token；若不存在则抛错。"""
    if provider == "tikhub":
        if not Config.TIKHUB_API_TOKEN:
            raise RuntimeError(
                "TIKHUB_API_TOKEN 未设置；当前 XHS_DATA_PROVIDER=tikhub，但本地 .env 没有 token。"
                "请通过环境变量或 .env 设置 TIKHUB_API_TOKEN。"
            )
        return Config.TIKHUB_API_TOKEN
    if provider == "redfox":
        return Config.REDFOX_API_KEY
    raise RuntimeError(f"unknown provider: {provider}")
