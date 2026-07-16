from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from content_hub.adapters.wechat import WechatAdapter, WechatSourceError
from content_hub.db.connection import connect
from content_hub.db.migrations import migrate
from content_hub.features.wechat.service import WechatService


def _sealed_root(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    freeze_name: str = "freeze_20260716T000000+0800",
) -> tuple[Path, Path]:
    freeze = tmp_path / freeze_name
    payload = freeze / "payload"
    payload.mkdir(parents=True)
    files = files or {"normalized/monitor-data.json": b"{}"}
    entries = []
    for relative, content in files.items():
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append({
            "path": f"payload/{relative}",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    (freeze / "file-manifest.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
        encoding="utf-8",
    )
    return freeze, payload


def test_freeze_seal_accepts_current_real_freeze(settings):
    result = WechatAdapter(settings).verify_freeze_seal()
    assert result["status"] == "verified"
    assert result["entry_count"] > 100_000


@pytest.mark.parametrize("mutation", ["tamper", "add", "delete", "symlink"])
def test_freeze_seal_rejects_mutation_without_rewriting_manifest(settings, tmp_path, mutation):
    freeze, payload = _sealed_root(tmp_path)
    if mutation == "tamper":
        (payload / "normalized/monitor-data.json").write_bytes(b'{"changed":true}')
    elif mutation == "add":
        (payload / "added.json").write_text("{}", encoding="utf-8")
    elif mutation == "delete":
        (payload / "normalized/monitor-data.json").unlink()
    else:
        outside = tmp_path / "outside.json"
        outside.write_text("outside", encoding="utf-8")
        os.symlink(outside, payload / "link.json")
    configured = replace(settings, wechat_source_root=payload)
    with pytest.raises(WechatSourceError, match="seal"):
        WechatAdapter(configured).verify_freeze_seal()
    assert (freeze / "file-manifest.jsonl").read_text(encoding="utf-8").count("monitor-data") == 1


def test_freeze_seal_accepts_same_size_ds_store_for_trusted_freeze_without_supplement(
    settings, tmp_path
):
    freeze, payload = _sealed_root(
        tmp_path,
        files={".DS_Store": b"sealed", "normalized/monitor-data.json": b"{}"},
        freeze_name="freeze_20260716T024524+0800",
    )
    (payload / ".DS_Store").write_bytes(b"change")
    configured = replace(settings, wechat_source_root=payload)
    result = WechatAdapter(configured).verify_freeze_seal()
    assert result["ignored_ds_store_mismatch_count"] == 1
    assert result["ignored_runtime_artifact_count"] == 0

    (payload / "code-snapshot/pkg/__pycache__/new.cpython-311.pyc").parent.mkdir(
        parents=True
    )
    (payload / "code-snapshot/pkg/__pycache__/new.cpython-311.pyc").write_bytes(b"pyc")
    with pytest.raises(WechatSourceError, match="seal"):
        WechatAdapter(configured).verify_freeze_seal()

def test_freeze_seal_rejects_tampered_trusted_supplement(settings, tmp_path):
    freeze, payload = _sealed_root(
        tmp_path,
        freeze_name="freeze_20260716T024524+0800",
    )
    source_supplement = (
        settings.project_root
        / "data/migration/wechat/freeze_20260716T024524+0800-runtime-artifacts.json"
    )
    supplement = freeze.parent / f"{freeze.name}-runtime-artifacts.json"
    supplement.write_bytes(source_supplement.read_bytes())
    configured = replace(settings, wechat_source_root=payload)
    assert WechatAdapter(configured).verify_freeze_seal()["status"] == "verified"
    supplement.write_bytes(supplement.read_bytes() + b"\n")
    with pytest.raises(WechatSourceError, match="seal"):
        WechatAdapter(configured).verify_freeze_seal()


def test_freeze_seal_rejects_unknown_freeze_supplement(settings, tmp_path):
    freeze, payload = _sealed_root(tmp_path)
    (freeze.parent / f"{freeze.name}-runtime-artifacts.json").write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": "code-snapshot/pkg/__pycache__/module.cpython-311.pyc",
                        "size": 3,
                        "sha256": hashlib.sha256(b"pyc").hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    configured = replace(settings, wechat_source_root=payload)
    with pytest.raises(WechatSourceError, match="seal"):
        WechatAdapter(configured).verify_freeze_seal()


def test_streaming_import_rolls_back_every_business_table_on_mid_phase_failure(
    settings, tmp_path, monkeypatch
):
    from test_wechat_import_resources import _source

    configured = _source(settings, tmp_path)
    migrate(configured)
    service = WechatService(configured)
    monkeypatch.setattr(service, "_large_source_requires_streaming", lambda: True)

    def fail_after_prior_phases(*args, **kwargs):
        raise RuntimeError("injected mid-stream failure")

    monkeypatch.setattr(service, "_stream_metric_phase", fail_after_prior_phases)
    with pytest.raises(RuntimeError, match="injected"):
        service.import_history(
            dry_run=False,
            limit=None,
            confirm=True,
            idempotency_key="atomic-mid-phase-failure",
        )

    business_tables = (
        "source_manifests", "source_manifest_entries", "ingestion_batches",
        "ingestion_checkpoints", "keywords", "creators", "contents",
        "content_identifiers", "wechat_article_paths", "search_snapshots",
        "search_hits", "content_discoveries", "metric_definitions",
        "metric_observations", "wechat_legacy_projections",
        "search_keyword_groups",
    )
    with connect(configured, readonly=True) as con:
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in business_tables
        }
        assert all(value == 0 for value in counts.values()), counts
        assert con.execute(
            "SELECT COUNT(*) FROM ingestion_batches WHERE status='running'"
        ).fetchone()[0] == 0
        command = con.execute(
            "SELECT status FROM command_runs WHERE idempotency_key=?",
            ("atomic-mid-phase-failure",),
        ).fetchone()
        assert command["status"] == "failed"
        assert con.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='wechat.import' AND outcome='failed'"
        ).fetchone()[0] >= 1

    monkeypatch.setattr(
        service,
        "_stream_metric_phase",
        lambda *args, **kwargs: None,
    )
    replay = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="atomic-mid-phase-failure",
    )
    assert replay["semantic_status"] == "succeeded"
    assert replay["verified"] is True
    with connect(configured, readonly=True) as con:
        assert con.execute(
            "SELECT status FROM command_runs WHERE idempotency_key=?",
            ("atomic-mid-phase-failure",),
        ).fetchone()["status"] == "succeeded"
        assert con.execute(
            "SELECT COUNT(*) FROM ingestion_batches WHERE status='running'"
        ).fetchone()[0] == 0
        first_counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in business_tables
        }
    replay_again = service.import_history(
        dry_run=False,
        limit=None,
        confirm=True,
        idempotency_key="atomic-mid-phase-failure",
    )
    assert replay_again["command_id"] == replay["command_id"]
    assert replay_again["batch_id"] == replay["batch_id"]
    with connect(configured, readonly=True) as con:
        assert {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in business_tables
        } == first_counts


