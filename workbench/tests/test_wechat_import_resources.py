from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import ConflictError, NotFoundError
from content_hub.features.wechat.service import (
    WechatService, _legacy_projection_payload, _legacy_refresh_history,
    _legacy_scheduler_status, _projection_account_prune, _projection_json,
    _projection_keyword_prune, _stream_json_array, _stream_json_count,
)
from content_hub.repositories.wechat_legacy import WechatLegacyRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source(settings, tmp_path: Path):
    root = tmp_path / "wechat-source"
    normalized = root / "normalized"
    normalized.mkdir(parents=True)
    def put(name, value):
        (normalized / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    put("monitor-data.json", {
        "generated_at": "2026-07-16T00:00:00",
        "keywords": [{"keyword_id": "kw_1", "keyword": "流式验收", "topic": "测试"}],
        "accounts": [{"account_id": "acct_1", "canonical_name": "测试账号"}],
    })
    put("accounts.json", [{"account_id": "acct_1", "canonical_name": "测试账号"}])
    put("articles.json", [
        {"article_id": f"art_{i}", "normalized_url": f"https://mp.weixin.qq.com/s/{i}",
         "title": f"文章{i}", "account_id": "acct_1"}
        for i in range(4)
    ])
    put("snapshots.json", [{"snapshot_id": "snap_1", "keyword_id": "kw_1",
                            "captured_at": "2026-07-16T00:00:00", "result_count": 4}])
    put("snapshot_registry.json", {})
    put("snapshot_terms.json", [])
    put("ranking_hits.json", [
        {"hit_id": f"hit_{i}", "snapshot_id": "snap_1", "rank": i + 1,
         "article_id": f"art_{i}"}
        for i in range(4)
    ])
    put("article_metric_observations.json", [])
    return replace(
        settings, database_path=tmp_path / "hub.sqlite",
        lock_path=tmp_path / "hub.lock", asset_store_path=tmp_path / "assets",
        wechat_source_root=root, wechat_source_url="http://127.0.0.1:1",
    )


def _add_runtime_registry(root: Path) -> None:
    path = root / "data/state/app.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE keyword_groups(
                group_id TEXT PRIMARY KEY,label TEXT,display_order INTEGER,
                created_at TEXT,updated_at TEXT,archived_at TEXT
            );
            CREATE TABLE keyword_registry(
                keyword_id TEXT PRIMARY KEY,keyword_text TEXT,status TEXT,
                group_id TEXT,archived_at TEXT
            );
            INSERT INTO keyword_groups VALUES(
                'grp_1','测试分组',1,'2026-07-01','2026-07-01',NULL
            );
            INSERT INTO keyword_registry VALUES(
                'kw_1','流式验收','active','grp_1',NULL
            );
            INSERT INTO keyword_registry VALUES(
                'kw_archived','已归档词','archived','grp_1','2026-07-15'
            );
            """
        )


def _add_runtime_projection_fixture(root: Path, snapshot_date: str) -> None:
    state_root = root / "data/state"
    config_root = root / "data/config"
    runs_root = root / "data/runs"
    state_root.mkdir(parents=True, exist_ok=True)
    config_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)
    (state_root / "scheduler.json").write_text(json.dumps({
        "enabled": True,
        "interval_hours": 1.0,
        "base_url": "http://127.0.0.1:9999",
        "daily_keyword_budget": 20,
        "max_keywords_per_batch": 5,
    }), encoding="utf-8")
    (config_root / "keyword_refresh_policy.json").write_text(json.dumps({
        "daily_keyword_budget": 30,
        "scheduled_keyword_budget": 20,
        "discovery_daily_search_budget": 3,
        "manual_reserve_budget": 7,
    }), encoding="utf-8")
    (state_root / "keyword_refresh_ledger.json").write_text(json.dumps({
        "daily_budget": {
            snapshot_date: {
                "scheduler_batches": {
                    "batch_old": {"reserved_count": 4},
                },
            },
        },
    }), encoding="utf-8")

    rows = (
        (
            "batch_mtime_first",
            {
                "batch_id": "batch_mtime_first",
                "status": "cancelled",
                "total_keywords": 2,
                "success_count": 1,
                "failed_count": 1,
                "started_at": "2026-01-01T00:00:00",
                "finished_at": "2026-01-01T00:10:00",
                "source": "scheduler",
                "refresh_round": 7,
                "cancel_reason": "人工停止",
            },
            300,
        ),
        (
            "batch_started_first",
            {
                "batch_id": "batch_started_first",
                "status": "completed",
                "total_keywords": 1,
                "success_count": 1,
                "failed_count": 0,
                "started_at": "2026-12-01T00:00:00",
                "finished_at": "2026-12-01T00:01:00",
                "source": "web_refresh_all",
            },
            200,
        ),
        (
            "discovery_today",
            {
                "batch_id": "discovery_today",
                "status": "completed",
                "total_keywords": 2,
                "success_count": 2,
                "failed_count": 0,
                "started_at": f"{snapshot_date}T01:00:00",
                "finished_at": f"{snapshot_date}T01:01:00",
                "source": "discovery",
            },
            100,
        ),
    )
    for name, state, mtime in rows:
        batch_dir = runs_root / name
        batch_dir.mkdir()
        (batch_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        os.utime(batch_dir, (mtime, mtime))
    failed_dir = runs_root / "batch_mtime_first"
    (failed_dir / "failed.jsonl").write_text(
        json.dumps({
            "keyword": "失败词",
            "stderr_tail": "ConnectError: Connection refused",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.utime(failed_dir, (300, 300))


def test_streaming_import_is_idempotent_and_compresses_large_projection(
    settings, tmp_path, monkeypatch
):
    configured = _source(settings, tmp_path)
    service = WechatService(configured)
    monkeypatch.setattr(service, "_large_source_requires_streaming", lambda: True)

    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="streaming-import")
    with connect(configured, readonly=True) as con:
        before = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("contents", "search_snapshots", "search_hits", "metric_observations")
        }
        stored = con.execute(
            "SELECT payload_json FROM wechat_legacy_projections "
            "WHERE projection_kind='article_detail' LIMIT 1"
        ).fetchone()[0]
    assert first["audit"]["streaming"] is True
    with connect(configured, readonly=True) as con:
        checkpoint = con.execute(
            "SELECT source_hash,batch_id FROM ingestion_checkpoints "
            "WHERE adapter_key='wechat-search' AND checkpoint_key='normalized'"
        ).fetchone()
        batch_payload = con.execute(
            "SELECT payload_json FROM ingestion_batches WHERE batch_id=?",
            (first["batch_id"],),
        ).fetchone()[0]
    assert first["audit"]["manifest_id"]
    assert checkpoint["source_hash"] == first["audit"]["manifest_id"]
    assert checkpoint["batch_id"] == first["batch_id"]
    assert json.loads(batch_payload)["manifest_id"] == first["audit"]["manifest_id"]
    assert first["audit"]["adapter_manifest_digest"] != first["audit"]["manifest_id"]
    assert json.loads(stored)["__compressed_json__"] == "zlib+base64"
    assert WechatLegacyRepository(configured).full()["keywords"][0]["keyword"] == "流式验收"

    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="streaming-import")
    assert second["batch_id"] == first["batch_id"]
    with connect(configured, readonly=True) as con:
        after = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


@pytest.mark.parametrize("streaming", [False, True])
def test_reconcile_excludes_preserved_legacy_wechat_metric_keys(
    settings, tmp_path, monkeypatch, streaming
):
    configured = _source(settings, tmp_path)
    metric_path = configured.wechat_source_root / "normalized/article_metric_observations.json"
    metric_path.write_text(
        json.dumps([{
            "observation_id": "canonical-observation",
            "article_id": "art_0",
            "observed_at": "2026-07-16T00:00:00Z",
            "read_count": 8,
            "like_count": 3,
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    legacy_rows = [
        ("canonical-observation:wechat.read_count", "wechat.read_count", 101),
        ("canonical-observation:wechat.like_count", "wechat.like_count", 202),
    ] + [
        ("legacy-read-" + str(index), "wechat.read_count", index + 1)
        for index in range(6)
    ] + [
        ("legacy-like-" + str(index), "wechat.like_count", index + 11)
        for index in range(5)
    ]
    legacy_expected = [
        (observation_id, "content", "legacy-content", metric_key,
         f"2026-07-15T00:{index:02d}:00Z", float(value), None, None, None, None,
         '{"legacy":true}')
        for index, (observation_id, metric_key, value) in enumerate(legacy_rows)
    ]
    with connect(configured) as con:
        for metric_key in {"wechat.read_count", "wechat.like_count"}:
            con.execute(
                """
                INSERT INTO metric_definitions(
                    metric_key,platform,subject_type,display_name,value_type,unit,
                    accumulation_mode,description
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    metric_key, "wechat-search", "content", metric_key,
                    "number", "count", "gauge", "legacy compatibility fact",
                ),
            )
        con.executemany(
            """
            INSERT INTO metric_observations(
                observation_id,subject_type,subject_id,metric_key,observed_at,
                numeric_value,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            [
                (observation_id, "content", "legacy-content", metric_key,
                 f"2026-07-15T00:{index:02d}:00Z", value, '{"legacy":true}')
                for index, (observation_id, metric_key, value) in enumerate(legacy_rows)
            ],
        )
        con.commit()

    service = WechatService(configured)
    monkeypatch.setattr(
        service, "_large_source_requires_streaming", lambda: streaming
    )
    result = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key=f"legacy-metric-{streaming}",
    )

    assert result["audit"]["rejected"] == []
    assert result["audit"]["reconcile"]["status"] == "matched"
    assert result["audit"]["reconcile"]["verified"] is True
    assert result["audit"]["reconcile"]["source"]["metric_observations"] == 2
    assert result["audit"]["reconcile"]["hub"]["metric_observations"] == 2
    with connect(configured, readonly=True) as con:
        preserved = [
            tuple(row)
            for row in con.execute(
                """
                SELECT observation_id,subject_type,subject_id,metric_key,observed_at,
                       numeric_value,text_value,snapshot_id,source_ref,confidence,payload_json
                FROM metric_observations
                WHERE metric_key IN ('wechat.read_count','wechat.like_count')
                ORDER BY observation_id
                """
            )
        ]
        assert preserved == sorted(legacy_expected)
        canonical = [
            tuple(row) for row in con.execute(
                """
                SELECT observation_id,metric_key,numeric_value
                FROM metric_observations
                WHERE metric_key IN ('wechat.article.read_count','wechat.article.like_count')
                ORDER BY metric_key
                """
            )
        ]
        assert canonical == [
            ("canonical-observation:wechat.article.like_count", "wechat.article.like_count", 3.0),
            ("canonical-observation:wechat.article.read_count", "wechat.article.read_count", 8.0),
        ]
        assert {row[0] for row in canonical}.isdisjoint({row[0] for row in preserved})
        assert con.execute(
            """
            SELECT COUNT(*) FROM metric_observations
            WHERE metric_key LIKE 'wechat.article.%'
               OR metric_key LIKE 'wechat.keyword.%'
            """
        ).fetchone()[0] == 2

    repeated = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key=f"legacy-metric-repeat-{streaming}",
    )
    assert repeated["audit"]["reconcile"]["status"] == "matched"
    assert repeated["audit"]["reconcile"]["verified"] is True
    with connect(configured, readonly=True) as con:
        assert [
            tuple(row) for row in con.execute(
                """
                SELECT observation_id,subject_type,subject_id,metric_key,observed_at,
                       numeric_value,text_value,snapshot_id,source_ref,confidence,payload_json
                FROM metric_observations
                WHERE metric_key IN ('wechat.read_count','wechat.like_count')
                ORDER BY observation_id
                """
            )
        ] == sorted(legacy_expected)
        assert con.execute(
            """
            SELECT COUNT(*) FROM metric_observations
            WHERE metric_key LIKE 'wechat.article.%'
               OR metric_key LIKE 'wechat.keyword.%'
            """
        ).fetchone()[0] == 2


def test_canonical_collision_preserves_external_content_fields_and_keeps_wechat_read(
    settings, tmp_path
):
    configured = _source(settings, tmp_path)
    article_path = configured.wechat_source_root / "normalized/articles.json"
    articles = json.loads(article_path.read_text(encoding="utf-8"))
    articles[0]["content_file_path"] = "collision.md"
    article_path.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
    (configured.wechat_source_root / "collision.md").write_text("微信正文", encoding="utf-8")
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with con:
                con.execute(
                    "INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                    ("xhs_owner", "XHS", "xhs", "xhs_owner", "2026-07-01", "2026-07-01", "{}"),
                )
                con.execute(
                    """
                    INSERT INTO contents(
                        content_id,content_type,title,canonical_url,creator_id,published_at,
                        first_seen_at,updated_at,md_path,file_hash,content_hash,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "xhs_shared", "external_article", "XHS 原题",
                        "https://mp.weixin.qq.com/s/0", "xhs_owner",
                        "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z",
                        "2025-01-02T00:00:00Z", "xhs/original.md", "old-file",
                        "old-content", '{"owner":"xhs"}',
                    ),
                )
    before = {
        "creator_id": "xhs_owner", "title": "XHS 原题",
        "published_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z", "md_path": "xhs/original.md",
        "file_hash": "old-file", "content_hash": "old-content",
        "payload_json": '{"owner":"xhs"}',
    }
    result = WechatService(configured).import_history(
        dry_run=False, limit=None, idempotency_key="collision-test", confirm=True
    )
    collision = next(x for x in result["audit"]["content_collisions"] if x["content_id"] == "xhs_shared")
    assert collision["preserved_fields"] == [
        "creator_id", "title", "published_at", "updated_at", "md_path",
        "payload_json", "file_hash", "content_hash",
    ]
    with connect(configured, readonly=True) as con:
        row = dict(con.execute("SELECT * FROM contents WHERE content_id='xhs_shared'").fetchone())
        assert {key: row[key] for key in before} == before
        assert con.execute(
            "SELECT content_id FROM content_identifiers WHERE namespace='wechat_article' AND external_id='art_0'"
        ).fetchone()[0] == "xhs_shared"
    detail = WechatLegacyRepository(configured).hit_detail("art_0", "")
    assert detail["article"]["article_id"] == "art_0"


