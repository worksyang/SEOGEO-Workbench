#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
TRUSTED_STATUSES = {"rpa_confirmed_alias", "strong_alias", "likely_alias"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: str, b: str) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_identity_groups(accounts: list[dict[str, Any]], aliases: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {a["account_id"]: a for a in accounts}
    uf = UnionFind()
    for account_id in by_id:
        uf.find(account_id)
    for alias in aliases.get("aliases", []):
        if alias.get("status") in TRUSTED_STATUSES:
            source_id = alias.get("source_account_id")
            target_id = alias.get("target_account_id")
            if source_id in by_id and target_id in by_id:
                uf.union(source_id, target_id)

    grouped: dict[str, list[str]] = defaultdict(list)
    for account_id in by_id:
        grouped[uf.find(account_id)].append(account_id)

    groups = []
    for ids in grouped.values():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda aid: by_id[aid].get("first_seen_at") or "")
        canonical_id = max(ids, key=lambda aid: by_id[aid].get("last_seen_at") or "")
        names = []
        for aid in ids:
            name = by_id[aid].get("canonical_name") or aid
            if name not in names:
                names.append(name)
        groups.append({
            "logical_account_id": "logical_" + "_".join(ids[:2]),
            "canonical_account_id": canonical_id,
            "canonical_account_name": by_id[canonical_id].get("canonical_name") or canonical_id,
            "account_ids": ids,
            "account_names": names,
            "historical_aliases": ",".join(names),
            "first_seen_at": min((by_id[aid].get("first_seen_at") or "" for aid in ids), default=None),
            "last_seen_at": max((by_id[aid].get("last_seen_at") or "" for aid in ids), default=None),
        })
    groups.sort(key=lambda g: (g["last_seen_at"] or "", len(g["account_ids"])), reverse=True)
    return groups


