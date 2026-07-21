from __future__ import annotations

import json

import content_hub.repositories.wechat_legacy as wechat_legacy_module
import content_hub.services.wechat_live_projection as live_projection_module
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.features.wechat.service import WechatService
from content_hub.repositories.wechat_legacy import WechatLegacyRepository


def _projection(
    projection_id: str,
    kind: str,
    generated_at: str,
    updated_at: str,
    payload: dict,
) -> tuple:
    return (
        projection_id,
        kind,
        "",
        json.dumps({"generated_at": generated_at, **payload}),
        projection_id,
        f"manifest-{projection_id}",
        "test://wechat",
        updated_at,
    )


def test_bootstrap_reads_compact_projection_only_and_decodes_one_payload(
    settings, monkeypatch
):
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    ("kw-1", "wechat-search", "港险", "active", "2026-07-18T00:00:00Z", "2026-07-18T00:00:00Z", "{}"),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "snap-new",
                        "wechat-search",
                        "港险",
                        "kw-1",
                        "2026-07-18T18:00:00Z",
                        1,
                        "{}",
                    ),
                )
                con.executemany(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    [
                        _projection(
                            "old",
                            "bootstrap",
                            "2026-07-18T16:00:00Z",
                            "2026-07-18T17:00:00Z",
                            {"keywords": [{"keyword_id": "kw-1", "today_best": 9, "runs": [{"huge": True}]}]},
                        ),
                        _projection(
                            "new",
                            "bootstrap",
                            "2026-07-18T18:00:00Z",
                            "2026-07-18T18:01:00Z",
                            {"keywords": [{"keyword_id": "kw-1", "today_best": 1, "runs": [{"huge": True}]}]},
                        ),
                        _projection(
                            "full",
                            "full",
                            "2026-07-18T18:00:00Z",
                            "2026-07-19T00:02:00Z",
                            {"runs": [{"huge": True}], "topics": [{"huge": True}], "keywords": [{"huge": True}]},
                        ),
                    ],
                )

    decode_calls = 0
    original_decode = wechat_legacy_module._projection_payload

    def counted_decode(raw):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(raw)

    monkeypatch.setattr(
        wechat_legacy_module, "_projection_payload", counted_decode
    )
    repo = WechatLegacyRepository(settings)
    seen: list[str] = []
    original = repo._projection

    def tracked(kind: str, subject_id: str = ""):
        seen.append(kind)
        return original(kind, subject_id)

    repo._projection = tracked  # type: ignore[method-assign]
    result = repo.bootstrap()

    assert seen == ["bootstrap"]
    assert decode_calls == 1
    assert result["generated_at"] == "2026-07-18T18:00:00"
    assert result["keywords"][0]["today_best"] == 1
    assert "runs" not in result["keywords"][0]
    assert "topics" not in result


def test_equal_timestamp_projection_repairs_stale_cross_day_window(settings):
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "kw-1",
                        "wechat-search",
                        "港险",
                        "active",
                        "2026-07-18T00:00:00Z",
                        "2026-07-18T00:00:00Z",
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "snap-late-utc",
                        "wechat-search",
                        "港险",
                        "kw-1",
                        "2026-07-18T18:15:12.947Z",
                        1,
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    _projection(
                        "stale-cross-day",
                        "bootstrap",
                        "2026-07-18T18:15:12.947Z",
                        "2026-07-18T18:16:00Z",
                        {
                            "window_days": 15,
                            "window_start": "2026-07-05",
                            "window_end": "2026-07-19",
                            "keywords": [
                                {
                                    "keyword_id": "kw-1",
                                    "window_days": 15,
                                    "window_start": "2026-07-05",
                                    "window_end": "2026-07-19",
                                    "keyword_read_delta": {
                                        "window_start": "2026-07-05",
                                        "window_end": "2026-07-19",
                                        "status": "ok",
                                    },
                                    "latest_run": {
                                        "id": "snap-late-utc",
                                        "date": "2026-07-19",
                                        "time": "02:15",
                                        "run_at": "2026-07-19 02:15",
                                    },
                                    "runs": [
                                        {
                                            "id": "snap-late-utc",
                                            "date": "2026-07-19",
                                            "time": "02:15",
                                            "run_at": "2026-07-19 02:15",
                                        }
                                    ],
                                }
                            ],
                        },
                    ),
                )

    result = WechatLegacyRepository(settings).bootstrap()

    assert result["generated_at"] == "2026-07-18T18:15:12.947"
    assert result["window_end"] == "2026-07-18"
    assert result["window_start"] == "2026-07-04"
    assert result["keywords"][0]["latest_run"]["date"] == "2026-07-18"
    assert result["keywords"][0]["latest_run"]["time"] == "18:15"


