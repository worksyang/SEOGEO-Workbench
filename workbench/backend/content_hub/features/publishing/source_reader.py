"""发布系统只读配置解析。

这里只读取 accounts.json 的公开投影字段；任何 profile/cookie/token 字段都不会
返回、写入数据库或进入日志。原发布系统目录本身永远不作为工作台写入目标。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...ingestion.source_manifests import manifest_id_for, manifest_ref
from ...validation.timestamps import utc_now_iso


@dataclass(frozen=True, slots=True)
class LegacyPublishAccount:
    account_id: str
    display_name: str
    source_index: int


@dataclass(frozen=True, slots=True)
class LegacyPublishConfig:
    accounts: tuple[LegacyPublishAccount, ...]
    manifest_id: str
    source_ref: str
    source_hash: str
    source_size_bytes: int
    captured_at: str


def read_legacy_accounts(path: Path) -> LegacyPublishConfig | None:
    """安全读取原系统 accounts.json，并生成不含绝对路径/凭据的 manifest。"""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return None
    raw_bytes = source.read_bytes()
    decoded = json.loads(raw_bytes.decode("utf-8"))
    raw_accounts = decoded.get("accounts") if isinstance(decoded, dict) else decoded
    if not isinstance(raw_accounts, list):
        raise ValueError("发布系统 accounts.json 必须包含 accounts 数组。")
    accounts: list[LegacyPublishAccount] = []
    for index, item in enumerate(raw_accounts):
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id", item.get("account_id"))
        raw_name = item.get("name", item.get("display_name"))
        if raw_id in (None, "") or not isinstance(raw_name, str) or not raw_name.strip():
            continue
        accounts.append(
            LegacyPublishAccount(
                account_id=f"legacy-{str(raw_id).strip()}",
                display_name=raw_name.strip(),
                source_index=index,
            )
        )
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    captured_at = utc_now_iso()
    entry = {
        "relative_path": "config/accounts.json",
        "content_hash": source_hash,
        "size_bytes": len(raw_bytes),
    }
    manifest_id = manifest_id_for(
        "publishing",
        {"source": "legacy_readonly", "file": "config/accounts.json"},
        [entry],
    )
    return LegacyPublishConfig(
        accounts=tuple(accounts),
        manifest_id=manifest_id,
        source_ref=manifest_ref("publishing", manifest_id, "config/accounts.json"),
        source_hash=source_hash,
        source_size_bytes=len(raw_bytes),
        captured_at=captured_at,
    )
