from __future__ import annotations

from collections import defaultdict

from app.ingest.common import (
    TZ,
    acct_id,
    art_id,
    article_identity_key,
    infer_trigger_type,
    is_placeholder_url,
    kw_id,
    normalize_url,
    now_iso,
    parse_captured_at_iso,
    parse_published_at,
    project_display_path,
    snap_id,
    to_iso,
)
from app.ingest.content.article_content_indexer import index_article_files
from app.ingest.content.article_content_matcher import find_content_for_hit
from app.ingest.content.metrics_parser import extract_metrics
from app.ingest.parsers.search_md_parser import ParsedHit, ParsedSnapshot


def build_entities(snapshots: list[ParsedSnapshot]) -> dict:
    snapshots = sorted(snapshots, key=lambda s: (s.keyword_text, s.captured_at))

    keywords_map: dict[str, dict] = {}
    accounts_map: dict[str, dict] = {}
    articles_map: dict[str, dict] = {}
    snapshots_out: list[dict] = []
    snapshot_terms_out: list[dict] = []
    ranking_hits_out: list[dict] = []

    by_kw_day: dict[tuple[str, str], list[ParsedSnapshot]] = defaultdict(list)
    for s in snapshots:
        by_kw_day[(s.keyword_text, s.captured_at.date().isoformat())].append(s)

    primary_set: set[tuple[str, object]] = set()
    for _, group in by_kw_day.items():
        scheduled = [s for s in group if infer_trigger_type(s.captured_at) == "scheduled"]
        if scheduled:
            pick = min(scheduled, key=lambda s: abs(s.captured_at.hour * 60 + s.captured_at.minute - 480))
        else:
            pick = min(group, key=lambda s: s.captured_at)
        primary_set.add((pick.keyword_text, pick.captured_at))

    article_index = index_article_files()
    recorded_at = now_iso()

    for snap in snapshots:
        kid = kw_id(snap.keyword_text)
        kentry = keywords_map.setdefault(kid, {
            "keyword_id": kid,
            "keyword_text": snap.keyword_text,
            "is_active": True,
            "notes": None,
            "first_seen_at": to_iso(snap.captured_at),
            "last_seen_at": to_iso(snap.captured_at),
            "snapshot_count": 0,
        })
        if snap.captured_at < parse_captured_at_iso(kentry["first_seen_at"]):
            kentry["first_seen_at"] = to_iso(snap.captured_at)
        if snap.captured_at > parse_captured_at_iso(kentry["last_seen_at"]):
            kentry["last_seen_at"] = to_iso(snap.captured_at)
        kentry["snapshot_count"] += 1

        sid = snap_id(kid, snap.captured_at)
        snapshots_out.append({
            "snapshot_id": sid,
            "keyword_id": kid,
            "captured_at": to_iso(snap.captured_at),
            "snapshot_date": snap.captured_at.date().isoformat(),
            "snapshot_time": snap.captured_at.strftime("%H:%M"),
            "timezone": TZ,
            "trigger_type": infer_trigger_type(snap.captured_at),
            "is_primary": (snap.keyword_text, snap.captured_at) in primary_set,
            "status": "success",
            "result_count": len(snap.hits),
            "result_limit": None,
            "source_name": "wechat_search_md",
            "source_version": "v1",
            "source_file_path": project_display_path(snap.source_file_path),
            "recorded_at": recorded_at,
        })

        for i, t in enumerate(snap.suggestions, 1):
            snapshot_terms_out.append({
                "term_id": f"term_{sid}_suggestion_{i}",
                "snapshot_id": sid,
                "term_type": "suggestion",
                "position": i,
                "term_text": t,
            })
        for i, t in enumerate(snap.related, 1):
            snapshot_terms_out.append({
                "term_id": f"term_{sid}_related_{i}",
                "snapshot_id": sid,
                "term_type": "related",
                "position": i,
                "term_text": t,
            })

        for hit in snap.hits:
            aid = acct_id(hit.account_name_raw)
            aentry = accounts_map.setdefault(aid, {
                "account_id": aid,
                "canonical_name": hit.account_name_raw,
                "first_seen_at": to_iso(snap.captured_at),
                "last_seen_at": to_iso(snap.captured_at),
                "is_focus": False,
                "notes": None,
                "wechat_biz": None,
                "headimg_url": None,
            })
            if snap.captured_at < parse_captured_at_iso(aentry["first_seen_at"]):
                aentry["first_seen_at"] = to_iso(snap.captured_at)
            if snap.captured_at > parse_captured_at_iso(aentry["last_seen_at"]):
                aentry["last_seen_at"] = to_iso(snap.captured_at)

            article_key = article_identity_key(hit.account_name_raw, hit.title_raw)
            artid = art_id(article_key)
            pub_dt = parse_published_at(hit.published_at_raw or "")
            artentry = articles_map.get(artid)
            if not artentry:
                cpath = find_content_for_hit(hit, article_index)
                m = extract_metrics(cpath) if cpath else None
                artentry = {
                    "article_id": artid,
                    "normalized_url": normalize_url(hit.url_raw),
                    "raw_url": hit.url_raw,
                    "title": hit.title_raw,
                    "account_id": aid,
                    "published_at": to_iso(pub_dt) if pub_dt else None,
                    "summary": hit.summary_raw,
                    "first_seen_at": to_iso(snap.captured_at),
                    "last_seen_at": to_iso(snap.captured_at),
                    "content_status": "available" if cpath else "missing",
                    "content_file_path": project_display_path(cpath) if cpath else None,
                    "cover_url": None,
                    "read_count": m.read_count if m else None,
                    "like_count": m.like_count if m else None,
                    "friends_follow_count": m.friends_follow_count if m else None,
                    "original_article_count": m.original_article_count if m else None,
                }
                articles_map[artid] = artentry
            else:
                artentry["title"] = hit.title_raw
                artentry["summary"] = hit.summary_raw or artentry["summary"]
                if artentry["published_at"] is None and pub_dt:
                    artentry["published_at"] = to_iso(pub_dt)
                if is_placeholder_url(artentry["raw_url"]) and not is_placeholder_url(hit.url_raw):
                    artentry["raw_url"] = hit.url_raw
                    artentry["normalized_url"] = normalize_url(hit.url_raw)
                if snap.captured_at < parse_captured_at_iso(artentry["first_seen_at"]):
                    artentry["first_seen_at"] = to_iso(snap.captured_at)
                if snap.captured_at > parse_captured_at_iso(artentry["last_seen_at"]):
                    artentry["last_seen_at"] = to_iso(snap.captured_at)
                cpath = find_content_for_hit(hit, article_index)
                if cpath and (artentry["content_status"] == "missing" or not is_placeholder_url(hit.url_raw)):
                    artentry["content_status"] = "available"
                    artentry["content_file_path"] = project_display_path(cpath)
                    m = extract_metrics(cpath)
                    artentry["read_count"] = m.read_count
                    artentry["like_count"] = m.like_count
                    artentry["friends_follow_count"] = m.friends_follow_count
                    artentry["original_article_count"] = m.original_article_count

            ranking_hits_out.append({
                "hit_id": f"hit_{sid}_{hit.rank}",
                "snapshot_id": sid,
                "rank": hit.rank,
                "article_id": artid,
                "account_id": aid,
                "title_raw": hit.title_raw,
                "summary_raw": hit.summary_raw,
                "account_name_raw": hit.account_name_raw,
                "published_at_raw": hit.published_at_raw,
                "url_raw": hit.url_raw,
                "created_at": recorded_at,
            })

    return {
        "keywords": list(keywords_map.values()),
        "snapshots": snapshots_out,
        "snapshot_terms": snapshot_terms_out,
        "accounts": list(accounts_map.values()),
        "articles": list(articles_map.values()),
        "ranking_hits": ranking_hits_out,
    }