def test_wechat_import_lock_is_single_flight_and_pure_external_article_is_404(
    settings, tmp_path
):
    configured = _source(settings, tmp_path)
    service = WechatService(configured)
    with writer_lock(service._import_lock_path(), timeout_seconds=1):
        with pytest.raises(ConflictError):
            service.import_history(
                dry_run=False, limit=None, confirm=True, idempotency_key="locked"
            )
    with writer_lock(configured.lock_path):
        with connect(configured) as con:
            with con:
                con.execute(
                    "INSERT INTO creators(creator_id,canonical_name,platform,external_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                    ("only_xhs", "XHS", "xhs", "only_xhs", "2026-07-01", "2026-07-01", "{}"),
                )
                con.execute(
                    "INSERT INTO contents(content_id,content_type,title,canonical_url,creator_id,first_seen_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                    ("only_xhs_article", "external_article", "XHS", "https://xhs.example/a", "only_xhs", "2026-07-01", "2026-07-01", "{}"),
                )
    with pytest.raises(NotFoundError):
        WechatLegacyRepository(configured).hit_detail("only_xhs_article", "")


def test_markdown_resolution_prefers_root_then_code_snapshot_and_rejects_symlinks(
    settings, tmp_path
) -> None:
    configured = _source(settings, tmp_path)
    root = configured.wechat_source_root
    (root / "正文").mkdir()
    (root / "code-snapshot/正文").mkdir(parents=True)
    (root / "正文/primary.md").write_text("root-primary", encoding="utf-8")
    (root / "code-snapshot/正文/primary.md").write_text(
        "fallback-primary", encoding="utf-8"
    )
    (root / "code-snapshot/正文/fallback.md").write_text(
        "code-snapshot-fallback", encoding="utf-8"
    )
    adapter = WechatAdapter(configured)

    assert adapter.read_markdown_with_source("正文/primary.md") == (
        "root-primary",
        "正文/primary.md",
    )
    assert adapter.read_markdown_with_source("正文/fallback.md") == (
        "code-snapshot-fallback",
        "code-snapshot/正文/fallback.md",
    )
    for unsafe in (
        "../escape.md",
        "/absolute.md",
        "正文/not-markdown.txt",
        "正文//empty.md",
    ):
        with pytest.raises(WechatSourceError) as error:
            adapter.read_markdown_with_source(unsafe)
        assert error.value.kind == "path_not_allowed"

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "正文/link.md").symlink_to(outside)
    (root / "code-snapshot/正文/link.md").write_text(
        "must-not-fallback-after-symlink", encoding="utf-8"
    )
    with pytest.raises(WechatSourceError) as error:
        adapter.read_markdown_with_source("正文/link.md")
    assert error.value.kind == "path_not_allowed"


