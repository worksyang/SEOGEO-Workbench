#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[3]
DROP_QUERY = {
    "f_link_type", "flow_extra", "use_xbridge3", "loader_name",
    "need_sec_link", "sec_link_scene", "theme", "scene_from",
}
METRICS = ("read_count", "like_count", "comment_count", "favorite_count", "share_count")


def clean_url(value: str | None) -> str:
    text = html.unescape(str(value or "").strip())
    if not text:
        return ""
    parsed = urlsplit(text)
    query = sorted(
        (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in DROP_QUERY and not key.lower().startswith("utm_")
    )
    host = (parsed.hostname or "").lower()
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)):
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(query), ""))


def safe_name(value: str, limit: int = 100) -> str:
    table = str.maketrans({"/": "／", "\\": "／", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜"})
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).translate(table)
    text = re.sub(r"\s+", " ", text).strip(" .") or "未命名"
    if len(text) <= limit:
        return text
    return f"{text[:limit - 9].rstrip()}_{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def timestamp(value: str | None, fallback: str) -> str:
    try:
        return datetime.fromisoformat(str(value or "")).strftime("%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return fallback


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def context(input_path: Path, payload: dict) -> tuple[str, str, str]:
    parts = input_path.resolve().parts
    try:
        pos = parts.index("raw")
        app, channel, path_mode = parts[pos + 1:pos + 4]
    except (ValueError, IndexError):
        app, channel, path_mode = "豆包", "mobile", "unknown"
    return app, channel, str(payload.get("mode") or path_mode or "unknown")


def number(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def source_from(item: dict, sources: dict[str, dict]) -> str | None:
    platform = str(item.get("platform") or "").strip()
    url = clean_url(item.get("link"))
    if not platform or not url:
        return None
    source_id = hashlib.sha256(f"{platform}\n{url}".encode()).hexdigest()[:20]
    source = sources.setdefault(source_id, {"id": source_id, "url": url, "platform": platform})
    for field in ("title", "summary", "favicon", "published_at", "author_profile_link", "cover_image"):
        if item.get(field) not in (None, ""):
            source[field] = item[field]
    author = "网友分享" if platform == "会计学堂" else item.get("author")
    if author not in (None, ""):
        source["author"] = author
    content = str(item.get("content") or "").strip()
    if content:
        source["content"] = content
        source["content_hash"] = hashlib.sha256(content.encode()).hexdigest()
        source["markdown_path"] = f"data/sources/{safe_name(platform, 60)}/{source_id}.md"
    return source_id


def relation(item: dict, sources: dict[str, dict], kind: str, position: int, tool_position: int | None = None) -> dict:
    metrics = {name: number(item.get(name)) for name in METRICS}
    return {
        "source_id": source_from(item, sources),
        "type": kind,
        "position": position,
        "tool_position": tool_position,
        "anchor_offset": item.get("anchor_offset"),
        "anchor_text": item.get("anchor_text"),
        "image_url": item.get("image_url"),
        "error": item.get("error"),
        "metrics": metrics,
    }


def parse(input_path: Path) -> dict:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    app, channel, batch_mode = context(input_path, payload)
    fallback = input_path.stem
    batch = {
        "raw_file": relative(input_path),
        "input_file": payload.get("input_file"),
        "output_file": payload.get("output_file"),
        "app": app,
        "channel": channel,
        "mode": batch_mode,
        "new_context": bool(payload.get("new")),
        "status": payload.get("status") or "unknown",
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "duration_seconds": payload.get("duration_seconds"),
        "total": payload.get("total"),
        "succeeded": payload.get("succeeded"),
        "failed": payload.get("failed"),
    }
    sources: dict[str, dict] = {}
    answers = []
    for row in payload.get("results") or []:
        question = str(row.get("question") or "").strip()
        answer = str(row.get("answer") or "").strip()
        finished_at = row.get("finished_at") or row.get("started_at") or batch.get("finished_at")
        name = timestamp(finished_at, fallback)
        answer_path = None
        if question and answer:
            answer_path = f"data/answers/{safe_name(app, 40)}/{safe_name(question)}/{name}_{safe_name(channel, 30)}_{safe_name(str(row.get('mode') or batch_mode), 30)}.md"
        tools = []
        relations = []
        for tool_position, tool in enumerate(row.get("tool") or [], 1):
            tool_type = str(tool.get("type") or "unknown")
            tools.append({
                "position": tool_position,
                "type": tool_type,
                "content": tool.get("content"),
                "keywords": [str(x) for x in tool.get("keywords") or [] if str(x).strip()],
            })
            if tool_type == "search":
                for position, item in enumerate(tool.get("items") or [], 1):
                    relations.append(relation(item, sources, "search_result", position, tool_position))
        for position, item in enumerate(row.get("answer_annotations") or [], 1):
            kind = "image_reference" if item.get("type") == "image" else "text_reference"
            relations.append(relation(item, sources, kind, position))
        for position, item in enumerate(row.get("related_videos") or [], 1):
            relations.append(relation(item, sources, "related_video", position))
        answers.append({
            "question": question or None,
            "status": row.get("status") or "unknown",
            "mode": row.get("mode") or batch_mode,
            "new_context": bool(row.get("new", batch["new_context"])),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "duration_seconds": row.get("duration_seconds"),
            "share_link": row.get("share_link"),
            "markdown_path": answer_path,
            "content": answer,
            "error": row.get("error"),
            "tools": tools,
            "suggested_questions": [str(x) for x in row.get("suggested_questions") or [] if str(x).strip()],
            "relations": relations,
        })
    return {"schema_version": 1, "batch": batch, "answers": answers, "sources": sources}


def main() -> int:
    parser = argparse.ArgumentParser(description="把原始豆包 GEO JSON 拆成统一批次、回答、工具、来源和关系结构。")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    data = parse(Path(args.input).expanduser().resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"output": str(output), "answers": len(data["answers"]), "sources": len(data["sources"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
