from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, replace

import pytest

from content_hub.app import create_app
from content_hub.adapters.xhs_search_provider import (
    TikHubSearchProvider,
    normalize_search_notes_response,
)
from content_hub.db.connection import connect
from content_hub.errors import ConflictError, ValidationAppError
from content_hub.features.xhs.service import XhsService


def _insert_keyword(settings) -> str:
    with connect(settings) as con:
        con.execute(
            """INSERT INTO keywords(keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json)
            VALUES(?,?,?,?,?,?,?)""",
            ("xhs_shadow_kw", "xiaohongshu", "香港保险", "active",
             "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z", "{}"),
        )
    return "xhs_shadow_kw"


def _envelope() -> dict:
    return {"data": {"data": {"items": [
        {"note_id": "n1", "title": "第一条", "url": "https://www.xiaohongshu.com/explore/n1"},
        {"note_id": "n1", "title": "重复", "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=secret"},
        {"note_id": "n2", "title": "第二条", "url": "https://www.xiaohongshu.com/explore/n2"},
    ]}}}


def _nested_envelope() -> dict:
    return {"code": 200, "data": {"data": {"items": [
        {
            "model_type": "note",
            "note": {
                "id": "nested-1",
                "title": "嵌套笔记",
                "timestamp": 1720000000,
                "user": {"userid": "user-1", "nickname": "作者一"},
                "liked_count": 11,
                "collected_count": 12,
                "comments_count": 13,
                "shared_count": 14,
                "images": ["https://img.example/a.jpg"],
                "cover": "https://img.example/cover.jpg",
            },
        },
        {
            "model_type": "note",
            "note": {
                "id": "nested-1",
                "title": "重复嵌套笔记",
                "user": {"userid": "user-1", "nickname": "作者一"},
            },
        },
    ]}}}


def test_search_notes_converter_reads_data_data_items_and_deduplicates():
    result = normalize_search_notes_response(_envelope())
    assert result["envelope_valid"] is True
    assert [item["note_id"] for item in result["hits"]] == ["n1", "n2"]
    assert result["raw_count"] == 3
    assert result["deduplicated_count"] == 1


def test_search_notes_converter_replays_nested_note_items():
    result = normalize_search_notes_response(_nested_envelope())
    assert [item["note_id"] for item in result["hits"]] == ["nested-1"]
    hit = result["hits"][0]
    assert hit["title_raw"] == "嵌套笔记"
    assert hit["creator_id"] == "user-1"
    assert hit["creator_name_raw"] == "作者一"
    assert hit["liked_count"] == 11
    assert hit["comment_count"] == 13
    assert hit["images"] == ["https://img.example/a.jpg"]
    assert hit["cover"] == "https://img.example/cover.jpg"


@dataclass
class FakeSearchProvider:
    kind: str = "fake-search_notes"

    def search(self, *, keyword_id: str, keyword: str) -> dict:
        return _envelope()


def test_shadow_refresh_replay_persists_facts_and_audit(settings):
    keyword_id = _insert_keyword(settings)
    first = XhsService(settings, provider=FakeSearchProvider()).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="shadow-one"
    )
    replay = XhsService(settings, provider=FakeSearchProvider()).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="shadow-one"
    )
    assert first["result_count"] == 2
    assert first["deduplicated_count"] == 1
    assert replay["replayed"] is True
    with connect(settings, readonly=True) as con:
        assert con.execute("SELECT count(*) FROM xhs_shadow_responses").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM search_hits").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM contents WHERE content_type='social_note'").fetchone()[0] == 2
        assert con.execute("SELECT status FROM search_refresh_jobs WHERE refresh_job_id=?",
                           (first["refresh_job_id"],)).fetchone()[0] == "succeeded"
        assert con.execute("SELECT count(*) FROM audit_log WHERE action='xhs.shadow_refresh'").fetchone()[0] == 1
        assert json.loads(con.execute("SELECT payload_json FROM xhs_shadow_responses").fetchone()[0])["data"]["data"]["items"]


