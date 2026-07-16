from __future__ import annotations

import asyncio
import hashlib
import gzip
import io
import json
import urllib.error
from collections import Counter
from dataclasses import replace
from email.message import Message
from pathlib import Path

import httpx
import pytest

from content_hub.app import create_app
from content_hub.adapters.wechat import RemoteResponse, WechatAdapter
from content_hub.config import Settings
from content_hub.db.connection import connect
from content_hub.errors import ValidationAppError
from content_hub.services.contract_diff import HTTPMetadata, PayloadWithHTTPMetadata
from content_hub.services.wechat_refresh import WechatRefreshService
from content_hub.services.migration import (
    MigrationResolver,
    WECHAT_HTTP_OPERATIONS,
    wechat_http_operation,
)
from content_hub.repositories.wechat_legacy import WechatLegacyRepository
from content_hub.features.wechat.service import (
    _legacy_projection_payload,
    _projection_hash,
    _projection_json,
)
from content_hub.features.wechat.legacy_read_router import (
    _REFERENCE_500_HTML,
    _idempotency,
    _json_response,
)
from starlette.requests import Request


def test_default_wechat_config_is_isolated_and_overridable(monkeypatch) -> None:
    monkeypatch.delenv("HUB_WECHAT_SOURCE_URL", raising=False)
    monkeypatch.delenv("HUB_WECHAT_FREEZE_ROOT", raising=False)
    monkeypatch.delenv("HUB_WECHAT_SOURCE_ROOT", raising=False)
    loaded = Settings.load()
    assert loaded.wechat_source_url == "http://127.0.0.1:8774"
    assert str(loaded.wechat_source_root).endswith(
        "data/migration/wechat/freeze_20260716T024524+0800/payload"
    )
    monkeypatch.setenv("HUB_WECHAT_SOURCE_URL", "http://localhost:8774/")
    monkeypatch.setenv(
        "HUB_WECHAT_FREEZE_ROOT",
        str(loaded.project_root / "data/migration/wechat/test-freeze"),
    )
    overridden = Settings.load()
    assert overridden.wechat_source_url == "http://127.0.0.1:8774"
    assert overridden.wechat_source_root == (
        loaded.project_root / "data/migration/wechat/test-freeze"
    ).resolve()


def test_router_order_aux_state_and_refresh_are_before_catch_all(settings) -> None:
    app = create_app(replace(settings, frontend_dist=Settings.load().frontend_dist))
    paths = [route.path for route in app.routes if getattr(route, "path", None)]
    catch_all = paths.index("/api/{path:path}")
    assert paths.index("/api/agent/manifest") < catch_all
    assert paths.index("/api/keywords/{keyword_id}/pin") < catch_all
    assert paths.index("/api/keywords/{keyword_id}/refresh") < catch_all


def test_cors_allows_migration_headers(settings) -> None:
    async def scenario() -> None:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.options(
                "/api/agent/manifest",
                headers={
                    "Origin": settings.cors_origins[0],
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": (
                        "Idempotency-Key, X-Idempotency-Key, X-Actor-ID"
                    ),
                },
            )
            assert response.status_code == 200
            allowed = response.headers["access-control-allow-headers"].lower()
            assert "idempotency-key" in allowed
            assert "x-idempotency-key" in allowed
            assert "x-actor-id" in allowed

    asyncio.run(scenario())


def test_reference_unavailable_is_reported_not_faked(settings) -> None:
    async def scenario() -> None:
        isolated = replace(settings, wechat_source_url="http://127.0.0.1:1")
        app = create_app(isolated)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/monitor-data")
                assert response.status_code == 409
                assert "error" in response.json()

    asyncio.run(scenario())