def enrich_accounts(accounts: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_member: dict[str, dict[str, Any]] = {}
    for group in groups:
        for account_id in group["account_ids"]:
            by_member[account_id] = group

    enriched = []
    for account in accounts:
        item = dict(account)
        group = by_member.get(item.get("account_id"))
        if group:
            item["logical_account_id"] = group["logical_account_id"]
            item["canonical_account_id"] = group["canonical_account_id"]
            item["canonical_account_name"] = group["canonical_account_name"]
            item["historical_aliases"] = group["historical_aliases"]
            item["is_canonical_alias_account"] = item.get("account_id") == group["canonical_account_id"]
        else:
            item.setdefault("historical_aliases", item.get("canonical_name") or "")
        enriched.append(item)
    return enriched


def build_clean_rankings(monitor: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
    alias_member_ids = {aid for group in groups for aid in group["account_ids"]}
    alias_canonical_ids = {group["canonical_account_id"] for group in groups}
    alias_by_canonical = {group["canonical_account_id"]: group for group in groups}

    accounts = monitor.get("accounts", [])
    account_by_id = {a.get("account_id"): a for a in accounts}

    polluted_alias_accounts = []
    for canonical_id in alias_canonical_ids:
        row = account_by_id.get(canonical_id)
        group = alias_by_canonical[canonical_id]
        if not row:
            continue
        polluted_alias_accounts.append({
            "name": row.get("name"),
            "account_id": canonical_id,
            "score": row.get("score"),
            "timeliness_score": row.get("timeliness_score"),
            "hit_days": row.get("hit_days"),
            "current_streak": row.get("current_streak"),
            "historical_aliases": group["historical_aliases"],
            "reason": "改名/身份延续账号，不应视为纯新黑马",
        })

    clean_accounts = [a for a in accounts if a.get("account_id") not in alias_member_ids]

    # 黑马口径：总分不高但时效分/连击强，用来找“近期冒头”的纯新势力。
    blackhorse = [
        a for a in clean_accounts
        if (a.get("score") or 0) <= 120
        and (a.get("timeliness_score") or 0) >= 8
        and (a.get("current_streak") or 0) >= 2
    ]
    blackhorse.sort(
        key=lambda a: (-(a.get("timeliness_score") or 0), -(a.get("current_streak") or 0), -(a.get("score") or 0))
    )

    # 上升榜口径：关键词新命中/上升多，且剔除 alias 身份延续。
    rising = []
    for a in clean_accounts:
        move = a.get("move_summary") or {}
        lift = int(move.get("new_count") or 0) * 2 + int(move.get("up_count") or 0)
        if lift <= 0:
            continue
        rising.append({**a, "rise_signal": lift})
    rising.sort(
        key=lambda a: (-a["rise_signal"], -(a.get("timeliness_score") or 0), -(a.get("score") or 0))
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": {
            "blackhorse": "剔除可信 alias 后，score<=120、timeliness_score>=8、current_streak>=2，按时效分排序。",
            "rising": "剔除可信 alias 后，rise_signal=new_count*2+up_count，按 rise_signal/时效分排序。",
        },
        "alias_groups": groups,
        "removed_alias_accounts": polluted_alias_accounts,
        "blackhorse_clean": [
            pick_account_fields(a) for a in blackhorse[:50]
        ],
        "rising_clean": [
            {**pick_account_fields(a), "rise_signal": a.get("rise_signal")}
            for a in rising[:50]
        ],
    }


def pick_account_fields(a: dict[str, Any]) -> dict[str, Any]:
    move = a.get("move_summary") or {}
    return {
        "name": a.get("name"),
        "account_id": a.get("account_id"),
        "score": a.get("score"),
        "timeliness_score": a.get("timeliness_score"),
        "hit_days": a.get("hit_days"),
        "current_streak": a.get("current_streak"),
        "article_count": a.get("article_count"),
        "new_count": move.get("new_count"),
        "up_count": move.get("up_count"),
        "down_count": move.get("down_count"),
        "primary_type": move.get("primary_type"),
        "primary_count": move.get("primary_count"),
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 账号身份归并与清洗榜单",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 可信身份组：{len(payload['alias_groups'])}",
        "",
        "## 匹配机制",
        "",
        "- 强特征：相同真实文章 URL、相同头像 URL、RPA 搜标题命中到兄弟账号。",
        "- 中强特征：多个相同标题在旧号消失、新号出现后延续，并且排名连续。",
        "- 当前未稳定入库：公众号原始 ID、微信号、认证主体名称；暂不能作为自动匹配依据。",
        "",
        "## 被剔除的假黑马/身份延续账号",
        "",
        "| 当前账号 | 分数 | 时效分 | 连击 | 历史名字 | 原因 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in payload["removed_alias_accounts"]:
        lines.append(
            f"| {row['name']} | {row.get('score')} | {row.get('timeliness_score')} | "
            f"{row.get('current_streak')} | {row.get('historical_aliases')} | {row.get('reason')} |"
        )
    lines.extend([
        "",
        "## 清洗后黑马榜 Top 20",
        "",
        "| 账号 | 分数 | 时效分 | 连击 | 命中天数 | 新命中 | 上升 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in payload["blackhorse_clean"][:20]:
        lines.append(
            f"| {row['name']} | {row.get('score')} | {row.get('timeliness_score')} | "
            f"{row.get('current_streak')} | {row.get('hit_days')} | {row.get('new_count')} | {row.get('up_count')} |"
        )
    lines.extend([
        "",
        "## 清洗后上升榜 Top 20",
        "",
        "| 账号 | 上升信号 | 分数 | 时效分 | 新命中 | 上升 | 连击 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in payload["rising_clean"][:20]:
        lines.append(
            f"| {row['name']} | {row.get('rise_signal')} | {row.get('score')} | {row.get('timeliness_score')} | "
            f"{row.get('new_count')} | {row.get('up_count')} | {row.get('current_streak')} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    accounts_path = ROOT / "normalized" / "accounts.json"
    aliases_path = ROOT / "normalized" / "account_aliases.json"
    monitor_path = ROOT / "normalized" / "monitor-data.json"

    accounts = load_json(accounts_path)
    aliases = load_json(aliases_path)
    monitor = load_json(monitor_path)

    groups = build_identity_groups(accounts, aliases)
    enriched_accounts = enrich_accounts(accounts, groups)
    dump_json(accounts_path, enriched_accounts)

    rankings = build_clean_rankings(monitor, groups)
    out_json = ROOT / "normalized" / "clean_account_rankings.json"
    out_md = ROOT / "历史记录" / "260704_执行记录_账号身份归并与清洗榜单.md"
    dump_json(out_json, rankings)
    out_md.write_text(render_report(rankings), encoding="utf-8")

    print(f"[ok] updated {accounts_path}")
    print(f"[ok] wrote {out_json}")
    print(f"[ok] wrote {out_md}")
    print(f"[ok] alias_groups={len(groups)} removed_alias_accounts={len(rankings['removed_alias_accounts'])}")


if __name__ == "__main__":
    main()