@pytest.mark.parametrize("streaming", [False, True])
def test_article_assets_keep_original_path_and_actual_source_ref_idempotently(
    settings, tmp_path, monkeypatch, streaming
) -> None:
    configured = _source(settings, tmp_path)
    root = configured.wechat_source_root
    articles_path = root / "normalized/articles.json"
    articles = json.loads(articles_path.read_text(encoding="utf-8"))
    articles[0]["content_file_path"] = "正文/primary.md"
    articles[1]["content_file_path"] = "正文/fallback.md"
    articles_path.write_text(
        json.dumps(articles, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "正文").mkdir()
    (root / "正文/primary.md").write_text("primary-body", encoding="utf-8")
    (root / "code-snapshot/正文").mkdir(parents=True)
    (root / "code-snapshot/正文/fallback.md").write_text(
        "fallback-body", encoding="utf-8"
    )
    service = WechatService(configured)
    monkeypatch.setattr(
        service,
        "_large_source_requires_streaming",
        lambda: streaming,
    )

    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="asset-paths")
    with connect(configured, readonly=True) as con:
        before_rows = [
            dict(row)
            for row in con.execute(
                """
                SELECT old_article_id,relative_path,asset_path,source_ref,created_at
                FROM wechat_article_paths
                ORDER BY old_article_id
                """
            )
        ]
    before_files = sorted(
        path.relative_to(configured.asset_store_path).as_posix()
        for path in configured.asset_store_path.rglob("*.md")
    )

    assert len(before_rows) == 2
    by_id = {row["old_article_id"]: row for row in before_rows}
    assert by_id["art_0"]["relative_path"] == "正文/primary.md"
    assert by_id["art_0"]["source_ref"].endswith("/正文/primary.md")
    assert by_id["art_1"]["relative_path"] == "正文/fallback.md"
    assert by_id["art_1"]["source_ref"].endswith(
        "/code-snapshot/正文/fallback.md"
    )
    assert all(row["asset_path"] for row in before_rows)
    assert len(before_files) == 2

    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="asset-paths")
    with connect(configured, readonly=True) as con:
        after_rows = [
            dict(row)
            for row in con.execute(
                """
                SELECT old_article_id,relative_path,asset_path,source_ref,created_at
                FROM wechat_article_paths
                ORDER BY old_article_id
                """
            )
        ]
    after_files = sorted(
        path.relative_to(configured.asset_store_path).as_posix()
        for path in configured.asset_store_path.rglob("*.md")
    )
    assert second["batch_id"] == first["batch_id"]
    assert after_rows == before_rows
    assert after_files == before_files