def test_nested_note_replay_persists_search_fields_only(settings):
    keyword_id = _insert_keyword(settings)

    @dataclass
    class NestedProvider:
        kind: str = "fake-nested-search_notes"

        def search(self, *, keyword_id: str, keyword: str) -> dict:
            return _nested_envelope()

    result = XhsService(settings, provider=NestedProvider()).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="nested-replay"
    )
    with connect(settings, readonly=True) as con:
        hit = con.execute("SELECT title_raw,payload_json FROM search_hits WHERE snapshot_id=?", (result["snapshot_id"],)).fetchone()
        payload = json.loads(hit["payload_json"])
        assert hit["title_raw"] == "嵌套笔记"
        assert payload["note_id"] == "nested-1"
        assert payload["liked_count"] == 11
        assert con.execute("SELECT count(*) FROM search_hits WHERE snapshot_id=?", (result["snapshot_id"],)).fetchone()[0] == 1
        content = con.execute(
            "SELECT creator_id,author_name,payload_json FROM contents WHERE content_id=?",
            ("xhs_shadow_note_" + "0" * 24,),
        ).fetchone()
        assert content is None
        content = con.execute(
            "SELECT c.creator_id,c.author_name,c.payload_json FROM contents c "
            "JOIN search_hits h ON h.content_id=c.content_id WHERE h.snapshot_id=?",
            (result["snapshot_id"],),
        ).fetchone()
        assert content["author_name"] == "作者一"
        assert content["creator_id"]
        assert json.loads(content["payload_json"])["shadow"] is True
        assert con.execute(
            "SELECT count(*) FROM creators WHERE platform='xiaohongshu' AND external_id='user-1'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT count(*) FROM metric_observations WHERE snapshot_id=?",
            (result["snapshot_id"],),
        ).fetchone()[0] == 4
        assert con.execute(
            "SELECT count(*) FROM content_discoveries WHERE snapshot_id=?",
            (result["snapshot_id"],),
        ).fetchone()[0] == 1


def test_shadow_refresh_reuses_existing_xhs_note_content_by_identifier_then_url(settings):
    keyword_id = _insert_keyword(settings)
    existing_url = "https://www.xiaohongshu.com/explore/existing-note"
    with connect(settings) as con:
        con.execute(
            """INSERT INTO contents(content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json)
            VALUES(?,?,?,?,?,?,?)""",
            ("historical-note", "social_note", "历史笔记", existing_url,
             "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "{}"),
        )
        con.execute(
            """INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json)
            VALUES(?,?,?,?,?)""",
            ("xiaohongshu_note", "existing-note", "historical-note",
             "2026-07-01T00:00:00Z", "{}"),
        )

    @dataclass
    class ExistingProvider:
        kind: str = "fake-existing"

        def search(self, *, keyword_id: str, keyword: str) -> dict:
            return {"data": {"data": {"items": [
                {"model_type": "note", "note": {"id": "existing-note", "title": "按 ID 命中", "url": existing_url}},
                {"model_type": "note", "note": {"id": "other-id", "title": "按 URL 命中", "url": existing_url}},
            ]}}}

    result = XhsService(settings, provider=ExistingProvider()).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="reuse-existing"
    )
    with connect(settings, readonly=True) as con:
        hits = con.execute(
            "SELECT content_id FROM search_hits WHERE snapshot_id=? ORDER BY rank",
            (result["snapshot_id"],),
        ).fetchall()
        assert [row["content_id"] for row in hits] == ["historical-note"]
        assert con.execute("SELECT count(*) FROM contents WHERE content_id='historical-note'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM contents WHERE canonical_url=?", (existing_url,)).fetchone()[0] == 1


