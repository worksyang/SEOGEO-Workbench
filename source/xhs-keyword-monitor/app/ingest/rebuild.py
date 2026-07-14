"""rebuild_all — 读取 raw + normalized，生成新的 monitor-data + 清缓存。

事实层文件：
  normalized/{keywords,snapshots,snapshot_terms,accounts,articles,ranking_hits,note_metric_observations}.json

派生层入口：
  build_monitor_data(ent, keyword_settings) → monitor-data.json
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        LOG.warning("[rebuild] failed to read %s: %s", path, e)
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )



def _merge_tikhub_user_cache(accounts: list[dict], users_dir: Path) -> int:
    """Merge TikHub user cache (data/raw/tikhub/xhs/users/*.json) into accounts.

    Returns the number of accounts updated.
    """
    if not users_dir.exists():
        return 0
    updated = 0
    for acct in accounts:
        uid = acct.get("account_id", "")
        if not uid:
            continue
        cache_path = users_dir / f"{uid}.json"
        if not cache_path.exists():
            continue
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        inner = raw.get("data")
        if not isinstance(inner, dict):
            continue
        user_data = inner.get("data")
        if not isinstance(user_data, dict):
            continue
        # Map TikHub fields → accounts.json fields
        headimg = None
        imgs = user_data.get("images")
        if isinstance(imgs, dict):
            headimg = imgs.get("large") or imgs.get("medium") or imgs.get("small") or imgs.get("url")
        elif isinstance(imgs, str):
            headimg = imgs
        elif isinstance(imgs, list) and imgs:
            first = imgs[0]
            if isinstance(first, dict):
                headimg = first.get("url") or first.get("large")
            elif isinstance(first, str):
                headimg = first

        note_num_stat = user_data.get("note_num_stat") or {}
        if not isinstance(note_num_stat, dict):
            note_num_stat = {}

        if headimg is not None:
            acct["headimg_url"] = str(headimg)
        if user_data.get("desc") is not None:
            acct["description"] = str(user_data["desc"])
        if user_data.get("fans") is not None:
            acct["fans"] = int(user_data["fans"])
        if user_data.get("ip_location") is not None:
            acct["ip_location"] = str(user_data["ip_location"])
        if note_num_stat.get("posted") is not None:
            acct["total_works"] = int(note_num_stat["posted"])
        if note_num_stat.get("liked") is not None:
            acct["likes"] = int(note_num_stat["liked"])
        if note_num_stat.get("collected") is not None:
            acct["collects"] = int(note_num_stat["collected"])
        if user_data.get("follows") is not None:
            acct["follows"] = int(user_data["follows"])
        verify_content = user_data.get("red_official_verify_content")
        if verify_content:
            acct["verify_info"] = str(verify_content)
        red_id = user_data.get("red_id")
        if red_id:
            pp = acct.setdefault("platform_payload", {})
            pp["red_id"] = str(red_id)
        updated += 1
    return updated
def rebuild_all(normalized_dir: Path | None = None, keyword_settings: dict | None = None, verbose: bool = True) -> dict:
    from app.config import Config

    normalized_dir = Path(normalized_dir or Config.NORMALIZED_DIR)
    if keyword_settings is None:
        try:
            from app.repositories.keyword_settings_repo import KeywordSettingsRepository
            settings_repo = KeywordSettingsRepository(Config.SQLITE_PATH)
            keyword_settings = settings_repo.list_all()
        except Exception as e:
            LOG.warning("[rebuild] cannot load keyword settings: %s", e)
            keyword_settings = {}

    # 1. 读事实层
    ent = {
        "keywords": _read_json(normalized_dir / "keywords.json", []),
        "snapshots": _read_json(normalized_dir / "snapshots.json", []),
        "snapshot_terms": _read_json(normalized_dir / "snapshot_terms.json", []),
        "accounts": _read_json(normalized_dir / "accounts.json", []),
        "articles": _read_json(normalized_dir / "articles.json", []),
        "ranking_hits": _read_json(normalized_dir / "ranking_hits.json", []),
        "note_metric_observations": _read_json(normalized_dir / "note_metric_observations.json", []),
    }

    # 1b. 合并 TikHub 博主缓存（data/raw/tikhub/xhs/users/*.json）到 accounts
    merged = _merge_tikhub_user_cache(ent["accounts"], Config.RAW_DIR / "users")
    if merged:
        _write_json(normalized_dir / "accounts.json", ent["accounts"])
        if verbose:
            print(f"[rebuild] merged TikHub user cache: {merged} accounts enriched", file=sys.stderr)

    # 2. 派生层
    from app.ingest.builders.monitor_builder import build_monitor_data

    payload = build_monitor_data(ent, keyword_settings)
    _write_json(normalized_dir / "monitor-data.json", payload)

    # 3. 通知 article_list_service 缓存失效
    try:
        from app.services import article_list_service
        article_list_service.invalidate_cache()
    except Exception:
        pass

    if verbose:
        print(
            f"[rebuild] keywords={len(payload['keywords'])} accounts={len(payload['accounts'])} "
            f"snapshots={len(ent['snapshots'])} articles={len(ent['articles'])} "
            f"hits={len(ent['ranking_hits'])}",
            file=sys.stderr,
        )

    return payload
