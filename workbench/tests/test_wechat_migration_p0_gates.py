from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from content_hub.app import create_app
from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.features.wechat.service import WechatService
from content_hub.repositories.wechat_legacy import WechatLegacyRepository


CANONICAL = "https://mp.weixin.qq.com/s/shared-canonical"
NOW = "2026-07-16T00:00:00Z"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """本文件所有测试都使用独立数据库、锁、资产和前端临时目录。"""
    base = Settings.load()
    isolated = replace(
        base,
        database_path=tmp_path / "hub.sqlite",
        lock_path=tmp_path / "hub.lock",
        asset_store_path=tmp_path / "asset_store",
        frontend_dist=tmp_path / "frontend-dist",
    )
    migrate(isolated)
    return isolated


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _records(
    *,
    title: str = "微信标题",
    article_id: str = "wx-1",
    canonical_url: str = CANONICAL,
    invalid: bool = False,
):
    return {
        "monitor": {"generated_at": NOW, "keywords": [], "accounts": []},
        "keywords": (
            [{"keyword_id": "kw-1", "keyword": ""}]
            if invalid
            else [{"keyword_id": "kw-1", "keyword": "测试词"}]
        ),
        "accounts": [{"account_id": "wx-creator", "canonical_name": "微信作者"}],
        "articles": [{
            "article_id": article_id,
            "normalized_url": canonical_url,
            "title": title,
            "account_id": "wx-creator",
            "author_name": "微信作者",
            "published_at": "2026-07-15T10:00:00+08:00",
            "content_file_path": "markdown/wechat.md",
        }],
        "snapshots": [],
        "hits": [],
        "terms": [],
        "observations": [],
        "keyword_read_deltas": [],
        "runtime": {},
    }


def _records_with_metric_collision(
    *,
    title: str,
    article_id: str,
    canonical_url: str,
) -> dict:
    records = _records(
        title=title,
        article_id=article_id,
        canonical_url=canonical_url,
    )
    records["observations"] = [
        {
            "observation_id": f"{article_id}-metric-a",
            "article_id": article_id,
            "observed_at": "2026-07-15T12:00:00Z",
            "read_count": 11,
        },
        {
            "observation_id": f"{article_id}-metric-b",
            "article_id": article_id,
            "observed_at": "2026-07-15T12:00:00Z",
            "read_count": 12,
        },
    ]
    return records


def _fake_source(settings, tmp_path: Path, records: dict) -> WechatService:
    root = tmp_path / "wechat-source"
    (root / "markdown").mkdir(parents=True)
    (root / "markdown/wechat.md").write_text("# 微信正文\n", encoding="utf-8")
    configured = replace(
        settings,
        wechat_source_root=root,
        wechat_source_url="http://127.0.0.1:1",
        asset_store_path=tmp_path / "assets",
    )
    service = WechatService(configured)
    manifest = {"articles": {"path": "normalized/articles.json", "size": 1, "sha256": "source"}}
    service.adapter.import_records = lambda limit=None: (records, manifest, {})
    return service


def _identity_domain_state(settings) -> dict[str, list[tuple]]:
    """只快照 dry-run 不得改写的业务事实表；命令与审计表允许留痕。"""
    statements = {
        "contents": """
            SELECT content_id,title,canonical_url,creator_id,author_name,published_at,
                   md_path,payload_json
            FROM contents ORDER BY content_id
        """,
        "identifiers": """
            SELECT namespace,external_id,content_id,payload_json
            FROM content_identifiers ORDER BY namespace,external_id
        """,
        "paths": """
            SELECT article_id,old_article_id,relative_path,asset_path,source_ref
            FROM wechat_article_paths
            ORDER BY article_id,old_article_id,source_ref
        """,
        "metrics": """
            SELECT observation_id,subject_type,subject_id,metric_key,
                   observed_at,numeric_value,snapshot_id
            FROM metric_observations ORDER BY observation_id
        """,
    }
    with connect(settings, readonly=True) as con:
        return {
            name: [tuple(row) for row in con.execute(statement)]
            for name, statement in statements.items()
        }


def _dry_metric_subjects(result: dict) -> set[str]:
    return {
        str(collision["natural_key"]["subject_id"])
        for collision in result["audit"]["metric_collisions"]
    }


