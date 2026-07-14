from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITOR_JS = ROOT / "app" / "static" / "js" / "monitor.js"
TURNOVER_JS = ROOT / "app" / "static" / "js" / "keyword-turnover.js"


def test_javascript_syntax() -> None:
    subprocess.run(["node", "--check", str(MONITOR_JS)], check=True)
    subprocess.run(["node", "--check", str(TURNOVER_JS)], check=True)


def test_main_page_uses_bootstrap_lazy_details_and_maps() -> None:
    source = MONITOR_JS.read_text(encoding="utf-8")
    assert "const DATA_URL = '/api/monitor-data/bootstrap';" in source
    assert "KEYWORD_DETAIL_API_BASE" in source
    assert "ACCOUNT_DETAIL_API_BASE" in source
    assert "const KEYWORD_BY_NAME = new Map();" in source
    assert "const ACCOUNT_BY_ID = new Map();" in source
    assert "fetchKeywordDetail(k).then" in source
    assert "fetchAccountDetail(a).then" in source
    assert "getTurnoverRuns" in source


def test_tooltip_is_lazy_and_has_no_pointer_hot_loop() -> None:
    source = MONITOR_JS.read_text(encoding="utf-8")
    assert "data-score-tooltip-html" not in source
    assert "elementFromPoint" not in source
    assert "addEventListener('mousemove'" not in source
    assert "data-account-id" in source
    assert "正在加载完整评分依据" in source


def test_account_pagination_contract() -> None:
    source = MONITOR_JS.read_text(encoding="utf-8")
    assert "const ACCOUNT_PAGE_SIZE = 100;" in source
    assert "visibleList = list.slice(0, accountPage * ACCOUNT_PAGE_SIZE);" in source
    assert "function loadMoreAccounts" in source
    assert "已显示 ${visibleList.length} / ${list.length}" in source
    assert "accountPageStateKey" in source


def test_turnover_page_never_downloads_full_monitor_payload() -> None:
    source = TURNOVER_JS.read_text(encoding="utf-8")
    assert "const BOOTSTRAP_URL = '/api/monitor-data/bootstrap';" in source
    assert "KEYWORD_DETAIL_API_BASE" in source
    assert "const DATA_URL = '/api/monitor-data';" not in source
