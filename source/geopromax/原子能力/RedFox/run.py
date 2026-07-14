#!/usr/bin/env python3
"""RedFox 豆包纯文字搜索 -> Markdown 搜索快照。

默认用法：
  REDFOX_API_KEY=... python3 原子能力/RedFox/run.py --question "225提领靠谱吗？"

测试/复用已有 RedFox 结果：
  python3 原子能力/RedFox/run.py --from-json result.json

默认输出目录：项目 `data/redfox/`。
脚本不会保存或打印 API Key。
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

SUBMIT_URL = "https://redfox.hk/story/api/deepSearch/dbSubmit"
RESULT_URL = "https://redfox.hk/story/api/deepSearch/dbResult"
SUCCESS_CODE = 2000
FINAL_SUCCESS_STATUSES = {"completed", "succeeded", "success"}
FINAL_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}
SCRIPT_VERSION = "0.1.0"

DEFAULT_TAXONOMY: dict[str, dict[str, Any]] = {
    "提领": {
        "description": "和保单提款、领取、减保、现金流释放相关的问题。",
        "aliases": ["提领", "领取", "取现", "减保", "withdrawal", "225", "255", "567"],
        "parent_paths": [
            ["财富配置", "保险", "香港保险", "售前阶段"],
            ["保险", "分红险", "领取方式"],
        ],
    },
    "分红险": {
        "description": "和分红实现率、非保证红利、保险公司分红机制相关的问题。",
        "aliases": ["分红", "分红险", "实现率", "红利", "终期红利", "周年红利", "复归红利", "非保证"],
        "parent_paths": [["财富配置", "保险", "香港保险", "产品类型"]],
    },
    "储蓄险": {
        "description": "和香港储蓄险、现金价值、IRR、教育金、养老金相关的问题。",
        "aliases": ["储蓄险", "储蓄", "现金价值", "IRR", "irr", "教育金", "养老金", "年金"],
        "parent_paths": [["财富配置", "保险", "香港保险", "产品类型"]],
    },
    "排行榜": {
        "description": "和排名、榜单、公司对比、产品排行相关的问题。",
        "aliases": ["排名", "排行", "排行榜", "榜单", "top", "TOP", "对比", "哪家好"],
        "parent_paths": [["财富配置", "保险", "香港保险", "决策参考"]],
    },
    "产品对比": {
        "description": "和具体产品、公司、方案之间比较相关的问题。",
        "aliases": ["对比", "比较", "哪个好", "哪款好", "友邦", "保诚", "宏利", "安盛"],
        "parent_paths": [["财富配置", "保险", "香港保险", "售前阶段"]],
    },
    "未分类": {
        "description": "暂时无法稳定归入现有主分类的问题。",
        "aliases": [],
        "parent_paths": [],
    },
}


@dataclass
class Job:
    question: str
    explicit_topic: str | None = None
    existing_task_id: str | None = None
    source_json: Path | None = None


@dataclass
class Snapshot:
    snapshot_id: str
    question: str
    primary_topic: str
    captured_at: str
    submitted_at: str | None
    task_id: str | None
    status: str
    content: str
    queries: list[str]
    suggests: list[str]
    citations: list[dict[str, Any]]
    answer_hash: str
    source_json: str | None
    raw_result_keys: list[str]


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def now_local() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None = None) -> str:
    return (dt or now_local()).isoformat(timespec="seconds")


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


def answer_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sanitize_name(text: str, *, max_len: int = 80) -> str:
    text = (text or "未命名").strip()
    text = re.sub(r"\s+", "", text)
    replacements = {
        "/": "／",
        "\\": "／",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
        "\0": "",
    }
    text = "".join(replacements.get(ch, ch) for ch in text)
    text = re.sub(r"[\r\n\t]+", "", text).strip(" .")
    if not text:
        text = "未命名"
    if len(text) > max_len:
        text = f"{text[:max_len]}_{short_hash(text, 6)}"
    return text


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for idx in range(2, 1000):
        candidate = path.with_name(f"{stem}__{idx}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many filename collisions near {path}")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def post_json(url: str, api_key: str, payload: dict[str, Any], timeout: int = 60, attempts: int = 4) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "REDFOX_API_KEY": api_key,
                "User-Agent": f"GEOProMax-RedFox-Markdown/{SCRIPT_VERSION}",
            },
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500:
                raise RuntimeError(f"HTTP {exc.code}: {raw[:500]}") from exc
            last_exc = RuntimeError(f"HTTP {exc.code}: {raw[:500]}")
        except (error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_exc}") from last_exc


def require_success(payload: dict[str, Any], stage: str) -> None:
    code = payload.get("code")
    if code != SUCCESS_CODE:
        msg = payload.get("msg") or payload.get("message") or ""
        raise RuntimeError(f"{stage} failed: code={code}, msg={msg}, payload_keys={list(payload.keys())}")


def submit(api_key: str, question: str) -> tuple[str, dict[str, Any]]:
    payload = post_json(SUBMIT_URL, api_key, {"inquiryText": question})
    require_success(payload, "submit")
    data = payload.get("data") or {}
    task_id = data.get("taskId") or payload.get("taskId")
    if not task_id:
        raise RuntimeError(f"submit succeeded but taskId missing: {payload}")
    return str(task_id), payload


def poll(api_key: str, task_id: str, *, timeout_seconds: int, interval_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = post_json(RESULT_URL, api_key, {"taskId": task_id})
        require_success(payload, "poll")
        last_payload = payload
        data = payload.get("data") or {}
        status = str(data.get("status") or payload.get("status") or "").lower()
        if status in FINAL_SUCCESS_STATUSES:
            return payload
        if status in FINAL_FAILED_STATUSES:
            raise RuntimeError(f"task failed: taskId={task_id}, failReason={data.get('failReason') or payload.get('failReason')}")
        print(f"[poll] task={task_id} status={status or 'unknown'}", flush=True)
        time.sleep(interval_seconds)
    raise TimeoutError(f"poll timeout: taskId={task_id}, last={last_payload}")


def result_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 RedFox 文档字段 search_result[] 与实测字段 searchGuid[].text_card。"""
    sr = result.get("search_result")
    if isinstance(sr, list):
        return [dict(item, _source_array="search_result") for item in sr if isinstance(item, dict)]

    items: list[dict[str, Any]] = []
    sg = result.get("searchGuid")
    if isinstance(sg, list):
        for idx, wrapper in enumerate(sg, 1):
            if not isinstance(wrapper, dict):
                continue
            card = wrapper.get("text_card") or wrapper.get("card") or wrapper
            if not isinstance(card, dict):
                continue
            items.append(
                {
                    "_source_array": "searchGuid.text_card",
                    "rank": idx,
                    "url": card.get("url"),
                    "title": card.get("title"),
                    "summary": card.get("summary") or card.get("snippet"),
                    "published_at": card.get("publish_time_second") or card.get("published_at"),
                    "site_icon": card.get("logo_url") or card.get("site_icon"),
                    "site_name": card.get("sitename") or card.get("site_name"),
                    "index": card.get("index"),
                    "original_doc_rank": card.get("original_doc_rank"),
                    "doc_id": card.get("doc_id"),
                    "search_id": card.get("search_id"),
                }
            )
    return items