def test_all_43_wechat_http_operations_resolve_to_unique_switch_contracts() -> None:
    legacy_operations = [
        item
        for item in WECHAT_HTTP_OPERATIONS
        if item["path"].startswith("/api/") and not item["path"].startswith("/api/v1/")
    ]
    assert len(legacy_operations) == 43
    contracts = [item["contract_key"] for item in legacy_operations]
    assert len(set(contracts)) == 43
    assert sum(item["kind"] == "read" for item in legacy_operations) == 22
    assert sum(item["kind"] == "write" for item in legacy_operations) == 21
    assert wechat_http_operation("GET", "/api/monitor-data")["contract_key"] == "monitor-data"
    assert wechat_http_operation("PATCH", "/api/keyword-manage/keywords/kw-1")["contract_key"] == "keyword-update"
    assert wechat_http_operation("GET", "/api/v1/wechat/keywords/kw-1")["contract_key"] == "keyword"
    assert wechat_http_operation("GET", "/api/v1/wechat/articles/art-1")["contract_key"] == "article-hit-detail"
    assert wechat_http_operation("POST", "/api/v1/wechat/keywords/kw-1/refresh") == {
        "method": "POST",
        "path": "/api/v1/wechat/keywords/{keyword_id}/refresh",
        "contract_key": "keywords-refresh",
        "kind": "write",
    }
    assert wechat_http_operation("POST", "/api/v1/wechat/import")["kind"] == "hub-only"


def test_43_operations_match_fastapi_route_operations(settings) -> None:
    app = create_app(settings)

    def normalize(path: str) -> str:
        return path.replace(":path}", "}")

    actual = []
    for route in app.routes:
        route_path = getattr(route, "path", "")
        if not route_path.startswith("/api/"):
            continue
        for method in getattr(route, "methods", set()):
            if method in {"GET", "POST", "PATCH", "DELETE"}:
                actual.append((method, normalize(route_path)))
    expected = {
        (item["method"], normalize(item["path"]))
        for item in WECHAT_HTTP_OPERATIONS
    }
    counts = Counter(actual)
    assert set(expected) <= set(counts)
    assert all(counts[item] == 1 for item in expected)
    assert len(expected) == 50


def test_legacy_and_compare_keep_legacy_http_metadata(settings, monkeypatch) -> None:
    metadata = HTTPMetadata(
        status_code=200,
        content_type="application/vnd.legacy+json",
        content_encoding="gzip",
        etag='"legacy-etag"',
        cache_control="max-age=17",
        vary="Accept-Encoding",
    )
    monkeypatch.setattr(
        WechatAdapter,
        "_request_response",
        lambda self, path, **kwargs: RemoteResponse(
            {"keywords": [{"keyword_id": "legacy"}]}, 200, metadata
        ),
    )

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                legacy = await client.get("/api/monitor-data")
                assert legacy.status_code == 200
                assert legacy.headers["content-type"].startswith("application/vnd.legacy+json")
                assert legacy.headers["content-encoding"] == "gzip"
                assert legacy.headers["etag"] == '"legacy-etag"'
                assert legacy.headers["cache-control"] == "max-age=17"
                assert legacy.headers["vary"] == "Accept-Encoding"

                with connect(settings) as con:
                    con.execute(
                        "UPDATE migration_switches SET data_mode='compare' "
                        "WHERE module_key='wechat-search' AND contract_key='monitor-data'"
                    )
                compared = await client.get("/api/monitor-data")
                assert compared.status_code == 200
                assert compared.headers["etag"] == '"legacy-etag"'
                assert compared.json()["keywords"][0]["keyword_id"] == "legacy"

    asyncio.run(scenario())
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT diff_json FROM contract_comparisons "
            "WHERE module_key='wechat-search' AND contract_key='monitor-data' "
            "AND diff_json LIKE '%legacy-etag%' LIMIT 1"
        ).fetchone()
    assert row is not None


