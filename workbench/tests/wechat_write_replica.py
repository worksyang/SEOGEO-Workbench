from __future__ import annotations

import importlib
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from flask import Flask

from content_hub.config import Settings
from content_hub.db.connection import connect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FREEZE_ROOT = (
    PROJECT_ROOT
    / "data/migration/wechat/freeze_20260716T024524+0800/payload"
).resolve()
FREEZE_CODE_ROOT = (FREEZE_ROOT / "code-snapshot").resolve()
FREEZE_DATABASE = (FREEZE_ROOT / "data/state/app.db").resolve()
FORBIDDEN_REAL_SOURCE = Path(
    "/Users/works14/.claude/监控/wechat-ybxhyyh-top3"
).resolve()


def _assert_import_boundary_clean() -> None:
    """在清理/导入模块前先拒绝冻结路径和旧真实源已进入解释器。"""

    forbidden_roots = (FORBIDDEN_REAL_SOURCE, FREEZE_CODE_ROOT)
    for entry in sys.path:
        if not entry:
            continue
        resolved = Path(entry).resolve()
        if any(
            resolved == root or root in resolved.parents
            for root in forbidden_roots
        ):
            raise AssertionError(f"禁止从受保护路径 import：{resolved}")
    for name, module in tuple(sys.modules.items()):
        if name != "app" and not name.startswith("app."):
            continue
        module_path = getattr(module, "__file__", None)
        if not module_path:
            continue
        resolved = Path(module_path).resolve()
        if any(
            resolved == root or root in resolved.parents
            for root in forbidden_roots
        ):
            raise AssertionError(f"受保护旧模块已被加载：{name} -> {resolved}")


