"""agent_projection_service — Agent 观察包（暂未启用，按规范属第三阶段）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def load_agent_artifact(project_root: Path, filename: str) -> dict[str, Any]:
    raise FileNotFoundError(f"agent artifact not generated: {filename}")


def load_agent_evidence(project_root: Path, evidence_id: str) -> dict[str, Any]:
    raise FileNotFoundError(f"agent evidence not generated: {evidence_id}")


def load_metric_dictionary(project_root: Path) -> dict[str, Any]:
    raise FileNotFoundError("agent metric dictionary not generated yet")