@pytest.mark.skipif(
    os.getenv("RUN_WECHAT_FREEZE_ASSETS") != "1",
    reason="真实 freeze 正文资产双次导入专项需显式运行",
)
def test_real_freeze_article_assets_are_complete_and_idempotent(
    settings, monkeypatch
) -> None:
    freeze = PROJECT_ROOT / "data/migration/wechat/freeze_20260716T024524+0800/payload"
    articles = json.loads(
        (freeze / "normalized/articles.json").read_text(encoding="utf-8")
    )
    source_no_content_path = sum(
        not bool(row.get("content_file_path")) for row in articles
    )

    def fail_http(*args, **kwargs):
        raise AssertionError("真实 freeze 资源专项不得调用 HTTP")

    monkeypatch.setattr(WechatAdapter, "_request_response", fail_http)
    with tempfile.TemporaryDirectory(
        prefix="wechat-freeze-assets-",
        dir="/tmp",
    ) as temp:
        temp_root = Path(temp)
        configured = replace(
            settings,
            database_path=temp_root / "hub.sqlite",
            lock_path=temp_root / "hub.lock",
            asset_store_path=temp_root / "asset_store",
            wechat_source_root=freeze,
            wechat_source_url="http://127.0.0.1:8774",
        )
        migrate(configured)
        service = WechatService(configured)

        first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="real-freeze-assets")
        with connect(configured, readonly=True) as con:
            before_paths = [
                tuple(row)
                for row in con.execute(
                    """
                    SELECT old_article_id,relative_path,asset_path,source_ref
                    FROM wechat_article_paths
                    ORDER BY old_article_id,source_ref
                    """
                )
            ]
            before_core = {
                table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "contents",
                    "content_identifiers",
                    "search_snapshots",
                    "search_hits",
                    "metric_observations",
                )
            }
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
        before_files = sorted(
            path.name
            for path in (configured.asset_store_path / "wechat").glob("*.md")
        )
        rows_by_article: dict[str, list[tuple]] = {}
        for row in before_paths:
            rows_by_article.setdefault(str(row[0]), []).append(row)
        asset_hash_count = imported_assets = 0
        missing = []
        fallback_rows = []
        for article in articles:
            article_id = str(article.get("article_id") or "")
            relative = str(article.get("content_file_path") or "").replace(
                "\\", "/"
            )
            if not relative:
                continue
            source = freeze / relative
            actual_relative = relative
            if not source.is_file():
                source = freeze / "code-snapshot" / relative
                actual_relative = f"code-snapshot/{relative}"
            if not source.is_file():
                missing.append((article_id, "source_missing"))
                continue
            asset_hash_count += 1
            rows = rows_by_article.get(article_id, [])
            if len(rows) != 1:
                missing.append((article_id, f"path_rows={len(rows)}"))
                continue
            row = rows[0]
            if row[1] != relative or not str(row[3]).endswith(
                "/" + actual_relative
            ):
                missing.append((article_id, "path_or_source_ref_mismatch"))
                continue
            asset_file = configured.asset_store_path / str(row[2] or "")
            if (
                not row[2]
                or not asset_file.is_file()
                or hashlib.sha256(asset_file.read_bytes()).hexdigest()
                != hashlib.sha256(source.read_bytes()).hexdigest()
            ):
                missing.append((article_id, "asset_missing_or_hash_mismatch"))
                continue
            imported_assets += 1
            if actual_relative.startswith("code-snapshot/"):
                fallback_rows.append(row)

        assert len(articles) == 6364
        assert source_no_content_path == 2036
        assert len(before_paths) == 4328
        assert asset_hash_count == 4328
        assert imported_assets == 4328
        assert len(missing) == 0
        assert len(fallback_rows) == 9
        assert len(before_files) == 4144
        assert integrity == "ok"
        assert foreign_keys == []

        second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="real-freeze-assets")
        with connect(configured, readonly=True) as con:
            after_paths = [
                tuple(row)
                for row in con.execute(
                    """
                    SELECT old_article_id,relative_path,asset_path,source_ref
                    FROM wechat_article_paths
                    ORDER BY old_article_id,source_ref
                    """
                )
            ]
            after_core = {
                table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before_core
            }
        after_files = sorted(
            path.name
            for path in (configured.asset_store_path / "wechat").glob("*.md")
        )

        assert second["batch_id"] == first["batch_id"]
        assert after_paths == before_paths
        assert after_core == before_core
        assert after_files == before_files
        print(json.dumps({
            "path_records": len(before_paths),
            "asset_hash_count": asset_hash_count,
            "imported_assets": imported_assets,
            "missing_path_asset_count": len(missing),
            "source_no_content_path": source_no_content_path,
            "fallback_code_snapshot": len(fallback_rows),
            "asset_blob_count": len(before_files),
            "second_import_new_assets": len(after_files) - len(before_files),
            "second_import_core_delta": {
                key: after_core[key] - before_core[key]
                for key in before_core
            },
            "integrity_check": integrity,
            "foreign_key_check_rows": len(foreign_keys),
        }, ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("streaming", [False, True])
def test_keyword_manage_excludes_archived_but_keeps_registry_history(
    settings, tmp_path, monkeypatch, streaming
):
    configured = _source(settings, tmp_path)
    _add_runtime_registry(configured.wechat_source_root)
    service = WechatService(configured)
    monkeypatch.setattr(service, "_large_source_requires_streaming", lambda: streaming)

    result = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="keyword-manage")
    projection = WechatLegacyRepository(configured).keyword_manage()

    assert projection["total"] == 1
    assert len(projection["groups"]) == 1
    assert [row["keyword_id"] for row in projection["groups"][0]["keywords"]] == ["kw_1"]
    with connect(configured, readonly=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM keywords WHERE platform='wechat-search'"
        ).fetchone()[0] == 2
    if streaming:
        assert result["audit"]["reconcile"]["scope"]["keywords_runtime_registry"] == {
            "source": 2, "hub": 2,
        }


def test_streaming_import_persists_exact_runtime_projections_idempotently(
    settings, tmp_path, monkeypatch
):
    configured = _source(settings, tmp_path)
    service = WechatService(configured)
    monkeypatch.setattr(service, "_large_source_requires_streaming", lambda: True)
    snapshot_date = service._runtime_projection_date()
    _add_runtime_projection_fixture(configured.wechat_source_root, snapshot_date)

    first = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="runtime-projections")
    expected_history = _legacy_refresh_history(configured.wechat_source_root)
    expected_scheduler = _legacy_scheduler_status(
        configured.wechat_source_root,
        base_url=configured.wechat_source_url,
        snapshot_date=snapshot_date,
        use_persisted_config=True,
    )
    repository = WechatLegacyRepository(configured)
    assert repository.runtime_history() == expected_history
    assert [row["batch_id"] for row in repository.runtime_history()] == [
        "batch_mtime_first", "batch_started_first", "discovery_today",
    ]
    assert repository.scheduler_runtime() == expected_scheduler
    assert expected_scheduler["budget"]["reserved_count"] == 4
    assert expected_scheduler["budget_breakdown"]["discovery"]["used"] == 2
    with connect(configured, readonly=True) as con:
        core_before = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "keywords", "contents", "search_snapshots",
                "search_hits", "metric_observations",
            )
        }
        projection_count_before = con.execute(
            "SELECT COUNT(*) FROM wechat_legacy_projections "
            "WHERE projection_kind='runtime'"
        ).fetchone()[0]
    assert projection_count_before == 4

    second = service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="runtime-projections")
    with connect(configured, readonly=True) as con:
        core_after = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in core_before
        }
        projection_count_after = con.execute(
            "SELECT COUNT(*) FROM wechat_legacy_projections "
            "WHERE projection_kind='runtime'"
        ).fetchone()[0]
    assert second["batch_id"] == first["batch_id"]
    assert core_after == core_before
    assert projection_count_after == projection_count_before
    # A fresh repository/connection after the second import proves restart read.
    assert WechatLegacyRepository(configured).runtime_history() == expected_history
    assert WechatLegacyRepository(configured).scheduler_runtime() == expected_scheduler


