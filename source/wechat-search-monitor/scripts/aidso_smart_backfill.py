#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIDSO DSO → WSO 智能补数脚本。

目标：把微信 WSO 每日有限次数优先用在“更可能有搜索量”的词上。

候选来源：
1. data/state/app.db 统一关键词注册表里的 active 关键词
2. normalized/aidso_dso_heat.json 里已经有抖音热度的关键词
3. DSO 下拉词 top20
4. normalized/snapshot_terms.json 里的微信搜索下拉词 / 相关词

筛选原则：
- DSO 月覆盖越高，WSO 优先级越高
- 主关键词优先于下拉词 / 相关词
- 下拉词 / 相关词里，出现频次高、位置靠前、父词热度高的优先
- 明显像文章标题、过长长尾、已有 WSO 低量/无数据记录的跳过

写入：
- WSO：normalized/aidso_wso_heat.json
- DSO 额外词：normalized/aidso_dso_heat.json
- 运行记录：data/runs/aidso_smart_backfill_<timestamp>/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.aidso_keyword_heat_service import (  # noqa: E402
    AidsoHeatError,
    AidsoLoginRequiredError,
    AidsoProfileBusyError,
    DEFAULT_BROWSER_CHANNEL,
)
from scripts.aidso_dso_batch import EXTRACT_JS as DSO_EXTRACT_JS  # noqa: E402
from scripts.aidso_dso_batch import parse_count as parse_display_count  # noqa: E402

ROOT = PROJECT_ROOT
SNAPSHOT_TERMS_PATH = ROOT / "normalized" / "snapshot_terms.json"
DSO_PATH = ROOT / "normalized" / "aidso_dso_heat.json"
WSO_PATH = ROOT / "normalized" / "aidso_wso_heat.json"
RUNS_ROOT = ROOT / "data" / "runs"
DEFAULT_PROFILE_DIR = str(ROOT / "data" / "state" / "aidso_playwright_profile")

DOMAIN_WORDS = (
    "香港", "保险", "港险", "友邦", "保诚", "安盛", "宏利", "万通", "富卫", "永明", "国寿",
    "储蓄", "分红", "传承", "高端医疗", "重疾", "理财", "保单", "投保", "退保", "提领",
)
TITLE_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:]|\s{2,}")
BAD_TEXT_RE = re.compile(r"https?://|[\n\r\t]|^\d+$")


@dataclass
class Candidate:
    keyword_text: str
    keyword_id: str
    score: float = 0.0
    source_scores: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    dso_month_cover_count: int = 0
    dso_week_avg_search: int = 0
    dso_down_keyword_count: int = 0
    snapshot_term_freq: int = 0
    snapshot_best_position: int | None = None
    wso_existing_count: int | None = None
    wso_existing_error: str | None = None
    skip_reason: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_id(prefix: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def normalize_keyword(text: Any) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def text_len(text: str) -> int:
    return len(text.replace(" ", ""))


def has_domain_signal(text: str) -> bool:
    return any(word in text for word in DOMAIN_WORDS)


def parse_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return parse_display_count(value)


def load_all_keywords() -> dict[str, dict[str, Any]]:
    from app.repositories.keyword_registry_repo import KeywordRegistryRepository
    payload = KeywordRegistryRepository(
        ROOT / "data/state/app.db"
    ).load_payload()
    out: dict[str, dict[str, Any]] = {}
    for group in payload.get("groups", []):
        group_label = str(group.get("label") or "未分类")
        for item in group.get("keywords", []):
            text = normalize_keyword(item.get("keyword_text"))
            if not text:
                continue
            out[text] = {
                **item,
                "group_label": group_label,
                "keyword_id": str(item.get("keyword_id") or stable_id("kw", text)),
            }
    return out


def default_dso_payload(profile_dir: str) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "aidso_dso",
        "channel": "dso",
        "platform": "douyin",
        "api_endpoints": {
            "detail": "https://task.aidso.com/dso/api/keyword/info/detail",
            "down_word": "https://api.aidso.com/dso/api/keyword/info/down_word",
            "trend": "https://api.aidso.com/dso/api/keyword/search/compare/trend",
        },
        "page_url_template": "https://dso.aidso.com/keyWordDetail/detail?keyword={keyword}",
        "profile_dir": profile_dir,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "total_keywords": 0,
        "fetched_count": 0,
        "error_count": 0,
        "items": [],
    }


