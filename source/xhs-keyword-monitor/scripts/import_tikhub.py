"""TikHub 数据导入主控 → normalized + raw。

第一阶段：所有 enabled/disabled 关键词（151）page1 general 20 条首抓；
即使 disabled 关键词也跑（确保 151 完整覆盖），后续 refresh 只跑 133 enabled。

raw 路径：data/raw/tikhub/xhs/<keyword_id>/<timestamp>.json
normalized 完全从 TikHub 数据重建；旧 RedFox normalized/*.json 在导入前备份到 .backup/<ts>/。

提供幂等 + 断点续跑：
- 事实表按稳定主键去重，重复执行不会重复追加 snapshot/hit/observation；
- 若启用跳过窗口，仅跳过网络请求，不伪造一条“当前时间”的历史快照。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.ingest.tikhub import (  # noqa: E402
    extract_notes_from_search_response,
    search_xhs_notes,
    TikHubError,
)


LOG = logging.getLogger("import_tikhub")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _now_compact() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _now_filename() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())


def _load_keywords(only_enabled: bool = False) -> list[dict]:
    cfg_path = PROJECT_ROOT / "data" / "config" / "keywords.json"
    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for g in payload.get("groups", []):
        for kw in g.get("keywords", []):
            if only_enabled and not kw.get("enabled", True):
                continue
            items.append({
                "keyword_id": kw["keyword_id"],
                "keyword_text": kw["keyword_text"],
                "group_id": g.get("group_id", ""),
                "group_label": g.get("label", ""),
                "enabled": kw.get("enabled", True),
            })
    return items


def _kw_raw_dir(kid: str) -> Path:
    """data/raw/tikhub/xhs/<keyword_id>/"""
    return PROJECT_ROOT / "data" / "raw" / "tikhub" / "xhs" / kid


def _save_raw(kid: str, payload: dict, page: int = 1) -> Path:
    raw_dir = _kw_raw_dir(kid)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{_now_filename()}_page_{page}.json"
    tmp = raw_path.with_suffix(raw_path.suffix + ".tmp")
    # 严格：顶层 envelope（不放 Authorization / token）
    safe = {k: v for k, v in payload.items() if k.lower() != "authorization"}
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(raw_path)
    return raw_path


def _project_relative(path: Path) -> str:
    """写入 normalized 的审计路径统一使用项目相对路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _has_recent_raw(kid: str, max_age_minutes: int = 60) -> bool:
    """断点续跑：若最近 60min 内已抓过则跳过。"""
    d = _kw_raw_dir(kid)
    if not d.exists():
        return False
    newest = None
    for p in d.glob("*.json"):
        if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
            newest = p
    if newest is None:
        return False
    age_min = (time.time() - newest.stat().st_mtime) / 60.0
    return age_min < max_age_minutes


def _backup_existing_normalized() -> None:
    """备份 RedFox 期间 normalized + raw，避免误覆盖。"""
    norm = PROJECT_ROOT / "normalized"
    if not norm.exists():
        return
    backup_dir = PROJECT_ROOT / f".backup/redfox_{_now_compact()}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for f in norm.glob("*.json"):
        shutil.copy(f, backup_dir / f.name)
    # 也备份 RedFox raw
    redfox_raw = PROJECT_ROOT / "data" / "raw" / "redfox"
    if redfox_raw.exists():
        rf_backup = PROJECT_ROOT / f".backup/redfox_raw_{_now_compact()}"
        rf_backup.mkdir(parents=True, exist_ok=True)
        shutil.copytree(redfox_raw, rf_backup / "redfox")
    LOG.info("backed up existing RedFox normalized to %s", backup_dir)