def _seed_non_wechat_shared_content(settings) -> None:
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO creators(
                creator_id,canonical_name,platform,external_id,
                first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("mp-creator", "MP 作者", "mp", "mp-creator", NOW, NOW, _json({"source": "mp"})),
        )
        con.execute(
            """
            INSERT INTO contents(
                content_id,content_type,title,canonical_url,creator_id,author_name,
                published_at,first_seen_at,updated_at,md_path,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "mp-content",
                "external_article",
                "MP 原标题",
                CANONICAL,
                "mp-creator",
                "MP 原作者",
                "2026-07-01T00:00:00Z",
                NOW,
                NOW,
                "mp/original.md",
                _json({"owner": "mp", "immutable": True}),
            ),
        )
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("mp_article", "mp-1", "mp-content", NOW, _json({"source": "mp"})),
        )


def _seed_wechat_content(settings, *, content_id: str = "wx-owned") -> None:
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO creators(
                creator_id,canonical_name,platform,external_id,
                first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("old-creator", "旧作者", "wechat-search", "old-creator", NOW, NOW, "{}"),
        )
        con.execute(
            """
            INSERT INTO contents(
                content_id,content_type,title,canonical_url,creator_id,author_name,
                published_at,first_seen_at,updated_at,md_path,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                content_id,
                "external_article",
                "旧微信标题",
                "https://mp.weixin.qq.com/s/owned",
                "old-creator",
                "旧作者",
                "2026-07-01T00:00:00Z",
                NOW,
                NOW,
                "wechat/old.md",
                _json({"old": True}),
            ),
        )
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("wechat_article", content_id, content_id, NOW, "{}"),
        )


def _seed_content(
    settings,
    *,
    content_id: str,
    canonical_url: str,
    title: str,
    payload: dict | None = None,
) -> None:
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO contents(
                content_id,content_type,title,canonical_url,
                first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                content_id,
                "external_article",
                title,
                canonical_url,
                NOW,
                NOW,
                _json(payload or {"owner": content_id}),
            ),
        )


def _set_switches(settings, *contracts: str) -> None:
    with connect(settings) as con:
        for contract in contracts:
            con.execute(
                """
                UPDATE migration_switches
                SET data_mode='hub', updated_by='p0-gates'
                WHERE module_key='wechat-search' AND contract_key=?
                """,
                (contract,),
            )


def _latest_import_rows(settings, key: str | None = None):
    with connect(settings, readonly=True) as con:
        return (
            con.execute(
                """
                SELECT batch_id,status,records_failed,payload_json
                FROM ingestion_batches
                WHERE adapter_key='wechat-search'
                ORDER BY created_at DESC,batch_id DESC
                LIMIT 1
                """
            ).fetchone(),
            con.execute(
                """
                SELECT cursor_value,source_hash,batch_id,last_success_at,payload_json
                FROM ingestion_checkpoints
                WHERE adapter_key='wechat-search' AND checkpoint_key='normalized'
                """
            ).fetchone(),
            con.execute(
                """
                SELECT outcome,details_json
                FROM audit_log
                WHERE action='wechat.import'
                ORDER BY occurred_at DESC,audit_id DESC
                LIMIT 1
                """
            ).fetchone(),
            con.execute(
                """
                SELECT command_id,status
                FROM command_runs
                WHERE module_key='wechat-search' AND command_type='history-import'
                  AND (? IS NULL OR idempotency_key=?)
                ORDER BY created_at DESC,command_id DESC
                LIMIT 1
                """,
                (key, key),
            ).fetchone(),
        )


