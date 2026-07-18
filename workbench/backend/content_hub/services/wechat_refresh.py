"""微信关键词刷新/调度写运行层。

这个模块刻意不依赖旧 8765 服务、Aidso、浏览器或网络。Provider 必须由调用方显式
注入；未注入时永远是 disabled。任务、事件、快照和审计都以 SQLite 为事实源。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import fcntl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.errors import ConflictError, NotFoundError, ValidationAppError
from content_hub.adapters.wechat_search_api import canonicalize_url, content_id_for_url


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _frontend_status(value: str) -> str:
    return {
        "succeeded": "completed",
        "partial_failed": "completed_with_failures",
        "blocked": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
        "queued": "running",
        "running": "running",
        "cancelling": "running",
    }.get(str(value), "failed")


class RefreshProvider(Protocol):
    kind: str

    def fetch(
        self,
        *,
        keyword_id: str,
        keyword: str,
        incremental: bool = False,
        refresh_round: Any = None,
    ) -> dict[str, Any]:
        """返回固定的 provider 结果；异常表示该词失败。"""


class DisabledRefreshProvider:
    kind = "disabled"

    def fetch(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("wechat refresh provider is disabled")


class FakeWechatRefreshProvider:
    """无网络的固定 Provider，供专项测试和显式 dry-run 注入使用。"""

    kind = "fake"

    def __init__(self, results: dict[str, dict[str, Any]] | None = None, *, fail_ids: set[str] | None = None):
        self.results = results or {}
        self.fail_ids = fail_ids or set()

    def fetch(self, *, keyword_id: str, keyword: str, incremental: bool = False, refresh_round: Any = None) -> dict[str, Any]:
        if keyword_id in self.fail_ids:
            raise RuntimeError(f"recorded provider failure: {keyword_id}")
        value = self.results.get(keyword_id)
        if value is not None:
            return json.loads(json.dumps(value, ensure_ascii=False))
        return {
            "captured_at": _now(),
            "result_count": 1,
            "features": {"suggestions": [], "related": [], "provider": "fake"},
            "hits": [{"rank": 1, "title_raw": f"{keyword} 固定样本", "url_raw": f"https://example.invalid/wechat/{keyword_id}"}],
            "metrics": [],
            "source_ref": "provider:fake",
            "incremental": incremental,
            "refresh_round": refresh_round,
        }


class InvalidKeywordIDsError(ValidationAppError):
    def __init__(self, invalid_keyword_ids: list[str]):
        self.invalid_keyword_ids = invalid_keyword_ids
        super().__init__("keyword_ids contains invalid items")


class BatchAlreadyRunningError(ConflictError):
    def __init__(self, state: dict[str, Any]):
        self.state = state
        super().__init__("batch already running")


class WechatRefreshService:
    MODULE = "wechat-search"
    PLATFORM = "wechat-search"
    RETRYABLE_REASON_CODES = frozenset({
        "remote_http",
        "remote_timeout",
        "remote_unavailable",
    })

    def __init__(
        self,
        settings: Any,
        *,
        provider: RefreshProvider | None = None,
        actor_id: str = "user",
        max_attempts: int | None = None,
        retry_delays_seconds: tuple[float, ...] | None = None,
    ):
        self.settings = settings
        self.provider = provider or DisabledRefreshProvider()
        self.actor_id = actor_id or "user"
        self.max_attempts = max(
            1,
            int(max_attempts or os.getenv("HUB_WECHAT_REFRESH_MAX_ATTEMPTS", "3")),
        )
        if retry_delays_seconds is None:
            raw_delays = os.getenv("HUB_WECHAT_REFRESH_RETRY_DELAYS_SECONDS", "20,60")
            retry_delays_seconds = tuple(
                max(0.0, float(value.strip()))
                for value in raw_delays.split(",")
                if value.strip()
            )
        self.retry_delays_seconds = retry_delays_seconds or (0.0,)

    def _failure_log_path(self) -> Path:
        configured = str(os.getenv("HUB_WECHAT_REFRESH_FAILURE_LOG", "")).strip()
        if configured:
            return Path(configured).expanduser().resolve()
        project_root = Path(__file__).resolve().parents[4]
        database_path = Path(self.settings.database_path).resolve()
        if project_root == database_path or project_root in database_path.parents:
            return project_root / "刷新失败点.md"
        return database_path.parent / "刷新失败点.md"

    def _failure_log_lock_path(self, failure_log_path: Path) -> Path:
        project_root = Path(__file__).resolve().parents[4]
        runtime_dir = (
            project_root / "data" / "runtime"
            if failure_log_path.parent == project_root
            else failure_log_path.parent / ".runtime"
        )
        path_hash = hashlib.sha256(str(failure_log_path).encode("utf-8")).hexdigest()[:12]
        return runtime_dir / f"wechat-refresh-failure-{path_hash}.lock"

    @staticmethod
    def _markdown_text(value: Any) -> str:
        return str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()

    def _append_failure_log(
        self,
        *,
        job_id: str,
        item_id: str,
        keyword: str,
        keyword_id: str,
        reason_code: str,
        error: str,
        attempt_count: int,
        source: str,
        status: str = "failed",
    ) -> None:
        path = self._failure_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._failure_log_lock_path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        occurred_at = datetime.now().astimezone().isoformat(timespec="seconds")
        row = (
            f"| {occurred_at} | {self._markdown_text(job_id)} | "
            f"{self._markdown_text(item_id)} | {self._markdown_text(keyword_id)} | "
            f"{self._markdown_text(keyword)} | {attempt_count} | "
            f"{self._markdown_text(reason_code)} | {self._markdown_text(error)} | "
            f"{self._markdown_text(source)} | {self._markdown_text(status)} |\n"
        )
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if not path.exists() or path.stat().st_size == 0:
                    path.write_text(
                        "# 微信关键词刷新失败点\n\n"
                        "> 由新系统增量记录。这里只记录最终失败、进程中断等需要人工关注的刷新点；"
                        "成功记录仍以 SQLite 刷新历史为准。\n\n"
                        "| 时间 | 批次 | 刷新项 | 关键词ID | 关键词 | 尝试次数 | 原因代码 | 错误详情 | 来源 | 状态 |\n"
                        "|---|---|---|---|---|---:|---|---|---|---|\n",
                        encoding="utf-8",
                    )
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(row)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _retry_delay(self, attempt_count: int) -> float:
        index = max(0, min(attempt_count - 1, len(self.retry_delays_seconds) - 1))
        return float(self.retry_delays_seconds[index])

    def _hub_base_url(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    def _manifest(self, con, *, source_ref: str, source_hash: str, now: str) -> str:
        manifest_id = f"manifest_wechat_refresh_{source_hash[:24]}"
        con.execute(
            """INSERT OR IGNORE INTO source_manifests(
                manifest_id,system_key,source_kind,root_fingerprint,manifest_hash,entry_count,captured_at,immutable,payload_json
            ) VALUES(?,?,?,?,?,0,?,1,?)""",
            (manifest_id, self.MODULE, "refresh-provider", source_hash, source_hash, now, _json({"source_ref": source_ref})),
        )
        return manifest_id

    def _audit(self, con, *, action: str, subject_type: str, subject_id: str, outcome: str, details: dict[str, Any], request_id: str | None = None) -> str:
        audit_id = generate_ulid_like("cmd").replace("cmd_", "audit_", 1)
        con.execute(
            """INSERT INTO audit_log(
                audit_id,occurred_at,actor_type,actor_id,action,subject_type,subject_id,request_id,outcome,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (audit_id, _now(), "user", self.actor_id, action, subject_type, subject_id, request_id, outcome, _json(details)),
        )
        return audit_id

    def _event(self, con, *, job_id: str, item_id: str | None, event_type: str, status: str, message: str = "", details: dict[str, Any] | None = None) -> None:
        con.execute(
            """INSERT INTO search_refresh_events(
                event_id,refresh_job_id,refresh_item_id,event_type,status,message,details_json,occurred_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (generate_ulid_like("evt"), job_id, item_id, event_type, status, message, _json(details or {}), _now()),
        )

    def _receipt(self, con, *, command_id: str, key: str, legacy_status: str, hub_status: str, reconcile_status: str, details: dict[str, Any]) -> dict[str, str]:
        receipt_id = generate_ulid_like("dwr")
        con.execute(
            """INSERT INTO dual_write_receipts(
                receipt_id,module_key,command_id,idempotency_key,legacy_status,hub_status,
                reconcile_status,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(module_key,idempotency_key) DO UPDATE SET
                command_id=excluded.command_id,legacy_status=excluded.legacy_status,
                hub_status=excluded.hub_status,reconcile_status=excluded.reconcile_status,
                details_json=excluded.details_json""",
            (receipt_id, self.MODULE, command_id, key, legacy_status, hub_status, reconcile_status, _json(details), _now()),
        )
        row = con.execute(
            "SELECT receipt_id,command_id,idempotency_key FROM dual_write_receipts WHERE module_key=? AND idempotency_key=?",
            (self.MODULE, key),
        ).fetchone()
        return {"receipt_id": row["receipt_id"], "command_id": row["command_id"], "idempotency_key": row["idempotency_key"]}

    def _existing(self, con, key: str, input_hash: str) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT c.*,r.refresh_job_id,r.status AS job_status,r.checkpoint_json
               FROM command_runs c LEFT JOIN search_refresh_jobs r ON r.command_id=c.command_id
               WHERE c.module_key=? AND c.idempotency_key=?""",
            (self.MODULE, key),
        ).fetchone()
        if not row:
            return None
        old_input = json.loads(row["input_json"] or "{}")
        if _hash(old_input) != input_hash:
            raise ConflictError("idempotency key 已用于不同输入。")
        output = json.loads(row["output_json"] or "{}")
        return output if isinstance(output, dict) else {"command_id": row["command_id"], "status": row["status"], "refresh_job_id": row["refresh_job_id"]}

    def _keyword(self, con, keyword_id: str) -> dict[str, Any]:
        row = con.execute(
            "SELECT keyword_id,platform,keyword,status FROM keywords WHERE keyword_id=?",
            (keyword_id,),
        ).fetchone()
        if not row or row["platform"] != self.PLATFORM:
            raise NotFoundError("微信关键词", keyword_id)
        if row["status"] == "archived":
            raise ValidationAppError("归档关键词不能刷新。")
        return dict(row)

    def _create_command(self, con, *, key: str, command_type: str, input_payload: dict[str, Any], confirmation: dict[str, Any] | None = None) -> tuple[str, str]:
        command_id = generate_ulid_like("cmd")
        job_id = generate_ulid_like("srj")
        now = _now()
        con.execute(
            """INSERT INTO command_runs(
                command_id,module_key,command_type,idempotency_key,actor_id,status,
                confirmation_json,input_json,output_json,error_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?)""",
            (command_id, self.MODULE, command_type, key, self.actor_id[:120], _json(confirmation or {}),
             _json(input_payload), "{}", "{}", now, now),
        )
        return command_id, job_id

    def _active_batch(self, con) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT * FROM search_refresh_jobs
               WHERE system_key=? AND platform=? AND trigger_type IN ('manual','scheduled')
                 AND command_id IS NOT NULL
                 AND status IN ('queued','running') AND cancel_requested=0
               ORDER BY created_at DESC LIMIT 1""",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        return dict(row) if row else None

    def _active_single(self, con) -> dict[str, Any] | None:
        row = con.execute(
            """SELECT * FROM search_refresh_jobs
               WHERE system_key=? AND platform=? AND trigger_type='manual'
                 AND command_id IS NOT NULL
                 AND requested_count=1 AND status IN ('queued','running')
                 AND cancel_requested=0
               ORDER BY created_at DESC LIMIT 1""",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        return dict(row) if row else None

    def _set_active_job(self, con, job_id: str) -> None:
        con.execute(
            """INSERT OR IGNORE INTO search_scheduler_state(
                system_key,platform,enabled,next_run_at,last_run_at,active_refresh_job_id,updated_at,payload_json
            ) VALUES(?,?,0,NULL,NULL,?,?,?)""",
            (self.MODULE, self.PLATFORM, job_id, _now(), "{}"),
        )
        con.execute(
            "UPDATE search_scheduler_state SET active_refresh_job_id=?,updated_at=? WHERE system_key=? AND platform=?",
            (job_id, _now(), self.MODULE, self.PLATFORM),
        )

    def _write_snapshot(self, con, *, keyword: dict[str, Any], job_id: str, item_id: str, result: dict[str, Any], trigger_type: str) -> tuple[str, dict[str, Any]]:
        now = str(result.get("captured_at") or _now())
        source_ref = str(result.get("source_ref") or f"provider:{getattr(self.provider, 'kind', 'unknown')}")
        source_hash = _hash({"keyword_id": keyword["keyword_id"], "job_id": job_id, "result": result})
        snapshot_id = f"snp_{source_hash[:24]}"
        manifest_id = self._manifest(con, source_ref=source_ref, source_hash=source_hash, now=now)
        hits = result.get("hits") if isinstance(result.get("hits"), list) else []
        stored_hits: list[dict[str, Any]] = []
        article_markdown_count = 0
        for hit in hits:
            hit = dict(hit)
            url = canonicalize_url(hit.get("canonical_url") or hit.get("url_raw") or hit.get("url"))
            content_id = self._upsert_content(
                con,
                hit=hit,
                url=url,
                captured_at=now,
            )
            if content_id:
                # Provider 计算的 ID 仅是候选值；若 Hub 已有同 canonical URL，
                # 快照也必须落实际复用的 content_id，避免跨来源出现“快照 ID”
                # 与 contents/search_hits 不一致。
                hit["content_id"] = content_id
            if content_id and hit.get("markdown_body"):
                self._persist_article_markdown(
                    con,
                    hit=hit,
                    content_id=content_id,
                    source_ref=source_ref,
                    captured_at=now,
                )
                article_markdown_count += 1
            hit.pop("markdown_body", None)
            stored_hits.append(hit)
        con.execute(
            """INSERT INTO search_snapshots(
                snapshot_id,platform,keyword,keyword_id,captured_at,trigger_type,result_count,features_json,source_ref,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id) DO NOTHING""",
            (snapshot_id, self.PLATFORM, keyword["keyword"], keyword["keyword_id"], now, trigger_type,
             result.get("result_count", len(hits)), _json(result.get("features") or {}), source_ref, _json({
                 **{key: value for key, value in result.items() if key not in {"hits", "raw_payload"}},
                 "hits": stored_hits,
                 "raw_payload": result.get("raw_payload") if isinstance(result.get("raw_payload"), dict) else {},
                 "article_markdown_count": article_markdown_count,
                 "markdown_count": 0,
             })),
        )
        for ordinal, hit in enumerate(stored_hits, 1):
            rank = int(hit.get("rank") or ordinal)
            hit_id = f"hit_{hashlib.sha256(f'{snapshot_id}:{rank}'.encode()).hexdigest()[:24]}"
            url = canonicalize_url(hit.get("canonical_url") or hit.get("url_raw") or hit.get("url"))
            content_id = con.execute("SELECT content_id FROM contents WHERE canonical_url=?", (url,)).fetchone()
            content_id = str(content_id["content_id"]) if content_id else None
            con.execute(
                """INSERT INTO search_hits(
                    hit_id,snapshot_id,rank,content_id,title_raw,url_raw,creator_name_raw,payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(hit_id) DO NOTHING""",
                (hit_id, snapshot_id, rank, content_id, hit.get("title_raw") or hit.get("title"), hit.get("url_raw") or hit.get("url"), hit.get("creator_name_raw") or hit.get("account"), _json(hit)),
            )
            if content_id:
                discovery_id = f"disc_{hashlib.sha256(f'{content_id}:wechat-search:keyword-rank:{snapshot_id}'.encode()).hexdigest()[:24]}"
                con.execute(
                    """INSERT OR IGNORE INTO content_discoveries(
                        discovery_id,content_id,discovery_system,discovery_channel,
                        discovered_at,snapshot_id,source_ref,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        discovery_id,
                        content_id,
                        self.MODULE,
                        "keyword-rank",
                        now,
                        snapshot_id,
                        source_ref,
                        _json(hit),
                    ),
                )
        metrics = result.get("metrics") if isinstance(result.get("metrics"), list) else []
        for metric in metrics:
            metric_key = str(metric.get("metric_key") or "").strip()
            if not metric_key:
                continue
            con.execute(
                """INSERT OR IGNORE INTO metric_definitions(
                    metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description,active
                ) VALUES(?,?,?,?,?,?,?,?,1)""",
                (metric_key, self.PLATFORM, str(metric.get("subject_type") or "keyword"), str(metric.get("display_name") or metric_key), "number", metric.get("unit"), str(metric.get("accumulation_mode") or "gauge"), str(metric.get("description") or "")),
            )
            value = metric.get("numeric_value", metric.get("value"))
            if value is None:
                continue
            subject_id = str(metric.get("subject_id") or keyword["keyword_id"])
            observation_id = f"obs_{hashlib.sha256(f'{snapshot_id}:{metric_key}:{subject_id}'.encode()).hexdigest()[:24]}"
            con.execute(
                """INSERT OR IGNORE INTO metric_observations(
                    observation_id,subject_type,subject_id,metric_key,observed_at,numeric_value,snapshot_id,source_ref,confidence,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (observation_id, str(metric.get("subject_type") or "keyword"), str(metric.get("subject_id") or keyword["keyword_id"]), metric_key, now, float(value), snapshot_id, source_ref, metric.get("confidence"), _json(metric)),
            )
        return snapshot_id, {
            "source_ref": source_ref,
            "source_manifest_id": manifest_id,
            "captured_at": now,
            "hit_count": len(hits),
            "metric_count": len(metrics),
            "markdown_count": 0,
            "article_markdown_count": article_markdown_count,
            "invalid_hit_count": int(result.get("invalid_hit_count") or 0),
        }

    def _upsert_content(self, con, *, hit: dict[str, Any], url: str | None, captured_at: str) -> str | None:
        if not url:
            return None
        content_id = str(hit.get("content_id") or content_id_for_url(url))
        existing = con.execute(
            "SELECT content_id FROM contents WHERE canonical_url=?",
            (url,),
        ).fetchone()
        if existing:
            content_id = str(existing["content_id"])
        title = hit.get("title_raw") or hit.get("title")
        author = hit.get("creator_name_raw") or hit.get("account") or hit.get("author")
        con.execute(
            """INSERT INTO contents(
                content_id,content_type,title,canonical_url,author_name,first_seen_at,updated_at,
                payload_json
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(content_id) DO UPDATE SET
                title=COALESCE(excluded.title,contents.title),
                author_name=COALESCE(excluded.author_name,contents.author_name),
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json""",
            (
                content_id,
                "wechat_article",
                str(title) if title is not None else None,
                url,
                str(author) if author is not None else None,
                captured_at,
                captured_at,
                _json({key: value for key, value in hit.items() if key != "markdown_body"}),
            ),
        )
        for namespace, external_id in (("wechat_url", url), ("wechat_article", content_id)):
            con.execute(
                """INSERT INTO content_identifiers(namespace,external_id,content_id,first_seen_at,payload_json)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(namespace,external_id) DO UPDATE SET
                       content_id=excluded.content_id,payload_json=excluded.payload_json""",
                (namespace, external_id, content_id, captured_at, _json({"canonical_url": url})),
            )
        return content_id

    def _persist_article_markdown(self, con, *, hit: dict[str, Any], content_id: str, source_ref: str, captured_at: str) -> str:
        body = str(hit.get("markdown_body") or "").strip() + "\n"
        content_hash = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
        asset_root = Path(self.settings.asset_store_path).resolve()
        asset_file = asset_root / "wechat" / f"{content_hash}.md"
        asset_file.parent.mkdir(parents=True, exist_ok=True)
        asset_file.resolve().relative_to(asset_root)
        if not asset_file.exists():
            fd, temporary = tempfile.mkstemp(prefix=f".{content_hash}.", suffix=".tmp", dir=str(asset_file.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, asset_file)
            finally:
                Path(temporary).unlink(missing_ok=True)
        asset_path = str(asset_file.relative_to(asset_root)).replace("\\", "/")
        file_hash = hashlib.sha256(asset_file.read_bytes()).hexdigest()
        con.execute(
            """UPDATE contents SET md_path=?,file_hash=?,content_hash=?,published_at=COALESCE(published_at,?),updated_at=?
               WHERE content_id=?""",
            (asset_path, file_hash, content_hash, hit.get("published_at"), captured_at, content_id),
        )
        path_source = f"remote:wechat-search-api:article:{content_id}"
        con.execute(
            """INSERT INTO wechat_article_paths(
                   article_id,old_article_id,relative_path,asset_path,source_ref,created_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(article_id,old_article_id,source_ref) DO UPDATE SET
                   asset_path=excluded.asset_path,relative_path=excluded.relative_path""",
            (content_id, content_id, None, asset_path, path_source, captured_at),
        )
        return asset_path

    def _runtime_projection(self, con, *, subject_id: str, payload: dict[str, Any], source_hash: str, manifest_id: str, source_ref: str) -> None:
        con.execute(
            """INSERT OR REPLACE INTO wechat_legacy_projections(
                projection_id,projection_kind,subject_id,payload_json,source_hash,source_manifest_id,source_ref,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (f"proj_{hashlib.sha256(f'runtime:{subject_id}:{source_hash}'.encode()).hexdigest()[:24]}", "runtime", subject_id,
             _json(payload), source_hash, manifest_id, source_ref, _now()),
        )

    def _batch_runtime_projection(self, con, *, job_id: str, payload: dict[str, Any]) -> None:
        """Persist the batch runtime after every short checkpoint transaction.

        Batch provider calls intentionally happen outside the writer lock, so the
        compatibility read path cannot wait for the final projection. Reuse an
        item manifest when one exists; otherwise create one deterministic,
        temporary manifest for the runtime record itself.
        """
        manifest = con.execute(
            """SELECT i.source_manifest_id,m.payload_json
               FROM search_refresh_items i
               JOIN source_manifests m ON m.manifest_id=i.source_manifest_id
               WHERE i.refresh_job_id=? AND i.source_manifest_id IS NOT NULL
               ORDER BY i.finished_at DESC, i.ordinal DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if manifest:
            manifest_id = str(manifest["source_manifest_id"])
            source_ref = str(json.loads(manifest["payload_json"] or "{}").get("source_ref") or "provider")
        else:
            source_ref = f"provider:{getattr(self.provider, 'kind', 'unknown')}"
            manifest_hash = _hash({"runtime_batch_manifest": job_id})
            manifest_id = self._manifest(
                con,
                source_ref=source_ref,
                source_hash=manifest_hash,
                now=_now(),
            )
        self._runtime_projection(
            con,
            subject_id=job_id,
            payload={"runtime_subtype": "batch", **payload},
            # Keep one mutable compatibility row per batch. This preserves
            # runtime_history's historical one-row-per-batch contract while
            # still making every checkpoint visible to polling readers.
            source_hash=_hash({"runtime_batch_projection": job_id}),
            manifest_id=manifest_id,
            source_ref=source_ref,
        )

    def _finish_item(self, con, *, job_id: str, item_id: str, keyword: dict[str, Any], status: str, result: dict[str, Any] | None, error: dict[str, Any] | None, trigger_type: str) -> dict[str, Any]:
        row = con.execute("SELECT ordinal FROM search_refresh_items WHERE refresh_item_id=?", (item_id,)).fetchone()
        ordinal = int(row["ordinal"]) if row else 0
        snapshot_id = None
        evidence: dict[str, Any] = {}
        if status == "succeeded":
            snapshot_id, evidence = self._write_snapshot(con, keyword=keyword, job_id=job_id, item_id=item_id, result=result or {}, trigger_type=trigger_type)
        now = _now()
        setting = con.execute(
            """SELECT setting_id,payload_json
               FROM search_keyword_settings
               WHERE system_key=? AND platform=? AND keyword_id=?""",
            (self.MODULE, self.PLATFORM, keyword["keyword_id"]),
        ).fetchone()
        if setting:
            try:
                setting_payload = json.loads(setting["payload_json"] or "{}")
            except json.JSONDecodeError:
                setting_payload = {}
            setting_payload.update(
                {
                    "last_refresh_attempt_at": evidence.get("captured_at") or now,
                    "last_refresh_status": "success" if status == "succeeded" else status,
                }
            )
            if status == "succeeded":
                setting_payload["last_refresh_at"] = evidence.get("captured_at") or now
                setting_payload["last_seen_at"] = evidence.get("captured_at") or now
                setting_payload["snapshot_count"] = int(
                    con.execute(
                        "SELECT COUNT(*) FROM search_snapshots WHERE keyword_id=? AND platform=?",
                        (keyword["keyword_id"], self.PLATFORM),
                    ).fetchone()[0]
                )
            con.execute(
                """UPDATE search_keyword_settings
                   SET payload_json=?,updated_at=?
                   WHERE setting_id=?""",
                (_json(setting_payload), now, setting["setting_id"]),
            )
        con.execute(
            """UPDATE search_refresh_items
               SET status=?,current_phase=?,snapshot_id=?,source_manifest_id=?,error_json=?,
                   finished_at=?
               WHERE refresh_item_id=?""",
            (status, "completed" if status == "succeeded" else status, snapshot_id, evidence.get("source_manifest_id"), _json(error or {}), now, item_id),
        )
        counts = con.execute(
            """SELECT
                SUM(status='succeeded') AS succeeded,
                SUM(status IN ('failed','blocked')) AS failed,
                SUM(status='cancelled') AS cancelled
               FROM search_refresh_items WHERE refresh_job_id=?""",
            (job_id,),
        ).fetchone()
        con.execute(
            "UPDATE search_refresh_jobs SET succeeded_count=?,failed_count=?,checkpoint_json=?,updated_at=? WHERE refresh_job_id=?",
            (
                int(counts["succeeded"] or 0),
                int(counts["failed"] or 0),
                _json({
                    "last_item_id": item_id,
                    "last_keyword_id": keyword["keyword_id"],
                    "last_status": status,
                    "succeeded_count": int(counts["succeeded"] or 0),
                    "failed_count": int(counts["failed"] or 0),
                    "cancelled_count": int(counts["cancelled"] or 0),
                }),
                now,
                job_id,
            ),
        )
        self._batch_runtime_projection(con, job_id=job_id, payload=self._job_payload(con, job_id))
        self._event(con, job_id=job_id, item_id=item_id, event_type=status, status=status, details={**evidence, **(error or {})})
        return {"keyword_id": keyword["keyword_id"], "keyword": keyword["keyword"], "status": status, "snapshot_id": snapshot_id, **evidence, **(error or {})}

    def _job_payload(self, con, job_id: str, *, semantic: bool = False) -> dict[str, Any]:
        row = con.execute("SELECT * FROM search_refresh_jobs WHERE refresh_job_id=?", (job_id,)).fetchone()
        if not row:
            raise NotFoundError("微信刷新批次", job_id)
        items = [dict(x) for x in con.execute("SELECT i.*,k.keyword FROM search_refresh_items i JOIN keywords k ON k.keyword_id=i.keyword_id WHERE i.refresh_job_id=? ORDER BY i.ordinal", (job_id,))]
        success = [x for x in items if x["status"] == "succeeded"]
        failed = [x for x in items if x["status"] in {"failed", "blocked"}]
        blocked = [x for x in items if x["status"] == "blocked"]
        cancelled = [x for x in items if x["status"] == "cancelled"]
        active = row["status"] in {"queued", "running"} and not row["cancel_requested"]
        internal_status = "cancelling" if row["cancel_requested"] and active else str(row["status"])
        if row["cancel_requested"] and not active and not failed and not row["status"] == "succeeded":
            internal_status = "cancelled"
        status = internal_status if semantic else _frontend_status(internal_status)
        failure_rows: list[dict[str, str]] = []
        failure_reasons: list[str] = []
        for item in failed:
            try:
                error = json.loads(item.get("error_json") or "{}")
            except json.JSONDecodeError:
                error = {}
            reason = str(error.get("reason_code") or error.get("error") or item["status"])
            failure_rows.append({"keyword": str(item["keyword"]), "reason": reason})
            if reason not in failure_reasons:
                failure_reasons.append(reason)
        snapshot_count = sum(1 for item in items if item.get("snapshot_id"))
        cancel_reason = str(row["cancel_reason"] or "") if "cancel_reason" in row.keys() else ""
        return {
            "batch_id": row["refresh_job_id"], "job_id": row["refresh_job_id"], "status": status,
            "hub_status": internal_status,
            "total": row["requested_count"], "requested_count": row["requested_count"],
            "success_count": len(success), "succeeded_count": len(success), "failed_count": len(failed),
            "blocked_count": len(blocked),
            "cancelled_count": len(cancelled), "processed_count": len(success) + len(failed) + len(cancelled),
            "pending_count": len(items) - len(success) - len(failed) - len(cancelled),
            "current_keyword": next((x["keyword"] for x in items if x["status"] == "running"), None),
            "completed_keywords": [x["keyword"] for x in success],
            "failed_keywords": failure_rows,
            "failure_reasons": failure_reasons,
            "cancelled_keywords": [x["keyword"] for x in cancelled],
            "cancel_reason": cancel_reason,
            "snapshot_count": snapshot_count,
            "started_at": row["started_at"], "finished_at": row["finished_at"], "updated_at": row["updated_at"],
            "cancel_requested": bool(row["cancel_requested"]), "is_active": active,
            "is_finished": not active and status not in {"running"},
            "source": row["trigger_source"],
        }

    def _complete_job(self, con, *, job_id: str, command_id: str, key: str, status: str, output: dict[str, Any], error: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        counts = con.execute(
            """SELECT
                SUM(status='succeeded') AS succeeded,
                SUM(status IN ('failed','blocked')) AS failed
               FROM search_refresh_items WHERE refresh_job_id=?""",
            (job_id,),
        ).fetchone()
        con.execute(
            """UPDATE search_refresh_jobs SET status=?,succeeded_count=?,failed_count=?,finished_at=?,updated_at=? WHERE refresh_job_id=?""",
            (
                status,
                int(output.get("success_count") if output.get("success_count") is not None else counts["succeeded"] or 0),
                int(output.get("failed_count") if output.get("failed_count") is not None else counts["failed"] or 0),
                now,
                now,
                job_id,
            ),
        )
        con.execute(
            "UPDATE command_runs SET status=?,output_json=?,error_json=?,updated_at=? WHERE command_id=?",
            ("succeeded" if status == "succeeded" else "failed" if status in {"failed", "partial_failed"} else status, _json(output), _json(error or {}), now, command_id),
        )
        con.execute("UPDATE search_scheduler_state SET active_refresh_job_id=NULL, last_run_at=?, updated_at=? WHERE system_key=? AND platform=?", (now, now, self.MODULE, self.PLATFORM))
        return output

    def refresh_one(
        self,
        *,
        keyword_id: str,
        key: str,
        request_keyword: str = "",
        request_id: str | None = None,
        confirm: bool = True,
        semantic: bool = False,
    ) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        payload = {"keyword_id": keyword_id, "platform": self.PLATFORM, "keyword": request_keyword.strip(), "confirm": bool(confirm)}
        input_hash = _hash(payload)
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    old = self._existing(con, key, input_hash)
                    if old:
                        return old
                    if self._active_single(con):
                        raise ConflictError("single refresh already running")
                    keyword = self._keyword(con, keyword_id)
                    command_id, job_id = self._create_command(con, key=key, command_type="wechat.keyword.refresh", input_payload=payload, confirmation={"confirmed": bool(confirm)})
                    now = _now()
                    con.execute("INSERT INTO search_refresh_jobs(refresh_job_id,system_key,platform,command_id,trigger_type,status,requested_count,started_at,created_at,updated_at,trigger_source) VALUES(?,?,?,?,?,'running',1,?,?,?,?)", (job_id, self.MODULE, self.PLATFORM, command_id, "manual", now, now, now, "web_refresh"))
                    self._set_active_job(con, job_id)
                    item_id = generate_ulid_like("sri")
                    con.execute("INSERT INTO search_refresh_items(refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase,started_at) VALUES(?,?,?,0,'running',1,'provider',?)", (item_id, job_id, keyword_id, now))
                    self._event(con, job_id=job_id, item_id=item_id, event_type="started", status="running")
                    try:
                        result = self.provider.fetch(keyword_id=keyword_id, keyword=keyword["keyword"])
                        item = self._finish_item(con, job_id=job_id, item_id=item_id, keyword=keyword, status="succeeded", result=result, error=None, trigger_type="manual")
                        output = {"job_id": job_id, "refresh_job_id": job_id, "command_id": command_id, "status": "succeeded", "keyword_id": keyword_id, "keyword": keyword["keyword"], "source": "hub", "provider": getattr(self.provider, "kind", "unknown"), "result": item}
                        self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status="succeeded", output=output)
                        output.update(self._job_payload(con, job_id, semantic=semantic))
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        manifest_id = item.get("source_manifest_id")
                        if manifest_id:
                            self._runtime_projection(con, subject_id=job_id, payload={"runtime_subtype": "single_job", **output}, source_hash=_hash(output), manifest_id=manifest_id, source_ref=str(item.get("source_ref") or "provider"))
                        receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"job_id": job_id, "provider": getattr(self.provider, "kind", "unknown")})
                        self._audit(con, action="wechat.refresh", subject_type="refresh_job", subject_id=job_id, outcome="succeeded", details={"keyword_id": keyword_id, **receipt}, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        return output
                    except Exception as exc:
                        error = {
                            "error": str(exc),
                            "reason_code": (
                                "provider_disabled"
                                if getattr(self.provider, "kind", "") == "disabled"
                                else str(getattr(exc, "reason_code", "provider_failed"))
                            ),
                        }
                        provider_payload = getattr(exc, "payload", None)
                        if isinstance(provider_payload, dict):
                            request_id = provider_payload.get("request_id")
                            if request_id:
                                error["remote_request_id"] = str(request_id)
                        self._finish_item(con, job_id=job_id, item_id=item_id, keyword=keyword, status="blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed", result=None, error=error, trigger_type="manual")
                        output = {"job_id": job_id, "refresh_job_id": job_id, "command_id": command_id, "status": "blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed", "keyword_id": keyword_id, "keyword": keyword["keyword"], "blocked": getattr(self.provider, "kind", "") == "disabled", "upstream_called": False, "error": error}
                        self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status="blocked" if output["blocked"] else "failed", output=output, error=error)
                        output.update(self._job_payload(con, job_id, semantic=semantic))
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        failure_hash = _hash(output)
                        failure_manifest = self._manifest(
                            con,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                            source_hash=failure_hash,
                            now=_now(),
                        )
                        self._runtime_projection(
                            con,
                            subject_id=job_id,
                            payload={"runtime_subtype": "single_job", **output},
                            source_hash=failure_hash,
                            manifest_id=failure_manifest,
                            source_ref=f"provider:{getattr(self.provider, 'kind', 'unknown')}",
                        )
                        receipt = self._receipt(
                            con,
                            command_id=command_id,
                            key=key,
                            legacy_status="not_attempted",
                            hub_status="blocked" if output["blocked"] else "failed",
                            reconcile_status="blocked",
                            details=error,
                        )
                        self._audit(con, action="wechat.refresh", subject_type="refresh_job", subject_id=job_id, outcome="blocked" if output["blocked"] else "failed", details={**error, **receipt}, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        return output

    def refresh_batch(self, *, keyword_ids: list[str] | None, key: str, source: str = "web_refresh_all", incremental: bool = False, refresh_round: Any = None, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        unique_ids = list(dict.fromkeys(str(item).strip() for item in (keyword_ids or []) if str(item).strip()))
        if incremental and not unique_ids:
            raise ValidationAppError("incremental refresh requires keyword_ids")

        # 这里只创建批次和 checkpoint；绝不能把网络调用放在这个事务里。
        # 这样 /api/refresh-all/status、取消接口以及其他写接口在 provider 慢时仍可访问。
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    candidate_rows = con.execute(
                        """SELECT k.keyword_id,k.platform,k.keyword,k.status
                           FROM keywords k
                           LEFT JOIN search_keyword_settings s
                             ON s.keyword_id=k.keyword_id
                            AND s.system_key=? AND s.platform=?
                           WHERE k.platform=? AND k.status='active'
                             AND COALESCE(s.batch_default_selected,1)=1
                           ORDER BY k.keyword_id""",
                        (self.MODULE, self.PLATFORM, self.PLATFORM),
                    ).fetchall()
                    candidate_map = {str(row["keyword_id"]): dict(row) for row in candidate_rows}
                    if not unique_ids:
                        unique_ids = list(candidate_map)
                    if not unique_ids:
                        raise ValidationAppError("no keywords found")
                    payload = {"keyword_ids": unique_ids, "incremental": bool(incremental), "refresh_round": refresh_round, "source": source}
                    input_hash = _hash(payload)
                    old = self._existing(con, key, input_hash)
                    if old:
                        return old
                    invalid_ids = [keyword_id for keyword_id in unique_ids if keyword_id not in candidate_map]
                    if invalid_ids:
                        raise InvalidKeywordIDsError(invalid_ids)
                    keywords = [candidate_map[keyword_id] for keyword_id in unique_ids]
                    active = self._active_batch(con)
                    if active:
                        raise BatchAlreadyRunningError(self._job_payload(con, active["refresh_job_id"]))
                    command_id, job_id = self._create_command(con, key=key, command_type="wechat.refresh_all", input_payload=payload)
                    now = _now()
                    con.execute("INSERT INTO search_refresh_jobs(refresh_job_id,system_key,platform,command_id,trigger_type,status,requested_count,started_at,created_at,updated_at,trigger_source) VALUES(?,?,?,?,?,'running',?,?,?, ?,?)", (job_id, self.MODULE, self.PLATFORM, command_id, "scheduled" if source == "scheduler" else "manual", len(unique_ids), now, now, now, source))
                    self._set_active_job(con, job_id)
                    for ordinal, keyword in enumerate(keywords):
                        item_id = generate_ulid_like("sri")
                        con.execute("INSERT INTO search_refresh_items(refresh_item_id,refresh_job_id,keyword_id,ordinal,status,attempt_count,current_phase) VALUES(?,?,?,?,'queued',0,'queued')", (item_id, job_id, keyword["keyword_id"], ordinal))
                    self._event(con, job_id=job_id, item_id=None, event_type="started", status="running", details={"count": len(keywords), "incremental": incremental})
                    self._batch_runtime_projection(con, job_id=job_id, payload=self._job_payload(con, job_id))

        return self.run_batch(job_id, request_id=request_id)

    def run_batch(self, job_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """SELECT j.*,c.idempotency_key,c.input_json
                   FROM search_refresh_jobs j
                   JOIN command_runs c ON c.command_id=j.command_id
                   WHERE j.refresh_job_id=? AND j.system_key=? AND j.platform=?""",
                (job_id, self.MODULE, self.PLATFORM),
            ).fetchone()
            if not row:
                raise NotFoundError("微信刷新批次", job_id)
            if row["status"] not in {"queued", "running"}:
                output = self._job_payload(con, job_id)
                output["hub_status"] = str(row["status"])
                output["status"] = _frontend_status(str(row["status"]))
                return output
            payload = json.loads(row["input_json"] or "{}")
            source = str(payload.get("source") or row["trigger_source"] or "web_refresh_all")
            incremental = bool(payload.get("incremental"))
            refresh_round = payload.get("refresh_round")
            command_id = str(row["command_id"])
            key = str(row["idempotency_key"])
            keywords = {
                str(item["keyword_id"]): {
                    "keyword_id": str(item["keyword_id"]),
                    "platform": str(item["platform"]),
                    "keyword": str(item["keyword"]),
                    "status": str(item["keyword_status"]),
                }
                for item in con.execute(
                    """SELECT i.keyword_id,k.platform,k.keyword,k.status AS keyword_status
                       FROM search_refresh_items i
                       JOIN keywords k ON k.keyword_id=i.keyword_id
                       WHERE i.refresh_job_id=?""",
                    (job_id,),
                )
            }

        # 每个关键词的 provider 调用都发生在事务/写锁之外；完成后只用短事务
        # 幂等写入内容、快照、指标和 checkpoint。已成功关键词不会因后续失败回滚。
        trigger_type = "scheduled" if source == "scheduler" else "manual"
        for item in self._batch_items(job_id):
            if item["status"] in {"succeeded", "failed", "blocked", "cancelled"}:
                continue
            keyword = keywords[item["keyword_id"]]
            finished = False
            while not finished:
                exhausted_error: dict[str, str] | None = None
                with writer_lock(self.settings.lock_path):
                    with connect(self.settings) as con:
                        with transaction(con):
                            current = con.execute(
                                """SELECT j.status AS job_status,j.cancel_requested,
                                          i.status AS item_status,i.attempt_count
                                   FROM search_refresh_jobs j
                                   JOIN search_refresh_items i ON i.refresh_job_id=j.refresh_job_id
                                   WHERE j.refresh_job_id=? AND i.refresh_item_id=?""",
                                (job_id, item["refresh_item_id"]),
                            ).fetchone()
                            if not current or current["job_status"] not in {"queued", "running"}:
                                finished = True
                                continue
                            if current["cancel_requested"]:
                                con.execute(
                                    """UPDATE search_refresh_items
                                       SET status='cancelled',current_phase='cancelled',finished_at=?
                                       WHERE refresh_item_id=? AND status IN ('queued','running')""",
                                    (_now(), item["refresh_item_id"]),
                                )
                                self._batch_runtime_projection(con, job_id=job_id, payload=self._job_payload(con, job_id))
                                finished = True
                                continue
                            if current["item_status"] in {"succeeded", "failed", "blocked", "cancelled"}:
                                finished = True
                                continue
                            if int(current["attempt_count"] or 0) >= self.max_attempts:
                                error = {
                                    "error": "刷新进程恢复时发现重试次数已经耗尽",
                                    "reason_code": "retry_exhausted_after_restart",
                                }
                                self._finish_item(
                                    con,
                                    job_id=job_id,
                                    item_id=item["refresh_item_id"],
                                    keyword=keyword,
                                    status="failed",
                                    result=None,
                                    error=error,
                                    trigger_type=trigger_type,
                                )
                                final_attempt = int(current["attempt_count"] or 0)
                                finished = True
                                exhausted_error = error
                            else:
                                con.execute(
                                    """UPDATE search_refresh_items
                                       SET status='running',current_phase='provider',
                                           attempt_count=attempt_count+1,started_at=?,error_json='{}'
                                       WHERE refresh_item_id=?""",
                                    (_now(), item["refresh_item_id"]),
                                )
                                attempt = int(current["attempt_count"] or 0) + 1
                                self._event(
                                    con,
                                    job_id=job_id,
                                    item_id=item["refresh_item_id"],
                                    event_type="attempt_started",
                                    status="running",
                                    details={"attempt": attempt, "max_attempts": self.max_attempts},
                                )
                                self._batch_runtime_projection(con, job_id=job_id, payload=self._job_payload(con, job_id))
                                final_attempt = attempt
                if finished:
                    if exhausted_error:
                        self._append_failure_log(
                            job_id=job_id,
                            item_id=item["refresh_item_id"],
                            keyword=keyword["keyword"],
                            keyword_id=keyword["keyword_id"],
                            reason_code=exhausted_error["reason_code"],
                            error=exhausted_error["error"],
                            attempt_count=final_attempt,
                            source=source,
                        )
                    break

                try:
                    # Provider 网络请求必须保持在锁外；客户端断开也不会中断已启动的调用。
                    result = self.provider.fetch(
                        keyword_id=keyword["keyword_id"],
                        keyword=keyword["keyword"],
                        incremental=incremental,
                        refresh_round=refresh_round,
                    )
                except Exception as exc:
                    reason_code = (
                        "provider_disabled"
                        if getattr(self.provider, "kind", "") == "disabled"
                        else str(getattr(exc, "reason_code", "provider_failed"))
                    )
                    error = {"error": str(exc), "reason_code": reason_code}
                    provider_payload = getattr(exc, "payload", None)
                    if isinstance(provider_payload, dict) and provider_payload.get("request_id"):
                        error["remote_request_id"] = str(provider_payload["request_id"])
                    retryable = (
                        reason_code in self.RETRYABLE_REASON_CODES
                        and final_attempt < self.max_attempts
                        and getattr(self.provider, "kind", "") != "disabled"
                    )
                    stale_attempt = False
                    with writer_lock(self.settings.lock_path):
                        with connect(self.settings) as con:
                            with transaction(con):
                                latest = con.execute(
                                    """SELECT status,attempt_count
                                       FROM search_refresh_items
                                       WHERE refresh_item_id=?""",
                                    (item["refresh_item_id"],),
                                ).fetchone()
                                if (
                                    not latest
                                    or latest["status"] != "running"
                                    or int(latest["attempt_count"] or 0) != final_attempt
                                ):
                                    stale_attempt = True
                                elif retryable:
                                    delay = self._retry_delay(final_attempt)
                                    con.execute(
                                        """UPDATE search_refresh_items
                                           SET status='queued',current_phase='retry_wait',error_json=?
                                           WHERE refresh_item_id=?""",
                                        (_json(error), item["refresh_item_id"]),
                                    )
                                    self._event(
                                        con,
                                        job_id=job_id,
                                        item_id=item["refresh_item_id"],
                                        event_type="retry_scheduled",
                                        status="queued",
                                        message=str(exc),
                                        details={
                                            **error,
                                            "attempt": final_attempt,
                                            "max_attempts": self.max_attempts,
                                            "delay_seconds": delay,
                                        },
                                    )
                                    self._batch_runtime_projection(con, job_id=job_id, payload=self._job_payload(con, job_id))
                                else:
                                    self._finish_item(
                                        con,
                                        job_id=job_id,
                                        item_id=item["refresh_item_id"],
                                        keyword=keyword,
                                        status="blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed",
                                        result=None,
                                        error=error,
                                        trigger_type=trigger_type,
                                    )
                    if stale_attempt:
                        finished = True
                        continue
                    if retryable:
                        time.sleep(self._retry_delay(final_attempt))
                        continue
                    self._append_failure_log(
                        job_id=job_id,
                        item_id=item["refresh_item_id"],
                        keyword=keyword["keyword"],
                        keyword_id=keyword["keyword_id"],
                        reason_code=reason_code,
                        error=str(exc),
                        attempt_count=final_attempt,
                        source=source,
                        status="blocked" if getattr(self.provider, "kind", "") == "disabled" else "failed",
                    )
                    finished = True
                    continue

                try:
                    with writer_lock(self.settings.lock_path):
                        with connect(self.settings) as con:
                            with transaction(con):
                                latest = con.execute(
                                    """SELECT status,attempt_count
                                       FROM search_refresh_items
                                       WHERE refresh_item_id=?""",
                                    (item["refresh_item_id"],),
                                ).fetchone()
                                if (
                                    not latest
                                    or latest["status"] != "running"
                                    or int(latest["attempt_count"] or 0) != final_attempt
                                ):
                                    finished = True
                                    continue
                                self._finish_item(
                                    con,
                                    job_id=job_id,
                                    item_id=item["refresh_item_id"],
                                    keyword=keyword,
                                    status="succeeded",
                                    result=result,
                                    error=None,
                                    trigger_type=trigger_type,
                                )
                except Exception as exc:
                    error = {
                        "error": str(exc),
                        "reason_code": "persistence_failed",
                    }
                    with writer_lock(self.settings.lock_path):
                        with connect(self.settings) as con:
                            with transaction(con):
                                self._finish_item(
                                    con,
                                    job_id=job_id,
                                    item_id=item["refresh_item_id"],
                                    keyword=keyword,
                                    status="failed",
                                    result=None,
                                    error=error,
                                    trigger_type=trigger_type,
                                )
                    self._append_failure_log(
                        job_id=job_id,
                        item_id=item["refresh_item_id"],
                        keyword=keyword["keyword"],
                        keyword_id=keyword["keyword_id"],
                        reason_code="persistence_failed",
                        error=str(exc),
                        attempt_count=final_attempt,
                        source=source,
                    )
                finished = True

        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    output = self._job_payload(con, job_id)
                    cancel_requested = bool(
                        con.execute(
                            "SELECT cancel_requested FROM search_refresh_jobs WHERE refresh_job_id=?",
                            (job_id,),
                        ).fetchone()["cancel_requested"]
                    )
                    if cancel_requested:
                        final = "cancelled"
                    elif output["blocked_count"] == output["total"] and output["success_count"] == 0:
                        final = "blocked"
                    elif output["success_count"] == output["total"]:
                        final = "succeeded"
                    elif output["success_count"] or output["failed_count"]:
                        final = "partial_failed"
                    else:
                        final = "failed"
                    self._complete_job(con, job_id=job_id, command_id=command_id, key=key, status=final, output=output)
                    output = self._job_payload(con, job_id)
                    output["hub_status"] = final
                    output["status"] = _frontend_status(final)
                    con.execute("UPDATE command_runs SET output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                    self._batch_runtime_projection(con, job_id=job_id, payload=output)
                    receipt = self._receipt(
                        con,
                        command_id=command_id,
                        key=key,
                        legacy_status="not_attempted",
                        hub_status=final,
                        reconcile_status="pending" if final == "succeeded" else "blocked",
                        details={"batch_id": job_id, "source": source},
                    )
                    self._audit(
                        con,
                        action="wechat.refresh_all",
                        subject_type="refresh_job",
                        subject_id=job_id,
                        outcome="succeeded" if final == "succeeded" else "blocked" if final == "blocked" else "failed",
                        details={**receipt, "source": source, "status": final},
                        request_id=request_id,
                    )
                    output["dual_write_receipt"] = receipt
                    return output

    def _batch_items(self, job_id: str) -> list[dict[str, Any]]:
        with connect(self.settings, readonly=True) as con:
            return [
                dict(row)
                for row in con.execute(
                    """SELECT refresh_item_id,keyword_id,status,attempt_count
                       FROM search_refresh_items
                       WHERE refresh_job_id=? ORDER BY ordinal""",
                    (job_id,),
                ).fetchall()
            ]

    def recover_active_batches(self) -> list[str]:
        interrupted: list[dict[str, Any]] = []
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    jobs = con.execute(
                        """SELECT refresh_job_id,trigger_source
                           FROM search_refresh_jobs
                           WHERE system_key=? AND platform=?
                             AND command_id IS NOT NULL
                             AND status IN ('queued','running')
                             AND cancel_requested=0
                           ORDER BY created_at""",
                        (self.MODULE, self.PLATFORM),
                    ).fetchall()
                    for job in jobs:
                        running_items = con.execute(
                            """SELECT i.refresh_item_id,i.keyword_id,i.attempt_count,k.keyword
                               FROM search_refresh_items i
                               JOIN keywords k ON k.keyword_id=i.keyword_id
                               WHERE i.refresh_job_id=? AND i.status='running'""",
                            (job["refresh_job_id"],),
                        ).fetchall()
                        for item in running_items:
                            error = {
                                "error": "Hub 进程中断后自动恢复；该次未完成尝试重新排队",
                                "reason_code": "process_restarted",
                            }
                            con.execute(
                                """UPDATE search_refresh_items
                                   SET status='queued',current_phase='recovered',error_json=?
                                   WHERE refresh_item_id=?""",
                                (_json(error), item["refresh_item_id"]),
                            )
                            self._event(
                                con,
                                job_id=job["refresh_job_id"],
                                item_id=item["refresh_item_id"],
                                event_type="process_restarted",
                                status="queued",
                                message=error["error"],
                                details={
                                    **error,
                                    "attempt_count": int(item["attempt_count"] or 0),
                                },
                            )
                            interrupted.append({
                                "job_id": str(job["refresh_job_id"]),
                                "item_id": str(item["refresh_item_id"]),
                                "keyword_id": str(item["keyword_id"]),
                                "keyword": str(item["keyword"]),
                                "attempt_count": int(item["attempt_count"] or 0),
                                "source": str(job["trigger_source"] or "recovery"),
                            })
                        con.execute(
                            "UPDATE search_refresh_jobs SET status='running',updated_at=? WHERE refresh_job_id=?",
                            (_now(), job["refresh_job_id"]),
                        )
                        self._batch_runtime_projection(
                            con,
                            job_id=job["refresh_job_id"],
                            payload=self._job_payload(con, job["refresh_job_id"]),
                        )
        for item in interrupted:
            self._append_failure_log(
                job_id=item["job_id"],
                item_id=item["item_id"],
                keyword=item["keyword"],
                keyword_id=item["keyword_id"],
                reason_code="process_restarted",
                error="Hub 进程中断后自动恢复；该次未完成尝试重新排队",
                attempt_count=item["attempt_count"],
                source=item["source"],
                status="requeued",
            )
        return [str(job["refresh_job_id"]) for job in jobs]

    def recover_stale_batches(self, *, stale_after_seconds: float | None = None) -> list[str]:
        threshold = max(
            60.0,
            float(
                stale_after_seconds
                if stale_after_seconds is not None
                else os.getenv("HUB_WECHAT_REFRESH_STALE_SECONDS", "1800")
            ),
        )
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=threshold)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        stale_items: list[dict[str, Any]] = []
        job_ids: list[str] = []
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    rows = con.execute(
                        """SELECT j.refresh_job_id,j.trigger_source,
                                  i.refresh_item_id,i.keyword_id,i.attempt_count,k.keyword
                           FROM search_refresh_jobs j
                           JOIN search_refresh_items i
                             ON i.refresh_job_id=j.refresh_job_id
                           JOIN keywords k ON k.keyword_id=i.keyword_id
                           WHERE j.system_key=? AND j.platform=?
                             AND j.command_id IS NOT NULL
                             AND j.status='running' AND j.cancel_requested=0
                             AND i.status='running'
                             AND i.started_at IS NOT NULL AND i.started_at<=?
                           ORDER BY j.created_at,i.ordinal""",
                        (self.MODULE, self.PLATFORM, cutoff),
                    ).fetchall()
                    for row in rows:
                        error = {
                            "error": f"关键词超过 {int(threshold)} 秒没有结束，已由看门狗重新排队",
                            "reason_code": "keyword_stale_timeout",
                        }
                        con.execute(
                            """UPDATE search_refresh_items
                               SET status='queued',current_phase='watchdog_recovered',error_json=?
                               WHERE refresh_item_id=? AND status='running'""",
                            (_json(error), row["refresh_item_id"]),
                        )
                        self._event(
                            con,
                            job_id=row["refresh_job_id"],
                            item_id=row["refresh_item_id"],
                            event_type="keyword_stale_timeout",
                            status="queued",
                            message=error["error"],
                            details={
                                **error,
                                "attempt_count": int(row["attempt_count"] or 0),
                                "stale_after_seconds": threshold,
                            },
                        )
                        stale_items.append({
                            "job_id": str(row["refresh_job_id"]),
                            "item_id": str(row["refresh_item_id"]),
                            "keyword_id": str(row["keyword_id"]),
                            "keyword": str(row["keyword"]),
                            "attempt_count": int(row["attempt_count"] or 0),
                            "source": str(row["trigger_source"] or "watchdog"),
                            "error": error["error"],
                        })
                        if row["refresh_job_id"] not in job_ids:
                            job_ids.append(str(row["refresh_job_id"]))
                    for job_id in job_ids:
                        con.execute(
                            "UPDATE search_refresh_jobs SET updated_at=? WHERE refresh_job_id=?",
                            (_now(), job_id),
                        )
                        self._batch_runtime_projection(
                            con,
                            job_id=job_id,
                            payload=self._job_payload(con, job_id),
                        )
        for item in stale_items:
            self._append_failure_log(
                job_id=item["job_id"],
                item_id=item["item_id"],
                keyword=item["keyword"],
                keyword_id=item["keyword_id"],
                reason_code="keyword_stale_timeout",
                error=item["error"],
                attempt_count=item["attempt_count"],
                source=item["source"],
                status="requeued",
            )
        return job_ids

    def cancel_batch(self, *, batch_id: str, key: str, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT * FROM search_refresh_jobs WHERE refresh_job_id=? AND system_key=? AND platform=?", (batch_id, self.MODULE, self.PLATFORM)).fetchone()
                    if not row:
                        raise NotFoundError("微信刷新批次", batch_id)
                    payload = {"batch_id": batch_id}
                    old = self._existing(con, key, _hash(payload))
                    if old:
                        return old
                    command_id, cancel_job = self._create_command(con, key=key, command_type="wechat.refresh_all.cancel", input_payload=payload)
                    if row["status"] in {"succeeded", "failed", "partial_failed", "cancelled", "blocked"}:
                        output = {"status": _frontend_status(row["status"]), "hub_status": row["status"], "message": "批次已结束", "batch": self._job_payload(con, batch_id)}
                        con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), _now(), command_id))
                        self._batch_runtime_projection(con, job_id=batch_id, payload=output["batch"])
                        receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"batch_id": batch_id, "finished": True})
                        self._audit(con, action="wechat.refresh_all.cancel", subject_type="refresh_job", subject_id=batch_id, outcome="succeeded", details=receipt, request_id=request_id)
                        output["dual_write_receipt"] = receipt
                        return output
                    now = _now()
                    cancel_reason = str(payload.get("cancel_reason") or "user_requested").strip()[:500]
                    con.execute("UPDATE search_refresh_jobs SET cancel_requested=1,cancel_requested_at=?,cancel_reason=?,updated_at=? WHERE refresh_job_id=?", (now, cancel_reason, now, batch_id))
                    con.execute("UPDATE search_refresh_items SET status='cancelled',current_phase='cancelled',finished_at=? WHERE refresh_job_id=? AND status='queued'", (now, batch_id))
                    output = {"status": "running", "hub_status": "cancelling", "message": "取消信号已发送，当前关键词跑完后停止", "batch": self._job_payload(con, batch_id)}
                    con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), now, command_id))
                    self._batch_runtime_projection(con, job_id=batch_id, payload=output["batch"])
                    receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"batch_id": batch_id})
                    self._audit(con, action="wechat.refresh_all.cancel", subject_type="refresh_job", subject_id=batch_id, outcome="succeeded", details=receipt, request_id=request_id)
                    output["dual_write_receipt"] = receipt
                    return output

    def scheduler_config(self, *, payload: dict[str, Any], key: str, request_id: str | None = None) -> dict[str, Any]:
        if not key.strip():
            raise ValidationAppError("必须提供 Idempotency-Key。")
        allowed = {"enabled", "interval_hours", "daily_keyword_budget", "max_keywords_per_batch"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationAppError(f"不支持的 scheduler 字段：{sorted(unknown)[0]}")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = con.execute("SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
                    current = json.loads(row["payload_json"] or "{}") if row else {}
                    merged = {**current, **payload}
                    if "enabled" in payload:
                        merged["enabled"] = bool(payload["enabled"])
                    elif row is not None:
                        merged["enabled"] = bool(con.execute(
                            "SELECT enabled FROM search_scheduler_state WHERE system_key=? AND platform=?",
                            (self.MODULE, self.PLATFORM),
                        ).fetchone()["enabled"])
                    else:
                        merged["enabled"] = False
                    try:
                        interval = float(merged.get("interval_hours", 3.0))
                        budget = int(merged.get("daily_keyword_budget", 1550))
                        maximum = int(merged.get("max_keywords_per_batch", 250))
                    except (TypeError, ValueError) as exc:
                        raise ValidationAppError("scheduler 数值字段格式无效。") from exc
                    if not 0.1 <= interval <= 168: raise ValidationAppError("interval_hours 必须在 0.1–168 之间。")
                    if not 1 <= budget <= 100000: raise ValidationAppError("daily_keyword_budget 超出范围。")
                    if not 1 <= maximum <= 10000: raise ValidationAppError("max_keywords_per_batch 超出范围。")
                    merged.update({"enabled": bool(merged.get("enabled", False)), "interval_hours": interval, "daily_keyword_budget": budget, "max_keywords_per_batch": maximum})
                    old = self._existing(con, key, _hash(merged))
                    if old: return old
                    command_id, _ = self._create_command(con, key=key, command_type="wechat.scheduler.config", input_payload=merged)
                    now = _now()
                    next_run = (
                        (datetime.now(UTC) + timedelta(hours=interval)).isoformat(timespec="seconds").replace("+00:00", "Z")
                        if merged["enabled"] else None
                    )
                    merged.update({
                        "next_run_at": next_run,
                        "last_error": current.get("last_error"),
                        "provider_kind": getattr(self.provider, "kind", "disabled"),
                        "base_url": self._hub_base_url(),
                        "last_triggered_at": current.get("last_triggered_at"),
                        "last_result": current.get("last_result"),
                        "last_plan": current.get("last_plan"),
                        "last_discovery": current.get("last_discovery"),
                        "budget": current.get("budget", {}),
                        "budget_breakdown": current.get("budget_breakdown", {}),
                    })
                    con.execute("""INSERT INTO search_scheduler_state(system_key,platform,enabled,next_run_at,updated_at,payload_json)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(system_key,platform) DO UPDATE SET enabled=excluded.enabled,next_run_at=excluded.next_run_at,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                        (self.MODULE, self.PLATFORM, 1 if merged["enabled"] else 0, next_run, now, _json(merged)))
                    output = {"status": "succeeded", **self.scheduler_status_from_connection(con), "command_id": command_id}
                    con.execute("UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?", (_json(output), now, command_id))
                    scheduler_hash = _hash(output)
                    scheduler_manifest = self._manifest(con, source_ref="provider:scheduler", source_hash=scheduler_hash, now=now)
                    self._runtime_projection(
                        con,
                        subject_id="scheduler",
                        payload={"runtime_subtype": "scheduler", **self.scheduler_status_from_connection(con)},
                        source_hash=scheduler_hash,
                        manifest_id=scheduler_manifest,
                        source_ref="provider:scheduler",
                    )
                    receipt = self._receipt(con, command_id=command_id, key=key, legacy_status="not_attempted", hub_status="succeeded", reconcile_status="pending", details={"config": merged})
                    self._audit(con, action="wechat.scheduler.config", subject_type="scheduler", subject_id="scheduler", outcome="succeeded", details=receipt, request_id=request_id)
                    output["dual_write_receipt"] = receipt
                    return output

    def scheduler_status_from_connection(self, con) -> dict[str, Any]:
        row = con.execute(
            "SELECT * FROM search_scheduler_state WHERE system_key=? AND platform=?",
            (self.MODULE, self.PLATFORM),
        ).fetchone()
        if not row:
            return {
                "enabled": False, "is_active": False, "interval_hours": 3.0,
                "base_url": self._hub_base_url(),
                "next_run_at": None, "last_triggered_at": None, "last_result": None,
                "daily_keyword_budget": 1550, "max_keywords_per_batch": 250,
                "budget": {}, "budget_breakdown": {}, "last_plan": None, "last_discovery": None,
            }
        payload = json.loads(row["payload_json"] or "{}")
        return {
            "enabled": bool(row["enabled"]),
            "is_active": bool(row["active_refresh_job_id"]),
            "interval_hours": payload.get("interval_hours", 3.0),
            "base_url": payload.get("base_url", self._hub_base_url()),
            "provider_kind": payload.get("provider_kind", getattr(self.provider, "kind", "disabled")),
            "daily_keyword_budget": payload.get("daily_keyword_budget", 1550),
            "max_keywords_per_batch": payload.get("max_keywords_per_batch", 250),
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "last_triggered_at": payload.get("last_triggered_at"),
            "last_result": payload.get("last_result"),
            "budget": payload.get("budget", {}),
            "budget_breakdown": payload.get("budget_breakdown", {}),
            "last_plan": payload.get("last_plan"),
            "last_discovery": payload.get("last_discovery"),
            "last_error": payload.get("last_error"),
        }

    def _scheduler_plan(self, con, *, config: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_budget = max(1, int(config.get("daily_keyword_budget", 1550)))
        max_batch = max(1, int(config.get("max_keywords_per_batch", 250)))
        used = int(
            con.execute(
                """SELECT COALESCE(SUM(requested_count),0)
                   FROM search_refresh_jobs
                   WHERE system_key=? AND platform=? AND trigger_type='scheduled'
                     AND started_at>=?""",
                (self.MODULE, self.PLATFORM, day_start.isoformat().replace("+00:00", "Z")),
            ).fetchone()[0]
            or 0
        )
        remaining = max(0, daily_budget - used)
        capacity = min(max_batch, remaining)
        rows = con.execute(
            """SELECT k.keyword_id,k.keyword,
                      s.refresh_strategy,s.refresh_interval_minutes,
                      s.batch_default_selected,s.keyword_order,s.payload_json,
                      (SELECT MAX(ss.captured_at)
                         FROM search_snapshots ss
                        WHERE ss.keyword_id=k.keyword_id AND ss.platform=?) AS latest_snapshot_at
                 FROM keywords k
                 LEFT JOIN search_keyword_settings s
                   ON s.keyword_id=k.keyword_id
                  AND s.system_key=? AND s.platform=?
                WHERE k.platform=? AND k.status='active'
                  AND COALESCE(s.refresh_strategy,'scheduled')='scheduled'
                  AND COALESCE(s.batch_default_selected,1)=1""",
            (self.PLATFORM, self.MODULE, self.PLATFORM, self.PLATFORM),
        ).fetchall()
        due: list[tuple[datetime, int, str, str]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            frequency_days = max(
                1,
                int(
                    payload.get("refresh_frequency_days")
                    or row["refresh_interval_minutes"]
                    or 1
                ),
            )
            effective_hours = (
                3.0
                if payload.get("lifecycle_stage") == "observing"
                and payload.get("refresh_frequency_source", "auto") != "manual"
                else float(frequency_days * 24)
            )
            last_refreshes = [
                value
                for value in (
                    _parse_time(payload.get("last_refresh_at")),
                    _parse_time(row["latest_snapshot_at"]),
                )
                if value is not None
            ]
            due_at = (
                max(last_refreshes) + timedelta(hours=effective_hours)
                if last_refreshes
                else datetime.min.replace(tzinfo=UTC)
            )
            if due_at <= now:
                due.append(
                    (
                        due_at,
                        int(row["keyword_order"] or 999999),
                        str(row["keyword"]),
                        str(row["keyword_id"]),
                    )
                )
        due.sort()
        selected = [item[3] for item in due[:capacity]]
        return {
            "keyword_ids": selected,
            "due_count": len(due),
            "selected_count": len(selected),
            "deferred_due_count": max(0, len(due) - len(selected)),
            "budget": {
                "date": day_start.date().isoformat(),
                "daily_keyword_budget": daily_budget,
                "used_count": used,
                "remaining_count": remaining,
            },
            "batch_capacity": {
                "configured_limit": max_batch,
                "effective_limit": capacity,
            },
        }

    def scheduler_trigger(self, *, key: str, request_id: str | None = None) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            state = con.execute("SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
            config = json.loads(state["payload_json"] or "{}") if state else {}
            plan = self._scheduler_plan(con, config=config)
            ids = list(plan["keyword_ids"])
        if not ids:
            with writer_lock(self.settings.lock_path):
                with connect(self.settings) as con:
                    with transaction(con):
                        state_row = con.execute(
                            "SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?",
                            (self.MODULE, self.PLATFORM),
                        ).fetchone()
                        state_payload = json.loads(state_row["payload_json"] or "{}") if state_row else {}
                        now = datetime.now(UTC)
                        interval = float(state_payload.get("interval_hours", 3.0))
                        state_payload.update(
                            {
                                "last_triggered_at": _now(),
                                "last_result": "skipped:no_keywords",
                                "last_error": None,
                                "last_plan": plan,
                                "budget": plan["budget"],
                            }
                        )
                        next_run = (now + timedelta(hours=interval)).isoformat(
                            timespec="seconds"
                        ).replace("+00:00", "Z")
                        con.execute(
                            """UPDATE search_scheduler_state
                               SET next_run_at=?,payload_json=?,updated_at=?
                               WHERE system_key=? AND platform=?""",
                            (
                                next_run,
                                _json(state_payload),
                                _now(),
                                self.MODULE,
                                self.PLATFORM,
                            ),
                        )
                        status = self.scheduler_status_from_connection(con)
            return {
                **status,
                "source": "scheduler",
                "trigger_status": "skipped",
                "blocked": False,
                "batch_id": None,
                "batch_status": "skipped",
                "batch": None,
            }
        batch = self.refresh_batch(keyword_ids=ids, key=key, source="scheduler", request_id=request_id)
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    state_row = con.execute(
                        "SELECT payload_json FROM search_scheduler_state WHERE system_key=? AND platform=?",
                        (self.MODULE, self.PLATFORM),
                    ).fetchone()
                    state_payload = json.loads(state_row["payload_json"] or "{}") if state_row else {}
                    interval = float(state_payload.get("interval_hours", 3.0))
                    next_run = (
                        datetime.now(UTC) + timedelta(hours=interval)
                    ).isoformat(timespec="seconds").replace("+00:00", "Z")
                    state_payload.update({
                        "last_triggered_at": _now(),
                        "last_result": f"done:{batch.get('status')}",
                        "last_error": None if batch.get("hub_status") == "succeeded" else batch.get("error"),
                        "last_plan": plan,
                        "budget": plan["budget"],
                        "last_discovery": state_payload.get("last_discovery"),
                    })
                    now = _now()
                    con.execute(
                        "UPDATE search_scheduler_state SET next_run_at=?,payload_json=?,updated_at=? WHERE system_key=? AND platform=?",
                        (next_run, _json(state_payload), now, self.MODULE, self.PLATFORM),
                    )
                    status = self.scheduler_status_from_connection(con)
                    status_hash = _hash(status)
                    status_manifest = self._manifest(con, source_ref="provider:scheduler", source_hash=status_hash, now=now)
                    self._runtime_projection(
                        con,
                        subject_id="scheduler",
                        payload={"runtime_subtype": "scheduler", **status},
                        source_hash=status_hash,
                        manifest_id=status_manifest,
                        source_ref="provider:scheduler",
                    )
                    self._audit(
                        con,
                        action="wechat.scheduler.trigger",
                        subject_type="scheduler",
                        subject_id="scheduler",
                        outcome="succeeded" if batch.get("hub_status") == "succeeded" else "blocked" if batch.get("hub_status") == "blocked" else "failed",
                        details={"batch_id": batch.get("batch_id"), "status": batch.get("status")},
                        request_id=request_id,
                    )
        return {
            **status,
            "source": "scheduler",
            "trigger_status": "blocked" if batch.get("hub_status") == "blocked" else "triggered",
            "blocked": batch.get("hub_status") == "blocked",
            "batch_id": batch.get("batch_id"),
            "batch_status": batch.get("status"),
            "batch": batch,
        }

    def runtime(self, job_id: str, *, batch: bool, semantic: bool = False) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """SELECT 1
                   FROM search_refresh_jobs
                   WHERE refresh_job_id=? AND system_key=? AND platform=?""",
                (job_id, self.MODULE, self.PLATFORM),
            ).fetchone()
            if not row:
                raise NotFoundError("微信刷新任务" if not batch else "微信刷新批次", job_id)
            return self._job_payload(con, job_id, semantic=semantic)

    def history(self) -> list[dict[str, Any]]:
        with connect(self.settings, readonly=True) as con:
            rows = con.execute("SELECT refresh_job_id FROM search_refresh_jobs WHERE system_key=? AND platform=? ORDER BY created_at DESC", (self.MODULE, self.PLATFORM)).fetchall()
            return [self._job_payload(con, row["refresh_job_id"]) for row in rows]

    def scheduler_status(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT * FROM search_scheduler_state WHERE system_key=? AND platform=?", (self.MODULE, self.PLATFORM)).fetchone()
            if not row:
                return {"enabled": False, "is_active": False, "interval_hours": 3.0, "daily_keyword_budget": 1550, "max_keywords_per_batch": 250, "next_run_at": None, "last_run_at": None}
            payload = json.loads(row["payload_json"] or "{}")
            return {"enabled": bool(row["enabled"]), "is_active": bool(row["active_refresh_job_id"]), "interval_hours": payload.get("interval_hours", 3.0), "daily_keyword_budget": payload.get("daily_keyword_budget", 1550), "max_keywords_per_batch": payload.get("max_keywords_per_batch", 250), "next_run_at": row["next_run_at"], "last_run_at": row["last_run_at"], "last_error": payload.get("last_error"), "last_plan": payload.get("last_plan"), "last_discovery": payload.get("last_discovery")}
