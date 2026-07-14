from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app


def _account_aliases_path() -> Path:
    normalized_dir = Path(current_app.config["NORMALIZED_DIR"])
    return normalized_dir / "account_aliases.json"


def load_account_aliases() -> dict[str, Any]:
    path = _account_aliases_path()
    if not path.exists():
        raise FileNotFoundError(f"account aliases not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