def test_core_auto_etag_is_materialized_for_all_four_contracts(settings) -> None:
    payload = {"keywords": [{"keyword_id": "kw", "keyword": "同一字节"}]}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    actual_etag = 'W/"' + hashlib.md5(raw).hexdigest() + '"'
    core_metadata = HTTPMetadata(
        status_code=200,
        content_type="application/json; charset=utf-8",
        etag=actual_etag,
        cache_control="no-cache, must-revalidate",
        vary="Accept-Encoding",
    )
    for contract in ("monitor-data", "bootstrap", "keyword", "account"):
        selected, _ = MigrationResolver(
            settings, module_key="wechat-search", contract_key=contract
        ).compare(
            request_fingerprint=f"etag:{contract}",
            legacy=lambda: PayloadWithHTTPMetadata(payload, core_metadata),
            hub=lambda: payload,
            hub_metadata=HTTPMetadata(
                status_code=200,
                content_type="application/json; charset=utf-8",
                etag="__AUTO__",
                cache_control="no-cache, must-revalidate",
                vary="Accept-Encoding",
            ),
            preserve_response=True,
        )
        assert selected.metadata.etag == actual_etag
    with connect(settings, readonly=True) as con:
        statuses = [
            row[0] for row in con.execute(
                "SELECT status FROM contract_comparisons "
                "WHERE request_fingerprint LIKE 'etag:%' ORDER BY request_fingerprint"
            )
        ]
    assert statuses == ["matched"] * 4

    MigrationResolver(
        settings, module_key="wechat-search", contract_key="monitor-data"
    ).compare(
        request_fingerprint="etag:wrong",
        legacy=lambda: PayloadWithHTTPMetadata(
            payload,
            HTTPMetadata(
                status_code=200,
                content_type="application/json; charset=utf-8",
                etag='"wrong-etag"',
                cache_control="no-cache, must-revalidate",
                vary="Accept-Encoding",
            ),
        ),
        hub=lambda: payload,
        hub_metadata=HTTPMetadata(
            status_code=200,
            content_type="application/json; charset=utf-8",
            etag="__AUTO__",
            cache_control="no-cache, must-revalidate",
            vary="Accept-Encoding",
        ),
        preserve_response=True,
    )
    with connect(settings, readonly=True) as con:
        assert con.execute(
            "SELECT status FROM contract_comparisons "
            "WHERE request_fingerprint='etag:wrong'"
        ).fetchone()[0] == "different"


def test_controlled_any_json_article_metadata_and_known_404_contracts(
    settings, monkeypatch
) -> None:
    class FakeResponse:
        status = 200
        headers = Message()

        def __enter__(self):
            self.headers["Content-Type"] = "application/json"
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"[]"

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResponse())
    adapter = WechatAdapter(settings)
    assert adapter._request_response(
        "/api/refresh-all/history", allow_any_json=True
    ).payload == []
    with pytest.raises(Exception):
        adapter._request_response("/api/refresh-all/history")

    not_found = PayloadWithHTTPMetadata(
        {"error": "job not found"},
        HTTPMetadata(
            status_code=404,
            content_type="application/json",
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
        ),
    )
    content = PayloadWithHTTPMetadata(
        {"path": "missing.md", "markdown": ""},
        HTTPMetadata(
            status_code=200,
            content_type="application/json",
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
        ),
    )
    for contract, value in (
        ("refresh-status", not_found),
        ("agent-evidence", PayloadWithHTTPMetadata(
            {"error": "evidence not found: missing-evidence"},
            not_found.metadata,
        )),
        ("article-content", content),
    ):
        MigrationResolver(
            settings, module_key="wechat-search", contract_key=contract
        ).compare(
            request_fingerprint=f"metadata:{contract}",
            legacy=lambda value=value: value,
            hub=lambda value=value: value,
            preserve_response=True,
        )
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            "SELECT status FROM contract_comparisons "
            "WHERE request_fingerprint LIKE 'metadata:%' ORDER BY request_fingerprint"
        ).fetchall()
    assert [row[0] for row in rows] == ["matched", "matched", "matched"]