def _seed_checkpoint_baseline(settings, suffix: str) -> dict[str, str]:
    batch_id = f"baseline-batch-{suffix}"
    baseline = {
        "cursor_value": f"baseline-cursor-{suffix}",
        "source_hash": f"baseline-source-{suffix}",
        "batch_id": batch_id,
        "last_success_at": "2026-01-01T00:00:00Z",
    }
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO ingestion_batches(
                batch_id,adapter_key,source_scope,status,started_at,finished_at,
                created_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                batch_id,
                "wechat-search",
                "history",
                "succeeded",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "{}",
            ),
        )
        con.execute(
            """
            INSERT INTO ingestion_checkpoints(
                adapter_key,checkpoint_key,cursor_value,source_hash,
                last_success_at,batch_id,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "wechat-search",
                "normalized",
                baseline["cursor_value"],
                baseline["source_hash"],
                baseline["last_success_at"],
                baseline["batch_id"],
                "{}",
            ),
        )
    return baseline


def _seed_reconcile_extra(settings, suffix: str = "") -> None:
    content_id = f"extra-wx{suffix}"
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO contents(
                content_id,content_type,title,canonical_url,first_seen_at,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                content_id,
                "external_article",
                "额外",
                f"https://mp.weixin.qq.com/s/{content_id}",
                NOW,
                NOW,
            ),
        )
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("wechat_article", content_id, content_id, NOW, "{}"),
        )


def test_gr1_shared_canonical_preserves_mp_facts_and_pure_wechat_updates(settings, tmp_path):
    _seed_non_wechat_shared_content(settings)
    service = _fake_source(settings, tmp_path, _records())

    service.import_history(
        dry_run=False, limit=1, confirm=True, idempotency_key="gr1-first"
    )
    with connect(settings, readonly=True) as con:
        row = con.execute(
            """
            SELECT title,creator_id,author_name,published_at,md_path,payload_json
            FROM contents WHERE content_id='mp-content'
            """
        ).fetchone()
        identifiers = con.execute(
            """
            SELECT namespace,external_id,content_id FROM content_identifiers
            WHERE content_id='mp-content' ORDER BY namespace,external_id
            """
        ).fetchall()
        paths = con.execute(
            """
            SELECT article_id,old_article_id,relative_path,asset_path
            FROM wechat_article_paths WHERE article_id='mp-content'
            """
        ).fetchall()

    assert tuple(row) == (
        "MP 原标题",
        "mp-creator",
        "MP 原作者",
        "2026-07-01T00:00:00Z",
        "mp/original.md",
        _json({"owner": "mp", "immutable": True}),
    )
    assert ("mp_article", "mp-1", "mp-content") in [tuple(x) for x in identifiers]
    assert ("wechat_article", "wx-1", "mp-content") in [tuple(x) for x in identifiers]
    assert len(paths) == 1
    assert paths[0]["relative_path"] == "markdown/wechat.md"
    assert paths[0]["asset_path"]

    service.import_history(
        dry_run=False, limit=1, confirm=True, idempotency_key="gr1-replay"
    )
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM wechat_article_paths WHERE article_id='mp-content'"
        ).fetchone()[0] == 1

    _seed_wechat_content(settings)
    owned = _records(
        title="新微信标题",
        article_id="wx-owned",
        canonical_url="https://mp.weixin.qq.com/s/owned",
    )
    owned_service = _fake_source(settings, tmp_path / "owned", owned)
    with connect(settings) as con:
        owned_service._upsert_wechat_article(
            con,
            row=owned["articles"][0],
            source_id="wx-owned",
            now=NOW,
            report={"content_collisions": [], "collision_count": 0},
        )
    with connect(settings, readonly=True) as con:
        owned_row = con.execute(
            "SELECT title,creator_id,author_name FROM contents WHERE content_id='wx-owned'"
        ).fetchone()
    assert tuple(owned_row) == ("新微信标题", "wx-creator", "微信作者")


def test_gr1_existing_pure_wechat_identity_wins_over_shared_url_without_rebind(
    settings, tmp_path
):
    url_a = "https://mp.weixin.qq.com/s/conflict-a"
    _seed_content(
        settings,
        content_id="content-a",
        canonical_url=url_a,
        title="A 原标题",
    )
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("mp_article", "mp-conflict-a", "content-a", NOW, "{}"),
        )
    source_id = "wx-conflict"
    _seed_content(
        settings,
        content_id="content-b",
        canonical_url="https://mp.weixin.qq.com/s/conflict-b",
        title="B 原标题",
    )
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("wechat_article", source_id, "content-b", NOW, "{}"),
        )

    records = _records(
        title="B 更新后的微信标题",
        article_id=source_id,
        canonical_url=url_a,
    )
    service = _fake_source(settings, tmp_path, records)
    result = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="gr1-identity-preserved",
    )

    with connect(settings, readonly=True) as con:
        rows = {
            row["content_id"]: dict(row)
            for row in con.execute(
                """
                SELECT content_id,title,canonical_url,payload_json
                FROM contents WHERE content_id IN (?,?)
                """,
                ("content-a", "content-b"),
            )
        }
        identifier = con.execute(
            """
            SELECT content_id FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id=?
            """,
            (source_id,),
        ).fetchone()

    assert rows["content-a"]["title"] == "A 原标题"
    assert rows["content-b"]["title"] == "B 更新后的微信标题"
    assert identifier["content_id"] == "content-b"
    assert result["audit"]["rejected"] == []
    collisions = result["audit"]["content_collisions"]
    assert collisions
    assert any(
        item.get("identity_preserved") is True
        or item.get("resolution") == "identity_preserved"
        or "identity_preserved" in _json(item)
        for item in collisions
    )
    assert result["semantic_status"] == "succeeded"
    assert result["verified"] is True


def test_gr1_identity_first_dry_run_and_formal_import_preserve_pure_wechat_anchor(
    settings, tmp_path
):
    shared_url = "https://mp.weixin.qq.com/s/identity-first-shared"
    _seed_content(
        settings,
        content_id="identity-shared-a",
        canonical_url=shared_url,
        title="Shared A 原标题",
        payload={"owner": "mp", "immutable": True},
    )
    _seed_content(
        settings,
        content_id="identity-wechat-b",
        canonical_url="https://mp.weixin.qq.com/s/identity-first-wechat",
        title="WeChat B 原标题",
        payload={"owner": "wechat"},
    )
    article_id = "wx-identity-first"
    with connect(settings) as con:
        con.executemany(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                ("mp_article", "mp-identity-shared", "identity-shared-a", NOW, "{}"),
                ("wechat_article", article_id, "identity-wechat-b", NOW, "{}"),
            ),
        )

    records = _records_with_metric_collision(
        title="WeChat B 更新标题",
        article_id=article_id,
        canonical_url=shared_url,
    )
    service = _fake_source(settings, tmp_path, records)
    before_dry_run = _identity_domain_state(settings)
    dry_run = service.import_history(
        dry_run=True,
        limit=None,
        confirm=True,
        idempotency_key="gr1-identity-first-dry",
    )

    assert _identity_domain_state(settings) == before_dry_run
    dry_collision = next(
        item
        for item in dry_run["audit"]["content_collisions"]
        if item.get("collision_type") == "identity_preserved"
    )
    assert dry_collision["identity_content_id"] == "identity-wechat-b"
    assert dry_collision["canonical_url_content_id"] == "identity-shared-a"
    assert _dry_metric_subjects(dry_run) == {"identity-wechat-b"}

    formal = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="gr1-identity-first-formal",
    )
    formal_collision = next(
        item
        for item in formal["audit"]["content_collisions"]
        if item.get("collision_type") == "identity_preserved"
    )
    assert formal_collision["identity_content_id"] == "identity-wechat-b"
    assert formal_collision["canonical_url_content_id"] == "identity-shared-a"

    with connect(settings, readonly=True) as con:
        rows = {
            row["content_id"]: dict(row)
            for row in con.execute(
                """
                SELECT content_id,title,canonical_url,payload_json
                FROM contents
                WHERE content_id IN ('identity-shared-a','identity-wechat-b')
                """
            )
        }
        identifier_target = con.execute(
            """
            SELECT content_id FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id=?
            """,
            (article_id,),
        ).fetchone()["content_id"]
        metric_subjects = {
            row["subject_id"]
            for row in con.execute(
                """
                SELECT DISTINCT subject_id FROM metric_observations
                WHERE metric_key LIKE 'wechat.article.%'
                """
            )
        }

    assert rows["identity-shared-a"] == {
        "content_id": "identity-shared-a",
        "title": "Shared A 原标题",
        "canonical_url": shared_url,
        "payload_json": _json({"owner": "mp", "immutable": True}),
    }
    assert rows["identity-wechat-b"]["title"] == "WeChat B 更新标题"
    assert rows["identity-wechat-b"]["canonical_url"] == (
        "https://mp.weixin.qq.com/s/identity-first-wechat"
    )
    assert identifier_target == "identity-wechat-b"
    assert metric_subjects == {"identity-wechat-b"}


def test_gr1_identity_first_dry_run_and_formal_reject_illegal_identifier_conflict(
    settings, tmp_path
):
    shared_url = "https://mp.weixin.qq.com/s/identity-reject-a"
    _seed_content(
        settings,
        content_id="identity-reject-a",
        canonical_url=shared_url,
        title="Reject A 原标题",
    )
    _seed_content(
        settings,
        content_id="identity-reject-b",
        canonical_url="https://mp.weixin.qq.com/s/identity-reject-b",
        title="Reject B 原标题",
    )
    article_id = "wx-identity-reject"
    with connect(settings) as con:
        con.executemany(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                ("mp_article", "mp-identity-reject-a", "identity-reject-a", NOW, "{}"),
                ("wechat_article", article_id, "identity-reject-b", NOW, "{}"),
                ("mp_article", "mp-identity-reject-b", "identity-reject-b", NOW, "{}"),
            ),
        )

    records = _records_with_metric_collision(
        title="不应映射的冲突标题",
        article_id=article_id,
        canonical_url=shared_url,
    )
    service = _fake_source(settings, tmp_path, records)
    before_dry_run = _identity_domain_state(settings)
    dry_run = service.import_history(
        dry_run=True,
        limit=None,
        confirm=True,
        idempotency_key="gr1-identity-reject-dry",
    )

    assert _identity_domain_state(settings) == before_dry_run
    assert any(
        item.get("kind") == "article_identity_conflict"
        for item in dry_run["audit"]["rejected"]
    )
    assert dry_run["audit"]["metric_fact_count"] == 0
    assert dry_run["audit"]["metric_unique_count"] == 0
    assert _dry_metric_subjects(dry_run) == set()

    formal = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="gr1-identity-reject-formal",
    )
    assert any(
        item.get("kind") == "article_identity_conflict"
        for item in formal["audit"]["rejected"]
    )
    assert formal["audit"]["metric_fact_count"] == 0
    assert formal["audit"]["metric_unique_count"] == 0
    with connect(settings, readonly=True) as con:
        titles = {
            row["content_id"]: row["title"]
            for row in con.execute(
                """
                SELECT content_id,title FROM contents
                WHERE content_id IN ('identity-reject-a','identity-reject-b')
                """
            )
        }
        identifier_target = con.execute(
            """
            SELECT content_id FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id=?
            """,
            (article_id,),
        ).fetchone()["content_id"]
        metric_count = con.execute(
            "SELECT COUNT(*) FROM metric_observations"
        ).fetchone()[0]

    assert titles == {
        "identity-reject-a": "Reject A 原标题",
        "identity-reject-b": "Reject B 原标题",
    }
    assert identifier_target == "identity-reject-b"
    assert metric_count == 0


def test_gr1_identity_first_dry_run_and_formal_map_shared_url_without_overwrite(
    settings, tmp_path
):
    _seed_non_wechat_shared_content(settings)
    records = _records_with_metric_collision(
        title="不应覆盖 Shared A 的微信标题",
        article_id="wx-shared-url-only",
        canonical_url=CANONICAL,
    )
    service = _fake_source(settings, tmp_path, records)
    before_dry_run = _identity_domain_state(settings)
    dry_run = service.import_history(
        dry_run=True,
        limit=None,
        confirm=True,
        idempotency_key="gr1-shared-url-dry",
    )

    assert _identity_domain_state(settings) == before_dry_run
    assert dry_run["audit"]["rejected"] == []
    dry_article_collision = next(
        item
        for item in dry_run["audit"]["content_collisions"]
        if item.get("content_id") == "mp-content"
    )
    assert dry_article_collision["preserved_fields"]
    assert _dry_metric_subjects(dry_run) == {"mp-content"}

    formal = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="gr1-shared-url-formal",
    )
    assert formal["audit"]["rejected"] == []
    formal_article_collision = next(
        item
        for item in formal["audit"]["content_collisions"]
        if item.get("content_id") == "mp-content"
    )
    assert formal_article_collision["preserved_fields"] == (
        dry_article_collision["preserved_fields"]
    )
    with connect(settings, readonly=True) as con:
        content = con.execute(
            """
            SELECT title,creator_id,author_name,published_at,md_path,payload_json
            FROM contents WHERE content_id='mp-content'
            """
        ).fetchone()
        identifier_target = con.execute(
            """
            SELECT content_id FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id='wx-shared-url-only'
            """
        ).fetchone()["content_id"]
        metric_subjects = {
            row["subject_id"]
            for row in con.execute(
                """
                SELECT DISTINCT subject_id FROM metric_observations
                WHERE metric_key LIKE 'wechat.article.%'
                """
            )
        }

    assert tuple(content) == (
        "MP 原标题",
        "mp-creator",
        "MP 原作者",
        "2026-07-01T00:00:00Z",
        "mp/original.md",
        _json({"owner": "mp", "immutable": True}),
    )
    assert identifier_target == "mp-content"
    assert metric_subjects == {"mp-content"}


def test_gr1_shared_existing_identity_cannot_be_rebound_to_other_url_content(
    settings, tmp_path
):
    url_a = "https://mp.weixin.qq.com/s/rebind-a"
    _seed_content(
        settings,
        content_id="rebind-a",
        canonical_url=url_a,
        title="A 原标题",
    )
    _seed_content(
        settings,
        content_id="shared-b",
        canonical_url="https://mp.weixin.qq.com/s/rebind-b",
        title="Shared B 原标题",
    )
    with connect(settings) as con:
        con.executemany(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                ("wechat_article", "wx-shared-b", "shared-b", NOW, "{}"),
                ("mp_article", "mp-shared-b", "shared-b", NOW, "{}"),
            ),
        )
    service = _fake_source(
        settings,
        tmp_path,
        _records(
            title="不应写入任一内容",
            article_id="wx-shared-b",
            canonical_url=url_a,
        ),
    )
    result = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="gr1-shared-rebind-rejected",
    )
    with connect(settings, readonly=True) as con:
        identifier = con.execute(
            """
            SELECT content_id FROM content_identifiers
            WHERE namespace='wechat_article' AND external_id='wx-shared-b'
            """
        ).fetchone()
        titles = {
            row["content_id"]: row["title"]
            for row in con.execute(
                "SELECT content_id,title FROM contents WHERE content_id IN ('rebind-a','shared-b')"
            )
        }
    assert identifier["content_id"] == "shared-b"
    assert titles == {"rebind-a": "A 原标题", "shared-b": "Shared B 原标题"}
    assert result["audit"]["rejected"]
    assert result["semantic_status"] != "succeeded"
    assert result["verified"] is False


@pytest.mark.parametrize("relation_kind", ["discovery", "hit"])
def test_gr1_wechat_relation_without_identifier_does_not_own_content(
    settings, tmp_path, relation_kind
):
    content_id = f"relation-only-{relation_kind}"
    canonical_url = f"https://mp.weixin.qq.com/s/{content_id}"
    _seed_content(
        settings,
        content_id=content_id,
        canonical_url=canonical_url,
        title="关系行原标题",
        payload={"owner": "external"},
    )
    with connect(settings) as con:
        if relation_kind == "discovery":
            con.execute(
                """
                INSERT INTO content_discoveries(
                    discovery_id,content_id,discovery_system,discovery_channel,
                    discovered_at,payload_json
                ) VALUES(?,?,?,?,?,?)
                """,
                ("disc-relation-only", content_id, "wechat-search", "keyword-rank", NOW, "{}"),
            )
        else:
            con.execute(
                """
                INSERT INTO keywords(
                    keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                ("kw-relation-only", "wechat-search", "关系词", "active", NOW, NOW, "{}"),
            )
            con.execute(
                """
                INSERT INTO search_snapshots(
                    snapshot_id,platform,keyword,keyword_id,captured_at,result_count
                ) VALUES(?,?,?,?,?,?)
                """,
                ("snap-relation-only", "wechat-search", "关系词", "kw-relation-only", NOW, 1),
            )
            con.execute(
                """
                INSERT INTO search_hits(hit_id,snapshot_id,rank,content_id,payload_json)
                VALUES(?,?,?,?,?)
                """,
                ("hit-relation-only", "snap-relation-only", 1, content_id, "{}"),
            )

    service = _fake_source(settings, tmp_path, _records())
    with connect(settings) as con:
        _, is_owned = service._upsert_wechat_article(
            con,
            row={
                "article_id": content_id,
                "normalized_url": canonical_url,
                "title": "不应覆盖的新标题",
                "account_id": "wx-creator",
            },
            source_id=content_id,
            now=NOW,
            report={"content_collisions": [], "collision_count": 0},
        )
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT title,payload_json FROM contents WHERE content_id=?",
            (content_id,),
        ).fetchone()
    assert is_owned is False
    assert row["title"] == "关系行原标题"
    assert json.loads(row["payload_json"]) == {"owner": "external"}