# ── 主流程 ────────────────────────────────────────────────────────────
def run(
    only_enabled: bool = False,
    page_size: int = 20,
    sort_type: str = "general",
    skip_recent_minutes: int = 60,
    backup: bool = True,
    rebuild_monitor: bool = True,
) -> int:
    if not Config.TIKHUB_API_TOKEN:
        LOG.error("TIKHUB_API_TOKEN 未设置；拒绝执行。")
        return 2

    if backup:
        _backup_existing_normalized()

    items = _load_keywords(only_enabled=only_enabled)
    LOG.info("importing %d keywords (page1, sort_type=%s)", len(items), sort_type)

    envelopes: list = []
    fail_records: list[dict] = []
    raw_records: list[dict] = []
    success_count = 0
    fail_count = 0
    skipped_count = 0

    failures_path = PROJECT_ROOT / "data" / "state" / "tikhub_import_failures.jsonl"
    failures_path.parent.mkdir(parents=True, exist_ok=True)

    for idx, item in enumerate(items, start=1):
        kid = item["keyword_id"]
        ktext = item["keyword_text"]
        if _has_recent_raw(kid, max_age_minutes=skip_recent_minutes):
            skipped_count += 1
            # 已抓取 raw 对应的 normalized 快照不应被“现在”的时间重新解释；
            # 跳过只影响网络调用，重建阶段仍会读取已有 normalized 历史。
            continue

        try:
            captured_at = _now()
            raw = search_xhs_notes(
                keyword=ktext, page=1, sort_type=sort_type,
            )
            raw_path = _save_raw(kid, raw, page=1)
            env = extract_notes_from_search_response(raw, keyword=ktext, captured_at=captured_at)
            env.raw_file_path = _project_relative(raw_path)
            envelopes.append(env)
            raw_records.append({
                "keyword_id": kid,
                "keyword_text": ktext,
                "captured_at": captured_at,
                "raw_file_path": env.raw_file_path,
                "result_count": env.result_count,
            })
            success_count += 1
            if idx % 10 == 0:
                LOG.info("[%d/%d] success=%d skip=%d fail=%d", idx, len(items), success_count, skipped_count, fail_count)
            time.sleep(Config.TIKHUB_INTER_REQUEST_DELAY)
        except TikHubError as e:
            fail_count += 1
            with failures_path.open("a") as fp:
                fp.write(json.dumps({
                    "keyword_id": kid,
                    "keyword_text": ktext,
                    "stage": "search_notes",
                    "error": str(e)[:300],
                    "captured_at": _now(),
                }, ensure_ascii=False) + "\n")
            fail_records.append({"keyword_id": kid, "error": str(e)})
        except Exception as e:
            fail_count += 1
            with failures_path.open("a") as fp:
                fp.write(json.dumps({
                    "keyword_id": kid,
                    "keyword_text": ktext,
                    "stage": "search_notes_unexpected",
                    "error": f"{type(e).__name__}: {e}"[:300],
                    "captured_at": _now(),
                }, ensure_ascii=False) + "\n")

    LOG.info("capture done: success=%d fail=%d skip=%d total_envelopes=%d",
             success_count, fail_count, skipped_count, len(envelopes))

    # ── 写 normalized（upsert） ──
    if envelopes:
        from app.ingest.builders.entity_builder import build_entities
        entities = build_entities(envelopes)
        upsert_stats = _upsert_normalized(entities)
        LOG.info("normalized: keywords=%d snapshots=%d articles=%d accounts=%d hits=%d obs=%d",
                 len(entities["keywords"]), len(entities["snapshots"]), len(entities["articles"]),
                 len(entities["accounts"]), len(entities["ranking_hits"]), len(entities["note_metric_observations"]))
    else:
        upsert_stats = {}

    # ── rebuild monitor ──
    if rebuild_monitor:
        from app.ingest.rebuild import rebuild_all
        m = rebuild_all(verbose=False)
        LOG.info("monitor-data rebuilt: keywords=%d accounts=%d", len(m["keywords"]), len(m["accounts"]))

    # 每次运行留下可审计结果；不记录鉴权信息或完整 API 响应。
    report = {
        "provider": "tikhub_xhs",
        "triggered_at": _now(),
        "only_enabled": bool(only_enabled),
        "page": 1,
        "sort_type": sort_type,
        "total_keywords": len(items),
        "success_count": success_count,
        "failure_count": fail_count,
        "skipped_count": skipped_count,
        "raw_records": raw_records,
        "failures": fail_records,
        "normalized_delta": upsert_stats,
    }
    report_path = PROJECT_ROOT / "data" / "state" / f"tikhub_import_{_now_compact()}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("import report: %s", report_path)

    return 0 if fail_count == 0 else 1


