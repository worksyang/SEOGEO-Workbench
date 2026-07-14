"""适配器抽象。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AdapterStatus:
    adapter_key: str
    source_scope: str = ""
    total: int = 0
    written: int = 0
    failed: int = 0
    last_seen_at: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_key": self.adapter_key,
            "source_scope": self.source_scope,
            "total": self.total,
            "written": self.written,
            "failed": self.failed,
            "last_seen_at": self.last_seen_at,
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class AdapterTask:
    adapter_key: str
    status: AdapterStatus
    records_written: int = 0
    records_failed: int = 0
    finished_at: str = ""
    error: str = ""