def test_frozen_reference_runtime_r19_r20_payload_hashes() -> None:
    root = PROJECT_ROOT / "data/migration/wechat/reference/instance"
    if not root.is_dir():
        pytest.skip("隔离 8774 reference root 不存在")
    history = _legacy_refresh_history(root)
    scheduler = _legacy_scheduler_status(
        root,
        base_url="http://127.0.0.1:8774",
        snapshot_date="2026-07-16",
        use_persisted_config=False,
    )
    history_wire = (
        json.dumps(
            history, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ) + "\n"
    ).encode()
    scheduler_wire = (
        json.dumps(
            scheduler, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ) + "\n"
    ).encode()

    assert len(history) == 15
    assert hashlib.sha256(history_wire).hexdigest() == (
        "4f843f6b5bdaa2cf95f6cf1346fe5ae205a48b2fd08527366ac7a5432eb704cc"
    )
    assert list(scheduler) == [
        "base_url", "budget", "budget_breakdown", "daily_keyword_budget",
        "enabled", "interval_hours", "last_discovery", "last_plan",
        "last_result", "last_triggered_at", "max_keywords_per_batch",
        "next_run_at",
    ]
    assert hashlib.sha256(scheduler_wire).hexdigest() == (
        "76216a24a5e31b30d0d108b1702b46ccf6262a227a1a10e0ab6c19ca19e879dd"
    )


