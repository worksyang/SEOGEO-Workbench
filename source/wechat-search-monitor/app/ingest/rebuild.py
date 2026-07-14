from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from app.ingest.builders.entity_builder import build_entities
from app.ingest.builders.monitor_builder import build_monitor_data
from app.ingest.common import NORMALIZED_DIR
from app.ingest.parsers.search_md_parser import discover_snapshots
from app.repositories.keyword_registry_repo import KeywordRegistryRepository
from app.config import Config


STATE_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "state" / "app.db"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rebuild_all(verbose: bool = True, full: bool = False) -> dict:
    snapshots = discover_snapshots(incremental=not full)
    if not snapshots:
        raise RuntimeError("no search-result markdown found")

    if verbose:
        print(f"[ok] parsed {len(snapshots)} snapshot(s)")
        by_kw = defaultdict(int)
        for s in snapshots:
            by_kw[s.keyword_text] += 1
        for k, v in by_kw.items():
            print(f"      - {k}: {v}")

    ent = build_entities(snapshots)
    registry_repo = KeywordRegistryRepository(STATE_DB_PATH)

    # 关键词黑名单：从 rebuild 结果中排除命中黑名单的关键词及其关联数据
    blocklist_path = Config.KEYWORD_BLOCKLIST_FILE
    blocked_kw_texts: set[str] = set()
    if blocklist_path.exists():
        try:
            block_cfg = json.loads(blocklist_path.read_text(encoding="utf-8"))
            blocked_kw_texts = {k for k in block_cfg.get("blocked_keywords", []) if k}
        except Exception:
            pass
    if blocked_kw_texts:
        blocked_kw_ids = {k["keyword_id"] for k in ent["keywords"] if k["keyword_text"] in blocked_kw_texts}
        ent["keywords"] = [k for k in ent["keywords"] if k["keyword_id"] not in blocked_kw_ids]
        ent["snapshots"] = [s for s in ent["snapshots"] if s["keyword_id"] not in blocked_kw_ids]
        kept_snap_ids = {s["snapshot_id"] for s in ent["snapshots"]}
        ent["snapshot_terms"] = [t for t in ent["snapshot_terms"] if t["snapshot_id"] in kept_snap_ids]
        ent["ranking_hits"] = [h for h in ent["ranking_hits"] if h["snapshot_id"] in kept_snap_ids]
        kept_hit_ids = {h["hit_id"] for h in ent["ranking_hits"]}
        ent["articles"] = [a for a in ent["articles"] if a["article_id"] in {h["article_id"] for h in ent["ranking_hits"]}]
        ent["accounts"] = [a for a in ent["accounts"] if a["account_id"] in {h["account_id"] for h in ent["ranking_hits"]}]
        if verbose:
            print(f"[blocklist] excluded {len(blocked_kw_ids)} keyword(s): {', '.join(sorted(blocked_kw_texts))}")

    registry_repo.sync_observations(ent["keywords"])
    keyword_settings = registry_repo.list_settings()

    # 写入 articles.json 前，从旧文件继承已有的 cover_url（rebuild 不应丢失已抓取的封面）
    articles_path = NORMALIZED_DIR / "articles.json"
    old_cover_map: dict[str, str | None] = {}
    if articles_path.exists():
        try:
            old_articles = json.loads(articles_path.read_text(encoding="utf-8"))
            for a in old_articles:
                if isinstance(a, dict) and a.get("article_id") and a.get("cover_url") is not None:
                    old_cover_map[a["article_id"]] = a["cover_url"]
        except Exception:
            pass
    if old_cover_map:
        for art in ent["articles"]:
            inherited = old_cover_map.get(art.get("article_id", ""))
            if inherited is not None and art.get("cover_url") is None:
                art["cover_url"] = inherited

    # 继承已有的 headimg_url（rebuild 不应丢失已抓取的公众号头像）
    accounts_path = NORMALIZED_DIR / "accounts.json"
    old_headimg_map: dict[str, str | None] = {}
    if accounts_path.exists():
        try:
            old_accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
            for a in old_accounts:
                if isinstance(a, dict) and a.get("account_id") and a.get("headimg_url") is not None:
                    old_headimg_map[a["account_id"]] = a["headimg_url"]
        except Exception:
            pass
    if old_headimg_map:
        for acct in ent["accounts"]:
            inherited = old_headimg_map.get(acct.get("account_id", ""))
            if inherited is not None and acct.get("headimg_url") is None:
                acct["headimg_url"] = inherited

    write_json(NORMALIZED_DIR / "snapshots.json", ent["snapshots"])
    write_json(NORMALIZED_DIR / "snapshot_terms.json", ent["snapshot_terms"])
    write_json(NORMALIZED_DIR / "accounts.json", ent["accounts"])
    write_json(NORMALIZED_DIR / "articles.json", ent["articles"])
    write_json(NORMALIZED_DIR / "ranking_hits.json", ent["ranking_hits"])

    active_keyword_ids = registry_repo.active_keyword_ids()
    configured_active_count = len(active_keyword_ids)
    monitor_snapshots = [
        item for item in ent["snapshots"]
        if item["keyword_id"] in active_keyword_ids
    ]
    monitor_snapshot_ids = {item["snapshot_id"] for item in monitor_snapshots}
    monitor_hits = [
        item for item in ent["ranking_hits"]
        if item["snapshot_id"] in monitor_snapshot_ids
    ]
    monitor_hit_article_ids = {item["article_id"] for item in monitor_hits}
    monitor_hit_account_ids = {item["account_id"] for item in monitor_hits}
    monitor_ent = {
        **ent,
        "keywords": [
            item for item in ent["keywords"]
            if item["keyword_id"] in active_keyword_ids
        ],
        "snapshots": monitor_snapshots,
        "snapshot_terms": [
            item for item in ent["snapshot_terms"]
            if item["snapshot_id"] in monitor_snapshot_ids
        ],
        "ranking_hits": monitor_hits,
        "articles": [
            item for item in ent["articles"]
            if item["article_id"] in monitor_hit_article_ids
        ],
        "accounts": [
            item for item in ent["accounts"]
            if item["account_id"] in monitor_hit_account_ids
        ],
    }
    monitor = build_monitor_data(monitor_ent, keyword_settings=keyword_settings)
    write_json(NORMALIZED_DIR / "monitor-data.json", monitor)

    if verbose:
        print(
            f"[ok] configured_active_keywords={configured_active_count} "
            f"observed_active_keywords={len(monitor_ent['keywords'])} "
            f"observed_keywords={len(ent['keywords'])} accounts={len(ent['accounts'])} "
            f"articles={len(ent['articles'])} hits={len(ent['ranking_hits'])}"
        )
        print("[ok] wrote normalized facts + monitor-data.json")

    return {
        "snapshots": len(snapshots),
        "keywords": configured_active_count,
        "observed_active_keywords": len(monitor_ent["keywords"]),
        "observed_keywords": len(ent["keywords"]),
        "accounts": len(ent["accounts"]),
        "articles": len(ent["articles"]),
        "hits": len(ent["ranking_hits"]),
    }


def main(full: bool = False) -> int:
    try:
        rebuild_all(verbose=True, full=full)
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0
