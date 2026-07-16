from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from content_hub.adapters.wechat import RemoteResponse, WechatAdapter
from content_hub.db.connection import connect
from content_hub.features.wechat.legacy_aux_router import router
from content_hub.services.contract_diff import HTTPMetadata
from content_hub.services.wechat_aux import (
    AGENT_METRIC_DICTIONARY,
    AidsoLoginRequired,
    AidsoProfileBusy,
    AuxCommandReplay,
    AuxIdempotencyConflict,
    AuxUpstreamError,
    AuxValidation,
    DisabledAidsoProvider,
    RecordedAidsoProvider,
    WechatAuxService,
)


def _png() -> bytes:
    # 1x1 PNG：足够验证魔数、MIME 和尺寸，不依赖 PIL/联网。
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\r\nIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    )


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "freeze"
    (root / "data/agent/evidence").mkdir(parents=True, exist_ok=True)
    (root / "data/config").mkdir(parents=True, exist_ok=True)
    (root / "normalized").mkdir(exist_ok=True)
    payloads = {
        "data/agent/manifest.json": {"schema_version": "x", "model_version": "m", "counts": {"signals": 1}},
        "data/agent/daily_brief.json": {"summary": "ok"},
        "data/config/agent_metric_dictionary.json": {"metrics": [{"metric_key": "reads"}]},
        "normalized/penalty_signals.json": {"model_version": "p1", "events": []},
        "normalized/account_aliases.json": {"model_version": "a1", "groups": [], "aliases": []},
        "data/agent/evidence/abc_1.json": {"evidence_id": "abc_1", "source_ref": "frozen"},
        "normalized/articles.json": [
            {"article_id": "cached", "cover_url": "https://cdn.example/cached.png", "raw_url": "https://cdn.example/x"},
            {"article_id": "frozen", "raw_url": "https://cdn.example/frozen.png"},
            {"article_id": "no-url"},
        ],
    }
    for relative, payload in payloads.items():
        path = root / relative
        path.write_text(json.dumps(payload), encoding="utf-8")
    return root


def _app(settings, service: WechatAuxService) -> FastAPI:
    app = FastAPI()
    app.state.settings = service.settings
    app.state.wechat_aux_service = service
    with connect(service.settings) as con:
        con.execute(
            """UPDATE migration_switches SET data_mode='hub'
               WHERE module_key='wechat-search' AND contract_key IN (
                 'agent-manifest','agent-daily-brief','agent-metric-dictionary',
                 'agent-evidence','penalty-signals','account-aliases',
                 'article-cover-image','aidso-keyword-heat-get'
               )"""
        )
    app.include_router(router)
    return app