@pytest.mark.skipif(
    os.getenv("RUN_WECHAT_MONITOR_HASH") != "1",
    reason="312/1361 冻结投影 hash 专项需显式运行，峰值内存约 2 GiB",
)
def test_frozen_monitor_fast_store_r01_r04_hashes() -> None:
    root = PROJECT_ROOT / "data/migration/wechat/reference/instance"
    monitor = json.loads((root / "normalized/monitor-data.json").read_text(encoding="utf-8"))
    deltas = json.loads((root / "normalized/keyword_read_deltas.json").read_text(encoding="utf-8"))
    meta = json.loads(
        (root / "normalized/article_metric_observations_meta.json").read_text(encoding="utf-8")
    )
    with sqlite3.connect(
        f"file:{root / 'data/state/app.db'}?mode=ro", uri=True
    ) as con:
        con.row_factory = sqlite3.Row
        runtime = {
            table: [dict(row) for row in con.execute(f"SELECT * FROM {table}")]
            for table in ("keyword_groups", "keyword_registry")
        }
    full, keywords, accounts = _legacy_projection_payload(
        {
            "monitor": monitor, "keyword_read_deltas": deltas,
            "article_metric_meta": meta, "runtime": runtime,
        },
        read_delta_source=str(root / "normalized/keyword_read_deltas.json"),
    )
    bootstrap = {
        "generated_at": full.get("generated_at"), "window_days": full.get("window_days"),
        "window_start": full.get("window_start"), "window_end": full.get("window_end"),
        "account_score_method": full.get("account_score_method"),
        "wso_fit_meta": full.get("wso_fit_meta"),
        "keyword_read_delta_meta": full.get("keyword_read_delta_meta"),
        "keyword_bucket_options": full.get("keyword_bucket_options"),
        "keyword_scope": full.get("keyword_scope"),
        "keyword_source_total": full.get("keyword_source_total"),
        "pinned_keyword_count": full.get("pinned_keyword_count"),
        "keywords": [_projection_keyword_prune(row) for row in keywords.values()],
        "accounts": [_projection_account_prune(row) for row in accounts.values()],
    }

    assert hashlib.md5(_projection_json(full).encode()).hexdigest() == "87bd32ec6318626a2920c3d623549399"
    assert hashlib.md5(_projection_json(bootstrap).encode()).hexdigest() == "2abb264c30d9d31d97196d753c819774"
    assert len(keywords) == 312
    assert all("is_pinned" in row and "pin_order" in row for row in keywords.values())

    def aggregate(rows):
        digest = hashlib.sha256()
        for row_id in sorted(rows):
            raw = _projection_json(rows[row_id]).encode()
            digest.update(row_id.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(raw).hexdigest().encode())
            digest.update(b"\0")
        return digest.hexdigest()

    assert aggregate(keywords) == "67d119e42882eb65f41430c5f8ad88987d45665f4ff6287d424334e5e4912187"
    assert len(accounts) == 1361
    assert aggregate(accounts) == "19158fd334cd3ffd54503a1138e89f4521382f71cc4323aa93775158206fe58e"