def test_r01_r04_gzip_all_modes_and_exact_304_headers(settings, monkeypatch) -> None:
    payload = {"source": "selected", "keywords": [{"keyword_id": "kw"}]}
    metadata = HTTPMetadata(
        status_code=200,
        content_type="application/json; charset=utf-8",
        etag='W/"reference-etag"',
        cache_control="no-cache, must-revalidate",
        vary="Accept-Encoding",
    )
    monkeypatch.setattr(
        WechatAdapter,
        "_request_response",
        lambda self, path, **kwargs: RemoteResponse(payload, 200, metadata),
    )
    monkeypatch.setattr(WechatLegacyRepository, "full", lambda self: payload)
    monkeypatch.setattr(WechatLegacyRepository, "bootstrap", lambda self: payload)
    monkeypatch.setattr(
        WechatLegacyRepository, "keyword", lambda self, keyword_id: payload
    )
    monkeypatch.setattr(
        WechatLegacyRepository, "account", lambda self, account_id: payload
    )
    routes = (
        ("monitor-data", "/api/monitor-data"),
        ("bootstrap", "/api/monitor-data/bootstrap"),
        ("keyword", "/api/monitor-data/keyword/kw"),
        ("account", "/api/monitor-data/account/acct"),
    )

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for mode in ("legacy", "compare", "hub"):
                    with connect(settings) as con:
                        for contract, _ in routes:
                            con.execute(
                                "UPDATE migration_switches SET data_mode=? "
                                "WHERE module_key='wechat-search' AND contract_key=?",
                                (mode, contract),
                            )
                    for _, path in routes:
                        response = await client.get(
                            path, headers={"Accept-Encoding": "gzip"}
                        )
                        assert response.status_code == 200
                        assert response.headers["content-encoding"] == "gzip"
                        assert response.headers["vary"] == "Accept-Encoding"
                        assert response.headers["cache-control"] == "no-cache, must-revalidate"
                        assert response.headers["content-type"] == "application/json; charset=utf-8"
                        assert response.json() == payload

                with connect(settings) as con:
                    con.execute(
                        "UPDATE migration_switches SET data_mode='legacy' "
                        "WHERE module_key='wechat-search' AND contract_key='monitor-data'"
                    )
                not_modified = await client.get(
                    "/api/monitor-data",
                    headers={
                        "Accept-Encoding": "gzip",
                        "If-None-Match": 'W/"reference-etag"',
                    },
                )
                assert not_modified.status_code == 304
                assert not_modified.content == b""
                assert {
                    key: not_modified.headers.get(key)
                    for key in (
                        "etag",
                        "vary",
                        "cache-control",
                        "content-type",
                        "content-encoding",
                    )
                } == {
                    "etag": 'W/"reference-etag"',
                    "vary": "Accept-Encoding",
                    "cache-control": "no-cache, must-revalidate",
                    "content-type": None,
                    "content-encoding": None,
                }
                strong_validator = await client.get(
                    "/api/monitor-data",
                    headers={
                        "Accept-Encoding": "identity",
                        "If-None-Match": '"reference-etag"',
                    },
                )
                assert strong_validator.status_code == 200
                assert "content-encoding" not in strong_validator.headers

    asyncio.run(scenario())


def test_r05_r22_no_store_json_does_not_auto_gzip() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/articles",
            "headers": [(b"accept-encoding", b"gzip")],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )
    response = _json_response(
        request,
        PayloadWithHTTPMetadata(
            {"ok": True},
            HTTPMetadata(
                status_code=200,
                content_type="application/json",
                cache_control="no-store, no-cache, must-revalidate, max-age=0",
            ),
        ),
    )
    assert response.body == b'{"ok":true}\n'
    assert "Content-Encoding" not in response.headers
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"
    core = _json_response(
        request,
        PayloadWithHTTPMetadata(
            {"ok": True},
            HTTPMetadata(
                status_code=200,
                content_type="application/json; charset=utf-8",
                etag="__AUTO__",
                cache_control="no-cache, must-revalidate",
                vary="Accept-Encoding",
            ),
        ),
    )
    assert core.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(core.body) == b'{"ok":true}'


