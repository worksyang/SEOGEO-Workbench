#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兼容旧入口：`python3 scripts/parse_search_md.py`

真实实现已迁移到：
  - app/ingest/parsers/search_md_parser.py
  - app/ingest/builders/entity_builder.py
  - app/ingest/builders/monitor_builder.py
  - app/ingest/rebuild.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ingest.rebuild import main


if __name__ == "__main__":
    raise SystemExit(main())
