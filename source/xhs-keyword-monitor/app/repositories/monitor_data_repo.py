from __future__ import annotations

import json
from pathlib import Path


class MonitorDataRepository:
    def __init__(self, monitor_data_path: Path) -> None:
        self.monitor_data_path = Path(monitor_data_path)

    def load(self) -> dict:
        if not self.monitor_data_path.exists():
            raise FileNotFoundError(f"monitor data not found: {self.monitor_data_path}")
        return json.loads(self.monitor_data_path.read_text(encoding="utf-8"))