def test_gr2_full_import_requires_key_replays_and_rejects_changed_input(settings, tmp_path):
    service = _fake_source(settings, tmp_path, _records())
    with pytest.raises(ValidationAppError, match="幂等键"):
        service.import_history(dry_run=False, limit=None, confirm=True, idempotency_key="")

    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                missing_import_key = await client.post(
                    "/api/v1/wechat/import",
                    json={"confirm": True},
                )
                return missing_import_key

    import_response = asyncio.run(scenario())
    assert import_response.status_code in {400, 422}
    assert "幂等键" in import_response.text

    first = service.import_history(
        dry_run=False, limit=1, confirm=True, idempotency_key="gr2-same"
    )
    replay = service.import_history(
        dry_run=False, limit=1, confirm=True, idempotency_key="gr2-same"
    )
    assert replay["command_id"] == first["command_id"]
    with pytest.raises(
        ConflictError,
        match="同一幂等键不能用于不同的微信导入请求参数",
    ):
        service.import_history(
            dry_run=True,
            limit=1,
            confirm=True,
            idempotency_key="gr2-same",
        )
    with pytest.raises(
        ConflictError,
        match="同一幂等键不能用于不同的微信导入请求参数",
    ):
        service.import_history(
            dry_run=False, limit=None, confirm=True, idempotency_key="gr2-same"
        )