def test_asset_integrity_hashes_fallback_code_snapshot_and_counts_unique_blob(
    settings, tmp_path
):
    source_root = tmp_path / "source"
    (source_root / "code-snapshot").mkdir(parents=True)
    markdown = b"# fallback body\n"
    (source_root / "code-snapshot/article.md").write_bytes(markdown)
    asset_root = tmp_path / "assets/wechat"
    asset_root.mkdir(parents=True)
    digest = hashlib.sha256(markdown).hexdigest()
    (asset_root / f"{digest}.md").write_bytes(markdown)
    configured = replace(
        settings,
        wechat_source_root=source_root,
        asset_store_path=tmp_path / "assets",
        database_path=tmp_path / "hub.sqlite",
        lock_path=tmp_path / "hub.lock",
    )
    migrate(configured)
    with connect(configured) as con:
        con.execute(
            "INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,payload_json) "
            "VALUES('c1','external_article','fallback','2026-07-16','2026-07-16','{}')"
        )
        con.execute(
            "INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json) "
            "VALUES('wechat_article','a1','c1','2026-07-16','{}')"
        )
        con.execute(
            "INSERT INTO wechat_article_paths(article_id,old_article_id,relative_path,asset_path,source_ref,created_at) "
            "VALUES('c1','a1','article.md',?, 'fixture://fallback','2026-07-16')",
            (f"wechat/{digest}.md",),
        )
    result = WechatService(configured)._asset_integrity(
        {"articles": [{"article_id": "a1", "content_file_path": "article.md"}]}
    )
    assert result["verified"] is True
    assert result["fallback_code_snapshot_count"] == 1
    assert result["source_unique_blob_count"] == 1

    (asset_root / f"{digest}.md").write_bytes(b"# tampered\n")
    result = WechatService(configured)._asset_integrity(
        {"articles": [{"article_id": "a1", "content_file_path": "article.md"}]}
    )
    assert result["verified"] is False
    assert result["sha256_mismatch_count"] == 1
    assert result["sha256_mismatches"][0]["reason"] == "sha256_mismatch"
