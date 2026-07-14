from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app


def _penalty_signals_path() -> Path:
    normalized_dir = Path(current_app.config["NORMALIZED_DIR"])
    return normalized_dir / "penalty_signals.json"


def load_penalty_signals() -> dict[str, Any]:
    path = _penalty_signals_path()
    if not path.exists():
        raise FileNotFoundError(f"penalty signals not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