def test_gr3_asset_integrity_does_not_multiply_paths_by_wechat_identifiers(
    settings,
):
    content_id = "asset-shared-content"
    _seed_content(
        settings,
        content_id=content_id,
        canonical_url="https://mp.weixin.qq.com/s/asset-shared",
        title="共享资产文章",
    )
    asset_root = settings.asset_store_path / "wechat"
    asset_root.mkdir(parents=True, exist_ok=True)
    (asset_root / "asset-a.md").write_bytes(b"# ASSET A\n")
    (asset_root / "asset-b.md").write_bytes(b"# ASSET B\n")
    with connect(settings) as con:
        con.executemany(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                ("wechat_article", "wx-asset-a", content_id, NOW, "{}"),
                ("wechat_article", "wx-asset-b", content_id, NOW, "{}"),
            ),
        )
        con.executemany(
            """
            INSERT INTO wechat_article_paths(
                article_id,old_article_id,relative_path,asset_path,source_ref,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                (
                    content_id,
                    "wx-asset-a",
                    "markdown/asset-a.md",
                    "wechat/asset-a.md",
                    "fixture://asset-a",
                    NOW,
                ),
                (
                    content_id,
                    "wx-asset-b",
                    "markdown/asset-b.md",
                    "wechat/asset-b.md",
                    "fixture://asset-b",
                    NOW,
                ),
            ),
        )
    records = {
        "articles": [
            {
                "article_id": "wx-asset-a",
                "content_file_path": "markdown/asset-a.md",
            },
            {
                "article_id": "wx-asset-b",
                "content_file_path": "markdown/asset-b.md",
            },
        ]
    }

    result = WechatService(settings)._asset_integrity(records)
    assert result["expected_path_records"] == 2
    assert result["path_records"] == 2
    assert result["readable_assets"] == 2
    assert result["asset_blob_count"] == 2
    assert result["sha256_mismatch_count"] == 2
    assert result["verified"] is False


def test_gr3_asset_integrity_exposes_real_duplicate_path_rows(settings):
    content_id = "asset-duplicate-content"
    _seed_content(
        settings,
        content_id=content_id,
        canonical_url="https://mp.weixin.qq.com/s/asset-duplicate",
        title="重复路径文章",
    )
    asset_root = settings.asset_store_path / "wechat"
    asset_root.mkdir(parents=True, exist_ok=True)
    (asset_root / "duplicate.md").write_bytes(b"# DUPLICATE\n")
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("wechat_article", "wx-asset-duplicate", content_id, NOW, "{}"),
        )
        con.executemany(
            """
            INSERT INTO wechat_article_paths(
                article_id,old_article_id,relative_path,asset_path,source_ref,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                (
                    content_id,
                    "wx-asset-duplicate",
                    "markdown/duplicate.md",
                    "wechat/duplicate.md",
                    "fixture://duplicate-first",
                    NOW,
                ),
                (
                    content_id,
                    "wx-asset-duplicate",
                    "markdown/duplicate.md",
                    "wechat/duplicate.md",
                    "fixture://duplicate-second",
                    NOW,
                ),
            ),
        )
    records = {
        "articles": [{
            "article_id": "wx-asset-duplicate",
            "content_file_path": "markdown/duplicate.md",
        }]
    }

    result = WechatService(settings)._asset_integrity(records)
    assert result["expected_path_records"] == 1
    assert result["path_records"] == 2
    assert result["readable_assets"] == 2
    assert result["asset_blob_count"] == 1
    assert result["sha256_mismatch_count"] == 2
    assert result["verified"] is False