def assert_safe_freeze_source(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == FORBIDDEN_REAL_SOURCE or FORBIDDEN_REAL_SOURCE in resolved.parents:
        raise AssertionError(f"禁止访问旧真实源：{resolved}")
    if resolved != FREEZE_ROOT and FREEZE_ROOT not in resolved.parents:
        raise AssertionError(f"A 副本来源必须位于冻结 payload：{resolved}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _freeze_source_manifest() -> dict[str, tuple[int, int, str]]:
    """记录所有可能被复制/import 的冻结源；包含既有 pyc 以检测再次改写。"""

    roots = (FREEZE_CODE_ROOT / "app", FREEZE_DATABASE)
    manifest: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        assert_safe_freeze_source(root)
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise AssertionError(f"冻结代码源不允许软链接：{path}")
            if not path.is_file():
                continue
            stat = path.stat()
            relative = str(path.relative_to(FREEZE_ROOT))
            manifest[relative] = (stat.st_size, stat.st_mtime_ns, _sha256(path))
    return manifest


class LegacyWriteReplica:
    """冻结旧写接口的进程内隔离副本；绝不启动端口或调度器。"""

    def __init__(self, tmp_path: Path) -> None:
        _assert_import_boundary_clean()
        assert_safe_freeze_source(FREEZE_CODE_ROOT)
        assert_safe_freeze_source(FREEZE_DATABASE)
        if not FREEZE_DATABASE.is_file():
            raise FileNotFoundError(FREEZE_DATABASE)
        self.freeze_manifest_before = _freeze_source_manifest()
        self.database_path = (tmp_path / "legacy-write-replica.sqlite").resolve()
        if FORBIDDEN_REAL_SOURCE in self.database_path.parents:
            raise AssertionError("旧写副本不得落到旧真实源")
        shutil.copy2(FREEZE_DATABASE, self.database_path)

        self.isolated_code_root = (tmp_path / "legacy-code-copy").resolve()
        shutil.copytree(
            FREEZE_CODE_ROOT / "app",
            self.isolated_code_root / "app",
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                "*.pyc",
                "*.pyo",
                ".DS_Store",
            ),
        )
        (self.isolated_code_root / "normalized").mkdir(parents=True, exist_ok=True)
        # Python 的 import 默认会回写 __pycache__；A 必须只在临时副本加载。
        sys.dont_write_bytecode = True
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        code_path = str(self.isolated_code_root)
        if code_path not in sys.path:
            sys.path.insert(0, code_path)
        legacy_api = importlib.import_module("app.web.api")
        imported_from = Path(legacy_api.__file__).resolve()
        if (
            imported_from != self.isolated_code_root
            and self.isolated_code_root not in imported_from.parents
        ):
            raise AssertionError(f"旧写模块未从临时代码副本加载：{imported_from}")
        monitor_service = importlib.import_module("app.services.monitor_service")
        keyword_manage_service = importlib.import_module(
            "app.services.keyword_manage_service"
        )

        # topic/bucket 的旧实现会触发全量 rebuild；双副本只验状态写入，
        # 这里用临时 normalized 探针记录调用语义，A 绝不改冻结文件。
        self.rebuild_calls: list[dict[str, Any]] = []
        self.rebuild_probe_path = (
            self.isolated_code_root / "normalized/rebuild-call-probe.json"
        )

        def record_rebuild(
            verbose: bool = False,
            full: bool = False,
        ) -> dict[str, Any]:
            call = {"verbose": bool(verbose), "full": bool(full)}
            self.rebuild_calls.append(call)
            self.rebuild_probe_path.write_text(
                json.dumps(
                    self.rebuild_calls,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return {"probe": True, **call}

        monitor_service.rebuild_all = record_rebuild
        # 管理页仍读取隔离 DB；动态榜单字段不属于 W02-W16 的写对账面。
        keyword_manage_service._build_keyword_stats = lambda: {}

        app = Flask("wechat-legacy-write-replica")
        app.config.update(
            TESTING=False,
            SQLITE_PATH=self.database_path,
            MONITOR_DATA_FILE=self.isolated_code_root
            / "normalized/monitor-data.json",
            NORMALIZED_DIR=self.isolated_code_root / "normalized",
            PROJECT_ROOT=self.isolated_code_root,
            AIDSO_PLAYWRIGHT_PROFILE_DIR=tmp_path / "unused-aidso-profile",
        )
        app.register_blueprint(legacy_api.bp)
        self.app = app
        self.client = app.test_client()
        self.assert_source_unchanged()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ):
        return self.client.open(path, method=method, json=json_body)

    def connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.database_path)
        con.row_factory = sqlite3.Row
        return con

    def active_keywords(self, limit: int = 3) -> list[dict[str, Any]]:
        with self.connection() as con:
            return [
                dict(row)
                for row in con.execute(
                    """SELECT * FROM keyword_registry
                       WHERE status='active'
                       ORDER BY keyword_id LIMIT ?""",
                    (limit,),
                )
            ]

    def keyword(self, keyword_id: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM keyword_registry WHERE keyword_id=?",
                (keyword_id,),
            ).fetchone()
        return dict(row) if row else None

    def assert_source_unchanged(self) -> None:
        after = _freeze_source_manifest()
        assert after == self.freeze_manifest_before, _manifest_difference(
            self.freeze_manifest_before, after
        )
        for name, module in tuple(sys.modules.items()):
            if name != "app" and not name.startswith("app."):
                continue
            module_path = getattr(module, "__file__", None)
            if not module_path:
                continue
            resolved = Path(module_path).resolve()
            assert (
                resolved == self.isolated_code_root
                or self.isolated_code_root in resolved.parents
            ), f"旧写模块越界加载：{name} -> {resolved}"
        assert str(FORBIDDEN_REAL_SOURCE) not in sys.path
        assert str(FREEZE_CODE_ROOT) not in sys.path


def _manifest_difference(
    before: dict[str, tuple[int, int, str]],
    after: dict[str, tuple[int, int, str]],
) -> dict[str, Any]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": {
            key: {"before": before[key], "after": after[key]}
            for key in sorted(set(before) & set(after))
            if before[key] != after[key]
        },
    }


