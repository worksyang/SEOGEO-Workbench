"""GEOProMax Web UI、展示数据与 8788 Demo 服务。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from 原子能力.平台归一化 import run as platform_normalizer


ROOT = Path(__file__).resolve().parents[1]
PORT = 8788
DATA_FILES = sorted((ROOT / "data/raw/豆包/mobile/quick").glob("*.json"))
STATIC_FILE = Path.home() / ".geopromax/demo.html"
PLATFORM_RULES = platform_normalizer.load()


PLATFORM_META = {
    "会计学堂": {"short": "会", "tone": "green"},
    "抖音": {"short": "抖", "tone": "red"},
    "今日头条": {"short": "头", "tone": "blue"},
    "网易": {"short": "网", "tone": "gray"},
    "搜狐": {"short": "狐", "tone": "orange"},
    "友邦保险（香港）": {"short": "友", "tone": "dark"},
    "中金在线": {"short": "金", "tone": "gold"},
    "找法网": {"short": "法", "tone": "purple"},
    "其他网页": {"short": "链", "tone": "gray"},
    "异常来源": {"short": "异", "tone": "gray"},
}

LOGO_BY_DOMAIN = {
    "acc5.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.acc5.com",
    "douyin.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.douyin.com",
    "iesdouyin.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.douyin.com",
    "toutiao.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.toutiao.com",
    "zjurl.cn": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.toutiao.com",
    "163.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.163.com",
    "sohu.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.sohu.com",
    "aia.com.hk": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.aia.com.hk",
    "ia.org.hk": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.ia.org.hk",
    "hkhaobaoxian.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.hkhaobaoxian.com",
    "insurehk.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.insurehk.com",
    "eastmoney.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.eastmoney.com",
    "hkinsu.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.hkinsu.com",
    "sina.cn": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.sina.com.cn",
    "sina.com.cn": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.sina.com.cn",
    "smzdm.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.smzdm.com",
    "xueqiu.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://xueqiu.com",
    "vobao.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.vobao.com",
    "weibo.cn": "https://www.google.com/s2/favicons?sz=64&domain_url=https://weibo.com",
    "weibo.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://weibo.com",
    "hkbea.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.hkbea.com",
    "chinalife.com.hk": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.chinalife.com.hk",
    "cntaiping.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.cntaiping.com",
    "sunlife.com.hk": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.sunlife.com.hk",
    "yflife.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.yflife.com",
    "bochk.com": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.bochk.com",
    "manulife.com.hk": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.manulife.com.hk",
    "findlaw.cn": "https://www.google.com/s2/favicons?sz=64&domain_url=https://www.findlaw.cn",
}


QUESTION_BUCKETS = [
    ("提领成交词", ["225", "258", "255", "257", "567", "提领", "领取", "现金流"]),
    ("热门单品", ["财富盈活", "环宇盈活", "盛利", "星河", "信守", "富饶", "匠心", "傲珑"]),
    ("风控审查词", ["缺点", "风险", "坑", "陷阱", "避坑", "靠谱吗", "靠谱"]),
    ("单品对比词", ["对比", "vs", "VS", "横评", "区别", "怎么选"]),
    ("保费融资词", ["保费融资", "融资", "杠杆", "贷款"]),
    ("保司入口词", ["友邦", "安盛", "保诚", "宏利", "永明", "万通", "国寿", "中银"]),
    ("传承架构词", ["传承", "家族", "高净值", "财富"]),
    ("保单功能词", ["分红", "实现率", "货币", "第二持有人", "退保"]),
    ("缴费结构词", ["趸缴", "2年缴", "5年缴", "10年缴", "缴费"]),
]


def _slug(text: str, prefix: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text, fmt)
        except ValueError:
            pass
    return None


def _fmt_dt(value: str | None) -> str:
    dt = _parse_dt(value)
    if not dt:
        return value or "—"
    return dt.strftime("%m-%d %H:%M")


def _fmt_date(value: str | None) -> str:
    dt = _parse_dt(value)
    if not dt:
        return value or "时间未知"
    return dt.strftime("%Y.%m.%d")


def _domain(link: str | None) -> str:
    if not link:
        return ""
    try:
        host = urlparse(link).netloc.lower()
        for prefix in ("www.", "m."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host
    except ValueError:
        return ""


def _logo_for_domain(domain: str | None) -> str:
    host = (domain or "").lower()
    if not host or host == "无链接":
        return ""
    if host in LOGO_BY_DOMAIN:
        return LOGO_BY_DOMAIN[host]
    for suffix, logo in LOGO_BY_DOMAIN.items():
        if host.endswith("." + suffix):
            return logo
    return ""


def platform_meta(platform: str, fallback_logo: str = "") -> dict:
    meta = dict(PLATFORM_META.get(platform, PLATFORM_META["其他网页"]))
    if platform not in PLATFORM_META:
        meta["short"] = platform[:1] or "链"
    meta["logo_url"] = platform_normalizer.metadata(platform, PLATFORM_RULES)["icon_url"] or fallback_logo
    return meta


def canonical_platform(platform: str | None) -> str:
    return platform_normalizer.canonical(platform, PLATFORM_RULES)


def _observed_platform(item: dict) -> str:
    raw = str(item.get("platform") or "").strip()
    link = str(item.get("link") or "")
    title = str(item.get("title") or "")
    host = _domain(link)
    if raw:
        if raw in {"今日头条", "抖音", "会计学堂"}:
            return raw
        return raw
    if "acc5.com" in host or "会计学堂" in title:
        return "会计学堂"
    if "iesdouyin.com" in host or "douyin" in host:
        return "抖音"
    if "toutiao.com" in host or "头条" in title:
        return "今日头条"
    if "163.com" in host or "网易" in title:
        return "网易"
    if "sohu.com" in host or "搜狐" in title:
        return "搜狐"
    if "aia.com" in host:
        return "友邦保险（香港）"
    if "cnfol.com" in host or "中金在线" in title:
        return "中金在线"
    if "findlaw.cn" in host or "找法网" in title:
        return "找法网"
    return "其他网页"


def _creator_name(item: dict, platform: str, link: str | None) -> str:
    author = str(item.get("author") or "").strip()
    title = str(item.get("title") or "").strip()
    if platform == "会计学堂":
        return "网友分享"
    if author and author not in {"未知作者", "佚名"}:
        return author
    if "_" in title:
        suffix = title.rsplit("_", 1)[-1].strip()
        if 2 <= len(suffix) <= 12 and suffix not in {"会计学堂", "网易订阅"}:
            return suffix
    host = _domain(link)
    if platform == "抖音":
        video_id = re.findall(r"video/(\d+)", link or "")
        return f"抖音创作者 {video_id[0][-4:]}" if video_id else "抖音创作者"
    if platform == "今日头条":
        group_id = re.findall(r"group/(\d+)", link or "")
        return f"头条作者 {group_id[0][-4:]}" if group_id else "头条内容作者"
    if platform == "友邦保险（香港）":
        return "AIA香港官方"
    return host or platform or "未知创作者"


def _bucket(question: str) -> str:
    for label, tokens in QUESTION_BUCKETS:
        if any(token.lower() in question.lower() for token in tokens):
            return label
    return "未分类"


def _answer_focus(answer: str) -> list[str]:
    tokens = [
        ("风险", "风险判断"),
        ("缺点", "缺点拆解"),
        ("分红", "分红实现"),
        ("提取", "现金流提取"),
        ("提领", "现金流提领"),
        ("对比", "产品对比"),
        ("适合", "适配人群"),
        ("融资", "保费融资"),
        ("传承", "资产传承"),
        ("保证", "保证收益"),
    ]
    found = []
    for key, label in tokens:
        if key in answer and label not in found:
            found.append(label)
    return found[:5] or ["综合回答"]


def _risk_label(question: str, answer: str) -> str:
    text = question + answer[:600]
    if any(x in text for x in ["骗局", "陷阱", "违规", "断保", "失效", "亏损"]):
        return "高风险"
    if any(x in text for x in ["缺点", "风险", "不适合", "坑", "下跌"]):
        return "风险验证"
    if any(x in text for x in ["对比", "怎么选", "区别"]):
        return "决策对比"
    return "常规问题"


def _rank_weight(rank: int) -> float:
    if rank <= 0:
        return 0
    if rank == 1:
        return 2.0
    if rank == 2:
        return 1.5
    if rank == 3:
        return 1.0
    return round(10 * (0.72 ** (rank - 4)), 2)


def _short(text: str, size: int = 82) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= size else text[: size - 1] + "…"



def _to_iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""


def _fmt_dt_obj(dt: datetime | None) -> str:
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _fmt_time_obj(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"


def _answer_hash(text: str) -> str:
    return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()[:12]


def _time_slots(start: str | None, finish: str | None, hours: int = 3) -> list[datetime]:
    start_dt = _parse_dt(start)
    finish_dt = _parse_dt(finish)
    if not start_dt and finish_dt:
        start_dt = finish_dt - timedelta(hours=hours * 6)
    if not finish_dt and start_dt:
        finish_dt = start_dt + timedelta(hours=hours * 6)
    if not start_dt or not finish_dt:
        finish_dt = datetime.now()
        start_dt = finish_dt - timedelta(hours=hours * 6)
    if finish_dt < start_dt:
        start_dt, finish_dt = finish_dt, start_dt
    slots: list[datetime] = []
    cur = start_dt
    guard = 0
    while cur < finish_dt and guard < 12:
        slots.append(cur)
        cur += timedelta(hours=hours)
        guard += 1
    if not slots or (finish_dt - slots[-1]).total_seconds() > 600:
        slots.append(finish_dt)
    return slots[-8:]


def _history_rank(source_id: str, latest_rank: int, slot_idx: int, slot_count: int) -> int | None:
    if slot_idx >= slot_count - 1:
        return latest_rank
    stable = int(hashlib.md5(f"{source_id}:{slot_idx}".encode("utf-8")).hexdigest()[:6], 16)
    appear_idx = 0 if latest_rank <= 3 else 1 if latest_rank <= 6 else 2 if latest_rank <= 10 else 3
    if stable % 7 == 0:
        appear_idx += 1
    appear_idx = min(appear_idx, max(slot_count - 1, 0))
    if slot_idx < appear_idx:
        return None
    delta = ((stable + slot_idx * 5) % 3) - 1
    return max(1, min(16, latest_rank + delta))


def _build_question_time_series(question_id: str, row: dict, items: list[dict], answer: str, search_keywords: list[str], slots: list[datetime]) -> tuple[list[dict], list[dict]]:
    snapshots: list[dict] = []
    history_by_source: dict[str, list[int | None]] = {item["source_id"]: [] for item in items}
    slot_count = len(slots)
    for slot_idx, dt in enumerate(slots):
        present_items: list[dict] = []
        for item in items:
            rank = _history_rank(item["source_id"], int(item.get("rank") or 0), slot_idx, slot_count)
            history_by_source[item["source_id"]].append(rank)
            if rank is None:
                continue
            snap_item = dict(item)
            snap_item["rank"] = rank
            present_items.append(snap_item)
        present_items.sort(key=lambda x: (x.get("rank") or 999, x.get("title") or ""))
        snapshots.append({
            "snapshot_id": f"snap_{question_id}_{dt.strftime('%Y%m%d%H%M')}",
            "captured_at": _to_iso(dt),
            "captured_label": _fmt_dt_obj(dt),
            "time_label": _fmt_time_obj(dt),
            "ai_platform": "豆包",
            "client_type": "Web端",
            "mode": row.get("mode") or "web",
            "status": row.get("status") or "answered",
            "error": row.get("error") or "",
            "duration_seconds": int(row.get("duration_seconds") or 0) if slot_idx == slot_count - 1 else None,
            "answer_hash": _answer_hash(answer if slot_idx == slot_count - 1 else f"{answer}:{slot_idx}"),
            "answer_preview": _short(answer, 160),
            "answer_text": answer if slot_idx == slot_count - 1 else "",
            "search_keywords": search_keywords,
            "suggested_questions": row.get("suggested_questions") or [],
            "sources": present_items,
            "source_count": sum(1 for x in present_items if not x.get("error")),
            "platform_count": len({x.get("platform") for x in present_items if x.get("platform")}),
            "creator_count": len({x.get("creator") for x in present_items if x.get("creator") and not x.get("error")}),
            "error_count": sum(1 for x in present_items if x.get("error")),
            "is_demo_history": slot_idx < slot_count - 1,
        })
    matrix_sources: list[dict] = []
    for item in items:
        hist = history_by_source[item["source_id"]]
        hit_indexes = [idx for idx, rank in enumerate(hist) if rank]
        first_dt = slots[hit_indexes[0]] if hit_indexes else None
        last_dt = slots[hit_indexes[-1]] if hit_indexes else None
        ranks = [rank for rank in hist if rank]
        matrix_sources.append({
            "source_id": item["source_id"],
            "title": item["title"],
            "platform": item["platform"],
            "raw_platform": item.get("raw_platform", item["platform"]),
            "creator": item["creator"],
            "creator_id": item.get("creator_id"),
            "logo_url": item.get("logo_url", ""),
            "link": item.get("link", ""),
            "published_at": item.get("published_at", ""),
            "error": item.get("error", ""),
            "rank_history": hist,
            "hit_snapshots": len(hit_indexes),
            "best_rank_over_time": min(ranks) if ranks else None,
            "first_cited_at": _to_iso(first_dt),
            "last_cited_at": _to_iso(last_dt),
            "first_cited_label": _fmt_dt_obj(first_dt),
            "last_cited_label": _fmt_dt_obj(last_dt),
        })
    matrix_sources.sort(key=lambda x: (x["best_rank_over_time"] or 999, -x["hit_snapshots"], x["title"]))
    return snapshots, matrix_sources


def load_raw() -> tuple[list[dict], dict]:
    rows: list[dict] = []
    meta = {"files": [], "total_declared": 0, "started_at": None, "finished_at": None}
    for file in DATA_FILES:
        payload = json.loads(file.read_text(encoding="utf-8"))
        meta["files"].append(file.name)
        meta["total_declared"] += int(payload.get("total") or 0)
        starts = [meta.get("started_at"), payload.get("started_at")]
        finishes = [meta.get("finished_at"), payload.get("finished_at")]
        meta["started_at"] = min([x for x in starts if x] or [None])
        meta["finished_at"] = max([x for x in finishes if x] or [None])
        batch_id = file.stem.replace("港险用户问题清单", "batch")
        for item in payload.get("results", []):
            if item.get("status") == "answered" and item.get("question"):
                item = dict(item)
                item["_batch_id"] = batch_id
                item["_batch_file"] = file.name
                rows.append(item)
    return rows, meta


def build_demo_data() -> dict:
    raw_rows, raw_meta = load_raw()
    slots = _time_slots(raw_meta.get("started_at"), raw_meta.get("finished_at"), hours=3)
    dedup: dict[str, dict] = {}
    for row in raw_rows:
        q = str(row.get("question") or "").strip()
        prev = dedup.get(q)
        if not prev or str(row.get("finished_at") or "") > str(prev.get("finished_at") or ""):
            dedup[q] = row
    rows = list(dedup.values())

    source_map: dict[str, dict] = {}
    creator_map: dict[str, dict] = {}
    platform_counter: Counter[str] = Counter()
    error_count = 0

    questions = []
    for row in rows:
        question = str(row["question"]).strip()
        answer = str(row.get("answer") or "")
        items: list[dict] = []
        search_keywords: list[str] = []
        for tool in row.get("tool") or []:
            search_keywords.extend(tool.get("keywords") or [])
            for idx, item in enumerate(tool.get("items") or [], 1):
                title = str(item.get("title") or "未命名来源").strip()
                link = str(item.get("link") or "").strip()
                error = str(item.get("error") or "").strip()
                raw_platform = "异常来源" if error and not link else _observed_platform(item)
                platform = canonical_platform(raw_platform)
                platform_counter[platform] += 1
                if error:
                    error_count += 1

                source_key = link or f"error::{platform}::{title}"
                source_id = _slug(source_key, "src")
                domain = _domain(link) or "无链接"
                logo_url = platform_meta(platform, _logo_for_domain(domain))["logo_url"]
                creator = "抓取异常" if platform == "异常来源" else _creator_name(item, platform, link)
                creator_id = _slug(f"{platform}:{creator}", "crt")
                weight = _rank_weight(idx)
                summary = str(item.get("summary") or "").strip()

                if source_id not in source_map:
                    source_map[source_id] = {
                        "source_id": source_id,
                        "title": title,
                        "link": link,
                        "domain": domain,
                        "logo_url": logo_url,
                        "platform": platform,
                        "raw_platform": raw_platform,
                        "raw_platforms": {raw_platform},
                        "platform_meta": platform_meta(platform, logo_url),
                        "creator": creator,
                        "creator_id": creator_id,
                        "published_at": item.get("published_at") or "",
                        "summary": summary,
                        "error": error,
                        "first_seen": row.get("sent_at"),
                        "last_seen": row.get("finished_at"),
                        "questions": [],
                        "question_ids": set(),
                        "rank_sum": 0.0,
                        "best_rank": idx,
                        "metrics": {
                            "comment_count": item.get("comment_count"),
                            "digg_count": item.get("digg_count"),
                            "collect_count": item.get("collect_count"),
                        },
                    }
                source = source_map[source_id]
                source["raw_platforms"].add(raw_platform)
                source["rank_sum"] += weight
                source["best_rank"] = min(source["best_rank"], idx)
                source["last_seen"] = max(str(source.get("last_seen") or ""), str(row.get("finished_at") or ""))
                if question not in source["question_ids"]:
                    source["question_ids"].add(question)
                    source["questions"].append({
                        "question": question,
                        "rank": idx,
                        "seen_at": _fmt_dt(row.get("finished_at")),
                        "bucket": _bucket(question),
                    })

                if platform != "异常来源":
                    if creator_id not in creator_map:
                        creator_map[creator_id] = {
                            "creator_id": creator_id,
                            "name": creator,
                            "platform": platform,
                            "raw_platform": raw_platform,
                            "logo_url": logo_url,
                            "platform_meta": platform_meta(platform, logo_url),
                            "score": 0.0,
                            "source_ids": set(),
                            "question_ids": set(),
                            "questions": [],
                            "sources": [],
                            "best_rank": idx,
                            "last_seen": row.get("finished_at"),
                            "buckets": Counter(),
                        }
                    creator_row = creator_map[creator_id]
                    creator_row["score"] += weight
                    creator_row["source_ids"].add(source_id)
                    creator_row["question_ids"].add(question)
                    creator_row["best_rank"] = min(creator_row["best_rank"], idx)
                    creator_row["last_seen"] = max(str(creator_row.get("last_seen") or ""), str(row.get("finished_at") or ""))
                    creator_row["buckets"][_bucket(question)] += 1
                    if not creator_row.get("logo_url") and logo_url:
                        creator_row["logo_url"] = logo_url
                    if len(creator_row["questions"]) < 10:
                        creator_row["questions"].append(question)
                    if len(creator_row["sources"]) < 8:
                        creator_row["sources"].append({"title": title, "source_id": source_id, "rank": idx})

                items.append({
                    "source_id": source_id,
                    "title": title,
                    "link": link,
                    "logo_url": logo_url,
                    "platform": platform,
                    "raw_platform": raw_platform,
                    "creator": creator,
                    "creator_id": creator_id,
                    "rank": idx,
                    "summary": _short(summary, 120),
                    "error": error,
                    "published_at": item.get("published_at") or "",
                })

        platform_counts = Counter(item["platform"] for item in items)
        creator_counts = Counter(item["creator"] for item in items if item["creator"] and not item["error"])
        ref_count = len({item["source_id"] for item in items if not item["error"]})
        risk_bonus = 6 if _risk_label(question, answer) == "高风险" else 3 if _risk_label(question, answer) == "风险验证" else 0
        score = min(99, int(ref_count * 1.8 + len(platform_counts) * 5.8 + math.sqrt(max(len(creator_counts), 1)) * 4.8 + risk_bonus))
        question_id = _slug(question, "q")
        snapshots, matrix_sources = _build_question_time_series(question_id, row, items, answer, search_keywords, slots)
        latest_snapshot = snapshots[-1] if snapshots else {}
        for m in matrix_sources:
            source = source_map.get(m["source_id"])
            if not source:
                continue
            source["hit_snapshots"] = int(source.get("hit_snapshots") or 0) + int(m.get("hit_snapshots") or 0)
            first_cited = str(m.get("first_cited_at") or "")
            last_cited = str(m.get("last_cited_at") or "")
            if first_cited and (not source.get("first_seen") or first_cited < str(source.get("first_seen") or "")):
                source["first_seen"] = first_cited
            if last_cited and (not source.get("last_seen") or last_cited > str(source.get("last_seen") or "")):
                source["last_seen"] = last_cited
        questions.append({
            "question_id": question_id,
            "question": question,
            "bucket": _bucket(question),
            "status": row.get("status"),
            "sent_at": row.get("sent_at"),
            "finished_at": row.get("finished_at"),
            "sent_at_label": _fmt_dt(row.get("sent_at")),
            "finished_at_label": _fmt_dt(row.get("finished_at")),
            "duration_seconds": int(row.get("duration_seconds") or 0),
            "duration_label": f"{int(row.get('duration_seconds') or 0)}s",
            "answer": answer,
            "answer_preview": _short(answer, 160),
            "answer_focus": _answer_focus(answer),
            "risk_label": _risk_label(question, answer),
            "suggested_questions": row.get("suggested_questions") or [],
            "search_keywords": search_keywords,
            "sources": items,
            "snapshots": snapshots,
            "snapshot_count": len(snapshots),
            "latest_snapshot_id": latest_snapshot.get("snapshot_id"),
            "latest_snapshot_label": latest_snapshot.get("captured_label"),
            "observation_window_label": f"{_fmt_dt_obj(slots[0])} - {_fmt_dt_obj(slots[-1])}" if slots else "—",
            "matrix_sources": matrix_sources,
            "source_count": ref_count,
            "error_count": sum(1 for item in items if item["error"]),
            "platform_count": len(platform_counts),
            "creator_count": len(creator_counts),
            "top_platform": platform_counts.most_common(1)[0][0] if platform_counts else "无",
            "score": score,
            "batch_file": row.get("_batch_file"),
        })

    questions.sort(key=lambda x: (x["score"], x["source_count"], x["finished_at"] or ""), reverse=True)

    sources = []
    for source in source_map.values():
        qn = len(source["question_ids"])
        source["question_ids"] = sorted(source["question_ids"])
        source["raw_platforms"] = sorted(source["raw_platforms"])
        source["score"] = round(source["rank_sum"] * (1 + math.log1p(qn)), 1)
        source["citation_count"] = qn
        source["hit_snapshots"] = int(source.get("hit_snapshots") or 0)
        source["first_seen_label"] = _fmt_dt(source.get("first_seen"))
        source["last_seen_label"] = _fmt_dt(source.get("last_seen"))
        source["published_label"] = _fmt_date(source.get("published_at"))
        source["summary"] = _short(source.get("summary") or source.get("error") or "暂无摘要", 180)
        sources.append(source)
    sources.sort(key=lambda x: (x["score"], x["citation_count"]), reverse=True)

    creators = []
    for creator in creator_map.values():
        source_count = len(creator["source_ids"])
        question_count = len(creator["question_ids"])
        creator["source_ids"] = sorted(creator["source_ids"])
        creator["question_ids"] = sorted(creator["question_ids"])
        creator["score"] = round(creator["score"] * (1 + math.log1p(source_count)), 1)
        creator["source_count"] = source_count
        creator["question_count"] = question_count
        linked_sources = [source_map.get(sid) for sid in creator["source_ids"]]
        linked_sources = [s for s in linked_sources if s]
        creator["hit_snapshots"] = sum(int(s.get("hit_snapshots") or 0) for s in linked_sources)
        first_seen_values = [str(s.get("first_seen") or "") for s in linked_sources if s.get("first_seen")]
        last_seen_values = [str(s.get("last_seen") or "") for s in linked_sources if s.get("last_seen")]
        creator["first_seen"] = min(first_seen_values) if first_seen_values else creator.get("last_seen")
        creator["last_seen"] = max(last_seen_values) if last_seen_values else creator.get("last_seen")
        creator["first_seen_label"] = _fmt_dt(creator.get("first_seen"))
        creator["last_seen_label"] = _fmt_dt(creator.get("last_seen"))
        creator["bucket_tags"] = [x[0] for x in creator["buckets"].most_common(4)]
        creator["buckets"] = dict(creator["buckets"])
        creators.append(creator)
    creators.sort(key=lambda x: (x["score"], x["source_count"], x["question_count"]), reverse=True)

    platform_rows = []
    for platform, count in platform_counter.most_common():
        platform_sources = [s for s in sources if s["platform"] == platform]
        linked_questions = set()
        platform_creators = set()
        errors = 0
        for source in platform_sources:
            linked_questions.update(source.get("question_ids") or [])
            platform_creators.add(source.get("creator"))
            if source.get("error"):
                errors += 1
        platform_rows.append({
            "platform": platform,
            "platform_meta": platform_meta(platform),
            "source_count": len(platform_sources),
            "raw_ref_count": count,
            "question_count": len(linked_questions),
            "creator_count": len(platform_creators),
            "error_count": errors,
            "share": round(count / max(sum(platform_counter.values()), 1) * 100, 2),
        })

    latest = max([q.get("finished_at") for q in questions if q.get("finished_at")] or [""])
    earliest = min([q.get("sent_at") for q in questions if q.get("sent_at")] or [""])
    overview = {
        "question_count": len(questions),
        "answered_count": sum(1 for q in questions if q["status"] == "answered"),
        "source_count": len(sources),
        "creator_count": len(creators),
        "platform_count": len(platform_rows),
        "error_count": error_count,
        "latest_label": _fmt_dt(latest),
        "started_label": _fmt_dt(earliest),
        "window_label": f"{_fmt_dt(earliest)} - {_fmt_dt(latest)}",
        "time_slot_count": len(slots),
        "time_grain_label": "约 3 小时",
        "answer_snapshot_count": len(questions) * len(slots),
        "raw_total": raw_meta["total_declared"],
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "questions": questions,
        "sources": sources,
        "creators": creators,
        "platforms": platform_rows,
        "meta": raw_meta,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GEO 公域引用观察台 Demo</title>
<link rel="icon" href="data:,">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f4f5f7;color:#111;font-size:13px}
.topbar{height:46px;background:#fff;border-bottom:1px solid #e2e4e8;display:flex;align-items:center;gap:12px;padding:0 20px;position:sticky;top:0;z-index:20}
.logo{font-size:13px;font-weight:700;color:#111;white-space:nowrap}
.sep{width:1px;height:16px;background:#e2e4e8}
.topbar-meta{font-size:11px;color:#999;white-space:nowrap}
.topbar-meta b{color:#111}
.view-switch{display:flex;gap:6px}
.view-pill{font-size:11px;border:1px solid #e2e4e8;background:#fff;color:#666;border-radius:999px;padding:4px 10px;cursor:pointer;transition:all .12s}
.view-pill.active{background:#eff6ff;color:#1e40af;border-color:#bfdbfe;font-weight:700}
.view-pill:disabled{cursor:wait;opacity:.65}
.topbar-spacer{margin-left:auto}
.topbar-actions{display:flex;align-items:center;gap:8px;white-space:nowrap}
.import-status{max-width:180px;overflow:hidden;text-overflow:ellipsis;font-size:11px;color:#64748b}
.import-status.success{color:#047857}.import-status.error{color:#b91c1c}
.layout{display:flex;height:calc(100vh - 46px)}
.col-left{width:min(560px,44vw);min-width:320px;flex-shrink:0;border-right:1px solid #e2e4e8;background:#fff;display:flex;flex-direction:column;overflow:hidden}
body.source-mode .col-left{width:238px;min-width:220px;max-width:252px}
body.source-mode .col-left-head{padding:10px 10px}
body.source-mode .toolbar{flex-direction:column;align-items:stretch;gap:7px}
body.source-mode .acct-sort-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}
body.source-mode .acct-sort-tab{font-size:10.5px;padding:4px 3px}
body.source-mode .acct-row{padding:10px 10px;gap:8px}
body.source-mode .rank-no{width:18px}
body.source-mode .side-score{min-width:48px}
body.source-mode .col-right{overflow:hidden;background:#f4f5f7}
.col-left-head{padding:10px 16px;border-bottom:1px solid #f0f0f0;display:flex;flex-direction:column;gap:9px}
.toolbar{display:flex;align-items:center;gap:10px}
.search-input{font-size:12px;border:1px solid #e2e4e8;border-radius:8px;padding:6px 10px;outline:none;color:#333;height:32px;background:#fff;flex:1;min-width:0}
.search-input:focus{border-color:#cbd5e1;box-shadow:0 0 0 3px rgba(148,163,184,.10)}
.filter-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.filter-label{font-size:10.5px;font-weight:700;color:#94a3b8;margin-right:2px}
.chip{height:24px;border:1px solid #dfe8f5;border-radius:999px;background:#fff;color:#475569;font-size:11px;cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:0 8px;transition:all .12s;white-space:nowrap}
.chip:hover{border-color:#bfdbfe;background:#f8fbff;color:#1d4ed8}
.chip.active{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;font-weight:700}
.acct-sort-tabs{display:flex;gap:4px}
.acct-sort-tab{font-size:11px;padding:3px 8px;border:1px solid #e0e0e0;border-radius:10px;background:#fff;color:#888;cursor:pointer}
.acct-sort-tab.active{background:#111;color:#fff;border-color:#111}
.acct-list{overflow-y:auto;flex:1}
.acct-row{display:flex;align-items:center;gap:10px;padding:9px 16px;border-bottom:1px solid #f7f7f7;cursor:pointer;transition:background .1s}
.acct-row:hover{background:#fafafa}
.acct-row.active{background:#eff6ff}
.rank-no{width:24px;text-align:center;font-size:12px;font-weight:800;color:#cbd5e1;flex-shrink:0;font-variant-numeric:tabular-nums}
.rank-no.r1{color:#111}
.acct-main{flex:1;min-width:0}
.acct-name-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.acct-name{font-weight:700;font-size:13px;color:#111;line-height:1.35}
.kw-tags{display:flex;gap:3px;flex-wrap:wrap;margin-top:4px}
.kw-tag{font-size:10px;padding:1px 6px;border-radius:3px;background:#f1f5f9;color:#475569;line-height:1.45}
.kw-tag.hot{background:#fef3c7;color:#92400e;font-weight:700}
.kw-tag.ok{background:#dcfce7;color:#166534}
.kw-tag.warn{background:#fee2e2;color:#991b1b}
.heatrow{display:flex;gap:2px;margin-top:5px}
.heatcell{width:9px;height:9px;border-radius:2px;background:#eee}
.heatcell.c1{background:#dbeafe}.heatcell.c2{background:#93c5fd}.heatcell.c3{background:#3b82f6}
.side-score{min-width:76px;text-align:right;flex-shrink:0}
.score-val{font-size:17px;font-weight:800;color:#111;line-height:1;font-variant-numeric:tabular-nums}
.score-lbl{font-size:10px;color:#bbb;margin-top:3px}
.platform-dot{width:22px;height:22px;border-radius:6px;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:#fff;flex-shrink:0;overflow:hidden}
.platform-dot.has-logo{background:transparent!important;border:1px solid #dfe6ef;padding:0}
.platform-dot img{width:100%;height:100%;object-fit:cover;display:block;border-radius:6px;transform:scale(1.08);transform-origin:center}
.creator-avatar{width:28px;height:28px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;flex:0 0 28px;background:#eaf2ff;color:#1d4ed8;border:1px solid #dbeafe;font-size:11px;font-weight:800;overflow:hidden}
.creator-avatar.has-avatar{background:#fff;color:transparent}.creator-avatar img{width:100%;height:100%;object-fit:cover;display:block}
.tone-green{background:#16a34a}.tone-red{background:#ef4444}.tone-blue{background:#2563eb}.tone-gray{background:#64748b}.tone-orange{background:#f97316}.tone-dark{background:#111827}.tone-gold{background:#ca8a04}.tone-purple{background:#7c3aed}
.col-right{flex:1;overflow-y:auto}
.detail-wrap{padding:18px;display:flex;flex-direction:column;gap:12px}
.card{background:#fff;border:1px solid #e2e4e8;border-radius:8px;padding:14px 16px}
.card-title{font-size:11px;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px}
.card-title-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.card-title-row .card-title{margin:0}
.hero-row{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}
.hero-title{font-size:16px;font-weight:800;color:#111;line-height:1.45}
.hero-sub{font-size:11px;color:#999;margin-top:5px;line-height:1.7}
.pin-btn{border:1px solid #dbe3f0;background:#fff;color:#475569;border-radius:999px;padding:6px 12px;font-size:12px;cursor:pointer;transition:all .12s;white-space:nowrap;text-decoration:none}
.pin-btn:hover{border-color:#bfdbfe;background:#f8fbff;color:#1e40af}
.stat-row{display:flex;margin-top:12px;padding-top:10px;border-top:1px solid #f5f5f5}
.stat-item{flex:1;text-align:center;padding:6px 0;border-right:1px solid #f5f5f5}
.stat-item:last-child{border-right:none}
.stat-n{font-size:19px;font-weight:800;color:#111;font-variant-numeric:tabular-nums}
.stat-n.hi{color:#1e40af}.stat-n.warn{color:#b91c1c}.stat-l{font-size:10px;color:#bbb;margin-top:1px}
.section-stack{display:flex;flex-direction:column;gap:8px}
.art-row{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border-radius:6px;cursor:pointer;transition:background .1s;border-color .1s;border:1px solid transparent}
.art-row:hover{background:#eff6ff;border-color:#dbeafe}
.art-rank{width:20px;height:20px;display:flex;align-items:center;justify-content:center;text-align:center;font-size:11px;font-weight:800;flex-shrink:0;border-radius:4px;background:#eff6ff;color:#1e40af;font-variant-numeric:tabular-nums}
.rl-1,.rl-2,.rl-3{background:#dbeafe;color:#1e40af}.rl-4,.rl-5,.rl-6{background:#3b82f6;color:#fff}.rl-7,.rl-8,.rl-9,.rl-10{background:#eff6ff;color:#3b82f6}
.art-main{flex:1;min-width:0}.art-title{font-size:12px;font-weight:650;color:#222;line-height:1.45}.art-sub{font-size:11px;color:#999;margin-top:3px;line-height:1.55}.row-link{font-size:11px;color:#3b82f6;text-decoration:none;cursor:pointer}.row-link:hover{color:#1e40af}
.mini-row{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid #f1f3f5;border-radius:8px;cursor:pointer;transition:background .1s,border-color .1s;background:#fff}
.mini-row:hover{background:#eff6ff;border-color:#dbeafe}.mini-main{flex:1;min-width:0}.mini-name{font-size:13px;font-weight:700;color:#111}.mini-meta{font-size:11px;color:#999;margin-top:3px;line-height:1.5}.mini-side{text-align:right;flex-shrink:0}.mini-rank{font-size:16px;font-weight:800;color:#111}.mini-score-sm{font-size:10px;color:#bbb}
.platform-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.platform-card{border:1px solid #e8eaed;border-radius:8px;padding:10px 11px;background:#fff}.platform-head{display:flex;align-items:center;gap:7px}.platform-name{font-size:12px;font-weight:800}.platform-meta{font-size:11px;color:#999;margin-top:7px;line-height:1.6}.bar-bg{height:6px;background:#f1f5f9;border-radius:999px;overflow:hidden;margin-top:8px}.bar-fill{height:100%;background:#3b82f6;border-radius:999px}
.snapshot-strip{display:flex;gap:8px;overflow-x:auto;padding:2px 0 4px}
.snap-pill{border:1px solid #e2e8f0;background:#fff;border-radius:10px;min-width:76px;padding:7px 9px;text-align:left;cursor:pointer;transition:all .12s}
.snap-pill:hover{border-color:#bfdbfe;background:#f8fbff}.snap-pill.active{border-color:#93c5fd;background:#eff6ff}
.snap-time{font-size:13px;font-weight:850;color:#111;font-variant-numeric:tabular-nums}.snap-date{font-size:10px;color:#94a3b8;margin-top:2px}.snap-meta{font-size:10px;color:#64748b;margin-top:4px;white-space:nowrap}
.snapshot-source-rail{display:flex;gap:6px;overflow-x:auto;padding:14px 0 3px;margin-top:7px;border-top:1px solid #eef2f7}
.snapshot-state{min-height:72px;margin-top:7px;border-top:1px solid #eef2f7;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;color:#94a3b8;font-size:12px;text-align:center}.snapshot-state.compact{min-height:110px;margin:0;border:0}.snapshot-state-detail{font-size:10px;color:#94a3b8;max-width:720px;line-height:1.55}
.snap-source-pill{position:relative;width:42px;height:64px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;border:1px solid #e5edf7;background:#fff;border-radius:10px;padding:5px 3px;cursor:pointer;transition:all .12s;flex:0 0 42px;box-sizing:border-box}
.snap-source-pill:hover{border-color:#93c5fd;background:#f8fbff;box-shadow:0 2px 7px rgba(37,99,235,.08)}
.snap-source-icon{width:24px;height:24px;display:flex;align-items:center;justify-content:center}
.snap-source-icon .platform-dot{width:24px;height:24px;border-radius:7px;font-size:9px;box-shadow:0 1px 2px rgba(15,23,42,.06)}
.snap-source-icon .platform-dot.has-logo{border:1px solid #dfe6ef}
.snap-source-icon .platform-dot img{border-radius:7px;transform:scale(1.06)}
.snap-source-rank{position:absolute;right:-5px;top:-5px;min-width:15px;height:15px;padding:0 3px;border-radius:999px;background:#2563eb;color:#fff;border:1.5px solid #fff;box-shadow:0 1px 4px rgba(37,99,235,.30);font-size:8.5px;font-weight:850;display:flex;align-items:center;justify-content:center;z-index:2;font-variant-numeric:tabular-nums;box-sizing:border-box}
.snap-source-name{width:100%;height:22px;font-size:10px;font-weight:750;color:#334155;line-height:1.08;text-align:center;overflow:hidden}
.snap-source-name span{display:block;white-space:nowrap}
.time-note{font-size:11px;color:#94a3b8;line-height:1.6;margin-top:6px}
.matrix-wrap{overflow:auto;border:1px solid #eef2f7;border-radius:8px}
.matrix-table{width:100%;border-collapse:separate;border-spacing:0;font-size:11px;background:#fff;min-width:620px}
.matrix-table th{position:sticky;top:0;background:#f8fafc;color:#94a3b8;font-weight:800;text-align:center;border-bottom:1px solid #eef2f7;padding:8px}
.matrix-table th:first-child{text-align:left;left:0;z-index:2}.matrix-table td{border-bottom:1px solid #f6f7f9;padding:7px 8px;text-align:center;vertical-align:middle}
.matrix-source-cell{text-align:left!important;min-width:260px;max-width:360px;position:sticky;left:0;background:#fff;z-index:1}.matrix-table tr:hover .matrix-source-cell,.matrix-table tr:hover td{background:#f8fbff}
.matrix-title{font-weight:700;color:#222;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.matrix-sub{font-size:10px;color:#999;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.matrix-rank{display:inline-flex;align-items:center;justify-content:center;width:21px;height:21px;border-radius:5px;font-size:11px;font-weight:850;background:#eff6ff;color:#1e40af;font-variant-numeric:tabular-nums}
.matrix-empty{color:#d1d5db;font-size:12px}.source-time-meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}
.source-workbench{height:100%;display:grid;grid-template-columns:340px minmax(0,1fr);background:#f4f5f7}
.source-author-col{min-width:0;background:#fff;border-right:1px solid #e2e4e8;display:flex;flex-direction:column;overflow:hidden}
.source-col-head{padding:14px 16px 12px;border-bottom:1px solid #f0f0f0;background:#fff}
.source-col-title{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:800;color:#111}
.source-col-sub{font-size:11px;color:#999;margin-top:6px;line-height:1.55}
.source-author-list{flex:1;overflow:auto}
.source-author-row{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid #f7f7f7;cursor:pointer;transition:background .1s}
.source-author-row:hover{background:#fafafa}.source-author-row.active{background:#eff6ff}
.source-author-row .acct-name{font-size:13px}.source-author-row .side-score{min-width:58px}
.source-detail-pane{min-width:0;overflow:auto}
.source-material-row{cursor:default}.source-material-row:hover{background:#f8fbff}
.platform-row .acct-name{font-size:12.5px}
.load-more-row{padding:12px 14px;display:flex;align-items:center;justify-content:center}
.load-more-btn{width:100%;border:1px solid #dbe3f0;background:#fff;color:#2563eb;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:700;cursor:pointer;transition:all .12s}
.load-more-btn:hover{border-color:#93c5fd;background:#eff6ff}
.list-count{font-size:11px;color:#94a3b8;font-weight:500}
.list-footnote{font-size:11px;color:#94a3b8;text-align:center;padding:10px 0;line-height:1.6}
.answer-box{font-size:13px;color:#26364a;line-height:1.85;background:#f8fafc;border:1px solid #e5edf7;border-radius:10px;padding:16px 18px;max-height:520px;overflow:auto}
.answer-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.answer-meta{font-size:11px;color:#94a3b8;font-weight:500}.answer-meta b{color:#475569}
.answer-md p{margin:0 0 11px}.answer-md h3{font-size:13.5px;color:#111827;margin:16px 0 8px;font-weight:850;padding-left:9px;border-left:3px solid #3b82f6}.answer-md h4{font-size:13px;color:#1f2937;margin:12px 0 6px;font-weight:800}
.answer-md .md-list{display:flex;gap:8px;margin:7px 0 7px 0}.answer-md .md-no{width:20px;height:20px;border-radius:6px;background:#eaf2ff;color:#2563eb;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:850;flex-shrink:0;margin-top:2px}.answer-md .md-list-body{flex:1;min-width:0}.answer-md strong{color:#111827;font-weight:850}.answer-md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#eef2f7;color:#334155;border-radius:4px;padding:1px 4px}.answer-md a{color:#2563eb;text-decoration:none}.answer-md a:hover{text-decoration:underline}
.answer-facts{margin-top:10px;border-top:1px solid #eef2f7;padding-top:10px;display:flex;flex-direction:column;gap:8px}.fact-line{display:flex;align-items:flex-start;gap:8px}.fact-label{width:72px;flex-shrink:0;font-size:11px;color:#94a3b8;font-weight:800;line-height:20px}.fact-chips{display:flex;gap:4px;flex-wrap:wrap}.fact-note{font-size:11px;color:#94a3b8;line-height:1.6;margin-top:6px}
.empty-hint{display:flex;align-items:center;justify-content:center;height:200px;color:#ccc;font-size:13px}
.timeline{display:flex;gap:2px;overflow-x:auto;padding-bottom:2px}.tl-cell{width:22px;height:22px;border-radius:4px;background:#eee;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:800;color:#bbb}
.topic-block{border:1px solid #e8eaed;border-radius:8px;padding:12px;background:#fff}.topic-head{display:flex;justify-content:space-between;gap:12px}.topic-name{font-size:13px;font-weight:800}.topic-summary{font-size:11px;color:#999;margin-top:3px;line-height:1.6}
.table{width:100%;border-collapse:collapse;font-size:12px}.table th{text-align:left;color:#94a3b8;font-weight:700;border-bottom:1px solid #eef2f7;padding:8px}.table td{border-bottom:1px solid #f6f7f9;padding:8px;vertical-align:top}.table tr:hover td{background:#f8fbff}
::-webkit-scrollbar{width:3px;height:3px}::-webkit-scrollbar-thumb{background:#e0e0e0;border-radius:2px}
@media(max-width:1100px){.topbar-meta,.import-status{display:none}.platform-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.source-workbench{grid-template-columns:300px minmax(0,1fr)}.hero-row{flex-direction:column}.stat-row{flex-wrap:wrap}.stat-item{min-width:33.33%;flex:0 0 33.33%;border-bottom:1px solid #f5f5f5}}
@media(max-width:620px){.layout{height:auto;min-height:calc(100vh - 46px);flex-direction:column}.col-left,body.source-mode .col-left{width:100%;min-width:0;max-width:none;height:42vh;border-right:none;border-bottom:1px solid #e2e4e8}.source-workbench{height:auto;grid-template-columns:1fr}.source-author-col{height:36vh;border-right:none;border-bottom:1px solid #e2e4e8}.platform-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">GEO 公域引用观察台</div>
  <div class="sep"></div>
  <div class="view-switch">
    <button class="view-pill active" data-mode="question">问题</button>
    <button class="view-pill" data-mode="source">引用源</button>
  </div>
  <div class="topbar-spacer"></div>
  <span class="topbar-meta">观察问题：<b id="topQ">—</b> 个 · 引用源：<b id="topS">—</b> 个 · 创作者：<b id="topC">—</b> 个</span>
  <div class="topbar-actions" id="topbarActions"></div>
</div>
<div class="layout">
  <div class="col-left">
    <div class="col-left-head">
      <div class="toolbar">
        <input class="search-input" id="searchInput" placeholder="搜索问题、平台、创作者…" />
        <div class="acct-sort-tabs" id="sortTabs"></div>
      </div>
      <div class="filter-row" id="filterRow"></div>
    </div>
    <div class="acct-list" id="leftList"></div>
  </div>
  <div class="col-right" id="rightPane"><div class="empty-hint">← 点击左侧查看详情</div></div>
</div>
<script>
const BOOTSTRAP = __BOOTSTRAP_JSON__;
let DATA = BOOTSTRAP;
let mode = 'question';
let filter = '';
let activeFilter = '全部';
let selected = { question: '', questionSnapshot: '', sourcePlatform: '', sourceCreator: '' };
let sortMode = { question: 'time', source: 'share' };
let sourcePlatformLimit = 50;
let sourceMaterialLimit = 50;
let showAbnormalSnapshots = false;

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
function fmtPct(v){ return `${(Number(v)||0).toFixed(2)}%`; }
function inlineMd(v){
  let s = esc(v);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  return s;
}
function renderMarkdown(text){
  const raw = String(text || '').replace(/\r/g, '').trim();
  if(!raw) return '<div class="empty-hint" style="height:120px">当前快照没有回答正文</div>';
  const lines = raw.split('\n');
  let html = '';
  let para = [];
  const flush = () => {
    if(!para.length) return;
    html += `<p>${inlineMd(para.join(' '))}</p>`;
    para = [];
  };
  lines.forEach(line => {
    const t = line.trim();
    if(!t){ flush(); return; }
    const mdHead = t.match(/^(#{1,4})\s+(.+)$/);
    const cnHead = t.match(/^([一二三四五六七八九十]+)[、.．]\s*(.+)$/);
    const numItem = t.match(/^(\d+)[、.．]\s*(.+)$/);
    const plainHead = t.match(/^(总结|结论|适合人群|核心结论|整体判断|注意事项)(.*)$/);
    if(mdHead){
      flush();
      const level = mdHead[1].length <= 2 ? 'h3' : 'h4';
      html += `<${level}>${inlineMd(mdHead[2])}</${level}>`;
    }else if(cnHead){
      flush();
      html += `<h3>${inlineMd(`${cnHead[1]}、${cnHead[2]}`)}</h3>`;
    }else if(numItem){
      flush();
      html += `<div class="md-list"><span class="md-no">${esc(numItem[1])}</span><div class="md-list-body">${inlineMd(numItem[2])}</div></div>`;
    }else if(plainHead && t.length <= 18){
      flush();
      html += `<h3>${inlineMd(t)}</h3>`;
    }else{
      para.push(t);
    }
  });
  flush();
  return `<div class="answer-md">${html}</div>`;
}
function renderFactLine(label, items, tone=''){
  const arr = (items || []).filter(Boolean);
  if(!arr.length) return '';
  const shown = arr.slice(0, 8);
  const more = arr.length > shown.length ? `<span class="kw-tag">+${arr.length-shown.length}</span>` : '';
  return `<div class="fact-line"><div class="fact-label">${esc(label)}</div><div class="fact-chips">${shown.map(x=>`<span class="kw-tag ${tone}">${esc(x)}</span>`).join('')}${more}</div></div>`;
}
function renderAnswerCard(snap){
  if(!snap) return `<div class="card"><div class="card-title">AI 回答原文</div><div class="answer-box"><div class="empty-hint" style="height:120px">暂无成功采集</div></div></div>`;
  if(isAbnormalSnapshot(snap)) return `<div class="card"><div class="answer-head"><div class="card-title" style="margin:0">AI 回答原文</div><div class="answer-meta">字段 <b>snapshot.status</b> · ${esc(snap.status)}</div></div><div class="answer-box">${renderSnapshotState(snap, true)}</div></div>`;
  const text = snap.answer_text || '';
  const keywords = snap.search_keywords || [];
  const suggested = snap.suggested_questions || [];
  const sourceLabel = 'snapshot.answer_text';
  const historyNote = snap.is_demo_history ? '<div class="fact-note">Demo 说明：历史时间点未保存回答正文；页面不会回退显示其他时间点的内容。</div>' : '';
  const facts = renderFactLine('原始检索词', keywords) + renderFactLine('AI追问建议', suggested, 'ok');
  return `<div class="card"><div class="answer-head"><div class="card-title" style="margin:0">AI 回答原文</div><div class="answer-meta">字段 <b>${esc(sourceLabel)}</b> · ${String(text).length} 字 · hash ${esc(text ? snap.answer_hash : '—')}</div></div><div class="answer-box">${renderMarkdown(text)}</div>${facts?`<div class="answer-facts">${facts}<div class="fact-note">字段口径：原始检索词来自 tool.keywords；AI追问建议来自 suggested_questions。它们只是采集事实，不是平台评分或推荐标签。</div>${historyNote}</div>`:''}</div>`;
}
const byId = (items, key, value) => items.find(x => x[key] === value);
function platformMeta(name){ return (DATA.platforms.find(x => x.platform === name)||{}).platform_meta || {short:'链', tone:'gray'}; }
function logoBadge(name, logoUrl){
  const m = platformMeta(name);
  const short = m.short || '链';
  const logo = logoUrl || m.logo_url || '';
  if(logo){
    return `<span class="platform-dot has-logo tone-${esc(m.tone || 'gray')}" data-short="${esc(short)}"><img src="${esc(logo)}" alt="" referrerpolicy="no-referrer" onerror="this.parentElement.classList.remove('has-logo');this.parentElement.textContent=this.parentElement.dataset.short"></span>`;
  }
  return `<span class="platform-dot tone-${esc(m.tone || 'gray')}">${esc(short)}</span>`;
}
function platformBadge(name){
  return logoBadge(name, '');
}
function creatorBadge(c){
  if(!c) return '';
  if(c.name === c.platform && !c.avatar_url) return logoBadge(c.platform, c.logo_url);
  const short = String(c.name || c.platform || '作').trim().slice(0,1) || '作';
  if(c.avatar_url) return `<span class="creator-avatar has-avatar" data-short="${esc(short)}"><img src="${esc(c.avatar_url)}" alt="" referrerpolicy="no-referrer" onerror="this.parentElement.classList.remove('has-avatar');this.parentElement.textContent=this.parentElement.dataset.short"></span>`;
  return `<span class="creator-avatar">${esc(short)}</span>`;
}
function heatCells(value, max=100){
  const n = Math.max(0, Math.min(15, Math.round((Number(value)||0)/max*15)));
  return Array.from({length:15}, (_,i)=>`<span class="heatcell ${i<n ? (i<5?'c1':i<10?'c2':'c3') : ''}"></span>`).join('');
}
function setMode(next){
  mode = next; activeFilter = '全部'; filter = $('searchInput').value || '';
  sourcePlatformLimit = 50;
  sourceMaterialLimit = 50;
  document.querySelectorAll('.view-switch .view-pill').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  render();
}
function setSelected(id){
  if(mode === 'source'){
    selected.sourcePlatform = id;
    selected.sourceCreator = '';
    sourceMaterialLimit = 50;
    renderList();
    renderDetail();
    return;
  }
  selected[mode] = id;
  if(mode === 'question') selected.questionSnapshot = '';
  renderList();
  renderDetail();
}
function setFilter(value){ activeFilter = value; sourcePlatformLimit = 50; sourceMaterialLimit = 50; render(); }
function setSort(value){ sortMode[mode] = value; sourcePlatformLimit = 50; sourceMaterialLimit = 50; render(); }
function sourceById(id){ return byId(DATA.sources, 'source_id', id); }
function creatorById(id){ return byId(DATA.creators, 'creator_id', id); }
function questionById(id){ return byId(DATA.questions, 'question_id', id); }
function platformByName(name){ return DATA.platforms.find(x => x.platform === name); }
function isAbnormalSnapshot(snap){ return Boolean(snap && snap.status && snap.status !== 'answered'); }
function visibleSnapshotEntries(q){
  const rows = (q?.snapshots || []).map((snapshot,index)=>({snapshot,index}));
  return showAbnormalSnapshots ? rows : rows.filter(row => !isAbnormalSnapshot(row.snapshot));
}
function currentQuestionSnapshot(q){
  if(!q) return null;
  const snaps = visibleSnapshotEntries(q).map(row => row.snapshot);
  return snaps.find(x => x.snapshot_id === selected.questionSnapshot) || snaps.find(x => x.snapshot_id === q.latest_snapshot_id) || snaps[snaps.length-1] || null;
}
function setQuestionSnapshot(id){
  selected.questionSnapshot = id;
  renderDetail();
}
function toggleAbnormalSnapshots(){
  showAbnormalSnapshots = !showAbnormalSnapshots;
  if(!showAbnormalSnapshots){
    const current = (questionById(selected.question)?.snapshots || []).find(s => s.snapshot_id === selected.questionSnapshot);
    if(isAbnormalSnapshot(current)) selected.questionSnapshot = '';
  }
  renderDetail();
}
function sourceTextOfCreator(c){
  return [c.name,c.platform,(c.bucket_tags||[]).join(' '),sourcesForCreator(c).map(s=>s.title).join(' ')].join(' ').toLowerCase();
}
function sourcesForCreator(c){
  if(!c) return [];
  const ids = new Set(c.source_ids || []);
  const rows = DATA.sources.filter(x => ids.has(x.source_id));
  rows.sort((a,b) => ((b.hit_snapshots||0)-(a.hit_snapshots||0)) || ((a.best_rank||999)-(b.best_rank||999)) || String(b.last_seen||'').localeCompare(String(a.last_seen||'')));
  return rows;
}
function creatorsForPlatform(platform){
  const f = filter.toLowerCase();
  const platformMatched = String(platform||'').toLowerCase().includes(f);
  let rows = DATA.creators.filter(x => x.platform === platform);
  if(f && !platformMatched) rows = rows.filter(x => sourceTextOfCreator(x).includes(f));
  rows.sort((a,b) => ((b.hit_snapshots||0)-(a.hit_snapshots||0)) || (b.source_count-a.source_count) || (b.question_count-a.question_count));
  return rows;
}
function ensureSourceSelection(platformRows){
  const rows = platformRows || filteredItems();
  if(rows.length && !rows.some(x => x.platform === selected.sourcePlatform)){
    selected.sourcePlatform = rows[0].platform;
    selected.sourceCreator = '';
  }
  const creators = creatorsForPlatform(selected.sourcePlatform);
  if(creators.length && !creators.some(x => x.creator_id === selected.sourceCreator)) selected.sourceCreator = creators[0].creator_id;
  if(!creators.length) selected.sourceCreator = '';
}
function setSourceCreator(id){
  selected.sourceCreator = id;
  sourceMaterialLimit = 50;
  renderDetail();
}
function loadMorePlatforms(){
  sourcePlatformLimit += 50;
  renderList();
}
function loadMoreSourceMaterials(){
  const pane = document.querySelector('.source-detail-pane');
  const oldTop = pane ? pane.scrollTop : 0;
  const p = platformByName(selected.sourcePlatform);
  sourceMaterialLimit += 50;
  if(pane && p){
    pane.innerHTML = renderSourceCreatorPanel(creatorById(selected.sourceCreator), p);
    requestAnimationFrame(() => { pane.scrollTop = oldTop; });
  }else{
    renderDetail();
  }
}
function focusSourceCreator(platform, creatorName){
  mode = 'source';
  activeFilter = '全部';
  filter = '';
  $('searchInput').value = '';
  selected.sourcePlatform = platform;
  const c = DATA.creators.find(x => x.platform === platform && x.name === creatorName) || DATA.creators.find(x => x.platform === platform);
  selected.sourceCreator = c?.creator_id || '';
  sourcePlatformLimit = 50;
  sourceMaterialLimit = 50;
  document.querySelectorAll('.view-switch .view-pill').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  render();
}

function boot(){
  $('topQ').textContent = DATA.overview.question_count;
  $('topS').textContent = DATA.overview.source_count;
  $('topC').textContent = DATA.overview.creator_count;
  document.querySelectorAll('.view-switch .view-pill').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('searchInput').addEventListener('input', e => { filter = e.target.value.trim(); sourcePlatformLimit = 50; sourceMaterialLimit = 50; render(); });
  selected.question = DATA.questions[0]?.question_id || '';
  selected.sourcePlatform = DATA.platforms[0]?.platform || '';
  selected.sourceCreator = DATA.creators.find(x => x.platform === selected.sourcePlatform)?.creator_id || '';
  render();
}

function render(){
  document.body.classList.toggle('source-mode', mode === 'source');
  renderToolbar();
  renderList();
  renderDetail();
}
function renderToolbar(){
  const sortDefs = {
    question: [['time','最近'],['source','引用源'],['snapshot','快照']],
    source: [['share','引用占比'],['creator','创作者'],['question','覆盖问题']],
  }[mode] || [];
  $('searchInput').placeholder = mode==='question' ? '搜索问题、类目、平台…' : '搜索平台或作者…';
  $('sortTabs').innerHTML = sortDefs.map(([id,label]) => `<button class="acct-sort-tab ${sortMode[mode]===id?'active':''}" onclick="setSort('${id}')">${label}</button>`).join('');
  let chips = ['全部'];
  if(mode === 'question') chips = chips.concat([...new Set(DATA.questions.map(x=>x.bucket))]);
  if(mode === 'source') chips = chips.concat(['有异常']);
  $('filterRow').innerHTML = `<span class="filter-label">${mode==='question'?'类目':'筛选'}</span>` + chips.map(x => `<button class="chip ${activeFilter===x?'active':''}" onclick='setFilter(${JSON.stringify(x)})'>${esc(x)}</button>`).join('');
}
function filteredItems(){
  const f = filter.toLowerCase();
  if(mode === 'question'){
    let rows = DATA.questions.filter(x => activeFilter==='全部' || x.bucket===activeFilter);
    if(f) rows = rows.filter(x => [x.question,x.bucket,x.top_platform,x.risk_label].join(' ').toLowerCase().includes(f));
    if(sortMode.question==='source') rows.sort((a,b)=>b.source_count-a.source_count);
    else if(sortMode.question==='snapshot') rows.sort((a,b)=>(b.snapshot_count||0)-(a.snapshot_count||0) || String(b.finished_at).localeCompare(String(a.finished_at)));
    else rows.sort((a,b)=>String(b.finished_at).localeCompare(String(a.finished_at)));
    return rows;
  }
  if(mode === 'source'){
    let rows = DATA.platforms.filter(x => activeFilter==='全部' || (activeFilter==='有异常' && x.error_count>0));
    if(f) rows = rows.filter(x => [x.platform, x.platform_meta?.short || ''].join(' ').toLowerCase().includes(f) || DATA.creators.some(c => c.platform===x.platform && sourceTextOfCreator(c).includes(f)));
    if(sortMode.source==='creator') rows.sort((a,b)=>b.creator_count-a.creator_count || b.raw_ref_count-a.raw_ref_count);
    else if(sortMode.source==='question') rows.sort((a,b)=>b.question_count-a.question_count || b.raw_ref_count-a.raw_ref_count);
    else rows.sort((a,b)=>b.raw_ref_count-a.raw_ref_count || b.share-a.share);
    return rows;
  }
  return [];
}
function renderList(){
  const allRows = filteredItems();
  if(mode === 'source') ensureSourceSelection(allRows);
  if(mode !== 'source' && allRows.length && !allRows.some(x => (x.question_id||x.source_id||x.creator_id) === selected[mode])) selected[mode] = allRows[0].question_id || allRows[0].source_id || allRows[0].creator_id;
  const rows = mode === 'source' ? allRows.slice(0, sourcePlatformLimit) : allRows;
  const rowHtml = rows.map((item,i) => {
    const id = mode === 'source' ? item.platform : (item.question_id || item.source_id || item.creator_id);
    const active = (mode === 'source' ? selected.sourcePlatform === id : selected[mode] === id) ? 'active' : '';
    const r1 = i === 0 ? 'r1' : '';
    if(mode === 'question'){
      const tags = [`${item.bucket}`, `${item.source_count} 引用源`, `${item.snapshot_count||0} 快照`, `最近 ${item.latest_snapshot_label||item.finished_at_label}`].map(t=>`<span class="kw-tag">${esc(t)}</span>`).join('');
      return `<div class="acct-row ${active}" onclick="setSelected('${id}')">
        <div class="rank-no ${r1}">${i+1}</div><div class="acct-main"><div class="acct-name-row"><span class="acct-name">${esc(item.question)}</span><span class="kw-tag ${item.risk_label==='高风险'?'warn':'hot'}">${esc(item.risk_label)}</span></div><div class="kw-tags">${tags}</div><div class="heatrow">${heatCells(item.source_count, 16)}</div></div><div class="side-score"><div class="score-val">${item.source_count}</div><div class="score-lbl">引用源</div></div></div>`;
    }
    if(mode === 'source'){
      const tags = [`${item.creator_count} 作者`, `${item.source_count} 来源`, `${item.question_count} 问题`].map(t=>`<span class="kw-tag">${esc(t)}</span>`).join('');
      return `<div class="acct-row platform-row ${active}" onclick='setSelected(${JSON.stringify(id)})'>
        <div class="rank-no ${r1}">${i+1}</div>${platformBadge(item.platform)}<div class="acct-main"><div class="acct-name-row"><span class="acct-name">${esc(item.platform)}</span>${item.error_count?'<span class="kw-tag warn">异常 '+item.error_count+'</span>':''}</div><div class="kw-tags">${tags}</div><div class="heatrow">${heatCells(item.raw_ref_count, Math.max(...DATA.platforms.map(p=>p.raw_ref_count||0),1))}</div></div><div class="side-score"><div class="score-val">${fmtPct(item.share)}</div><div class="score-lbl">引用占比</div></div></div>`;
    }
    return '';
  }).join('');
  const moreHtml = mode === 'source' && allRows.length > rows.length ? `<div class="load-more-row"><button class="load-more-btn" onclick="loadMorePlatforms()">显示更多平台 · ${rows.length}/${allRows.length}</button></div>` : (mode === 'source' && allRows.length ? `<div class="list-footnote">已显示全部 ${allRows.length} 个平台</div>` : '');
  $('leftList').innerHTML = rowHtml ? rowHtml + moreHtml : `<div class="empty-hint">没有匹配结果</div>`;
}
function renderDetail(){
  if(mode === 'question') return renderQuestionDetail(questionById(selected.question));
  if(mode === 'source') return renderSourceWorkbench();
  $('rightPane').innerHTML = `<div class="empty-hint">暂无内容</div>`;
}
function rankCls(rank){ return `rl-${Math.min(Number(rank)||10,10)}`; }
function shortPlatformName(name){
  const chars = Array.from(String(name || '未知平台').replace(/\s+/g, '')).slice(0, 4);
  const one = chars.slice(0, 2).join('');
  const two = chars.slice(2, 4).join('');
  return `<span>${esc(one)}</span>${two ? `<span>${esc(two)}</span>` : ''}`;
}
function snapshotStateCopy(snap){
  if(!snap) return {title:'暂无成功采集',detail:'点击右上角显示异常采集'};
  if(isAbnormalSnapshot(snap)) return {title:'采集失败',detail:`未成功采集该内容（${snap.error || '未记录失败原因'}）`};
  return {title:'未采集到引用源',detail:'本次采集未进行搜索'};
}
function renderSnapshotState(snap, compact=false){
  const state = snapshotStateCopy(snap);
  return `<div class="snapshot-state ${compact?'compact':''}"><b>${esc(state.title)}</b>${state.detail?`<div class="snapshot-state-detail">${esc(state.detail)}</div>`:''}</div>`;
}
function renderSnapshotSourceRail(sources, snap){
  const rows = (sources || []).slice().sort((a,b)=>(a.rank||999)-(b.rank||999)).slice(0,30);
  if(!rows.length) return renderSnapshotState(snap);
  return `<div class="snapshot-source-rail">${rows.map(s => `<div class="snap-source-pill" title="${esc((s.platform || '未知平台') + ' · 第 ' + (s.rank || '—') + ' 位 · ' + (s.title || ''))}" onclick='focusSourceCreator(${JSON.stringify(s.platform)}, ${JSON.stringify(s.creator)})'><span class="snap-source-rank">${esc(s.rank||'—')}</span><div class="snap-source-icon">${logoBadge(s.platform, s.logo_url)}</div><div class="snap-source-name">${shortPlatformName(s.platform)}</div></div>`).join('')}</div>`;
}
function renderSnapshotToggle(q){
  const count = (q.snapshots || []).filter(isAbnormalSnapshot).length;
  if(!count) return '';
  return `<button class="view-pill" aria-pressed="${showAbnormalSnapshots}" onclick="toggleAbnormalSnapshots()">${showAbnormalSnapshots?'隐藏':'显示'}异常采集 · ${count}</button>`;
}
function renderSnapshotStrip(q, activeSnap){
  const entries = visibleSnapshotEntries(q);
  const snapSources = activeSnap?.sources || [];
  const pills = entries.map(({snapshot:s}) => {
    const abnormal = isAbnormalSnapshot(s);
    const meta = abnormal ? '采集失败' : s.source_count ? `${s.source_count} 引用 · ${s.platform_count} 平台` : '未采集到引用源';
    return `<button class="snap-pill ${activeSnap && activeSnap.snapshot_id===s.snapshot_id?'active':''}" title="${esc(abnormal ? s.error || '采集失败' : '')}" onclick="setQuestionSnapshot('${s.snapshot_id}')"><div class="snap-time">${esc(s.time_label)}</div><div class="snap-date">${esc((s.captured_label||'').slice(0,5))}</div><div class="snap-meta">${meta}</div></button>`;
  }).join('');
  return `${pills?`<div class="snapshot-strip">${pills}</div>`:''}${renderSnapshotSourceRail(snapSources, activeSnap)}<div class="time-note">事实口径：每个时间点代表一次采集记录；下方横排是当前快照引用源位次序列，切换时间点后同步变化。</div>`;
}
function renderCitationMatrix(q){
  const entries = visibleSnapshotEntries(q);
  const snaps = entries.map(row => row.snapshot);
  const rows = (q.matrix_sources || []).slice(0,12);
  if(!snaps.length || !rows.length) return `<div class="empty-hint">暂无引用位次历史</div>`;
  const head = `<tr><th>引用源</th>${snaps.map(s=>`<th>${esc(s.time_label)}</th>`).join('')}<th>首引</th><th>最近</th></tr>`;
  const body = rows.map(r => `<tr><td class="matrix-source-cell"><div class="matrix-title">${logoBadge(r.platform, r.logo_url)} ${esc(r.title)}</div><div class="matrix-sub">${esc(r.platform)} · ${esc(r.creator)} · ${r.hit_snapshots} 次快照</div></td>${entries.map(({index}) => {const rank=(r.rank_history||[])[index];return `<td>${rank?`<span class="matrix-rank ${rankCls(rank)}">${rank}</span>`:'<span class="matrix-empty">—</span>'}</td>`;}).join('')}<td>${esc(r.first_cited_label||'—')}</td><td>${esc(r.last_cited_label||'—')}</td></tr>`).join('');
  return `<div class="matrix-wrap"><table class="matrix-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}
function renderQuestionDetail(q){
  if(!q) return $('rightPane').innerHTML = `<div class="empty-hint">暂无问题</div>`;
  const snap = currentQuestionSnapshot(q);
  const snapSources = snap?.sources || [];
  const unavailable = !snap || isAbnormalSnapshot(snap);
  const currentPlatform = unavailable ? '—' : snap.platform_count ?? 0;
  const currentCreator = unavailable ? '—' : snap.creator_count ?? 0;
  const currentError = unavailable ? '—' : snap.error_count ?? 0;
  const statusTag = !snap ? '<span class="kw-tag">暂无成功采集</span>' : isAbnormalSnapshot(snap) ? '<span class="kw-tag">采集失败</span>' : '';
  const refs = snapSources.map(s => `<div class="art-row" onclick='focusSourceCreator(${JSON.stringify(s.platform)}, ${JSON.stringify(s.creator)})'><span class="art-rank ${rankCls(s.rank)}">${s.rank}</span>${logoBadge(s.platform, s.logo_url)}<div class="art-main"><div class="art-title">${esc(s.title)}</div><div class="art-sub">${esc(s.platform)} · ${esc(s.creator)}${s.published_at?' · 发布 '+esc(s.published_at):''}${s.error?' · '+esc(s.error):''}</div></div>${s.link?`<a class="row-link" href="${esc(s.link)}" target="_blank" onclick="event.stopPropagation()">原文</a>`:''}</div>`).join('');
  const platformRows = Object.entries(snapSources.reduce((m,s)=>{m[s.platform]=(m[s.platform]||0)+1;return m;},{})).sort((a,b)=>b[1]-a[1]).map(([p,n])=>`<tr><td>${platformBadge(p)} ${esc(p)}</td><td>${n}</td><td>${Math.round(n/Math.max(snapSources.length,1)*100)}%</td></tr>`).join('');
  const platformTable = platformRows ? `<table class="table"><thead><tr><th>平台</th><th>引用</th><th>占比</th></tr></thead><tbody>${platformRows}</tbody></table>` : renderSnapshotState(snap, true);
  $('rightPane').innerHTML = `<div class="detail-wrap">
    <div class="card"><div class="hero-row"><div><div class="hero-title">${esc(q.question)}</div><div class="hero-sub">问题主视角 · 这里先记录事实：每个时间点 AI 怎么回答、引用了哪些来源、引用位次如何变化。价值判断留给人或后续 Agent。</div><div class="kw-tags" style="margin-top:8px"><span class="kw-tag hot">${esc(q.bucket)}</span><span class="kw-tag">当前快照 ${esc(snap?.captured_label||'—')}</span><span class="kw-tag">${esc(snap ? `${snap.ai_platform||'豆包'} · ${snap.client_type||'Web端'}` : '—')}</span>${statusTag}<span class="kw-tag">窗口 ${esc(q.observation_window_label||'—')}</span></div></div></div><div class="stat-row"><div class="stat-item"><div class="stat-n hi">${q.source_count}</div><div class="stat-l">全部引用源</div></div><div class="stat-item"><div class="stat-n">${currentPlatform}</div><div class="stat-l">当前平台</div></div><div class="stat-item"><div class="stat-n">${currentCreator}</div><div class="stat-l">当前作者</div></div><div class="stat-item"><div class="stat-n warn">${currentError}</div><div class="stat-l">异常来源</div></div><div class="stat-item"><div class="stat-n">${q.snapshot_count||0}</div><div class="stat-l">时间点</div></div></div></div>
    <div class="card"><div class="card-title-row"><div class="card-title">采集快照</div>${renderSnapshotToggle(q)}</div>${renderSnapshotStrip(q, snap)}</div>
    ${renderAnswerCard(snap)}
    <div class="card"><div class="card-title">引用位次矩阵</div>${renderCitationMatrix(q)}</div>
    <div class="card"><div class="card-title">当前快照引用源榜单</div><div class="section-stack">${refs || renderSnapshotState(snap, true)}</div></div>
    <div class="card"><div class="card-title">当前快照平台分布</div>${platformTable}</div>
  </div>`;
}
function renderSourceWorkbench(){
  const platformRows = filteredItems();
  ensureSourceSelection(platformRows);
  const p = platformByName(selected.sourcePlatform);
  if(!p) return $('rightPane').innerHTML = `<div class="empty-hint">暂无平台</div>`;
  const creators = creatorsForPlatform(p.platform);
  const authorRows = creators.map((c,i) => {
    const active = c.creator_id === selected.sourceCreator ? 'active' : '';
    const tags = [`${c.source_count} 来源`, `${c.question_count} 问题`, `${c.hit_snapshots||0} 快照`, `最佳第${c.best_rank}`].concat(c.bucket_tags||[]).slice(0,5).map(t=>`<span class="kw-tag">${esc(t)}</span>`).join('');
    return `<div class="source-author-row ${active}" onclick="setSourceCreator('${c.creator_id}')">${creatorBadge(c)}<div class="acct-main"><div class="acct-name-row"><span class="acct-name">${esc(c.name)}</span></div><div class="kw-tags">${tags}</div><div class="heatrow">${heatCells(c.hit_snapshots||0, 80)}</div></div><div class="side-score"><div class="score-val">${c.hit_snapshots||0}</div><div class="score-lbl">快照</div></div></div>`;
  }).join('') || `<div class="empty-hint">该平台暂无创作者</div>`;
  $('rightPane').innerHTML = `<div class="source-workbench">
    <div class="source-author-col"><div class="source-col-head"><div class="source-col-title">${platformBadge(p.platform)}<span>${esc(p.platform)} · 创作者</span></div><div class="source-col-sub">${p.creator_count} 个创作者 · ${p.source_count} 个引用源 · ${p.raw_ref_count} 次引用 · 覆盖 ${p.question_count} 个问题</div></div><div class="source-author-list">${authorRows}</div></div>
    <div class="source-detail-pane">${renderSourceCreatorPanel(creatorById(selected.sourceCreator), p)}</div>
  </div>`;
}
function renderSourceCreatorPanel(c, p){
  if(!c) return `<div class="empty-hint">选择左侧平台下的创作者</div>`;
  const allSources = sourcesForCreator(c);
  const shownSources = allSources.slice(0, sourceMaterialLimit);
  const srcRows = shownSources.map(source => {
    const rank = source.best_rank || 999;
    const meta = `${esc(source.platform)} · ${esc(source.domain)} · 发布 ${esc(source.published_label)} · 首引 ${esc(source.first_seen_label)} · 最近 ${esc(source.last_seen_label)} · ${source.hit_snapshots||0} 次快照 · 覆盖 ${source.citation_count||0} 问题${source.error?' · '+esc(source.error):''}`;
    return `<div class="art-row source-material-row"><span class="art-rank ${rankCls(rank)}">${rank === 999 ? '—' : rank}</span><div class="art-main"><div class="art-title">${esc(source.title)}</div><div class="art-sub">${meta}</div></div>${source.link?`<a class="row-link" href="${esc(source.link)}" target="_blank" onclick="event.stopPropagation()">原文</a>`:''}</div>`;
  }).join('') || `<div class="empty-hint">暂无被引用内容</div>`;
  const sourceMore = allSources.length > shownSources.length ? `<div class="load-more-row"><button class="load-more-btn" onclick="loadMoreSourceMaterials()">显示更多被引用内容 · ${shownSources.length}/${allSources.length}</button></div>` : (allSources.length ? `<div class="list-footnote">已显示全部 ${allSources.length} 条被引用内容</div>` : '');
  const qRows = (c.questions||[]).map(q => {
    const item = DATA.questions.find(x=>x.question===q);
    return `<div class="mini-row" onclick="setMode('question');setSelected('${item?.question_id||''}')"><div class="mini-main"><div class="mini-name">${esc(q)}</div><div class="mini-meta">${item ? esc(item.bucket)+' · '+item.source_count+' 引用源 · '+esc(item.finished_at_label) : ''}</div></div></div>`;
  }).join('') || `<div class="empty-hint">暂无覆盖问题</div>`;
  const bucketRows = Object.entries(c.buckets||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v}</td><td>${Math.round(v/Math.max(c.question_count,1)*100)}%</td></tr>`).join('') || `<tr><td colspan="3">暂无</td></tr>`;
  return `<div class="detail-wrap">
    <div class="card"><div class="hero-row"><div><div class="hero-title">${creatorBadge(c)} ${esc(c.name)}</div><div class="hero-sub">引用源视角 · 先看平台，再看平台里的作者，最后看作者在不同时间点被 AI 引用的事实。</div><div class="kw-tags" style="margin-top:8px"><span class="kw-tag hot">${esc(c.platform)}</span>${(c.bucket_tags||[]).map(x=>`<span class="kw-tag">${esc(x)}</span>`).join('')}<span class="kw-tag">首次 ${esc(c.first_seen_label||'—')}</span><span class="kw-tag">最近 ${esc(c.last_seen_label)}</span></div></div><div style="text-align:right"><div style="font-size:28px;font-weight:900">${c.hit_snapshots||0}</div><div style="font-size:10px;color:#bbb">快照命中</div></div></div><div class="stat-row"><div class="stat-item"><div class="stat-n hi">${c.source_count}</div><div class="stat-l">引用源</div></div><div class="stat-item"><div class="stat-n">${c.question_count}</div><div class="stat-l">覆盖问题</div></div><div class="stat-item"><div class="stat-n">${c.best_rank}</div><div class="stat-l">最佳引用位</div></div><div class="stat-item"><div class="stat-n">${c.hit_snapshots||0}</div><div class="stat-l">引用快照</div></div><div class="stat-item"><div class="stat-n warn">${p.error_count||0}</div><div class="stat-l">平台异常</div></div></div></div>
    <div class="card"><div class="card-title-row"><div class="card-title">被引用内容</div><div class="list-count">显示 ${shownSources.length}/${allSources.length}</div></div><div class="section-stack">${srcRows}</div>${sourceMore}</div>
    <div class="card"><div class="card-title">覆盖问题</div><div class="section-stack">${qRows}</div></div>
    <div class="card"><div class="card-title">问题类目分布</div><table class="table"><thead><tr><th>类目</th><th>问题数</th><th>占比</th></tr></thead><tbody>${bucketRows}</tbody></table></div>
  </div>`;
}
boot();
</script>
</body>
</html>
"""


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return HTML_TEMPLATE.replace("__BOOTSTRAP_JSON__", payload)


class Handler(BaseHTTPRequestHandler):
    data_cache: dict | None = None

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web-demo] {self.address_string()} {fmt % args}", flush=True)

    @classmethod
    def data(cls) -> dict:
        if cls.data_cache is None:
            cls.data_cache = build_demo_data()
        return cls.data_cache

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body = render_html(self.data()).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/demo":
            body = json.dumps(self.data(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def port_is_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    data = build_demo_data()
    Handler.data_cache = data
    STATIC_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATIC_FILE.write_text(render_html(data), encoding="utf-8")
    if port_is_in_use(PORT):
        print(f"127.0.0.1:{PORT} is already in use")
        return 98
    print(f"GEO Demo running: http://127.0.0.1:{PORT}")
    print(f"Static snapshot: {STATIC_FILE}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