def default_wso_payload(profile_dir: str) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "aidso_wso",
        "channel": "wso",
        "platform": "wechat",
        "api_endpoints": {
            "detail": "https://task.aidso.com/dso/api/keyword/info/wx/detail",
            "down_word": "https://api.aidso.com/dso/api/keyword/info/wx/down_word",
        },
        "page_url_template": "https://dso.aidso.com/WsoKeyWordDetail/detail?keyword={keyword}",
        "profile_dir": profile_dir,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "total_keywords": 0,
        "fetched_count": 0,
        "error_count": 0,
        "items": [],
    }


def load_dso_payload(profile_dir: str) -> dict[str, Any]:
    payload = read_json(DSO_PATH, default_dso_payload(profile_dir))
    payload.setdefault("items", [])
    payload["profile_dir"] = profile_dir
    return payload


def load_wso_payload(profile_dir: str) -> dict[str, Any]:
    payload = read_json(WSO_PATH, default_wso_payload(profile_dir))
    payload.setdefault("items", [])
    payload["profile_dir"] = profile_dir
    return payload


def refresh_heat_counts(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.setdefault("items", [])
    payload["updated_at"] = now_iso()
    payload["total_keywords"] = len(items)
    payload["fetched_count"] = sum(1 for item in items if item.get("error") is None)
    payload["error_count"] = sum(1 for item in items if item.get("error") is not None)
    return payload


def item_lookup(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in payload.get("items", []):
        text = normalize_keyword(item.get("keyword_text"))
        if text:
            out[text] = item
    return out


def upsert_item(payload: dict[str, Any], item: dict[str, Any]) -> None:
    text = normalize_keyword(item.get("keyword_text"))
    items = payload.setdefault("items", [])
    for idx, old in enumerate(items):
        if normalize_keyword(old.get("keyword_text")) == text:
            items[idx] = item
            return
    items.append(item)


def add_candidate(candidates: dict[str, Candidate], text: Any, keyword_id: str | None, source: str, score: float, reason: str) -> None:
    clean = normalize_keyword(text)
    if not clean:
        return
    if BAD_TEXT_RE.search(clean):
        return
    candidate = candidates.get(clean)
    if not candidate:
        candidate = Candidate(
            keyword_text=clean,
            keyword_id=keyword_id or stable_id("aidso", clean),
        )
        candidates[clean] = candidate
    if keyword_id and candidate.keyword_id.startswith("aidso_"):
        candidate.keyword_id = keyword_id
    candidate.score += score
    candidate.source_scores[source] = round(candidate.source_scores.get(source, 0.0) + score, 3)
    candidate.reasons.append(reason)


def log_score(value: int, multiplier: float, cap: float) -> float:
    if value <= 0:
        return 0.0
    return min(cap, math.log10(value + 1) * multiplier)


def apply_dso_metrics(candidate: Candidate, item: dict[str, Any]) -> None:
    candidate.dso_month_cover_count = max(candidate.dso_month_cover_count, parse_int(item.get("month_cover_count")))
    candidate.dso_week_avg_search = max(candidate.dso_week_avg_search, parse_int(item.get("week_avg_search")))
    candidate.dso_down_keyword_count = max(candidate.dso_down_keyword_count, parse_int(item.get("down_keyword_count")))


def apply_wso_metrics(candidate: Candidate, item: dict[str, Any]) -> None:
    if item is None:
        return
    count = item.get("month_cover_count")
    candidate.wso_existing_count = int(count) if isinstance(count, (int, float)) else None
    err = item.get("error")
    candidate.wso_existing_error = str(err) if err else None


def load_snapshot_term_stats() -> tuple[Counter, dict[str, int], dict[str, set[str]]]:
    payload = read_json(SNAPSHOT_TERMS_PATH, [])
    freq: Counter = Counter()
    best_pos: dict[str, int] = {}
    types: dict[str, set[str]] = defaultdict(set)
    for item in payload:
        text = normalize_keyword(item.get("term_text"))
        if not text:
            continue
        pos = int(item.get("position") or 99)
        freq[text] += 1
        best_pos[text] = min(best_pos.get(text, 99), pos)
        types[text].add(str(item.get("term_type") or "term"))
    return freq, best_pos, types


def build_candidates(profile_dir: str, *, min_dso_for_extra: int = 20) -> tuple[list[Candidate], list[Candidate]]:
    enabled = load_all_keywords()
    dso_payload = load_dso_payload(profile_dir)
    wso_payload = load_wso_payload(profile_dir)
    dso_map = item_lookup(dso_payload)
    wso_map = item_lookup(wso_payload)
    candidates: dict[str, Candidate] = {}

    for text, item in enabled.items():
        add_candidate(
            candidates,
            text,
            item.get("keyword_id"),
            "config",
            45.0,
            f"监控主词 / {item.get('group_label')}",
        )

    for text, item in dso_map.items():
        if item.get("error"):
            continue
        dso_count = parse_int(item.get("month_cover_count"))
        week_avg = parse_int(item.get("week_avg_search"))
        down_count = parse_int(item.get("down_keyword_count"))
        if dso_count <= 0 and week_avg <= 0 and down_count <= 0:
            continue
        add_candidate(
            candidates,
            text,
            item.get("keyword_id"),
            "dso_keyword",
            18.0 + log_score(dso_count, 18, 70) + min(20, week_avg * 0.08) + min(12, down_count * 0.04),
            f"DSO 已有热度：月覆盖 {dso_count}",
        )
        parent_bonus = min(32, log_score(dso_count, 8, 24) + min(8, down_count * 0.02))
        for down in item.get("down_words_top20", []) or []:
            down_text = normalize_keyword(down.get("text") or down.get("keyword"))
            if not down_text or down_text == text:
                continue
            rank = int(down.get("rank") or 99)
            down_cover = parse_int(down.get("month_cover_count"))
            if rank > 12 and down_cover < min_dso_for_extra:
                continue
            score = 8.0 + parent_bonus + log_score(down_cover, 10, 24) + max(0, 10 - rank) * 0.8
            add_candidate(
                candidates,
                down_text,
                None,
                "dso_down_word",
                score,
                f"DSO 下拉词：父词 {text} / rank {rank} / 月覆盖 {down_cover}",
            )

    freq, best_pos, types = load_snapshot_term_stats()
    for text, count in freq.items():
        pos = best_pos.get(text, 99)
        type_set = types.get(text) or set()
        type_boost = 4.0 if "suggestion" in type_set else 2.0
        score = type_boost + min(18, count * 1.6) + max(0, 11 - pos) * 0.7
        add_candidate(
            candidates,
            text,
            None,
            "wechat_terms",
            score,
            f"微信搜索{'/'.join(sorted(type_set))}：出现 {count} 次 / 最高位置 {pos}",
        )

    for text, candidate in candidates.items():
        dso_item = dso_map.get(text)
        if dso_item:
            apply_dso_metrics(candidate, dso_item)
        wso_item = wso_map.get(text)
        if wso_item:
            apply_wso_metrics(candidate, wso_item)
        candidate.snapshot_term_freq = freq.get(text, 0)
        candidate.snapshot_best_position = best_pos.get(text)
        if has_domain_signal(text):
            candidate.score += 8.0
            candidate.source_scores["domain_signal"] = candidate.source_scores.get("domain_signal", 0.0) + 8.0
            candidate.reasons.append("含港险/保司/产品等业务信号")
        candidate.skip_reason = resolve_skip_reason(candidate)
        candidate.score = round(candidate.score, 3)

    all_candidates = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    runnable = [c for c in all_candidates if c.skip_reason is None]
    skipped = [c for c in all_candidates if c.skip_reason is not None]
    return runnable, skipped


def resolve_skip_reason(candidate: Candidate) -> str | None:
    text = candidate.keyword_text
    n = text_len(text)

    if candidate.wso_existing_error is None and candidate.wso_existing_count is not None:
        if candidate.wso_existing_count >= 20:
            return "已有 WSO 有效数据，不重复消耗次数"
        return "已有 WSO 低量/零量记录，先跳过省次数"
    if candidate.wso_existing_error == "no_data":
        return "已有 WSO no_data 记录，先跳过省次数"

    if n <= 1:
        return "词太短"
    if n > 30:
        return "明显过长，像文章标题"
    if n > 22 and candidate.dso_month_cover_count < 1000:
        return "长尾过长且 DSO 热度不够"
    if n > 18 and TITLE_PUNCT_RE.search(text) and candidate.dso_month_cover_count < 2000:
        return "像文章标题/句子，不优先搜"
    if n > 16 and not has_domain_signal(text) and candidate.dso_month_cover_count < 300:
        return "长尾且业务信号弱"
    if candidate.dso_month_cover_count < 10 and candidate.snapshot_term_freq < 2 and "config" not in candidate.source_scores:
        return "缺少 DSO/微信下拉重复信号"
    return None


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "keyword_id": candidate.keyword_id,
        "keyword_text": candidate.keyword_text,
        "score": candidate.score,
        "source_scores": {k: round(v, 3) for k, v in sorted(candidate.source_scores.items())},
        "dso_month_cover_count": candidate.dso_month_cover_count,
        "dso_week_avg_search": candidate.dso_week_avg_search,
        "dso_down_keyword_count": candidate.dso_down_keyword_count,
        "snapshot_term_freq": candidate.snapshot_term_freq,
        "snapshot_best_position": candidate.snapshot_best_position,
        "wso_existing_count": candidate.wso_existing_count,
        "wso_existing_error": candidate.wso_existing_error,
        "skip_reason": candidate.skip_reason,
        "reasons": candidate.reasons[:8],
    }


def wso_result_to_item(candidate: Candidate, result: dict[str, Any]) -> dict[str, Any]:
    """把 WSO 搜索页列表接口的一行结果落成现有 normalized 结构。"""
    exact = result.get("exact") or {}
    rows = result.get("rows") or []
    found = bool(exact)
    down_words = [row for row in rows if normalize_keyword(row.get("keyword")) != candidate.keyword_text]
    return {
        "keyword_id": candidate.keyword_id,
        "keyword_text": candidate.keyword_text,
        "fetched_at": utc_now_z(),
        "month_cover_count": parse_int(exact.get("month_cover_count")) if found else None,
        "month_click_count": parse_int(exact.get("month_click_count")) if found else None,
        "month_cover_count_str": exact.get("month_cover_count_str") if found else None,
        "down_keyword_count": parse_int(exact.get("down_keyword_count")) if found else 0,
        "down_keyword_month_covercount": parse_int(exact.get("down_keyword_month_covercount")) if found else 0,
        "competition": exact.get("competition") if found else None,
        "competition_cn": exact.get("competition_cn") if found else None,
        "word_length": parse_int(exact.get("word_length")) if found else text_len(candidate.keyword_text),
        "type_enums": exact.get("type_enums") or [],
        "tags": exact.get("type_enums") or [],
        "down_words_top20": [
            {
                "rank": idx + 1,
                "text": row.get("keyword"),
                "month_cover_count": parse_int(row.get("month_cover_count")),
                "month_click_count": parse_int(row.get("month_click_count")),
                "competition": row.get("competition"),
                "competition_cn": row.get("competition_cn"),
                "type_enums": row.get("type_enums") or [],
            }
            for idx, row in enumerate(down_words[:20])
        ],
        "error": None if found else "no_data",
        "smart_backfill": {
            "score": candidate.score,
            "source_scores": candidate.source_scores,
            "dso_month_cover_count": candidate.dso_month_cover_count,
            "snapshot_term_freq": candidate.snapshot_term_freq,
            "search_page_total_record": result.get("total_record"),
            "search_page_original_total_record": result.get("original_total_record"),
        },
    }


def _extract_wso_search_result(keyword: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("result") if isinstance(data, dict) else None
    rows = rows if isinstance(rows, list) else []
    exact = None
    for row in rows:
        if normalize_keyword(row.get("keyword")) == keyword:
            exact = row
            break
    if exact is None and rows:
        first = rows[0]
        if normalize_keyword(first.get("keyword")) == keyword or str(first.get("id") or "") == keyword:
            exact = first
    return {
        "keyword": keyword,
        "found": exact is not None,
        "exact": exact,
        "rows": rows,
        "total_record": data.get("total_record") if isinstance(data, dict) else None,
        "original_total_record": data.get("original_total_record") if isinstance(data, dict) else None,
        "fetched_at": utc_now_z(),
    }


def _fetch_wso_search_page_result(page, keyword: str, wait_timeout_ms: int) -> dict[str, Any]:
    payload_holder: dict[str, Any] = {"payload": None, "status": None, "url": None}

    def handle_response(response) -> None:
        if "/dso/api/keyword/info/wx/list" not in response.url:
            return
        try:
            payload_holder["payload"] = response.json()
            payload_holder["status"] = response.status
            payload_holder["url"] = response.url
        except Exception as exc:
            payload_holder["payload"] = {"code": "parse_error", "msg": str(exc)}
            payload_holder["status"] = response.status
            payload_holder["url"] = response.url

    page.on("response", handle_response)
    try:
        page.goto(f"https://dso.aidso.com/KeywordWSO/searchWord?keyword={quote(keyword)}", wait_until="domcontentloaded", timeout=max(15_000, wait_timeout_ms))
        try:
            page.wait_for_function("() => !!localStorage.getItem('token')", timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise AidsoLoginRequiredError("当前 AIDSO profile 未登录，或登录态已失效。") from exc

        start = time.time()
        while time.time() - start < wait_timeout_ms / 1000:
            payload = payload_holder.get("payload")
            if isinstance(payload, dict):
                code = payload.get("code")
                if code == 200:
                    return _extract_wso_search_result(keyword, payload)
                if code in (401, 403) or "登录" in str(payload.get("msg") or ""):
                    raise AidsoLoginRequiredError(str(payload.get("msg") or "AIDSO 登录态不可用"))
                if code is not None:
                    raise AidsoHeatError(f"WSO list 接口返回异常：{payload.get('msg') or code}")
            page.wait_for_timeout(250)
        raise AidsoHeatError("未捕获到 WSO list 响应。请确认搜索页可正常打开。")
    finally:
        page.remove_listener("response", handle_response)


def dso_extract_to_item(candidate: Candidate, extracted: dict[str, Any]) -> dict[str, Any]:
    stats = extracted.get("stats") or {}
    item = {
        "keyword_id": candidate.keyword_id,
        "keyword_text": candidate.keyword_text,
        "fetched_at": utc_now_z(),
        "month_cover_count": parse_display_count(stats.get("月覆盖人次")),
        "_month_cover_count_raw": stats.get("月覆盖人次"),
        "week_avg_search": parse_display_count(stats.get("7日平均搜索")),
        "down_keyword_count": parse_display_count(stats.get("下拉词数量")),
        "down_keyword_month_covercount": parse_display_count(stats.get("下拉词月覆盖")),
        "competition": stats.get("竞争度", "n/a"),
        "city_level": stats.get("占比最大城市", "-"),
        "tags_raw": stats.get("类型", "-"),
        "down_words_top20": extracted.get("down_words", [])[:20],
        "down_words_total": len(extracted.get("down_words", [])),
        "error": None,
        "smart_backfill": {
            "score": candidate.score,
            "source_scores": candidate.source_scores,
            "snapshot_term_freq": candidate.snapshot_term_freq,
        },
    }
    tags_raw = item.get("tags_raw")
    item["tags"] = [] if not tags_raw or tags_raw == "-" else [t.strip() for t in str(tags_raw).split() if t.strip()]
    return item


def run_dso_extra(candidates: list[Candidate], run_dir: Path, *, profile_dir: str, max_count: int, sleep_sec: float, channel: str | None) -> dict[str, int]:
    if max_count <= 0:
        return {"attempted": 0, "success": 0, "failed": 0}

    dso_payload = load_dso_payload(profile_dir)
    dso_map = item_lookup(dso_payload)
    queue = [c for c in candidates if c.keyword_text not in dso_map]
    queue = queue[:max_count]
    write_json(run_dir / "dso_extra_queue.json", [candidate_to_dict(c) for c in queue])
    if not queue:
        return {"attempted": 0, "success": 0, "failed": 0}

    stats = {"attempted": 0, "success": 0, "failed": 0}
    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(Path(profile_dir).expanduser().resolve()),
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15_000)
            for idx, candidate in enumerate(queue, start=1):
                stats["attempted"] += 1
                event_base = {
                    "channel": "dso",
                    "index": idx,
                    "total": len(queue),
                    "keyword_text": candidate.keyword_text,
                    "score": candidate.score,
                }
                try:
                    url = f"https://dso.aidso.com/keyWordDetail/detail?keyword={quote(candidate.keyword_text)}"
                    page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    try:
                        page.wait_for_selector("text=月覆盖人次", timeout=8_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(900)
                    extracted = page.evaluate(DSO_EXTRACT_JS)
                    if not extracted or extracted.get("error"):
                        raise RuntimeError(f"extract failed: {extracted}")
                    item = dso_extract_to_item(candidate, extracted)
                    upsert_item(dso_payload, item)
                    refresh_heat_counts(dso_payload)
                    write_json(DSO_PATH, dso_payload)
                    stats["success"] += 1
                    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), **event_base, "status": "ok", "month_cover_count": item.get("month_cover_count")})
                    print(f"[DSO-extra] [{idx}/{len(queue)}] OK {candidate.keyword_text} 月覆盖={item.get('month_cover_count')}", flush=True)
                except Exception as exc:
                    stats["failed"] += 1
                    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), **event_base, "status": "failed", "error": str(exc)[:300]})
                    print(f"[DSO-extra] [{idx}/{len(queue)}] FAIL {candidate.keyword_text}: {str(exc)[:160]}", flush=True)
                time.sleep(max(0.0, sleep_sec + random.uniform(-0.4, 0.6)))
        finally:
            context.close()
    return stats


