from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time

from starlette.requests import Request

from content_hub import app as app_module
from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.features.wechat import service as wechat_service_module
from content_hub.features.wechat.service import WechatService
from content_hub.repositories import wechat_legacy as wechat_legacy_module
from content_hub.repositories.wechat_legacy import WechatLegacyRepository


def _insert_bootstrap(settings, payload: dict, *, updated_at: str) -> None:
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO wechat_legacy_projections(
                        projection_id,projection_kind,subject_id,payload_json,
                        source_hash,source_manifest_id,source_ref,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "bootstrap-cache-row",
                        "bootstrap",
                        "",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        f"hash-{updated_at}",
                        "manifest-test",
                        "test://bootstrap",
                        updated_at,
                    ),
                )


def _payload(label: str) -> dict:
    return {
        "generated_at": label,
        "keywords": [{"keyword_id": "kw_1", "keyword": label, "today_count": 1}],
        "accounts": [],
    }


def _enable_bootstrap_hub(settings) -> None:
    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    INSERT INTO migration_switches(
                        switch_id,module_key,contract_key,data_mode,updated_at,updated_by
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(module_key,contract_key) DO UPDATE SET
                        data_mode=excluded.data_mode,
                        enabled=1,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by
                    """,
                    (
                        "bootstrap-cache-switch",
                        "wechat-search",
                        "bootstrap",
                        "hub",
                        "2026-07-18T01:00:00Z",
                        "test",
                    ),
                )


def test_bootstrap_cache_reuses_decode_and_serialization(settings, monkeypatch):
    _insert_bootstrap(settings, _payload("first"), updated_at="2026-07-18T01:00:00Z")
    WechatLegacyRepository._bootstrap_cache.clear()
    repository = WechatLegacyRepository(settings)

    decode_calls = 0
    serialize_calls = 0
    original_decode = wechat_legacy_module._projection_payload
    original_dumps = wechat_legacy_module.json.dumps

    def counted_decode(raw):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(raw)

    def counted_dumps(*args, **kwargs):
        nonlocal serialize_calls
        serialize_calls += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(wechat_legacy_module, "_projection_payload", counted_decode)
    monkeypatch.setattr(wechat_legacy_module.json, "dumps", counted_dumps)

    first = repository.bootstrap_cache_entry()
    second = repository.bootstrap_cache_entry()

    assert first.payload_json == second.payload_json
    assert first.payload["generated_at"] == "first"
    assert decode_calls == 1
    assert serialize_calls == 1


def test_bootstrap_cache_hit_skips_sqlite_probe_and_connect(settings, monkeypatch):
    _insert_bootstrap(settings, _payload("hot"), updated_at="2026-07-18T01:00:00Z")
    WechatLegacyRepository._bootstrap_cache.clear()
    repository = WechatLegacyRepository(settings)
    repository.bootstrap_cache_entry()

    version_calls = 0

    def unexpected_version_probe(*args, **kwargs):
        nonlocal version_calls
        version_calls += 1
        raise AssertionError("TTL 内命中不应执行 _bootstrap_version")

    def unexpected_connect(*args, **kwargs):
        raise AssertionError("TTL 内命中不应 connect SQLite")

    monkeypatch.setattr(repository, "_bootstrap_version", unexpected_version_probe)
    monkeypatch.setattr(wechat_legacy_module, "connect", unexpected_connect)
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(lambda _: repository.bootstrap_cache_entry(), range(4)))

    assert {entry.payload["generated_at"] for entry in entries} == {"hot"}
    assert version_calls == 0


def test_bootstrap_cache_version_change_reloads_projection(settings):
    _insert_bootstrap(settings, _payload("first"), updated_at="2026-07-18T01:00:00Z")
    WechatLegacyRepository._bootstrap_cache.clear()
    repository = WechatLegacyRepository(settings)
    entry = repository.bootstrap_cache_entry()
    assert entry.payload["generated_at"] == "first"
    cache_key = str(repository.settings.database_path.resolve())
    WechatLegacyRepository._bootstrap_cache[cache_key] = replace(
        entry,
        expires_at=time.monotonic() - 1,
    )

    with writer_lock(settings.lock_path):
        with connect(settings) as con:
            with transaction(con):
                con.execute(
                    """
                    UPDATE wechat_legacy_projections
                    SET payload_json=?,source_hash=?,updated_at=?
                    WHERE projection_id=?
                    """,
                    (
                        json.dumps(_payload("second"), ensure_ascii=False, separators=(",", ":")),
                        "hash-second",
                        "2026-07-18T01:00:01Z",
                        "bootstrap-cache-row",
                    ),
                )

    assert WechatLegacyRepository(settings).bootstrap()["generated_at"] == "second"


def test_bootstrap_cache_is_process_memory_only(settings):
    _insert_bootstrap(settings, _payload("persisted"), updated_at="2026-07-18T01:00:00Z")
    WechatLegacyRepository._bootstrap_cache.clear()
    first = WechatLegacyRepository(settings).bootstrap_cache_entry()
    assert first.payload["generated_at"] == "persisted"

    # Simulate a process restart: the in-memory entry is gone, while the
    # projection remains the sole durable source of truth.
    WechatLegacyRepository._bootstrap_cache.clear()
    restarted = WechatLegacyRepository(settings).bootstrap_cache_entry()
    assert restarted.payload["generated_at"] == "persisted"
    assert restarted is not first


def test_bootstrap_http_response_single_flight_under_concurrent_miss(settings, monkeypatch):
    _insert_bootstrap(settings, _payload("concurrent"), updated_at="2026-07-18T01:00:00Z")
    _enable_bootstrap_hub(settings)
    WechatLegacyRepository._bootstrap_cache.clear()
    WechatService._bootstrap_http_cache.clear()

    decode_calls = 0
    serialize_calls = 0
    original_decode = wechat_legacy_module._projection_payload
    original_dumps = wechat_service_module.json.dumps

    def counted_decode(raw):
        nonlocal decode_calls
        decode_calls += 1
        time.sleep(0.02)
        return original_decode(raw)

    def counted_dumps(*args, **kwargs):
        nonlocal serialize_calls
        serialize_calls += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(wechat_legacy_module, "_projection_payload", counted_decode)
    monkeypatch.setattr(wechat_service_module.json, "dumps", counted_dumps)

    def read_once() -> bytes:
        return WechatService(settings).bootstrap_http_response() or b""

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: read_once(), range(4)))

    assert len(set(responses)) == 1
    # One projection decode and two serializations: compact legacy payload +
    # v1 envelope. The other concurrent callers reuse both results.
    assert decode_calls == 1
    assert serialize_calls == 2


def test_bootstrap_guard_absorbs_only_runaway_keepalive_burst():
    app_module._bootstrap_guard_recent.clear()

    first_connection = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/monitor-data/bootstrap",
        "headers": [],
        "client": ("127.0.0.1", 41001),
        "server": ("127.0.0.1", 8799),
        "scheme": "http",
        "query_string": b"",
    })
    second_connection = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/monitor-data/bootstrap",
        "headers": [],
        "client": ("127.0.0.1", 41002),
        "server": ("127.0.0.1", 8799),
        "scheme": "http",
        "query_string": b"",
    })
    other_path_same_connection = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/wechat/bootstrap",
        "headers": [],
        "client": ("127.0.0.1", 41001),
        "server": ("127.0.0.1", 8799),
        "scheme": "http",
        "query_string": b"",
    })

    for offset in (0.0, 0.1, 0.2, 0.3):
        assert not app_module._bootstrap_request_is_runaway(
            first_connection,
            10.0 + offset,
        )
    assert app_module._bootstrap_request_is_runaway(first_connection, 10.4)
    assert not app_module._bootstrap_request_is_runaway(second_connection, 10.4)
    assert not app_module._bootstrap_request_is_runaway(
        other_path_same_connection,
        10.4,
    )
    assert not app_module._bootstrap_request_is_runaway(first_connection, 11.5)