def test_keyword_detail_repairs_nested_read_delta_window(settings):
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "kw-1",
                        "wechat-search",
                        "港险",
                        "active",
                        "2026-07-18T00:00:00Z",
                        "2026-07-18T00:00:00Z",
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        "snap-late-utc",
                        "wechat-search",
                        "港险",
                        "kw-1",
                        "2026-07-18T18:15:12.947Z",
                        1,
                        "{}",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "keyword-stale-cross-day",
                        "keyword",
                        "kw-1",
                        json.dumps(
                            {
                                "generated_at": "2026-07-18T18:15:12.947Z",
                                "keyword_id": "kw-1",
                                "keyword": "港险",
                                "window_days": 15,
                                "window_start": "2026-07-05",
                                "window_end": "2026-07-19",
                                "keyword_read_delta": {
                                    "window_start": "2026-07-05",
                                    "window_end": "2026-07-19",
                                    "status": "ok",
                                },
                                "latest_run": {
                                    "id": "snap-late-utc",
                                    "date": "2026-07-19",
                                    "time": "02:15",
                                    "run_at": "2026-07-19 02:15",
                                },
                                "runs": [],
                            }
                        ),
                        "keyword-stale-cross-day",
                        "manifest-keyword-stale-cross-day",
                        "test://wechat",
                        "2026-07-18T18:16:00Z",
                    ),
                )

    result = WechatLegacyRepository(settings).keyword("kw-1")

    assert result["latest_run"]["date"] == "2026-07-18"
    assert result["latest_run"]["time"] == "18:15"
    assert result["window_start"] == "2026-07-04"
    assert result["window_end"] == "2026-07-18"
    assert result["keyword_read_delta"]["window_start"] == "2026-07-04"
    assert result["keyword_read_delta"]["window_end"] == "2026-07-18"


def test_rebuild_reads_only_latest_projection_payload_per_identity(settings, monkeypatch):
    """Historical projection blobs must not be decoded during a rebuild."""
    old_payload = json.dumps({"generated_at": "2026-07-17T00:00:00Z", "blob": "x" * 200_000})
    new_payload = json.dumps({"generated_at": "2026-07-18T18:00:00Z", "keyword_source_total": 0})
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    ("kw-1", "wechat-search", "港险", "active", "2026-07-18T00:00:00Z", "2026-07-18T00:00:00Z", "{}"),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    ("snap-1", "wechat-search", "港险", "kw-1", "2026-07-18T18:00:00Z", 0, "{}"),
                )
                con.executemany(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    [
                        ("old-bootstrap", "bootstrap", "", old_payload, "old-b", "old", "test://old", "2026-07-17T01:00:00Z"),
                        ("new-bootstrap", "bootstrap", "", new_payload, "new-b", "new", "test://new", "2026-07-18T19:00:00Z"),
                    ],
                )

    decoded: list[str] = []
    original = live_projection_module._old_payload

    def tracked(raw):
        decoded.append(str(raw))
        return original(raw)

    monkeypatch.setattr(live_projection_module, "_old_payload", tracked)
    with connect(settings, readonly=True) as con:
        live_projection_module.rebuild(con)

    assert old_payload not in decoded
    assert new_payload in decoded


def test_live_projection_write_compresses_large_full_and_bootstrap_payloads(settings, monkeypatch):
    payload = {"generated_at": "2026-07-18T18:00:00Z", "blob": "x" * (2 << 20)}
    monkeypatch.setattr(
        live_projection_module,
        "rebuild",
        lambda connection, *, window_days: {"full": payload, "bootstrap": payload},
    )
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                live_projection_module.write(con)
                rows = con.execute(
                    """
                    SELECT projection_kind,payload_json
                    FROM wechat_legacy_projections
                    WHERE projection_kind IN ('full','bootstrap')
                    ORDER BY projection_kind
                    """
                ).fetchall()

    assert len(rows) == 2
    assert all(json.loads(row["payload_json"]).get("__compressed_json__") == "zlib+base64" for row in rows)
    assert all(len(row["payload_json"]) < 100_000 for row in rows)