def seed_hub_from_legacy(settings: Settings, legacy: LegacyWriteReplica) -> None:
    """把 A 的冻结 registry 映射到 B；不调用全量历史导入。"""

    with legacy.connection() as source, connect(settings) as target:
        target.execute(
            """UPDATE migration_switches
               SET data_mode='hub',updated_by='write-dual-replica'
               WHERE module_key='wechat-search'
                 AND contract_key IN ('monitor-data','bootstrap','keyword','keyword-manage')"""
        )
        groups = [dict(row) for row in source.execute("SELECT * FROM keyword_groups")]
        keywords = [
            dict(row) for row in source.execute("SELECT * FROM keyword_registry")
        ]
        for row in groups:
            target.execute(
                """INSERT INTO search_keyword_groups(
                   group_id,system_key,platform,group_name,sort_order,
                   created_at,updated_at,archived_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    row["group_id"],
                    "wechat-search",
                    "wechat-search",
                    row["label"],
                    row["display_order"],
                    row["created_at"],
                    row["updated_at"],
                    row["archived_at"],
                ),
            )
        for row in keywords:
            payload_json = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            first_seen = row["first_seen_at"] or row["created_at"]
            target.execute(
                """INSERT INTO keywords(
                   keyword_id,platform,keyword,status,topic,keyword_bucket,
                   first_seen_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    row["keyword_id"],
                    "wechat-search",
                    row["keyword_text"],
                    row["status"],
                    row["topic"],
                    row["keyword_bucket"],
                    first_seen,
                    row["updated_at"],
                    payload_json,
                ),
            )
            target.execute(
                """INSERT INTO search_keyword_settings(
                   setting_id,system_key,platform,keyword_id,group_id,pinned,
                   refresh_strategy,refresh_interval_minutes,commercial_value,note,
                   archived_at,updated_at,payload_json,pin_order,
                   batch_default_selected,refresh_policy_reason,
                   commercial_value_source,commercial_value_reason,
                   auto_archive_locked,keyword_order
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"wechat-search:{row['keyword_id']}",
                    "wechat-search",
                    "wechat-search",
                    row["keyword_id"],
                    row["group_id"],
                    row["is_pinned"],
                    "disabled" if row["status"] == "archived" else "scheduled",
                    int(row["refresh_frequency_days"] or 1) * 1440,
                    row["commercial_value_score"],
                    row["note"],
                    row["archived_at"],
                    row["updated_at"],
                    payload_json,
                    row["pin_order"],
                    row["batch_default_selected"],
                    row["refresh_policy_reason"],
                    row["commercial_value_source"],
                    row["commercial_value_reason"],
                    row["auto_archive_locked"],
                    row["keyword_order"],
                ),
            )

        manage_response = legacy.request("GET", "/api/keyword-manage")
        if manage_response.status_code != 200:
            raise AssertionError(manage_response.get_data(as_text=True))
        manage = manage_response.get_json()
        active = [row for row in keywords if row["status"] == "active"]
        keyword_nodes = [
            {
                "keyword_id": row["keyword_id"],
                "keyword": row["keyword_text"],
                "runs": [{"marker": "frozen-dynamic"}],
                "today_best": None,
            }
            for row in active
        ]
        projections: list[tuple[str, str, dict[str, Any]]] = [
            (
                "keyword_manage",
                "",
                manage,
            ),
            (
                "bootstrap",
                "",
                {
                    "scope": {
                        "total": len(active),
                        "pinned": sum(bool(row["is_pinned"]) for row in active),
                    },
                    "keywords": keyword_nodes,
                },
            ),
            (
                "full",
                "",
                {
                    "keywords": keyword_nodes,
                    "pinned_keyword_count": sum(
                        bool(row["is_pinned"]) for row in active
                    ),
                    "keyword_bucket_options": sorted(
                        {
                            row["keyword_bucket"]
                            for row in active
                            if row["keyword_bucket"]
                        }
                    ),
                },
            ),
        ]
        projections.extend(
            (
                "keyword",
                row["keyword_id"],
                {
                    "keyword_id": row["keyword_id"],
                    "keyword": row["keyword_text"],
                    "runs": [{"marker": "frozen-dynamic"}],
                    "features": {"marker": "frozen-dynamic"},
                },
            )
            for row in active
        )
        for index, (kind, subject_id, payload) in enumerate(projections):
            target.execute(
                """INSERT INTO wechat_legacy_projections(
                   projection_id,projection_kind,subject_id,payload_json,
                   source_hash,source_manifest_id,source_ref,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    f"write-replica-{index}",
                    kind,
                    subject_id,
                    json.dumps(payload, ensure_ascii=False),
                    f"write-replica-hash-{index}",
                    "freeze-write-replica",
                    "freeze-write-replica",
                    "2026-07-16T00:00:00Z",
                ),
            )


def normalized_body(value: Any) -> Any:
    """仅屏蔽 A/B 各自生成的本次写入时间，其余字段逐值比较。"""

    if isinstance(value, dict):
        return {
            key: normalized_body(item)
            for key, item in value.items()
            if key not in {"created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [normalized_body(item) for item in value]
    return value


STATE_FIELDS = (
    "keyword_id",
    "keyword_text",
    "status",
    "enabled",
    "is_active",
    "source",
    "group_id",
    "keyword_order",
    "note",
    "archived_at",
    "is_pinned",
    "pin_order",
    "topic",
    "keyword_bucket",
    "batch_default_selected",
    "first_seen_at",
    "last_seen_at",
    "snapshot_count",
    "refresh_frequency_days",
    "effective_refresh_interval_hours",
    "refresh_frequency_source",
    "refresh_policy_reason",
    "last_refresh_at",
    "last_refresh_attempt_at",
    "last_refresh_status",
    "next_refresh_at",
    "refresh_age_days",
    "is_refresh_due",
    "commercial_value_score",
    "commercial_value_source",
    "commercial_value_reason",
    "lifecycle_stage",
    "observation_started_at",
    "observation_deadline_at",
    "discovery_candidate_id",
    "auto_archive_locked",
    "archive_reason_code",
    "archive_reason_detail",
)


def projected_state(node: dict[str, Any]) -> dict[str, Any]:
    return {key: node.get(key) for key in STATE_FIELDS}