def run_wso(candidates: list[Candidate], run_dir: Path, *, profile_dir: str, max_count: int, sleep_sec: float, wait_timeout_ms: int, channel: str | None) -> dict[str, int]:
    if max_count <= 0:
        return {"attempted": 0, "success": 0, "failed": 0, "stopped": 0}

    wso_payload = load_wso_payload(profile_dir)
    wso_map = item_lookup(wso_payload)
    queue = []
    for candidate in candidates:
        existing = wso_map.get(candidate.keyword_text)
        if existing and (existing.get("error") in (None, "no_data") or existing.get("month_cover_count") is not None):
            continue
        queue.append(candidate)
        if len(queue) >= max_count:
            break

    write_json(run_dir / "wso_queue.json", [candidate_to_dict(c) for c in queue])
    if not queue:
        return {"attempted": 0, "success": 0, "failed": 0, "stopped": 0}

    stats = {"attempted": 0, "success": 0, "failed": 0, "stopped": 0}
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(Path(profile_dir).expanduser().resolve()),
        "headless": True,
        "viewport": {"width": 1440, "height": 960},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if channel:
        launch_kwargs["channel"] = channel

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        except PlaywrightError as exc:
            if "ProcessSingleton" in str(exc) or "SingletonLock" in str(exc) or "profile is already in use" in str(exc):
                raise AidsoProfileBusyError("AIDSO profile 当前正被其他浏览器实例占用。") from exc
            raise

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(wait_timeout_ms)
            for idx, candidate in enumerate(queue, start=1):
                stats["attempted"] += 1
                event_base = {
                    "channel": "wso",
                    "index": idx,
                    "total": len(queue),
                    "keyword_text": candidate.keyword_text,
                    "score": candidate.score,
                }
                try:
                    result = _fetch_wso_search_page_result(page, candidate.keyword_text, wait_timeout_ms)
                    item = wso_result_to_item(candidate, result)
                    upsert_item(wso_payload, item)
                    refresh_heat_counts(wso_payload)
                    write_json(WSO_PATH, wso_payload)
                    stats["success"] += 1
                    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), **event_base, "status": "ok", "month_cover_count": item.get("month_cover_count"), "error": item.get("error")})
                    print(f"[WSO-list] [{idx}/{len(queue)}] OK {candidate.keyword_text} 月覆盖={item.get('month_cover_count')} error={item.get('error')}", flush=True)
                except AidsoLoginRequiredError as exc:
                    stats["failed"] += 1
                    stats["stopped"] = 1
                    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), **event_base, "status": "stopped", "error": str(exc)[:300]})
                    print(f"[WSO-list] 登录态不可用，停止批跑：{exc}", flush=True)
                    break
                except AidsoHeatError as exc:
                    stats["failed"] += 1
                    err = str(exc)
                    item = {
                        "keyword_id": candidate.keyword_id,
                        "keyword_text": candidate.keyword_text,
                        "fetched_at": utc_now_z(),
                        "month_cover_count": None,
                        "month_click_count": None,
                        "down_keyword_count": 0,
                        "down_keyword_month_covercount": 0,
                        "competition": None,
                        "type_enums": [],
                        "tags": [],
                        "down_words_top20": [],
                        "error": err[:200],
                        "smart_backfill": {"score": candidate.score, "source_scores": candidate.source_scores},
                    }
                    upsert_item(wso_payload, item)
                    refresh_heat_counts(wso_payload)
                    write_json(WSO_PATH, wso_payload)
                    append_jsonl(run_dir / "events.jsonl", {"at": now_iso(), **event_base, "status": "failed", "error": err[:300]})
                    print(f"[WSO-list] [{idx}/{len(queue)}] FAIL {candidate.keyword_text}: {err[:160]}", flush=True)
                time.sleep(max(0.0, sleep_sec + random.uniform(-0.4, 0.8)))
        finally:
            context.close()
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIDSO DSO → WSO 智能补数")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="AIDSO Playwright 持久化 profile 目录")
    parser.add_argument("--max-dso-extra", type=int, default=0, help="额外补抓 DSO 候选词数量")
    parser.add_argument("--max-wso", type=int, default=180, help="补抓 WSO 候选词数量")
    parser.add_argument("--min-score", type=float, default=35.0, help="低于该分数的候选不跑")
    parser.add_argument("--sleep-sec", type=float, default=2.5, help="每个词之间的基础等待秒数")
    parser.add_argument("--wait-timeout-ms", type=int, default=30_000, help="WSO 单词抓取等待毫秒")
    parser.add_argument("--dry-run", action="store_true", help="只生成队列，不实际抓取")
    parser.add_argument("--no-channel", action="store_true", help="WSO 抓取不指定 Playwright channel")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    channel = None if args.no_channel else DEFAULT_BROWSER_CHANNEL
    run_id = f"aidso_smart_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    candidates, skipped = build_candidates(args.profile_dir)
    candidates = [c for c in candidates if c.score >= args.min_score]

    write_json(run_dir / "candidate_queue.json", [candidate_to_dict(c) for c in candidates])
    write_json(run_dir / "skipped_candidates.json", [candidate_to_dict(c) for c in skipped[:3000]])
    write_json(
        run_dir / "run_meta.json",
        {
            "run_id": run_id,
            "started_at": now_iso(),
            "profile_dir": args.profile_dir,
            "channel": channel,
            "max_dso_extra": args.max_dso_extra,
            "max_wso": args.max_wso,
            "min_score": args.min_score,
            "candidate_count": len(candidates),
            "skipped_count": len(skipped),
            "dry_run": args.dry_run,
        },
    )

    print(f"[smart] run_dir={run_dir}", flush=True)
    print(f"[smart] 候选可跑={len(candidates)} 跳过={len(skipped)} min_score={args.min_score}", flush=True)
    for idx, candidate in enumerate(candidates[:20], start=1):
        print(
            f"[smart] top{idx:02d} score={candidate.score:.1f} dso={candidate.dso_month_cover_count} "
            f"freq={candidate.snapshot_term_freq} {candidate.keyword_text}",
            flush=True,
        )

    if args.dry_run:
        return 0

    wso_stats = run_wso(
        candidates,
        run_dir,
        profile_dir=args.profile_dir,
        max_count=args.max_wso,
        sleep_sec=args.sleep_sec,
        wait_timeout_ms=args.wait_timeout_ms,
        channel=channel,
    )

    refreshed_candidates, _ = build_candidates(args.profile_dir)
    refreshed_candidates = [c for c in refreshed_candidates if c.score >= args.min_score]

    if args.max_dso_extra > 0:
        dso_stats = run_dso_extra(
            refreshed_candidates,
            run_dir,
            profile_dir=args.profile_dir,
            max_count=args.max_dso_extra,
            sleep_sec=args.sleep_sec,
            channel=channel,
        )
    else:
        dso_stats = {"attempted": 0, "success": 0, "failed": 0}

    summary = {
        "run_id": run_id,
        "finished_at": now_iso(),
        "wso": wso_stats,
        "dso": dso_stats,
    }
    write_json(run_dir / "summary.json", summary)
    print(f"[smart] 完成: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return 0 if not wso_stats.get("stopped") else 2


if __name__ == "__main__":
    raise SystemExit(main())