@pytest.mark.parametrize(
    "case",
    ["reconcile_mismatch", "asset_integrity", "rejected_rows", "success"],
)
def test_gr3_import_gate_semantics_and_checkpoint(
    case, settings, tmp_path, monkeypatch
):
    if case == "reconcile_mismatch":
        _seed_reconcile_extra(settings)
        records = _records()
    elif case == "asset_integrity":
        records = _records()
        source = _fake_source(settings, tmp_path, records)
        source.adapter.read_markdown_with_source = lambda relative: (_ for _ in ()).throw(
            WechatSourceError(relative, status=404)
        )
    elif case == "rejected_rows":
        records = _records(invalid=True)
    else:
        records = _records()

    checkpoint_before = (
        _seed_checkpoint_baseline(settings, case)
        if case != "success"
        else None
    )
    service = locals().get("source") or _fake_source(settings, tmp_path, records)
    service_error = None
    try:
        result = service.import_history(
            dry_run=False,
            limit=None,
            confirm=True,
            idempotency_key=f"gr3-{case}",
        )
    except sqlite3.IntegrityError as exc:
        # Keep the contract assertions below observable even when the current
        # implementation attempts to persist an invalid command status.
        service_error = exc
        result = None
    batch, checkpoint, audit, command = _latest_import_rows(
        settings, f"gr3-{case}"
    )
    report = json.loads(batch["payload_json"])
    audit_details = json.loads(audit["details_json"])
    assert audit_details["batch_id"] == batch["batch_id"]
    assert audit_details["command_id"] == command["command_id"]

    if case == "success":
        assert service_error is None
        assert result["audit"]["reconcile"]["status"] == "matched"
        assert result["audit"]["asset_integrity"]["verified"] is True
        assert batch["status"] == "succeeded"
        assert command["status"] == "succeeded"
        assert audit["outcome"] == "succeeded"
        assert checkpoint["last_success_at"] is not None
        assert checkpoint["batch_id"] == batch["batch_id"]
    else:
        assert service_error is None, (
            "正式导入应返回可审计的非成功结果，当前在 command_runs 落盘时失败："
            f"{service_error}"
        )
        assert batch["status"] != "succeeded"
        assert command["status"] != "succeeded"
        assert audit["outcome"] == "failed"
        assert {
            field: checkpoint[field]
            for field in (
                "cursor_value",
                "source_hash",
                "batch_id",
                "last_success_at",
            )
        } == checkpoint_before
        assert report["reconcile"]["status"] == (
            "mismatch" if case == "reconcile_mismatch" else report["reconcile"]["status"]
        )
        if case == "asset_integrity":
            assert report["asset_integrity"]["verified"] is False
        if case == "rejected_rows":
            assert report["rejected"]
            assert batch["records_failed"] > 0
        assert audit_details["semantic_status"] == batch["status"]

        http_settings = replace(
            service.settings,
            database_path=tmp_path / f"http-{case}.sqlite",
            lock_path=tmp_path / f"http-{case}.lock",
            asset_store_path=tmp_path / f"http-{case}-assets",
        )
        migrate(http_settings)
        _seed_checkpoint_baseline(http_settings, f"http-{case}")
        if case == "reconcile_mismatch":
            _seed_reconcile_extra(http_settings, "-http")
        manifest = {
            "articles": {
                "path": "normalized/articles.json",
                "size": 1,
                "sha256": f"http-{case}",
            }
        }
        monkeypatch.setattr(
            WechatAdapter,
            "import_records",
            lambda self, limit=None: (records, manifest, {}),
        )
        if case == "asset_integrity":
            monkeypatch.setattr(
                WechatAdapter,
                "read_markdown_with_source",
                lambda self, relative: (_ for _ in ()).throw(
                    WechatSourceError(relative, status=404)
                ),
            )

        async def http_import():
            app = create_app(http_settings)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=app,
                        raise_app_exceptions=False,
                    ),
                    base_url="http://test",
                ) as client:
                    return await client.post(
                        "/api/v1/wechat/import",
                        json={
                            "confirm": True,
                            "idempotency_key": f"gr3-http-{case}",
                        },
                    )

        response = asyncio.run(http_import())
        assert response.status_code >= 400
        payload = response.json()
        assert payload["ok"] is False
        assert payload["data"]["semantic_status"] in {"failed", "partial_failed"}


