"""共享 pytest fixtures 与 T-编号映射。

测试矩阵 T001-T180 由以下模块覆盖：
- test_api / test_backup / test_migrations / test_schema / test_validation（既有）
  └ T001-T040、T086-T130 的核心链路
- test_wechat / test_mp / test_xhs / test_geo（既有）
  └ 各系统端到端核心场景
- test_domain_ids.py        T041-T058 部分
- test_ingestion.py         T042、T045、T053、T054-T060
- test_signals.py           T131-T145 部分
- test_wiki_writing_publishing.py  T146-T165
- test_governance_api.py    T166-T180
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from content_hub.config import Settings
from content_hub.db.migrations import migrate


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    # 用 copy/replace 覆盖数据库路径与 frontend_dist，避免动到 home 真实目录
    database = tmp_path / "hub.sqlite"
    frontend = tmp_path / "frontend-dist"
    base_db = replace(base, database_path=database, frontend_dist=frontend)
    migrate(base_db)
    return base_db