def test_projection_cleanup_keeps_newest_derived_row_and_core_snapshot(settings):
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO keywords(
                        keyword_id,platform,keyword,status,
                        first_seen_at,updated_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    ("kw-1", "wechat-search", "港险", "active", "2026-07-18T00:00:00Z", "2026-07-18T00:00:00Z", "{}"),
                )
                con.execute(
                    """
                    INSERT INTO search_snapshots(
                        snapshot_id,platform,keyword,keyword_id,captured_at,
                        result_count,payload_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    ("snap-core", "wechat-search", "港险", "kw-1", "2026-07-18T18:00:00Z", 1, "{}"),
                )
                con.executemany(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    [
                        _projection("p-old", "bootstrap", "2026-07-18T16:00:00Z", "2026-07-19T00:00:00Z", {"keywords": []}),
                        _projection("p-new", "bootstrap", "2026-07-18T18:00:00Z", "2026-07-18T18:01:00Z", {"keywords": []}),
                    ],
                )
    deleted = WechatService(settings).cleanup_derived_projections()
    assert deleted == 1
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM wechat_legacy_projections WHERE projection_kind='bootstrap' AND subject_id=''"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM search_snapshots WHERE snapshot_id='snap-core'"
        ).fetchone()[0] == 1


def test_runtime_history_bounds_payload_read_for_large_projection(settings, monkeypatch):
    giant = json.dumps(
        {
            "runtime_subtype": "batch",
            "batch_id": "giant-batch",
            "status": "completed",
            "results": "x" * (2 * 1024 * 1024),
        },
        ensure_ascii=False,
    )
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,
                source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "giant-runtime",
                "runtime",
                "giant-batch",
                giant,
                "giant-runtime",
                "manifest-giant",
                "test://giant",
                "2026-07-18T19:00:00Z",
            ),
        )

    decode_inputs: list[object] = []
    original_decode = wechat_legacy_module._runtime_value

    def tracked_decode(raw):
        decode_inputs.append(raw)
        return original_decode(raw)

    monkeypatch.setattr(wechat_legacy_module, "_runtime_value", tracked_decode)
    result = WechatLegacyRepository(settings).runtime_history()

    assert result[0]["batch_id"] == "giant-batch"
    assert result[0]["status"] == "completed"
    assert "results" not in result[0]
    assert decode_inputs == [None]

    repository = WechatLegacyRepository(settings)
    assert repository.compact_runtime_projections() == 1
    with connect(settings, readonly=True) as con:
        compacted = con.execute(
            """
            SELECT projection_id,source_hash,source_manifest_id,source_ref,
                   length(payload_json) AS payload_size
            FROM wechat_legacy_projections
            WHERE projection_id='giant-runtime'
            """
        ).fetchone()
    assert compacted["projection_id"] == "giant-runtime"
    assert compacted["source_hash"] != "giant-runtime"
    assert compacted["source_manifest_id"] == "manifest-giant"
    assert compacted["source_ref"] == "test://giant"
    assert compacted["payload_size"] < (1 << 20)


def test_active_status_uses_core_job_without_runtime_payload(settings, monkeypatch):
    with connect(settings) as con:
        con.execute(
            """
            INSERT INTO search_refresh_jobs(
                refresh_job_id,system_key,platform,trigger_type,status,
                requested_count,created_at,updated_at,trigger_source
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "core-running",
                "wechat-search",
                "wechat-search",
                "manual",
                "running",
                1,
                "2026-07-18T19:00:00Z",
                "2026-07-18T19:00:00Z",
                "test",
            ),
        )
        con.execute(
            """
            INSERT INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,
                source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "stale-giant-runtime",
                "runtime",
                "core-running",
                json.dumps(
                    {
                        "runtime_subtype": "batch",
                        "status": "failed",
                        "results": "x" * (2 * 1024 * 1024),
                    }
                ),
                "stale-giant-runtime",
                "manifest-stale",
                "test://stale",
                "2026-07-18T19:01:00Z",
            ),
        )

    def fail_decode(_raw):
        raise AssertionError("active status decoded runtime payload")

    monkeypatch.setattr(wechat_legacy_module, "_runtime_value", fail_decode)
    result = WechatLegacyRepository(settings).active_batch_runtime()

    assert result is not None
    assert result["batch_id"] == "core-running"
    assert result["hub_status"] == "running"

    repository = WechatLegacyRepository(settings)
    assert repository.compact_runtime_projections() == 1
    with connect(settings, readonly=True) as con:
        compacted = con.execute(
            """
            SELECT source_manifest_id,source_ref,length(payload_json) AS payload_size,
                   json_extract(payload_json,'$.status') AS compacted_status
            FROM wechat_legacy_projections
            WHERE projection_id='stale-giant-runtime'
            """
        ).fetchone()
    assert compacted["source_manifest_id"] == "manifest-stale"
    assert compacted["source_ref"] == "test://stale"
    assert compacted["payload_size"] < (1 << 20)
    assert compacted["compacted_status"] == "running"