def test_gr4_reads_wechat_asset_only_and_rejects_missing_traversal_and_symlink(
    settings, tmp_path
):
    isolated = replace(settings, asset_store_path=tmp_path / "asset_store")
    _set_switches(isolated, "article-content", "article-hit-detail")
    _seed_non_wechat_shared_content(isolated)
    asset_root = isolated.asset_store_path
    asset_root.mkdir(parents=True, exist_ok=True)
    asset = asset_root / "wechat/shared.md"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"# WECHAT ASSET\n")

    with connect(isolated) as con:
        con.execute(
            """
            INSERT INTO wechat_article_paths(
                article_id,old_article_id,relative_path,asset_path,source_ref,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("mp-content", "wx-1", "wechat/source.md", "wechat/shared.md", "fixture", NOW),
        )
        con.execute(
            "UPDATE contents SET md_path='mp/original.md' WHERE content_id='mp-content'"
        )
        con.execute(
            """
            INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)
            """,
            ("wechat_article", "wx-1", "mp-content", NOW, "{}"),
        )

    async def request(path: str):
        app = create_app(isolated)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get(path)

    content = asyncio.run(request("/api/article-content?path=wechat/source.md"))
    assert content.status_code == 200
    assert content.json() == {"path": "wechat/source.md", "markdown": "# WECHAT ASSET\n"}
    detail = asyncio.run(request("/api/article-hit-detail?article_id=wx-1"))
    assert detail.status_code == 200
    assert {item["path"] for item in detail.json()["content_files"]} == {"wechat/source.md"}

    with connect(isolated) as con:
        con.execute(
            "DELETE FROM wechat_article_paths WHERE article_id='mp-content'"
        )
    missing = asyncio.run(request("/api/article-content?path=mp/original.md"))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    traversal = asyncio.run(request("/api/article-content?path=../mp/original.md"))
    assert traversal.status_code == 400

    outside = tmp_path / "outside.md"
    outside.write_text("# OUTSIDE\n", encoding="utf-8")
    symlink = asset_root / "wechat/symlink.md"
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("当前文件系统不允许创建软链接")
    with connect(isolated) as con:
        con.execute(
            """
            INSERT INTO wechat_article_paths(
                article_id,old_article_id,relative_path,asset_path,source_ref,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("mp-content", "wx-1", "wechat/symlink.md", "wechat/symlink.md", "fixture-symlink", NOW),
        )
    blocked = asyncio.run(request("/api/article-content?path=wechat/symlink.md"))
    assert blocked.status_code in {400, 404, 422}
