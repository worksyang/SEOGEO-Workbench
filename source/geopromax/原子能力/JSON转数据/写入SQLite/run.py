#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY,
  raw_file TEXT NOT NULL UNIQUE,
  input_file TEXT,
  output_file TEXT,
  app TEXT NOT NULL,
  channel TEXT NOT NULL,
  mode TEXT,
  new_context INTEGER NOT NULL CHECK(new_context IN(0,1)),
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  duration_seconds REAL,
  total INTEGER,
  succeeded INTEGER,
  failed INTEGER
);
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  question TEXT,
  status TEXT NOT NULL,
  mode TEXT,
  new_context INTEGER NOT NULL CHECK(new_context IN(0,1)),
  started_at TEXT,
  finished_at TEXT,
  duration_seconds REAL,
  share_link TEXT,
  markdown_path TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS tools(
  id INTEGER PRIMARY KEY,
  answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  type TEXT NOT NULL,
  content TEXT,
  UNIQUE(answer_id,position)
);
CREATE TABLE IF NOT EXISTS tool_search_keywords(
  tool_id INTEGER NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  keyword TEXT NOT NULL,
  PRIMARY KEY(tool_id,position)
);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY CHECK(length(id)=20),
  url TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  platform TEXT NOT NULL,
  favicon TEXT,
  published_at TEXT,
  author TEXT,
  author_profile_link TEXT,
  cover_image TEXT,
  markdown_path TEXT,
  content_hash TEXT,
  UNIQUE(platform,url)
);
CREATE TABLE IF NOT EXISTS source_relations(
  id INTEGER PRIMARY KEY,
  answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  tool_id INTEGER REFERENCES tools(id) ON DELETE CASCADE,
  source_id TEXT REFERENCES sources(id),
  type TEXT NOT NULL CHECK(type IN('search_result','text_reference','image_reference','related_video')),
  position INTEGER NOT NULL,
  anchor_index INTEGER,
  anchor_text TEXT,
  image_url TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS suggested_questions(
  answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  question TEXT NOT NULL,
  PRIMARY KEY(answer_id,position)
);
CREATE TABLE IF NOT EXISTS source_metrics(
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  read_count INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  favorite_count INTEGER,
  share_count INTEGER,
  PRIMARY KEY(source_id,answer_id)
);
CREATE INDEX IF NOT EXISTS answers_question_time ON answers(question,finished_at);
CREATE INDEX IF NOT EXISTS source_relations_source ON source_relations(source_id);
CREATE INDEX IF NOT EXISTS source_relations_answer_type ON source_relations(answer_id,type);
CREATE INDEX IF NOT EXISTS tool_search_keywords_keyword ON tool_search_keywords(keyword);
CREATE INDEX IF NOT EXISTS source_metrics_source ON source_metrics(source_id);
"""
SOURCE_FIELDS = (
    "id", "url", "title", "summary", "platform", "favicon", "published_at",
    "author", "author_profile_link", "cover_image", "markdown_path", "content_hash",
)


def upsert_source(db: sqlite3.Connection, source: dict) -> None:
    old = db.execute("SELECT url,platform FROM sources WHERE id=?", (source["id"],)).fetchone()
    if old and old != (source["url"], source["platform"]):
        raise RuntimeError(f"source id collision: {source['id']}")
    values = [source.get(name) for name in SOURCE_FIELDS]
    db.execute(
        """INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          title=COALESCE(NULLIF(excluded.title,''),sources.title),
          summary=COALESCE(NULLIF(excluded.summary,''),sources.summary),
          favicon=COALESCE(NULLIF(excluded.favicon,''),sources.favicon),
          published_at=COALESCE(NULLIF(excluded.published_at,''),sources.published_at),
          author=COALESCE(NULLIF(excluded.author,''),sources.author),
          author_profile_link=COALESCE(NULLIF(excluded.author_profile_link,''),sources.author_profile_link),
          cover_image=COALESCE(NULLIF(excluded.cover_image,''),sources.cover_image),
          markdown_path=COALESCE(NULLIF(excluded.markdown_path,''),sources.markdown_path),
          content_hash=COALESCE(NULLIF(excluded.content_hash,''),sources.content_hash)""",
        values,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="把统一数据写入 GEOProMax SQLite 八表索引。")
    parser.add_argument("--input", required=True)
    parser.add_argument("--database", default=str(ROOT / "data/index/geopromax.sqlite"))
    args = parser.parse_args()
    data = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    path = Path(args.database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    batch = data["batch"]
    if db.execute("SELECT 1 FROM batches WHERE raw_file=?", (batch["raw_file"],)).fetchone():
        print(json.dumps({"status": "skipped", "raw_file": batch["raw_file"]}, ensure_ascii=False))
        db.close()
        return 0
    try:
        with db:
            for source in (data.get("sources") or {}).values():
                upsert_source(db, source)
            cursor = db.execute(
                """INSERT INTO batches(raw_file,input_file,output_file,app,channel,mode,new_context,status,started_at,finished_at,duration_seconds,total,succeeded,failed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch.get("raw_file"), batch.get("input_file"), batch.get("output_file"),
                    batch.get("app"), batch.get("channel"), batch.get("mode"), int(bool(batch.get("new_context"))),
                    batch.get("status"), batch.get("started_at"), batch.get("finished_at"),
                    batch.get("duration_seconds"), batch.get("total"), batch.get("succeeded"), batch.get("failed"),
                ),
            )
            batch_id = cursor.lastrowid
            for answer in data.get("answers") or []:
                answer_id = db.execute(
                    """INSERT INTO answers(batch_id,question,status,mode,new_context,started_at,finished_at,duration_seconds,share_link,markdown_path,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id, answer.get("question"), answer.get("status"), answer.get("mode"),
                        int(bool(answer.get("new_context"))),
                        answer.get("started_at"), answer.get("finished_at"), answer.get("duration_seconds"),
                        answer.get("share_link"), answer.get("markdown_path"), answer.get("error"),
                    ),
                ).lastrowid
                tool_ids = {}
                for tool in answer.get("tools") or []:
                    tool_id = db.execute(
                        "INSERT INTO tools(answer_id,position,type,content) VALUES(?,?,?,?)",
                        (answer_id, tool["position"], tool["type"], tool.get("content")),
                    ).lastrowid
                    tool_ids[tool["position"]] = tool_id
                    for position, keyword in enumerate(tool.get("keywords") or [], 1):
                        db.execute("INSERT INTO tool_search_keywords VALUES(?,?,?)", (tool_id, position, keyword))
                for position, question in enumerate(answer.get("suggested_questions") or [], 1):
                    db.execute("INSERT INTO suggested_questions VALUES(?,?,?)", (answer_id, position, question))
                for item in answer.get("relations") or []:
                    source_id = item.get("source_id")
                    db.execute(
                        """INSERT INTO source_relations(answer_id,tool_id,source_id,type,position,anchor_index,anchor_text,image_url,error)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            answer_id, tool_ids.get(item.get("tool_position")), source_id, item["type"], item["position"],
                            item.get("anchor_offset"), item.get("anchor_text"), item.get("image_url"), item.get("error"),
                        ),
                    )
                    metrics = item.get("metrics") or {}
                    if source_id and any(value is not None for value in metrics.values()):
                        db.execute(
                            """INSERT INTO source_metrics(source_id,answer_id,read_count,like_count,comment_count,favorite_count,share_count)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,answer_id) DO UPDATE SET
                              read_count=COALESCE(excluded.read_count,source_metrics.read_count),
                              like_count=COALESCE(excluded.like_count,source_metrics.like_count),
                              comment_count=COALESCE(excluded.comment_count,source_metrics.comment_count),
                              favorite_count=COALESCE(excluded.favorite_count,source_metrics.favorite_count),
                              share_count=COALESCE(excluded.share_count,source_metrics.share_count)""",
                            (source_id, answer_id, *(metrics.get(name) for name in ("read_count", "like_count", "comment_count", "favorite_count", "share_count"))),
                        )
        counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("batches", "answers", "tools", "tool_search_keywords", "sources", "source_relations", "suggested_questions", "source_metrics")}
        print(json.dumps({"status": "imported", "database": str(path), "counts": counts}, ensure_ascii=False))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