def test_adapter_http_error_keeps_non_2xx_json_and_metadata(settings, monkeypatch) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8774/api/example",
        500,
        "Internal Server Error",
        headers,
        io.BytesIO(b'{"error":"legacy-500"}\n'),
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    response = WechatAdapter(settings)._request_response(
        "/api/example", allow_http_errors=True
    )
    assert response.status == 500
    assert response.payload == {"error": "legacy-500"}
    assert response.metadata == HTTPMetadata(
        status_code=500,
        content_type="application/json",
        cache_control="no-store, no-cache, must-revalidate, max-age=0",
    )


def test_legacy_http_errors_keep_status_body_and_headers_in_compare(settings, monkeypatch) -> None:
    state = {"status": 404}

    def fake_request(self, path, **kwargs):
        status = state["status"]
        return RemoteResponse(
            {"error": f"old-{status}"},
            status,
            HTTPMetadata(
                status_code=status,
                content_type="application/json",
                cache_control="no-store, no-cache, must-revalidate, max-age=0",
            ),
        )

    monkeypatch.setattr(WechatAdapter, "_request_response", fake_request)

    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                for mode in ("legacy", "compare"):
                    with connect(settings) as con:
                        con.execute(
                            "UPDATE migration_switches SET data_mode=? "
                            "WHERE module_key='wechat-search' AND contract_key='monitor-data'",
                            (mode,),
                        )
                    for status in (404, 500, 400):
                        state["status"] = status
                        response = await client.get("/api/monitor-data")
                        assert response.status_code == status
                        assert response.json() == {"error": f"old-{status}"}
                        assert response.headers["content-type"] == "application/json"
                        assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
                        assert "etag" not in response.headers
                        assert "vary" not in response.headers

    asyncio.run(scenario())


def test_real_freeze_projection_keeps_monitor_keyword_count_and_order() -> None:
    root = Path(__file__).resolve().parents[2]
    freeze = root / "data/migration/wechat/freeze_20260716T024524+0800/payload"
    monitor = json.loads(
        (freeze / "normalized/monitor-data.json").read_text(encoding="utf-8")
    )
    deltas = json.loads(
        (freeze / "normalized/keyword_read_deltas.json").read_text(encoding="utf-8")
    )
    records = {
        "monitor": monitor,
        # The real freeze contains more registry rows than the monitor payload.
        # Supplying it here proves the initial projection does not widen R01.
        "runtime": {"keyword_registry": deltas},
        "keyword_read_deltas": deltas,
    }
    full, keywords, _ = _legacy_projection_payload(records)
    assert len(monitor["keywords"]) == 312
    assert len(full["keywords"]) == 312
    assert len(keywords) == 312
    # MonitorFastStore 会在保留冻结字段插入顺序的基础上补 pin 兼容字段。
    assert list(full["keywords"][0])[: len(monitor["keywords"][0])] == list(
        monitor["keywords"][0]
    )
    assert list(full["keywords"][0])[-2:] == ["is_pinned", "pin_order"]
    assert full["keywords"][0]["keyword_id"] == monitor["keywords"][0]["keyword_id"]


def test_projection_payload_keeps_wire_order_but_hash_is_canonical() -> None:
    left = {"b": 1, "a": 2}
    right = {"a": 2, "b": 1}
    assert _projection_json(left) == '{"b":1,"a":2}'
    assert _projection_hash(left) == _projection_hash(right)