def test_shadow_refresh_does_not_erase_historical_content_payload(settings):
    keyword_id = _insert_keyword(settings)
    existing_url = "https://www.xiaohongshu.com/explore/preserved"
    with connect(settings) as con:
        con.execute(
            """INSERT INTO contents(
                content_id,content_type,title,canonical_url,first_seen_at,updated_at,payload_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "historical-preserved",
                "social_note",
                "历史标题",
                existing_url,
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
                json.dumps({"cover_url": "https://cdn.example/cover", "legacy_field": "keep"}),
            ),
        )
        con.execute(
            """INSERT INTO content_identifiers(
                namespace,external_id,content_id,first_seen_at,payload_json
            ) VALUES(?,?,?,?,?)""",
            ("xiaohongshu_note", "preserved", "historical-preserved", "2026-07-01T00:00:00Z", "{}"),
        )

    @dataclass
    class PreservingProvider:
        kind: str = "fake-preserving"

        def search(self, *, keyword_id: str, keyword: str) -> dict:
            return {"data": {"data": {"items": [
                {"model_type": "note", "note": {
                    "id": "preserved",
                    "title": "新搜索标题",
                    "url": existing_url,
                }},
            ]}}}

    XhsService(settings, provider=PreservingProvider()).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="preserve-payload"
    )
    with connect(settings, readonly=True) as con:
        payload = json.loads(
            con.execute(
                "SELECT payload_json FROM contents WHERE content_id='historical-preserved'"
            ).fetchone()[0]
        )
        assert payload["legacy_field"] == "keep"
        assert payload["shadow"] is True
        assert con.execute(
            "SELECT title FROM contents WHERE content_id='historical-preserved'"
        ).fetchone()[0] == "新搜索标题"


def test_live_provider_is_gated_before_network(settings):
    keyword_id = _insert_keyword(settings)

    class LiveFake(FakeSearchProvider):
        kind = "tikhub-search_notes"
        is_live = True

    service = XhsService(settings, provider=LiveFake())
    with pytest.raises(ValidationAppError):
        service.shadow_refresh(keyword_id, dry_run=False, confirm=False, idempotency_key="live-no-confirm")
    with pytest.raises(ConflictError):
        service.shadow_refresh(keyword_id, dry_run=False, confirm=True, idempotency_key="live-no-token")
    with connect(settings, readonly=True) as con:
        row = con.execute(
            """SELECT r.status FROM search_refresh_jobs r
               JOIN command_runs c ON c.command_id=r.command_id
               WHERE c.idempotency_key='live-no-token'"""
        ).fetchone()
        assert row["status"] == "failed"


def test_tikhub_provider_requires_explicit_environment_token(monkeypatch):
    monkeypatch.delenv("HUB_XHS_TIKHUB_TOKEN", raising=False)
    monkeypatch.delenv("HUB_XHS_TIKHUB_SEARCH_NOTES_URL", raising=False)
    with pytest.raises(ValueError):
        TikHubSearchProvider.from_environment()


def test_tikhub_provider_uses_search_notes_get_contract(monkeypatch):
    calls = []

    class Response:
        def read(self):
            return b'{"data":{"data":{"items":[]}}}'
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    def opener(request, timeout):
        calls.append((
            request.method, request.full_url, request.data,
            request.headers.get("Authorization"), request.headers.get("User-agent"),
        ))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", opener)
    result = TikHubSearchProvider(token="secret-token", endpoint="https://api.example/api/v1/xiaohongshu/app_v2/search_notes").search(
        keyword_id="kw", keyword="测试"
    )
    assert result["data"]["data"]["items"] == []
    assert calls[0][0] == "GET"
    assert calls[0][1].startswith("https://api.example/api/v1/xiaohongshu/app_v2/search_notes?")
    assert "keyword=%E6%B5%8B%E8%AF%95" in calls[0][1]
    assert "page=1" in calls[0][1]
    assert "sort_type=general" in calls[0][1]
    assert "source=explore_feed" in calls[0][1]
    assert calls[0][2] is None
    assert calls[0][3] == "Bearer secret-token"
    assert calls[0][4] == "xhs-keyword-monitor/2.0 (TikHub)"


def test_app_explicit_live_config_is_injected_without_persisting_token(settings, monkeypatch):
    monkeypatch.setenv("HUB_XHS_SHADOW_PROVIDER_KIND", "tikhub")
    monkeypatch.setenv("HUB_XHS_TIKHUB_TOKEN", "secret-token")
    monkeypatch.setenv("HUB_XHS_TIKHUB_SEARCH_NOTES_URL", "https://api.example/search_notes")
    live_settings = settings.__class__.load()
    live_settings = live_settings.with_database(settings.database_path)
    app = create_app(live_settings)
    assert app.state.xhs_shadow_provider.kind == "tikhub-search_notes"
    assert "secret-token" not in repr(app.state.xhs_shadow_provider)


def test_freeze_state_reports_actual_injected_provider(settings):
    service = XhsService(settings, provider=TikHubSearchProvider(token="not-persisted", endpoint="https://api.example/search_notes"))
    assert service.frozen_state()["shadow_refresh"]["default_provider"] == "tikhub-search_notes"


def test_live_http_error_returns_auditable_failed_result_without_orphan(settings, monkeypatch):
    keyword_id = _insert_keyword(settings)
    provider = TikHubSearchProvider(token="not-persisted", endpoint="https://api.example/search_notes")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://api.example", 403, "forbidden", {}, None)
        ),
    )
    live_settings = replace(settings, xhs_tikhub_token_configured=True)
    result = XhsService(live_settings, provider=provider).shadow_refresh(
        keyword_id, dry_run=False, confirm=True, idempotency_key="live-http-error"
    )
    assert result["http_status"] == 502
    assert result["failed"] is True
    assert result["reason_code"] == "tikhub_http_error"
    with connect(settings, readonly=True) as con:
        row = con.execute(
            """SELECT r.status,c.error_json FROM search_refresh_jobs r
               JOIN command_runs c ON c.command_id=r.command_id
               WHERE c.idempotency_key='live-http-error'"""
        ).fetchone()
        assert row["status"] == "failed"
        assert "403" in row["error_json"]


def test_note_detail_continues_to_serve_frozen_hub_fact_without_upstream(settings, monkeypatch):
    with connect(settings) as con:
        con.execute(
            """INSERT INTO contents(content_id,content_type,title,first_seen_at,updated_at,payload_json)
            VALUES(?,?,?,?,?,?)""",
            ("frozen-note", "social_note", "冻结笔记", "2026-07-16T00:00:00Z",
             "2026-07-16T00:00:00Z", "{}"),
        )
        con.execute(
            """INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json)
            VALUES(?,?,?,?,?)""",
            ("xiaohongshu_article", "frozen-note", "frozen-note",
             "2026-07-16T00:00:00Z", "{}"),
        )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("详情接口不得访问旧系统")
    ))
    result = XhsService(settings).article("frozen-note")
    assert result["source_status"]["source"] == "hub_db"
    assert result["article"]["content_type"] == "social_note"