# ---------------------------------------------------------------------------
# _stream_json_array bounded-scan + UTF-8 BOM regression tests
# ---------------------------------------------------------------------------
# These cover the P1 fix in ``_stream_json_array``:
#   * The initial leading-whitespace scan is hard-capped at 1 MiB; a frozen
#     source whose preamble exceeds the cap must raise ValueError rather
#     than allocate the whole preamble into the in-memory buffer.
#   * A UTF-8 BOM at the start of the file is recognised and stripped so
#     Windows-produced freezes still parse without preprocessing.
#   * Legitimate small-leading-whitespace, BOM+whitespace, and cross-chunk
#     array bodies continue to stream correctly.

def _write_array(tmp_path: Path, *, prefix: bytes, body: str) -> Path:
    path = tmp_path / "freeze.json"
    path.write_bytes(prefix + body.encode("utf-8"))
    return path


def test_stream_json_array_handles_normal_top_level_array(tmp_path: Path) -> None:
    path = _write_array(tmp_path, prefix=b"", body='[{"id":"a"},{"id":"b"}]')
    assert list(_stream_json_array(path)) == [{"id": "a"}, {"id": "b"}]


def test_stream_json_array_handles_leading_whitespace(tmp_path: Path) -> None:
    path = _write_array(
        tmp_path,
        prefix=b"   \n\t",
        body='[{"id":"a"}]',
    )
    assert list(_stream_json_array(path)) == [{"id": "a"}]