def test_reference_invalid_samples_keep_hub_status_body_and_cache_headers(settings) -> None:
    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            with connect(settings) as con:
                for contract in ("keyword", "account", "article-content", "article-hit-detail", "articles"):
                    con.execute(
                        "UPDATE migration_switches SET data_mode='hub' "
                        "WHERE module_key='wechat-search' AND contract_key=?",
                        (contract,),
                    )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                cases = (
                    ("/api/monitor-data/keyword/not-a-real-keyword", 404, b'{"error":"keyword not found: not-a-real-keyword"}\n', None),
                    ("/api/monitor-data/account/not-a-real-account", 404, b'{"error":"account not found: not-a-real-account"}\n', None),
                    ("/api/article-content?path=../../etc/passwd", 400, b'{"error":"path escapes project root"}\n', "no-store, no-cache, must-revalidate, max-age=0"),
                    ("/api/article-content", 404, b'{"error":"empty content path"}\n', "no-store, no-cache, must-revalidate, max-age=0"),
                    ("/api/article-hit-detail", 404, b'{"error":"article not found"}\n', "no-store, no-cache, must-revalidate, max-age=0"),
                )
                for path, status, body, cache_control in cases:
                    response = await client.get(path)
                    assert response.status_code == status
                    assert response.content == body
                    assert response.headers["content-type"] == "application/json"
                    assert response.headers.get("cache-control") == cache_control
                    if cache_control:
                        assert response.headers["pragma"] == "no-cache"
                        assert response.headers["expires"] == "0"
                response = await client.get(
                    "/api/articles?page=0&page_size=9999&sort=not-real&time_range=bad"
                )
                assert response.status_code == 500
                assert response.text == _REFERENCE_500_HTML
                assert response.headers["content-type"] == "text/html; charset=utf-8"
                assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
                assert response.headers["pragma"] == "no-cache"
                assert response.headers["expires"] == "0"

    asyncio.run(scenario())


def test_refresh_requires_explicit_key_and_explicit_key_replays_same_result(settings) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/keywords/kw-1/refresh",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )
    with pytest.raises(ValidationAppError):
        _idempotency(request, {"keyword": "same"}, operation="keywords-refresh", subject="kw-1")

    explicit_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/keywords/kw-1/refresh",
            "headers": [(b"idempotency-key", b"stable-1")],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )
    assert _idempotency(explicit_request, {}, operation="keywords-refresh", subject="kw-1") == "stable-1"
    assert _idempotency(explicit_request, {}, operation="keywords-refresh", subject="kw-1") == "stable-1"

    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO keywords(
                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "kw-idempotency-contract",
                "wechat-search",
                "幂等契约测试词",
                "active",
                "2026-07-16T00:00:00Z",
                "2026-07-16T00:00:00Z",
                "{}",
            ),
        )
    service = WechatRefreshService(settings)
    with pytest.raises(ValidationAppError):
        service.refresh_one(
            keyword_id="kw-idempotency-contract",
            request_keyword="幂等契约测试词",
            key="",
        )
    first_result = service.refresh_one(
        keyword_id="kw-idempotency-contract",
        request_keyword="幂等契约测试词",
        key="stable-refresh-replay",
    )
    replay_result = service.refresh_one(
        keyword_id="kw-idempotency-contract",
        request_keyword="幂等契约测试词",
        key="stable-refresh-replay",
    )
    assert replay_result == first_result


def test_r18_and_w18_missing_batch_keep_legacy_404(settings) -> None:
    async def scenario() -> None:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            with connect(settings) as con:
                for contract in ("refresh-all-status", "refresh-all-cancel"):
                    con.execute(
                        "UPDATE migration_switches SET data_mode='hub' "
                        "WHERE module_key='wechat-search' AND contract_key=?",
                        (contract,),
                    )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                read = await client.get(
                    "/api/refresh-all/status",
                    params={"batch_id": "missing-batch"},
                )
                write = await client.post(
                    "/api/refresh-all/cancel",
                    json={"batch_id": "missing-batch"},
                    headers={"Idempotency-Key": "missing-batch-cancel"},
                )
                for response in (read, write):
                    assert response.status_code == 404
                    assert response.content == b'{"error":"batch not found"}\n'
                    assert response.headers["content-type"] == "application/json"
                    assert response.headers["cache-control"] == (
                        "no-store, no-cache, must-revalidate, max-age=0"
                    )
                    assert response.headers["pragma"] == "no-cache"
                    assert response.headers["expires"] == "0"

    asyncio.run(scenario())