def load_taxonomy(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "taxonomy.json"
    existing = load_json(path, {})
    if not isinstance(existing, dict):
        existing = {}
    topics = existing.get("topics") if isinstance(existing.get("topics"), dict) else {}
    merged_topics = json.loads(json.dumps(DEFAULT_TAXONOMY, ensure_ascii=False))
    for name, meta in topics.items():
        if isinstance(meta, dict):
            merged = merged_topics.get(name, {})
            merged.update(meta)
            merged_topics[name] = merged
    return {
        "schema_version": "1.0",
        "updated_at": iso(),
        "description": "GEO 搜索快照的主分类表。物理文件夹只存主分类；复杂父子层级、别名和多路径归属放在这里。",
        "topics": merged_topics,
    }


def save_taxonomy(output_dir: Path, taxonomy: dict[str, Any]) -> None:
    taxonomy["updated_at"] = iso()
    write_json(output_dir / "taxonomy.json", taxonomy)


def classify_topic(question: str, taxonomy: dict[str, Any], explicit_topic: str | None = None) -> str:
    if explicit_topic:
        return sanitize_name(explicit_topic, max_len=40)
    text = question.lower()
    scores: dict[str, int] = {}
    topics = taxonomy.get("topics") or {}
    for topic, meta in topics.items():
        if topic == "未分类":
            continue
        aliases = [topic] + list((meta or {}).get("aliases") or [])
        for alias in aliases:
            alias_text = str(alias).lower().strip()
            if alias_text and alias_text in text:
                scores[topic] = scores.get(topic, 0) + max(1, len(alias_text))
    if not scores:
        return "未分类"
    # 排序稳定：先分数，再名称，避免同分时每次随机。
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


def parse_result_payload(question: str, payload: dict[str, Any], *, explicit_topic: str | None, taxonomy: dict[str, Any], submitted_at: str | None, captured_at: str | None, source_json: Path | None) -> Snapshot:
    data = payload.get("data") or {}
    result = data.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    content = str(result.get("content") or "")
    queries = [str(x) for x in (result.get("queries") or []) if str(x).strip()]
    suggests = [str(x) for x in (result.get("suggest") or []) if str(x).strip()]
    citations = result_items(result)
    captured_at = captured_at or iso()
    dt_for_id = parse_datetime(captured_at) or now_local()
    qhash = short_hash(question)
    snapshot_id = f"rf_{qhash}_{dt_for_id.strftime('%Y%m%dT%H%M%S')}"
    primary_topic = classify_topic(question, taxonomy, explicit_topic)
    if primary_topic not in (taxonomy.get("topics") or {}):
        taxonomy.setdefault("topics", {})[primary_topic] = {
            "description": "用户或脚本新增的主分类。",
            "aliases": [primary_topic],
            "parent_paths": [],
        }
    return Snapshot(
        snapshot_id=snapshot_id,
        question=question,
        primary_topic=primary_topic,
        captured_at=captured_at,
        submitted_at=submitted_at,
        task_id=str(data.get("taskId") or payload.get("taskId") or "") or None,
        status=str(data.get("status") or payload.get("status") or "unknown"),
        content=content,
        queries=queries,
        suggests=suggests,
        citations=citations,
        answer_hash=answer_hash(content),
        source_json=str(source_json) if source_json else None,
        raw_result_keys=sorted(result.keys()),
    )


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d_%H%M%S"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    return None


def json_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def topic_paths_for(snapshot: Snapshot, taxonomy: dict[str, Any]) -> list[str]:
    meta = (taxonomy.get("topics") or {}).get(snapshot.primary_topic) or {}
    paths = []
    for path in meta.get("parent_paths") or []:
        if isinstance(path, list) and path:
            paths.append(" > ".join(str(x) for x in [*path, snapshot.primary_topic]))
    return paths


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def clean_inline(text: Any, *, max_len: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" #\t\r\n")
    if len(value) > max_len:
        return f"{value[:max_len]}……"
    return value


def render_markdown(snapshot: Snapshot, rel_file: str, taxonomy: dict[str, Any]) -> str:
    topic_paths = topic_paths_for(snapshot, taxonomy)
    lines: list[str] = []
    lines.append("---")
    frontmatter = {
        "snapshot_id": snapshot.snapshot_id,
        "question_id": f"q_{short_hash(snapshot.question)}",
        "question": snapshot.question,
        "primary_topic": snapshot.primary_topic,
        "captured_at": snapshot.captured_at,
        "submitted_at": snapshot.submitted_at,
        "ai_platform": "豆包",
        "provider": "RedFox",
        "provider_mode": "doubao_text_search",
        "task_id": snapshot.task_id,
        "status": snapshot.status,
        "answer_hash": snapshot.answer_hash,
        "citation_count": len(snapshot.citations),
        "search_query_count": len(snapshot.queries),
        "suggest_count": len(snapshot.suggests),
        "file": rel_file,
        "script_version": SCRIPT_VERSION,
    }
    for key, value in frontmatter.items():
        if value is None:
            continue
        lines.append(f"{key}: {json_string(value)}")
    if topic_paths:
        lines.append("topic_paths:")
        for path in topic_paths:
            lines.append(f"  - {json_string(path)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {snapshot.question}")
    lines.append("")
    lines.append(
        f"> 采集时间：{snapshot.captured_at}；主分类：{snapshot.primary_topic}；引用源：{len(snapshot.citations)} 条；搜索词：{len(snapshot.queries)} 个。"
    )
    lines.append("")
    lines.append("## AI 回答")
    lines.append("")
    lines.append(snapshot.content.strip() or "（本次结果没有返回 AI 回答正文。）")
    lines.append("")
    lines.append("## 搜索词")
    lines.append("")
    if snapshot.queries:
        for idx, query in enumerate(snapshot.queries, 1):
            lines.append(f"{idx}. {query}")
    else:
        lines.append("（本次结果没有返回搜索词。）")
    lines.append("")
    lines.append("## 建议追问")
    lines.append("")
    if snapshot.suggests:
        for idx, suggest in enumerate(snapshot.suggests, 1):
            lines.append(f"{idx}. {suggest}")
    else:
        lines.append("（本次结果没有返回建议追问。）")
    lines.append("")
    lines.append("## 引用源")
    lines.append("")
    if snapshot.citations:
        for idx, item in enumerate(snapshot.citations, 1):
            title = clean_inline(item.get("title") or "未命名引用源")
            url = str(item.get("url") or "").strip()
            platform = clean_inline(item.get("site_name") or domain_of(url) or "未知平台", max_len=60)
            published_at = str(item.get("published_at") or "")
            summary = str(item.get("summary") or "").strip()
            site_icon = str(item.get("site_icon") or "").strip()
            lines.append(f"### {idx}. {title}")
            lines.append("")
            if url:
                lines.append(f"- URL：{url}")
            lines.append(f"- 平台：{platform}")
            if published_at:
                lines.append(f"- 发布时间：{published_at}")
            if site_icon:
                lines.append(f"- 平台图标：{site_icon}")
            if item.get("doc_id"):
                lines.append(f"- doc_id：{item.get('doc_id')}")
            if item.get("search_id"):
                lines.append(f"- search_id：{item.get('search_id')}")
            if item.get("original_doc_rank") is not None:
                lines.append(f"- 原始排序：{item.get('original_doc_rank')}")
            if summary:
                lines.append(f"- 摘要：{summary}")
            lines.append("")
    else:
        lines.append("（本次结果没有返回引用源。）")
        lines.append("")
    lines.append("## 机器可读补充")
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps(
            {
                "snapshot_id": snapshot.snapshot_id,
                "question_id": f"q_{short_hash(snapshot.question)}",
                "question": snapshot.question,
                "primary_topic": snapshot.primary_topic,
                "captured_at": snapshot.captured_at,
                "provider": "RedFox",
                "task_id": snapshot.task_id,
                "status": snapshot.status,
                "answer_hash": snapshot.answer_hash,
                "queries": snapshot.queries,
                "suggests": snapshot.suggests,
                "raw_result_keys": snapshot.raw_result_keys,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def history_default() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "updated_at": iso(),
        "description": "GEO/RedFox 搜索快照总索引。人读 Markdown 文件夹；机器读这个 JSON。",
        "snapshots": [],
        "questions": {},
        "topics": {},
    }


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def update_history(output_dir: Path, snapshot: Snapshot, md_path: Path) -> dict[str, Any]:
    history_path = output_dir / "history.json"
    history = load_json(history_path, history_default())
    if not isinstance(history, dict):
        history = history_default()
    history.setdefault("snapshots", [])
    history.setdefault("questions", {})
    history.setdefault("topics", {})
    rel_file = relpath(md_path, output_dir)
    record = {
        "snapshot_id": snapshot.snapshot_id,
        "question": snapshot.question,
        "primary_topic": snapshot.primary_topic,
        "captured_at": snapshot.captured_at,
        "submitted_at": snapshot.submitted_at,
        "provider": "RedFox",
        "ai_platform": "豆包",
        "task_id": snapshot.task_id,
        "status": snapshot.status,
        "file": rel_file,
        "answer_hash": snapshot.answer_hash,
        "citation_count": len(snapshot.citations),
        "search_query_count": len(snapshot.queries),
        "suggest_count": len(snapshot.suggests),
    }
    existing_ids = {item.get("snapshot_id") for item in history["snapshots"] if isinstance(item, dict)}
    if record["snapshot_id"] in existing_ids:
        record["snapshot_id"] = f"{record['snapshot_id']}_{short_hash(rel_file, 4)}"
        snapshot.snapshot_id = record["snapshot_id"]
    history["snapshots"].append(record)
    history["snapshots"] = sorted(
        history["snapshots"],
        key=lambda item: str(item.get("captured_at") or ""),
    )

    q = history["questions"].setdefault(
        snapshot.question,
        {
            "question": snapshot.question,
            "question_id": f"q_{short_hash(snapshot.question)}",
            "primary_topic": snapshot.primary_topic,
            "first_captured_at": snapshot.captured_at,
            "last_captured_at": snapshot.captured_at,
            "snapshot_count": 0,
            "files": [],
        },
    )
    q["primary_topic"] = q.get("primary_topic") or snapshot.primary_topic
    q["first_captured_at"] = min(str(q.get("first_captured_at") or snapshot.captured_at), snapshot.captured_at)
    q["last_captured_at"] = max(str(q.get("last_captured_at") or snapshot.captured_at), snapshot.captured_at)
    if rel_file not in q.setdefault("files", []):
        q["files"].append(rel_file)
    q["snapshot_count"] = len(q["files"])

    t = history["topics"].setdefault(
        snapshot.primary_topic,
        {"topic": snapshot.primary_topic, "snapshot_count": 0, "question_count": 0, "files": []},
    )
    if rel_file not in t.setdefault("files", []):
        t["files"].append(rel_file)
    topic_questions = {item.get("question") for item in history["snapshots"] if item.get("primary_topic") == snapshot.primary_topic}
    t["snapshot_count"] = len(t["files"])
    t["question_count"] = len(topic_questions)

    history["updated_at"] = iso()
    write_json(history_path, history)
    return history


def write_snapshot(output_dir: Path, snapshot: Snapshot, taxonomy: dict[str, Any]) -> Path:
    captured_dt = parse_datetime(snapshot.captured_at) or now_local()
    topic_dir = output_dir / sanitize_name(snapshot.primary_topic, max_len=40)
    filename = f"{captured_dt.strftime('%y%m%d')}_{sanitize_name(snapshot.question)}_{captured_dt.strftime('%H%M')}.md"
    md_path = unique_path(topic_dir / filename)
    rel_file = relpath(md_path, output_dir)
    md = render_markdown(snapshot, rel_file, taxonomy)
    write_text(md_path, md)
    return md_path


def generate_collections(output_dir: Path, history: dict[str, Any], taxonomy: dict[str, Any]) -> None:
    collection_dir = output_dir / "Collections"
    collection_dir.mkdir(parents=True, exist_ok=True)
    snapshots = [item for item in history.get("snapshots", []) if isinstance(item, dict)]
    snapshots = sorted(snapshots, key=lambda x: str(x.get("captured_at") or ""), reverse=True)

    lines = ["# GEO 搜索快照总览", "", f"> 更新时间：{iso()}。这个目录页由 RedFox Markdown 脚本自动生成。", ""]
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for item in snapshots:
        by_topic.setdefault(str(item.get("primary_topic") or "未分类"), []).append(item)
    for topic in sorted(by_topic.keys()):
        lines.append(f"## {topic}")
        lines.append("")
        for item in by_topic[topic][:100]:
            file = str(item.get("file") or "")
            question = str(item.get("question") or "未命名问题")
            captured_at = str(item.get("captured_at") or "")
            citations = item.get("citation_count")
            lines.append(f"- [{question}](../{file}) — {captured_at}，引用源 {citations} 条")
        lines.append("")
    write_text(collection_dir / "GEO搜索快照总览.md", "\n".join(lines))

    for topic, items in by_topic.items():
        safe_topic = sanitize_name(topic, max_len=40)
        meta = (taxonomy.get("topics") or {}).get(topic) or {}
        lines = [f"# {topic}", "", f"> {meta.get('description') or '该主分类下的 GEO 搜索快照。'}", ""]
        parent_paths = meta.get("parent_paths") or []
        if parent_paths:
            lines.append("## 分类路径")
            lines.append("")
            for path in parent_paths:
                if isinstance(path, list):
                    lines.append(f"- {' > '.join(str(x) for x in [*path, topic])}")
            lines.append("")
        lines.append("## 快照")
        lines.append("")
        for item in items:
            file = str(item.get("file") or "")
            question = str(item.get("question") or "未命名问题")
            captured_at = str(item.get("captured_at") or "")
            lines.append(f"- [{question}](../{file}) — {captured_at}")
        lines.append("")
        write_text(collection_dir / f"{safe_topic}.md", "\n".join(lines))


def parse_question_line(line: str) -> Job | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "\t" in raw:
        topic, question = raw.split("\t", 1)
        return Job(question=question.strip(), explicit_topic=topic.strip() or None)
    if "::" in raw:
        topic, question = raw.split("::", 1)
        return Job(question=question.strip(), explicit_topic=topic.strip() or None)
    return Job(question=raw)


def collect_jobs(args: argparse.Namespace) -> list[Job]:
    jobs: list[Job] = []
    if args.question:
        parsed = parse_question_line(args.question)
        if parsed:
            if args.topic and not parsed.explicit_topic:
                parsed.explicit_topic = args.topic
            jobs.append(parsed)
    if args.positional_question:
        parsed = parse_question_line(args.positional_question)
        if parsed:
            if args.topic and not parsed.explicit_topic:
                parsed.explicit_topic = args.topic
            jobs.append(parsed)
    if args.existing_task:
        item = args.existing_task
        if "=" not in item:
            raise SystemExit("--existing-task 必须写成 QUESTION=TASK_ID")
        question, task_id = item.split("=", 1)
        jobs.append(Job(question=question.strip(), explicit_topic=args.topic, existing_task_id=task_id.strip()))
    if args.from_json:
        jobs.append(Job(question="", explicit_topic=args.topic, source_json=Path(args.from_json)))
    return jobs


def payload_from_probe_json(path: Path) -> tuple[str, dict[str, Any], str | None, str | None, str | None]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if "final_response" in record:
        question = str(record.get("question") or "")
        return (
            question,
            record.get("final_response") or {},
            record.get("submitted_at"),
            record.get("captured_at"),
            str(record.get("task_id") or "") or None,
        )
    # 兼容直接保存 RedFox dbResult 响应的 JSON。
    question = str(record.get("question") or record.get("inquiryText") or "")
    return question, record, record.get("submitted_at"), record.get("captured_at"), None


def process_snapshot(output_dir: Path, snapshot: Snapshot, taxonomy: dict[str, Any], *, make_collections: bool) -> Path:
    md_path = write_snapshot(output_dir, snapshot, taxonomy)
    history = update_history(output_dir, snapshot, md_path)
    save_taxonomy(output_dir, taxonomy)
    if make_collections:
        generate_collections(output_dir, history, taxonomy)
    return md_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="调用 RedFox 豆包纯文字搜索 API，并把结果落为人类/Agent 都可读的 Markdown 搜索快照。"
    )
    parser.add_argument("positional_question", nargs="?", help="要搜索的单个问题。")
    parser.add_argument("--question", "-q", help="要搜索的单个问题，支持 '分类::问题'。")
    parser.add_argument("--topic", help="给本次所有未显式指定分类的问题强制指定主分类。")
    parser.add_argument("--existing-task", help="复用已有任务，格式 QUESTION=TASK_ID。")
    parser.add_argument("--from-json", help="从已有 RedFox JSON 生成 Markdown，不调用 API。")
    parser.add_argument("--output-dir", default=str(project_root_from_script() / "data/redfox"), help="Markdown 输出目录。")
    parser.add_argument("--timeout", type=int, default=300, help="单个 RedFox 任务轮询超时时间，秒。")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔，秒。")
    parser.add_argument("--no-collections", action="store_true", help="不生成 Collections 目录页。")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    jobs = collect_jobs(args)
    if not jobs:
        parser.print_help()
        return 2
    if len(jobs) != 1:
        parser.error("每次只允许一个问题或一份已有结果")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    taxonomy = load_taxonomy(output_dir)
    make_collections = not args.no_collections

    api_key: str | None = None
    live_jobs = [job for job in jobs if not job.source_json]
    if live_jobs:
        api_key = os.environ.get("REDFOX_API_KEY")
        if not api_key:
            api_key = getpass.getpass("REDFOX_API_KEY: ").strip()
        if not api_key:
            print("missing REDFOX_API_KEY", file=sys.stderr)
            return 2

    written: list[Path] = []
    for job in jobs:
        try:
            if job.source_json:
                question, payload, submitted_at, captured_at, task_id = payload_from_probe_json(job.source_json)
                if not question:
                    raise RuntimeError(f"{job.source_json} 缺少 question/inquiryText，无法生成可读文件名")
                if task_id and isinstance(payload.get("data"), dict) and not payload["data"].get("taskId"):
                    payload["data"]["taskId"] = task_id
                snapshot = parse_result_payload(
                    question,
                    payload,
                    explicit_topic=job.explicit_topic,
                    taxonomy=taxonomy,
                    submitted_at=submitted_at,
                    captured_at=captured_at,
                    source_json=job.source_json,
                )
            else:
                submitted_at = iso()
                if job.existing_task_id:
                    task_id = job.existing_task_id
                    print(f"[reuse] {job.question} -> {task_id}", flush=True)
                else:
                    print(f"[submit] {job.question}", flush=True)
                    task_id, _submit_payload = submit(api_key or "", job.question)
                    print(f"[task] {task_id}", flush=True)
                payload = poll(api_key or "", task_id, timeout_seconds=args.timeout, interval_seconds=args.interval)
                captured_at = iso()
                snapshot = parse_result_payload(
                    job.question,
                    payload,
                    explicit_topic=job.explicit_topic,
                    taxonomy=taxonomy,
                    submitted_at=submitted_at,
                    captured_at=captured_at,
                    source_json=None,
                )
            md_path = process_snapshot(output_dir, snapshot, taxonomy, make_collections=make_collections)
            written.append(md_path)
            print(
                f"[ok] {snapshot.primary_topic}/{md_path.name} citations={len(snapshot.citations)} queries={len(snapshot.queries)}",
                flush=True,
            )
        except Exception as exc:
            print(f"[error] {job.question or job.source_json}: {exc}", file=sys.stderr)
            return 1

    print(json.dumps({"output_dir": str(output_dir), "markdown_files": [str(path.relative_to(output_dir)) for path in written]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