def test_stream_json_array_handles_utf8_bom_only(tmp_path: Path) -> None:
    """A frozen source whose very first bytes are a UTF-8 BOM must parse
    without the parser mistakenly thinking the first character is ``[``."""
    path = _write_array(
        tmp_path,
        prefix=b"\xef\xbb\xbf",
        body='[{"id":"bom"}]',
    )
    assert list(_stream_json_array(path)) == [{"id": "bom"}]


def test_stream_json_array_handles_bom_plus_whitespace(tmp_path: Path) -> None:
    path = _write_array(
        tmp_path,
        prefix=b"\xef\xbb\xbf   \n",
        body='[{"id":"both"}]',
    )
    assert list(_stream_json_array(path)) == [{"id": "both"}]


def test_stream_json_array_streams_rows_split_across_chunks(tmp_path: Path) -> None:
    """Each row is larger than the main-loop read chunk (64 KiB) and split
    across multiple reads.  The streaming decoder must still emit every row
    exactly once and never grow the row buffer unboundedly."""
    huge_value = "x" * (80 * 1024)  # 80 KiB, > the 64 KiB read chunk
    body = (
        '[{"id":"a","blob":"'
        + huge_value
        + '"},{"id":"b","blob":"'
        + huge_value
        + '"}]'
    )
    path = _write_array(tmp_path, prefix=b"", body=body)
    rows = list(_stream_json_array(path))
    assert len(rows) == 2
    assert [row["id"] for row in rows] == ["a", "b"]
    assert all(len(row["blob"]) == 80 * 1024 for row in rows)


def test_stream_json_array_handles_utf8_character_split_at_chunk_boundary(
    tmp_path: Path,
) -> None:
    opening = b'{"text":"'
    padding = b"a" * (64 * 1024 - len(opening) - 1)
    path = tmp_path / "utf8-boundary.json"
    path.write_bytes(b"[" + opening + padding + "世".encode("utf-8") + b'"}]')
    assert list(_stream_json_array(path)) == [{"text": "a" * len(padding) + "世"}]
    assert _stream_json_count(path) == 1


def test_stream_json_array_rejects_invalid_utf8_and_truncated_json(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid-utf8.json"
    invalid.write_bytes(b'[{"text":"\xe7"}]')
    with pytest.raises(ValueError, match="invalid UTF-8"):
        list(_stream_json_array(invalid))

    truncated = tmp_path / "truncated.json"
    truncated.write_bytes(b'[{"text":"ok"}')
    with pytest.raises(ValueError, match="unterminated JSON array"):
        list(_stream_json_array(truncated))


def test_stream_json_array_rejects_leading_whitespace_above_scan_cap(tmp_path: Path) -> None:
    """A frozen source whose preamble is larger than the 1 MiB scan cap must
    raise ValueError rather than allocate the whole preamble into memory.

    The cap keeps ``_stream_json_array`` from regressing to the old behaviour
    where the initial scan accumulated ``prefix += chunk`` until the file was
    exhausted.
    """
    cap = 1 << 20
    path = _write_array(
        tmp_path,
        prefix=b" " * (cap * 2),
        body='[{"id":"never-reached"}]',
    )
    with pytest.raises(ValueError, match="leading whitespace"):
        list(_stream_json_array(path))


def test_stream_json_array_rejects_bom_then_oversized_whitespace(tmp_path: Path) -> None:
    """A BOM at the start must not reset the leading-whitespace cap.  Files
    that begin with a BOM and then add pathological whitespace still fail
    fast instead of being read into memory."""
    cap = 1 << 20
    path = _write_array(
        tmp_path,
        prefix=b"\xef\xbb\xbf" + b" " * (cap * 2),
        body='[{"id":"never-reached"}]',
    )
    with pytest.raises(ValueError, match="leading whitespace"):
        list(_stream_json_array(path))


def test_stream_json_array_rejects_non_array_first_byte(tmp_path: Path) -> None:
    """Sanity check: the first non-whitespace byte must be ``[``.  This is
    the existing contract and we don't want the BOM/scan changes to weaken
    it."""
    path = _write_array(tmp_path, prefix=b"", body='{"id":"a"}')
    with pytest.raises(ValueError, match="expected JSON array"):
        list(_stream_json_array(path))


def test_stream_json_array_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty or whitespace-only"):
        list(_stream_json_array(path))