def _upsert_normalized(entities: dict) -> dict[str, dict[str, int]]:
    """Upsert 当前实体，并对历史事实表按稳定主键幂等追加。

    snapshot/hit/observation 均是不可变事实。重复 import 只会跳过同主键，
    不会产生重复行；若同一主键来自不同 provider，则拒绝混入而不是静默覆盖。
    """
    norm = PROJECT_ROOT / "normalized"
    norm.mkdir(parents=True, exist_ok=True)
    stats: dict[str, dict[str, int]] = {}

    def upsert(fn, items, key):
        existing = json.loads((norm / fn).read_text(encoding="utf-8")) if (norm / fn).exists() else []
        if not isinstance(existing, list):
            existing = []
        by_key = {it.get(key): it for it in existing if it.get(key)}
        for it in items:
            k = it.get(key)
            if k is None:
                continue
            cur = by_key.get(k)
            if cur is None:
                by_key[k] = it
            else:
                merged = {**cur, **it}
                cur_pp = cur.get("platform_payload") or {}
                new_pp = it.get("platform_payload") or {}
                if cur_pp or new_pp:
                    merged["platform_payload"] = {**cur_pp, **new_pp}
                by_key[k] = merged
        (norm / fn).write_text(
            json.dumps(list(by_key.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stats[fn] = {"inserted": sum(1 for it in items if it.get(key) not in {x.get(key) for x in existing}), "skipped": 0}

    def append_unique(fn, items, key, provider_field: str | None = None):
        existing = json.loads((norm / fn).read_text(encoding="utf-8")) if (norm / fn).exists() else []
        if not isinstance(existing, list):
            existing = []
        by_key = {it.get(key): it for it in existing if it.get(key)}
        inserted = 0
        skipped = 0
        for item in items:
            item_key = item.get(key)
            if not item_key:
                continue
            prior = by_key.get(item_key)
            if prior is None:
                existing.append(item)
                by_key[item_key] = item
                inserted += 1
                continue
            # 同一事实主键被不同 provider 占用时必须人工处理，禁止跨源覆盖。
            if provider_field and prior.get(provider_field) and item.get(provider_field) and prior.get(provider_field) != item.get(provider_field):
                raise RuntimeError(
                    f"{fn} 主键冲突且 provider 不同: {item_key} "
                    f"({prior.get(provider_field)} != {item.get(provider_field)})"
                )
            skipped += 1
        (norm / fn).write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stats[fn] = {"inserted": inserted, "skipped": skipped}

    upsert("keywords.json", entities["keywords"], "keyword_id")
    upsert("accounts.json", entities["accounts"], "account_id")
    upsert("articles.json", entities["articles"], "article_id")
    append_unique("snapshots.json", entities.get("snapshots", []) or [], "snapshot_id", "source_name")
    append_unique("snapshot_terms.json", entities.get("snapshot_terms", []) or [], "term_id")
    append_unique("ranking_hits.json", entities.get("ranking_hits", []) or [], "hit_id", "source")
    append_unique(
        "note_metric_observations.json",
        entities.get("note_metric_observations", []) or [],
        "observation_id",
        "source",
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-enabled", action="store_true",
                        help="只跑 enabled 关键词；默认全 151（含 disabled）")
    parser.add_argument("--sort-type", default="general")
    parser.add_argument("--skip-recent-minutes", type=int, default=60,
                        help="断点续跑窗口（默认 60min 内已抓过的跳过）")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--no-rebuild", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return run(
        only_enabled=args.only_enabled,
        sort_type=args.sort_type,
        skip_recent_minutes=args.skip_recent_minutes,
        backup=not args.no_backup,
        rebuild_monitor=not args.no_rebuild,
    )


if __name__ == "__main__":
    sys.exit(main())