def _request(app: FastAPI, method: str, path: str, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def test_aux_artifacts_import_and_read_after_freeze_removed(settings, tmp_path):
    source = _source(tmp_path)
    service = WechatAuxService(replace(settings, wechat_source_root=source))
    assert service.import_frozen_artifacts() == 6
    shutil.rmtree(source)
    assert service.artifact("manifest")["schema_version"] == "x"
    assert service.artifact("evidence", "abc_1")["evidence_id"] == "abc_1"
    with connect(settings, readonly=True) as con:
        rows = con.execute("SELECT artifact_kind,source_hash,source_ref,model_version FROM wechat_aux_artifacts").fetchall()
        assert len(rows) == 6
        assert all(row["source_hash"] and not str(row["source_ref"]).startswith("/") for row in rows)


def test_artifact_errors_are_legacy_json_shape(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    app = _app(settings, service)
    assert _request(app, "GET", "/api/agent/evidence/../bad").status_code in {400, 404}
    invalid = _request(app, "GET", "/api/agent/evidence/%2E%2E%2Fbad")
    assert invalid.status_code in {400, 404}
    if invalid.status_code == 400:
        assert invalid.json() == {"error": "invalid evidence id"}
    missing = _request(app, "GET", "/api/agent/evidence/missing-evidence")
    assert missing.status_code == 404
    assert missing.content == b'{"error":"evidence not found: missing-evidence"}\n'
    assert missing.headers["content-type"] == "application/json"
    assert missing.headers["cache-control"] == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )


def test_r05_r10_and_r13_route_golden_errors(settings, tmp_path):
    source = _source(tmp_path)
    service = WechatAuxService(replace(settings, wechat_source_root=source))
    app = _app(settings, service)
    for path in (
        "/api/agent/manifest",
        "/api/agent/daily-brief",
        "/api/agent/metric-dictionary",
        "/api/penalty-signals",
        "/api/account-aliases",
    ):
        assert _request(app, "GET", path).status_code == 200
    # evidence 400/404 are covered separately; these assertions verify stable top-level error keys.
    missing_app = _app(settings, WechatAuxService(replace(settings, wechat_source_root=tmp_path / "missing")))
    for path in (
        "/api/agent/manifest",
        "/api/agent/daily-brief",
        "/api/penalty-signals",
        "/api/account-aliases",
    ):
        response = _request(missing_app, "GET", path)
        assert response.status_code == 404 and set(response.json()) == {"error"}

    for url in ("file:///tmp/x", "https://cdn.example/x.png"):
        blocked = _request(
            app, "GET", "/api/article-cover-image", params={"url": url}
        )
        assert blocked.status_code == 409
        assert blocked.json() == {
            "code": "REFERENCE_EXTERNAL_BLOCKED",
            "error": "external side effect blocked",
            "kind": "external_blocked",
            "method": "GET",
            "path": "/api/article-cover-image",
        }


def test_r13_r14_read_switches_and_w01_w07_stay_hub(
    settings, tmp_path, monkeypatch
):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    app = _app(settings, service)
    hub_calls = {"cover": 0, "aidso": 0}
    remote_calls: list[str] = []

    def hub_cover(url):
        hub_calls["cover"] += 1
        return _png(), "image/png", "cover-digest"

    def hub_aidso(params, **kwargs):
        hub_calls["aidso"] += 1
        return {"keyword": params.get("keyword"), "source": "hub"}

    def fake_remote(self, path, **kwargs):
        remote_calls.append(path)
        endpoint = path.split("?", 1)[0]
        body = {
            "code": "REFERENCE_EXTERNAL_BLOCKED",
            "error": "external side effect blocked",
            "kind": "external_blocked",
            "method": "GET",
            "path": endpoint,
        }
        return RemoteResponse(
            body,
            409,
            HTTPMetadata(
                status_code=409,
                content_type="application/json",
                cache_control="no-store, no-cache, must-revalidate, max-age=0",
            ),
        )

    monkeypatch.setattr(service, "cover_image", hub_cover)
    monkeypatch.setattr(service, "aidso_heat", hub_aidso)
    monkeypatch.setattr(WechatAdapter, "_request_response", fake_remote)
    cases = (
        (
            "article-cover-image",
            "/api/article-cover-image",
            {"url": "https://example.invalid/blocked-cover.jpg"},
        ),
        (
            "aidso-keyword-heat-get",
            "/api/aidso/keyword-heat",
            {"keyword": "隔离测试"},
        ),
    )
    for mode in ("legacy", "compare", "hub"):
        for contract, path, params in cases:
            with connect(settings) as con:
                con.execute(
                    "UPDATE migration_switches SET data_mode=? "
                    "WHERE module_key='wechat-search' AND contract_key=?",
                    (mode, contract),
                )
            response = _request(
                app,
                "GET",
                path,
                params=params,
                headers={"Accept-Encoding": "gzip"},
            )
            assert response.status_code == 409
            assert response.content == (
                json.dumps(
                    {
                        "code": "REFERENCE_EXTERNAL_BLOCKED",
                        "error": "external side effect blocked",
                        "kind": "external_blocked",
                        "method": "GET",
                        "path": path,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            assert response.headers["cache-control"] == (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            assert response.headers["content-type"] == "application/json"
            assert response.headers["pragma"] == "no-cache"
            assert response.headers["expires"] == "0"
            assert "content-encoding" not in response.headers

    with connect(settings) as con:
        con.execute(
            "UPDATE migration_switches SET data_mode='hub' "
            "WHERE module_key='wechat-search' AND contract_key='article-cover-image'"
        )
        con.execute(
            "UPDATE migration_switches SET data_mode='hub' "
            "WHERE module_key='wechat-search' AND contract_key='aidso-keyword-heat-get'"
        )
    remote_before_writes = len(remote_calls)
    w01 = _request(
        app,
        "POST",
        "/api/article-covers",
        json={"articles": []},
    )
    w07 = _request(
        app,
        "POST",
        "/api/aidso/keyword-heat",
        json={"keyword": "write-word"},
    )
    assert w01.status_code == w07.status_code == 200
    assert len(remote_calls) == remote_before_writes
    assert hub_calls == {"cover": 0, "aidso": 1}


def test_metric_dictionary_is_fixed_agent_projection_not_core_definitions(
    settings, tmp_path
):
    with connect(settings) as con:
        con.execute(
            "INSERT INTO metric_definitions(metric_key,platform,subject_type,display_name,value_type) VALUES(?,?,?,?,?)",
            ("db_metric", "wechat-search", "content", "DB metric", "number"),
        )
        con.commit()
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    value = service.artifact("metric_dictionary")
    assert value == AGENT_METRIC_DICTIONARY
    assert [item["metric_id"] for item in value["metrics"]] == [
        "account_score",
        "timeliness_score",
        "today_score",
        "keyword_trend",
        "steady_read",
        "read_delta_15d",
        "external_heat",
        "article_interaction_proxy",
    ]
    app = _app(settings, service)
    response = _request(app, "GET", "/api/agent/metric-dictionary")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    assert "content-encoding" not in response.headers
    assert "etag" not in response.headers
    assert "vary" not in response.headers
    assert len(response.content) == 4977
    assert hashlib.sha256(response.content).hexdigest() == (
        "5e2d7ac849e834b218e9e4acc5a99e6fda0ab30ed6b90ba3a374f3008184dbae"
    )


def test_w01_exact_shape_and_all_partial_statuses(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    result = service.article_covers([
        {"article_id": "cached"},
        {"article_id": "frozen"},
        {"article_id": "no-url"},
        {"article_id": "missing"},
    ])
    assert list(result) == ["items", "count"]
    assert result["count"] == 4
    assert [item["status"] for item in result["items"]] == ["cached", "frozen", "no_url", "missing_article"]
    assert service.article_covers([]) == {"items": [], "count": 0}
    with pytest.raises(AuxValidation):
        service.article_covers([{"bad": "item"}])
    with pytest.raises(AuxValidation):
        service.article_covers([{"article_id": str(i)} for i in range(11)])


def test_w01_idempotency_restart_and_audit(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    first = service.article_covers([{"article_id": "frozen"}], idempotency_key="covers-1")
    assert service.article_covers([{"article_id": "frozen"}], idempotency_key="covers-1") == first
    with pytest.raises(AuxIdempotencyConflict):
        service.article_covers([{"article_id": "different"}], idempotency_key="covers-1")
    restarted = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    assert restarted.article_covers([{"article_id": "frozen"}], idempotency_key="covers-1") == first
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM command_runs WHERE idempotency_key='covers-1'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat_aux.article_covers'").fetchone()[0] == 1


def test_w01_implicit_key_is_unique(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    service.article_covers([{"article_id": "frozen"}])
    service.article_covers([{"article_id": "frozen"}])
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            "SELECT idempotency_key FROM command_runs "
            "WHERE module_key='wechat-aux' AND command_type='article_covers' "
            "ORDER BY created_at"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


def test_w01_uses_hub_after_freeze_removed(settings, tmp_path):
    source = _source(tmp_path)
    with connect(settings) as con:
        con.execute(
            """INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                "hub-content-1",
                "external_article",
                "Hub article",
                "https://article.example/page",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                json.dumps({"cover_url": "https://cdn.example/hub-cover.png", "raw_url": "https://article.example/raw"}),
            ),
        )
        con.execute(
            """INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json)
               VALUES(?,?,?,?,?)""",
            ("wechat_article", "legacy-hub-id", "hub-content-1", "2026-01-01T00:00:00Z", "{}"),
        )
        con.commit()
    service = WechatAuxService(replace(settings, wechat_source_root=source))
    shutil.rmtree(source)
    result = service.article_covers([{"article_id": "legacy-hub-id"}])
    assert result == {
        "items": [{"article_id": "legacy-hub-id", "cover_url": "https://cdn.example/hub-cover.png", "status": "cached"}],
        "count": 1,
    }


def test_w01_route_and_concurrent_idempotency(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    app = _app(settings, service)
    response = _request(
        app,
        "POST",
        "/api/article-covers",
        json={"articles": [{"article_id": "cached"}, {"article_id": "frozen"}]},
        headers={"Idempotency-Key": "route-cover-1"},
    )
    assert response.status_code == 200
    assert response.json() == service.article_covers([{"article_id": "cached"}, {"article_id": "frozen"}])

    def run_once(_):
        return service.article_covers([{"article_id": "frozen"}], idempotency_key="parallel-cover")

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(run_once, range(4)))
    assert values.count(values[0]) == 4
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM command_runs WHERE idempotency_key='parallel-cover'").fetchone()[0] == 1

    def run_conflict(item):
        try:
            return service.article_covers([{"article_id": item}], idempotency_key="parallel-conflict")
        except AuxIdempotencyConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run_conflict, ["cached", "frozen"]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, AuxIdempotencyConflict) for item in results) == 1

    route_conflict = _request(
        app,
        "POST",
        "/api/article-covers",
        json={"articles": [{"article_id": "no-url"}]},
        headers={"Idempotency-Key": "route-cover-1"},
    )
    assert route_conflict.status_code == 409
    assert route_conflict.json() == {"error": "idempotency key already used with different input"}


def test_r13_cached_only_default_and_explicit_provider(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("content_hub.services.wechat_aux.socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))])
    source = _source(tmp_path)
    disabled = WechatAuxService(replace(settings, wechat_source_root=source))
    with pytest.raises(AuxUpstreamError):
        disabled.cover_image("https://cdn.example/a.png")

    class Response:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": str(len(_png()))}
        connected_addresses = ("8.8.8.8",)

        def iter_content(self, chunk_size):
            yield _png()[:10]
            yield _png()[10:]

    class FakeImageProvider:
        kind = "fake-image"
        calls = 0

        def fetch(self, url, *, resolved_addresses):
            self.calls += 1
            assert resolved_addresses == ("8.8.8.8",)
            return Response()

    provider = FakeImageProvider()
    service = WechatAuxService(replace(settings, wechat_source_root=source), image_provider=provider)
    body, ctype, digest = service.cover_image("https://cdn.example/a.png")
    assert body == _png() and ctype == "image/png" and len(digest) == 64
    assert service.cover_image("https://cdn.example/b.png")[2] == digest
    assert provider.calls == 2
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM wechat_aux_cover_cache WHERE asset_hash=?", (digest,)).fetchone()[0] == 2


def test_r13_ssrf_redirect_mime_and_stream_limit(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("content_hub.services.wechat_aux.socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.1", 0))])
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)))
    with pytest.raises(AuxValidation):
        service.cover_image("https://cdn.example/a.png")
    monkeypatch.setattr("content_hub.services.wechat_aux.socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))])

    class BadResponse:
        status_code = 302
        headers = {}
        connected_addresses = ("8.8.8.8",)

    class BadProvider:
        kind = "bad"
        def fetch(self, url, *, resolved_addresses):
            return BadResponse()

    with pytest.raises(AuxUpstreamError):
        WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), image_provider=BadProvider()).cover_image("https://cdn.example/a.png")

    class HugeResponse:
        status_code = 200
        headers = {"content-type": "image/png"}
        connected_addresses = ("8.8.8.8",)

        def iter_content(self, chunk_size):
            yield b"x" * (8 * 1024 * 1024 + 1)

    class HugeProvider:
        kind = "huge"
        def fetch(self, url, *, resolved_addresses):
            return HugeResponse()

    with pytest.raises(AuxUpstreamError):
        WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), image_provider=HugeProvider()).cover_image("https://cdn.example/huge.png")


@pytest.mark.parametrize(
    ("method", "expected"),
    [("GET", 409), ("POST", 200)],
)
def test_aidso_get_does_not_need_idempotency_post_does(settings, tmp_path, method, expected):
    provider = RecordedAidsoProvider({"keyword": "x", "model_version": "m1", "profile_dir": "/secret"})
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)
    app = _app(settings, service)
    kwargs = {"params": {"keyword": "x", "headless": "false", "auto_login": "0", "wait_timeout_ms": "10", "channel": "fake"}}
    if method == "POST":
        kwargs = {"json": {"keyword": "x"}, "headers": {"Idempotency-Key": "aidso-1"}}
    response = _request(app, method, "/api/aidso/keyword-heat", **kwargs)
    assert response.status_code == expected
    if method == "GET":
        assert response.json() == {
            "code": "REFERENCE_EXTERNAL_BLOCKED",
            "error": "external side effect blocked",
            "kind": "external_blocked",
            "method": "GET",
            "path": "/api/aidso/keyword-heat",
        }
        assert provider.calls == 0
    else:
        assert response.json()["keyword"] == "x"


def test_aidso_boolean_normalization_and_error_shapes(settings, tmp_path):
    seen = {}

    def fake(**params):
        seen.update(params)
        return {"keyword": params["keyword"], "profile_dir": "/private/profile"}

    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=RecordedAidsoProvider(fake))
    app = _app(settings, service)
    response = _request(
        app,
        "POST",
        "/api/aidso/keyword-heat",
        json={
            "keyword": "x",
            "headless": "false",
            "auto_login": "false",
            "no_channel": "true",
        },
        headers={"Idempotency-Key": "normalize-aidso"},
    )
    assert response.status_code == 200 and seen["headless"] is False and seen["auto_login"] is False and seen["channel"] is None
    assert "/private/profile" not in response.text

    for index, (provider, expected) in enumerate([
        (RecordedAidsoProvider(lambda **p: (_ for _ in ()).throw(AidsoLoginRequired("secret /profile"))), {"error": "login required", "login_required": True}),
        (RecordedAidsoProvider(lambda **p: (_ for _ in ()).throw(AidsoProfileBusy("secret /profile"))), {"error": "profile busy", "profile_busy": True}),
        (DisabledAidsoProvider(), {"error": "provider failed"}),
    ]):
        app.state.wechat_aux_service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)
        response = _request(
            app,
            "POST",
            "/api/aidso/keyword-heat",
            json={"keyword": "new"},
            headers={"Idempotency-Key": f"error-{index}-{provider.kind}"},
        )
        assert response.status_code in {409, 502}
        assert response.json() == expected


def test_aidso_route_400_and_post_implicit_key_shapes(settings, tmp_path):
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=DisabledAidsoProvider())
    app = _app(settings, service)
    missing = _request(app, "GET", "/api/aidso/keyword-heat")
    assert missing.status_code == 409
    assert missing.json()["code"] == "REFERENCE_EXTERNAL_BLOCKED"
    missing_key = _request(app, "POST", "/api/aidso/keyword-heat", json={"keyword": "x"})
    assert missing_key.status_code == 502 and missing_key.json() == {"error": "provider failed"}
    with connect(settings, readonly=True) as con:
        row = con.execute(
            "SELECT idempotency_key,status FROM command_runs "
            "WHERE module_key='wechat-aux' AND command_type='aidso_keyword_heat'"
        ).fetchone()
        assert row["idempotency_key"].startswith("implicit:wechat-aux:aidso-keyword-heat:")
        assert row["status"] == "failed"


def test_aux_implicit_key_is_unique_and_explicit_key_still_replays(settings, tmp_path):
    provider = RecordedAidsoProvider({"keyword": "x", "model_version": "m1"})
    service = WechatAuxService(
        replace(settings, wechat_source_root=_source(tmp_path)),
        aidso_provider=provider,
    )
    first = service.aidso_heat({"keyword": "x"}, write=True)
    second = service.aidso_heat({"keyword": "x"}, write=True)
    assert second == first
    with connect(settings, readonly=True) as con:
        rows = con.execute(
            "SELECT idempotency_key FROM command_runs "
            "WHERE module_key='wechat-aux' AND command_type='aidso_keyword_heat' "
            "ORDER BY created_at"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
    assert provider.calls == 1  # provider result cache is separate from command replay
    explicit = service.aidso_heat({"keyword": "x"}, idempotency_key="aidso-1", write=True)
    assert service.aidso_heat({"keyword": "x"}, idempotency_key="aidso-1", write=True) == explicit
    with connect(settings, readonly=True) as con:
        key = con.execute(
            "SELECT idempotency_key FROM command_runs "
            "WHERE module_key='wechat-aux' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["idempotency_key"]
    with pytest.raises(AuxIdempotencyConflict):
        service.aidso_heat({"keyword": "y"}, idempotency_key=key, write=True)


def test_aidso_post_failed_command_and_audit_are_persisted(settings, tmp_path):
    provider = RecordedAidsoProvider(lambda **p: (_ for _ in ()).throw(AuxUpstreamError("cookie /secret")))
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)
    with pytest.raises(AuxUpstreamError):
        service.aidso_heat({"keyword": "x"}, idempotency_key="failed-1", write=True)
    assert provider.calls == 1
    with pytest.raises(AuxCommandReplay) as replay:
        service.aidso_heat({"keyword": "x"}, idempotency_key="failed-1", write=True)
    assert replay.value.status_code == 502 and replay.value.payload == {"error": "provider failed"}
    assert provider.calls == 1
    with pytest.raises(AuxIdempotencyConflict):
        service.aidso_heat({"keyword": "other"}, idempotency_key="failed-1", write=True)
    restarted = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)
    with pytest.raises(AuxCommandReplay):
        restarted.aidso_heat({"keyword": "x"}, idempotency_key="failed-1", write=True)
    assert provider.calls == 1
    app = _app(settings, service)
    first_http = _request(
        app,
        "POST",
        "/api/aidso/keyword-heat",
        json={"keyword": "http-fail"},
        headers={"Idempotency-Key": "http-fail-1"},
    )
    second_http = _request(
        app,
        "POST",
        "/api/aidso/keyword-heat",
        json={"keyword": "http-fail"},
        headers={"Idempotency-Key": "http-fail-1"},
    )
    assert first_http.status_code == second_http.status_code == 502
    assert first_http.json() == second_http.json() == {"error": "provider failed"}
    assert provider.calls == 2  # x 与 http-fail 各首次一次，第二次 HTTP 请求不再调用
    with connect(settings, readonly=True) as con:
        command = con.execute("SELECT status,error_json,input_json FROM command_runs WHERE idempotency_key='failed-1'").fetchone()
        assert command["status"] == "failed"
        assert "/secret" not in command["error_json"] and "cookie" not in command["input_json"]
        assert con.execute("SELECT outcome FROM audit_log WHERE action='wechat_aux.aidso_keyword_heat'").fetchone()["outcome"] == "failed"
        assert con.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='wechat_aux.aidso_keyword_heat' AND subject_id='x'"
        ).fetchone()[0] == 1


def test_aidso_post_conflict_is_409_and_success_audit_is_once(settings, tmp_path):
    provider = RecordedAidsoProvider({"keyword": "x", "model_version": "m1"})
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)
    app = _app(settings, service)
    first = _request(app, "POST", "/api/aidso/keyword-heat", json={"keyword": "x"}, headers={"Idempotency-Key": "same-1"})
    conflict = _request(app, "POST", "/api/aidso/keyword-heat", json={"keyword": "y"}, headers={"Idempotency-Key": "same-1"})
    assert first.status_code == 200
    assert conflict.status_code == 409 and conflict.json() == {"error": "idempotency key already used with different input"}
    assert provider.calls == 1
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='wechat_aux.aidso_keyword_heat'").fetchone()[0] == 1


def test_aidso_concurrent_different_payload_has_one_winner(settings, tmp_path):
    provider = RecordedAidsoProvider(lambda **params: {"keyword": params["keyword"]})
    service = WechatAuxService(replace(settings, wechat_source_root=_source(tmp_path)), aidso_provider=provider)

    def run(keyword):
        try:
            return service.aidso_heat({"keyword": keyword}, idempotency_key="parallel-aidso", write=True)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["one", "two"]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, AuxIdempotencyConflict) for item in results) == 1
    assert provider.calls == 1
