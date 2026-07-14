"""GEOProMax Web 服务。"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from web import ui

ROOT = Path(__file__).resolve().parents[1]
PORT = 8790
DATABASE = ROOT / "data/index/geopromax.sqlite"
MAX_IMPORT_BYTES = 100 * 1024 * 1024


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:n]


def qid_for(question: str) -> str:
    return f"q_{short_hash(question)}"


def creator_id_for(platform: str, creator: str) -> str:
    return f"crt_{short_hash(platform + ':' + creator)}"


def answer_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def read_markdown(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def channel_label(channel: str | None) -> str:
    return {"mobile": "Mobile", "web": "Web", "api": "API"}.get((channel or "").lower(), channel or "未知渠道")


def load_local_snapshots() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not DATABASE.exists():
        return {}, {"batch_count": 0, "answer_count": 0, "earliest": "", "latest": ""}
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    try:
        answers = list(db.execute(
            """
            SELECT a.id,a.question,a.status,COALESCE(a.mode,b.mode) mode,a.new_context,
                   a.started_at,a.finished_at,a.duration_seconds,a.markdown_path,a.error,
                   b.app,b.channel,b.raw_file,b.started_at batch_started_at,b.finished_at batch_finished_at
            FROM answers a JOIN batches b ON b.id=a.batch_id
            WHERE trim(COALESCE(a.question,''))<>''
            ORDER BY COALESCE(a.finished_at,a.started_at,b.finished_at,b.started_at),a.id
            """
        ))
        keywords: dict[int, list[str]] = defaultdict(list)
        for row in db.execute(
            """
            SELECT t.answer_id,k.keyword
            FROM tools t JOIN tool_search_keywords k ON k.tool_id=t.id
            ORDER BY t.answer_id,t.position,k.position
            """
        ):
            keywords[row["answer_id"]].append(row["keyword"])
        suggestions: dict[int, list[str]] = defaultdict(list)
        for row in db.execute("SELECT answer_id,question FROM suggested_questions ORDER BY answer_id,position"):
            suggestions[row["answer_id"]].append(row["question"])
        sources_by_answer: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in db.execute(
            """
            SELECT r.id relation_id,r.answer_id,t.position tool_position,r.position,r.source_id,r.error relation_error,
                   s.url,s.title,s.summary,s.platform,s.favicon,s.published_at,s.author,s.author_profile_link,
                   m.read_count,m.like_count,m.comment_count,m.favorite_count,m.share_count
            FROM source_relations r
            JOIN tools t ON t.id=r.tool_id
            LEFT JOIN sources s ON s.id=r.source_id
            LEFT JOIN source_metrics m ON m.answer_id=r.answer_id AND m.source_id=r.source_id
            WHERE r.type='search_result'
            ORDER BY r.answer_id,t.position,r.position,r.id
            """
        ):
            error = row["relation_error"] or ""
            raw_platform = row["platform"] or ("异常来源" if error else "其他网页")
            platform = ui.canonical_platform(raw_platform)
            creator = "抓取异常" if error and not row["source_id"] else "网友分享" if platform == "会计学堂" else row["author"] or platform
            logo_url = ui.platform_meta(platform, row["favicon"] or "")["logo_url"]
            sources_by_answer[row["answer_id"]].append({
                "source_id": row["source_id"] or f"error_{row['relation_id']}",
                "title": row["title"] or "未命名来源",
                "link": row["url"] or "",
                "logo_url": logo_url,
                "platform": platform,
                "raw_platform": raw_platform,
                "creator": creator,
                "creator_id": creator_id_for(platform, creator),
                "creator_avatar_url": "",
                "creator_profile_link": row["author_profile_link"] or "",
                "rank": row["position"],
                "tool_position": row["tool_position"],
                "summary": ui._short(row["summary"] or "", 120),
                "error": error,
                "published_at": row["published_at"] or "",
                "metrics": {
                    "read_count": row["read_count"],
                    "like_count": row["like_count"],
                    "comment_count": row["comment_count"],
                    "favorite_count": row["favorite_count"],
                    "share_count": row["share_count"],
                },
            })
        by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
        captured_values: list[str] = []
        for row in answers:
            answer_id = row["id"]
            question = row["question"].strip()
            captured_at = row["finished_at"] or row["started_at"] or row["batch_finished_at"] or row["batch_started_at"] or ""
            if captured_at:
                captured_values.append(captured_at)
            answer = read_markdown(row["markdown_path"])
            sources = sources_by_answer[answer_id]
            snapshot = {
                "snapshot_id": f"answer_{answer_id}",
                "time_label": ui._fmt_dt(captured_at).split(" ")[-1] if captured_at else "—",
                "captured_at": captured_at,
                "captured_label": ui._fmt_dt(captured_at),
                "ai_platform": row["app"],
                "client_type": channel_label(row["channel"]),
                "mode": row["mode"] or "",
                "new_context": bool(row["new_context"]),
                "status": row["status"],
                "error": row["error"] or "",
                "duration_seconds": row["duration_seconds"],
                "answer_hash": answer_hash(answer),
                "answer_preview": ui._short(answer, 160),
                "answer_text": answer,
                "search_keywords": keywords[answer_id],
                "suggested_questions": suggestions[answer_id],
                "sources": sources,
                "source_count": len(sources),
                "platform_count": len({source["platform"] for source in sources}),
                "creator_count": len({source["creator_id"] for source in sources}),
                "error_count": sum(bool(source["error"]) for source in sources),
                "is_demo_history": False,
            }
            by_question[question].append({
                "snapshot": snapshot,
                "answer": answer,
                "search_keywords": keywords[answer_id],
                "suggested_questions": suggestions[answer_id],
                "sources": sources,
                "captured_at": captured_at,
                "started_at": row["started_at"] or "",
                "duration_seconds": row["duration_seconds"],
                "status": row["status"],
                "error": row["error"] or "",
                "raw_file": row["raw_file"],
            })
        for rows in by_question.values():
            rows.sort(key=lambda item: (item["captured_at"], item["snapshot"]["snapshot_id"]))
        return dict(by_question), {
            "batch_count": db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
            "answer_count": len(answers),
            "earliest": min(captured_values, default=""),
            "latest": max(captured_values, default=""),
        }
    finally:
        db.close()


def matrix_for_snapshot_rows(snapshot_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slot_count = len(snapshot_rows)
    by_source: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(snapshot_rows):
        snapshot = row["snapshot"]
        for source in row["sources"]:
            source_id = source["source_id"]
            if source_id not in by_source:
                by_source[source_id] = {
                    "source_id": source_id,
                    "title": source["title"],
                    "platform": source["platform"],
                    "raw_platform": source["raw_platform"],
                    "creator": source["creator"],
                    "logo_url": source["logo_url"],
                    "rank_history": [None] * slot_count,
                    "hit_snapshots": 0,
                    "best_rank_over_time": None,
                    "first_cited_at": "",
                    "last_cited_at": "",
                }
            item = by_source[source_id]
            rank = source["rank"]
            previous_rank = item["rank_history"][index]
            item["rank_history"][index] = min(previous_rank, rank) if previous_rank else rank
            if previous_rank is None:
                item["hit_snapshots"] += 1
            item["best_rank_over_time"] = min(item["best_rank_over_time"], rank) if item["best_rank_over_time"] else rank
            captured_at = snapshot["captured_at"]
            if captured_at and (not item["first_cited_at"] or captured_at < item["first_cited_at"]):
                item["first_cited_at"] = captured_at
            if captured_at and (not item["last_cited_at"] or captured_at > item["last_cited_at"]):
                item["last_cited_at"] = captured_at
    rows = list(by_source.values())
    for item in rows:
        item["first_cited_label"] = ui._fmt_dt(item.pop("first_cited_at"))
        item["last_cited_label"] = ui._fmt_dt(item.pop("last_cited_at"))
    rows.sort(key=lambda item: (-(item["hit_snapshots"]), item["best_rank_over_time"] or 999, item["title"]))
    return rows


def build_web_data() -> dict[str, Any]:
    local, meta = load_local_snapshots()
    questions: list[dict[str, Any]] = []
    source_map: dict[str, dict[str, Any]] = {}
    creator_map: dict[str, dict[str, Any]] = {}
    platform_counter: Counter[str] = Counter()
    platform_logos: dict[str, str] = {}
    error_count = 0

    for question, rows in local.items():
        latest = rows[-1]
        latest_snapshot = latest["snapshot"]
        latest_sources = latest["sources"]
        all_source_ids = {source["source_id"] for row in rows for source in row["sources"]}
        all_platforms = {source["platform"] for row in rows for source in row["sources"]}
        question_id = qid_for(question)
        first_label = rows[0]["snapshot"]["captured_label"]
        last_label = latest_snapshot["captured_label"]
        question_row = {
            "question_id": question_id,
            "question": question,
            "bucket": ui._bucket(question),
            "status": latest["status"],
            "sent_at": latest["started_at"],
            "finished_at": latest["captured_at"],
            "sent_at_label": ui._fmt_dt(latest["started_at"]),
            "finished_at_label": last_label,
            "duration_seconds": latest["duration_seconds"] or 0,
            "duration_label": f"{latest['duration_seconds']:.1f}s" if latest["duration_seconds"] is not None else "—",
            "answer": latest["answer"],
            "answer_preview": ui._short(latest["answer"], 160) or f"本次采集状态：{latest['status']}",
            "answer_focus": ui._answer_focus(latest["answer"]) if latest["answer"] else [latest["status"]],
            "risk_label": ui._risk_label(question, latest["answer"]),
            "suggested_questions": latest["suggested_questions"],
            "search_keywords": latest["search_keywords"],
            "sources": latest_sources,
            "snapshots": [row["snapshot"] for row in rows],
            "snapshot_count": len(rows),
            "latest_snapshot_id": latest_snapshot["snapshot_id"],
            "latest_snapshot_label": last_label,
            "observation_window_label": f"{first_label} - {last_label}",
            "matrix_sources": matrix_for_snapshot_rows(rows),
            "source_count": len(all_source_ids),
            "error_count": latest_snapshot["error_count"],
            "platform_count": latest_snapshot["platform_count"],
            "creator_count": latest_snapshot["creator_count"],
            "top_platform": Counter(source["platform"] for source in latest_sources).most_common(1)[0][0] if latest_sources else "无",
            "score": min(99, len(all_source_ids) * 2 + len(all_platforms) * 5 + 10),
            "batch_file": latest["raw_file"],
        }
        questions.append(question_row)

        for snapshot_row in rows:
            snapshot = snapshot_row["snapshot"]
            captured_at = snapshot_row["captured_at"]
            captured_label = snapshot["captured_label"]
            seen_sources: set[str] = set()
            for source in snapshot_row["sources"]:
                platform_counter[source["platform"]] += 1
                error_count += bool(source["error"])
                if source["logo_url"] and not platform_logos.get(source["platform"]):
                    platform_logos[source["platform"]] = source["logo_url"]
                source_id = source["source_id"]
                if source_id not in source_map:
                    source_map[source_id] = {
                        "source_id": source_id,
                        "title": source["title"],
                        "link": source["link"],
                        "domain": ui._domain(source["link"]) or "无链接",
                        "logo_url": source["logo_url"],
                        "platform": source["platform"],
                        "raw_platform": source["raw_platform"],
                        "raw_platforms": {source["raw_platform"]},
                        "platform_meta": ui.platform_meta(source["platform"], source["logo_url"]),
                        "creator": source["creator"],
                        "creator_id": source["creator_id"],
                        "creator_avatar_url": source["creator_avatar_url"],
                        "creator_profile_link": source["creator_profile_link"],
                        "published_at": source["published_at"],
                        "summary": source["summary"],
                        "error": source["error"],
                        "first_seen": captured_at,
                        "last_seen": captured_at,
                        "questions": [],
                        "question_ids": set(),
                        "rank_sum": 0.0,
                        "best_rank": source["rank"],
                        "metrics": source["metrics"],
                        "hit_snapshots": 0,
                    }
                aggregate = source_map[source_id]
                aggregate["raw_platforms"].add(source["raw_platform"])
                aggregate["first_seen"] = min(aggregate["first_seen"], captured_at) if aggregate["first_seen"] and captured_at else aggregate["first_seen"] or captured_at
                aggregate["last_seen"] = max(aggregate["last_seen"], captured_at) if aggregate["last_seen"] and captured_at else aggregate["last_seen"] or captured_at
                aggregate["rank_sum"] += ui._rank_weight(source["rank"])
                aggregate["best_rank"] = min(aggregate["best_rank"], source["rank"])
                if source_id not in seen_sources:
                    aggregate["hit_snapshots"] += 1
                    seen_sources.add(source_id)
                if question not in aggregate["question_ids"]:
                    aggregate["question_ids"].add(question)
                    aggregate["questions"].append({"question": question, "rank": source["rank"], "seen_at": captured_label, "bucket": question_row["bucket"]})

                creator_id = source["creator_id"]
                if creator_id not in creator_map:
                    creator_map[creator_id] = {
                        "creator_id": creator_id,
                        "name": source["creator"],
                        "platform": source["platform"],
                        "raw_platform": source["raw_platform"],
                        "logo_url": source["logo_url"],
                        "avatar_url": source["creator_avatar_url"],
                        "profile_link": source["creator_profile_link"],
                        "platform_meta": ui.platform_meta(source["platform"], source["logo_url"]),
                        "score": 0.0,
                        "source_ids": set(),
                        "question_ids": set(),
                        "questions": [],
                        "sources": [],
                        "best_rank": source["rank"],
                        "first_seen": captured_at,
                        "last_seen": captured_at,
                        "buckets": Counter(),
                        "hit_snapshots": 0,
                        "snapshot_ids": set(),
                    }
                creator = creator_map[creator_id]
                creator["score"] += ui._rank_weight(source["rank"])
                creator["source_ids"].add(source_id)
                is_new_question = question not in creator["question_ids"]
                creator["question_ids"].add(question)
                if is_new_question:
                    creator["questions"].append(question)
                    creator["buckets"][question_row["bucket"]] += 1
                creator["best_rank"] = min(creator["best_rank"], source["rank"])
                creator["first_seen"] = min(creator["first_seen"], captured_at) if creator["first_seen"] and captured_at else creator["first_seen"] or captured_at
                creator["last_seen"] = max(creator["last_seen"], captured_at) if creator["last_seen"] and captured_at else creator["last_seen"] or captured_at
                if snapshot["snapshot_id"] not in creator["snapshot_ids"]:
                    creator["snapshot_ids"].add(snapshot["snapshot_id"])
                    creator["hit_snapshots"] += 1
                if len(creator["sources"]) < 8 and source_id not in {item["source_id"] for item in creator["sources"]}:
                    creator["sources"].append({"title": source["title"], "source_id": source_id, "rank": source["rank"]})

    questions.sort(key=lambda item: (item["source_count"], item["snapshot_count"], item["question"]), reverse=True)
    sources: list[dict[str, Any]] = []
    for source in source_map.values():
        source["question_ids"] = sorted(source["question_ids"])
        source["raw_platforms"] = sorted(source["raw_platforms"])
        source["score"] = round(source.pop("rank_sum") * (1 + max(len(source["question_ids"]), 1) ** 0.5), 1)
        source["citation_count"] = len(source["question_ids"])
        source["first_seen_label"] = ui._fmt_dt(source["first_seen"])
        source["last_seen_label"] = ui._fmt_dt(source["last_seen"])
        source["published_label"] = ui._fmt_date(source["published_at"])
        source["summary"] = ui._short(source["summary"] or "暂无摘要", 180)
        sources.append(source)
    sources.sort(key=lambda item: (item["hit_snapshots"], item["citation_count"], item["score"]), reverse=True)

    creators: list[dict[str, Any]] = []
    for creator in creator_map.values():
        creator["source_ids"] = sorted(creator["source_ids"])
        creator["question_ids"] = sorted(creator["question_ids"])
        creator["source_count"] = len(creator["source_ids"])
        creator["question_count"] = len(creator["question_ids"])
        creator["last_seen_label"] = ui._fmt_dt(creator["last_seen"])
        creator["first_seen_label"] = ui._fmt_dt(creator["first_seen"])
        creator["bucket_tags"] = [name for name, _ in creator["buckets"].most_common(3)]
        creator["score"] = round(creator["score"], 1)
        creator["buckets"] = dict(creator["buckets"])
        creator.pop("snapshot_ids")
        creators.append(creator)
    creators.sort(key=lambda item: (item["hit_snapshots"], item["source_count"]), reverse=True)

    platform_rows: list[dict[str, Any]] = []
    total_references = sum(platform_counter.values())
    for platform, count in platform_counter.most_common():
        platform_sources = [source for source in sources if source["platform"] == platform]
        linked_questions = {question for source in platform_sources for question in source["question_ids"]}
        platform_creators = {source["creator_id"] for source in platform_sources}
        platform_rows.append({
            "platform": platform,
            "platform_meta": ui.platform_meta(platform, platform_logos.get(platform, "")),
            "source_count": len(platform_sources),
            "raw_ref_count": count,
            "question_count": len(linked_questions),
            "creator_count": len(platform_creators),
            "error_count": sum(bool(source["error"]) for source in platform_sources),
            "share": round(count / max(total_references, 1) * 100, 2),
        })

    window_label = f"{ui._fmt_dt(meta['earliest'])} - {ui._fmt_dt(meta['latest'])}"
    overview = {
        "question_count": len(questions),
        "answered_count": sum(any(snapshot["status"] == "answered" for snapshot in question["snapshots"]) for question in questions),
        "source_count": len(sources),
        "creator_count": len(creators),
        "platform_count": len(platform_rows),
        "error_count": error_count,
        "latest_label": ui._fmt_dt(meta["latest"]),
        "started_label": ui._fmt_dt(meta["earliest"]),
        "latest_run_label": ui._fmt_dt(meta["latest"]),
        "observation_window_label": window_label,
        "window_label": window_label,
        "time_slot_count": meta["batch_count"],
        "time_grain_label": "采集批次",
        "answer_snapshot_count": meta["answer_count"],
        "raw_total": meta["answer_count"],
    }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "questions": questions,
        "sources": sources,
        "creators": creators,
        "platforms": platform_rows,
        "meta": meta,
    }


IMPORT_INJECTION = r'''
const WEB_API = {
  async data(){
    const res = await fetch('/api/data', {headers:{'Accept':'application/json'}});
    const body = await res.json();
    if(!res.ok || !body.ok) throw new Error(body.error || '加载失败');
    return body.data;
  },
  async importJson(file){
    const res = await fetch('/api/import-json', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-File-Name':encodeURIComponent(file.name)},
      body:file,
    });
    const body = await res.json();
    if(!res.ok || !body.ok) throw new Error(body.error || '导入失败');
    return body.data;
  }
};
let webImportTimer;
function webSetImportStatus(message, state=''){
  const el = document.getElementById('localImportStatus');
  if(!el) return;
  el.textContent = message;
  el.title = message;
  el.className = `import-status ${state}`;
}
async function webImportLocal(input){
  const file = input.files?.[0];
  if(!file) return;
  const btn = document.getElementById('localImportBtn');
  clearTimeout(webImportTimer);
  if(btn){ btn.disabled = true; btn.textContent = '导入中…'; }
  webSetImportStatus(`正在导入 ${file.name}`);
  try{
    const result = await WEB_API.importJson(file);
    DATA = await WEB_API.data();
    $('topQ').textContent = DATA.overview.question_count;
    $('topS').textContent = DATA.overview.source_count;
    $('topC').textContent = DATA.overview.creator_count;
    render();
    const message = result.status === 'skipped' ? '数据已存在' : `已导入 ${result.answers || 0} 条回答`;
    webSetImportStatus(message, 'success');
    webImportTimer = setTimeout(() => webSetImportStatus(''), 6500);
  }catch(err){
    const message = `导入失败：${err.message || err}`;
    webSetImportStatus(message, 'error');
    alert(message);
  }finally{
    input.value = '';
    if(btn){ btn.disabled = false; btn.textContent = '导入本地数据'; }
  }
}
'''


def render_web_html(data: dict[str, Any]) -> str:
    html = ui.render_html(data)
    html = html.replace(
        '<div class="topbar-actions" id="topbarActions"></div>',
        '<div class="topbar-actions" id="topbarActions"><span class="import-status" id="localImportStatus"></span><input id="localJsonInput" type="file" accept=".json,application/json" hidden onchange="webImportLocal(this)"><button class="view-pill" id="localImportBtn" onclick="document.getElementById(\'localJsonInput\').click()">导入本地数据</button></div>',
    )
    return html.replace("boot();", IMPORT_INJECTION + "\nboot();")


class WebHandler(BaseHTTPRequestHandler):
    data_cache: dict[str, Any] | None = None
    import_lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)

    @classmethod
    def data(cls, refresh: bool = False) -> dict[str, Any]:
        if refresh or cls.data_cache is None:
            cls.data_cache = build_web_data()
        return cls.data_cache

    def send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        self.send_body(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in {"/", "/index.html"}:
            self.send_body(200, render_web_html(self.data()).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path in {"/api/data", "/api/demo"}:
            self.send_json(200, {"ok": True, "data": self.data(refresh=True)})
            return
        if path == "/health":
            self.send_json(200, {"ok": True, "data": {"status": "ok", "service": "GEOProMax Web"}})
            return
        self.send_body(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/import-json":
            self.import_json()
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def import_json(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("没有选择 JSON 文件")
            if length > MAX_IMPORT_BYTES:
                raise ValueError("JSON 文件不能超过 100 MB")
            filename = Path(unquote(self.headers.get("X-File-Name") or "本地数据.json")).name
            if not filename.lower().endswith(".json"):
                raise ValueError("只支持 JSON 文件")
            content = self.rfile.read(length)
            from 原子能力.JSON转数据 import run as json_to_data
            with self.__class__.import_lock:
                result = json_to_data.ingest_bytes(content)
            self.__class__.data_cache = None
            self.send_json(200, {"ok": True, "data": {**result, "file_name": filename}})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})


def port_is_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    WebHandler.data_cache = build_web_data()
    if port_is_in_use(PORT):
        print(f"127.0.0.1:{PORT} is already in use")
        return 98
    print(f"GEOProMax Web running: http://127.0.0.1:{PORT}")
    print("Data source: data/index/geopromax.sqlite")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), WebHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
