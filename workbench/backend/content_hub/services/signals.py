"""跨系统平台回填与可复算信号。

本模块只消费 Hub 已存在的事实表，不访问旧系统、不创建评论、不创建生产任务。
回填和重算均使用稳定指纹，重复执行不会新增重复事实。
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..services.audit import AuditService
from ..validation.timestamps import utc_now_iso

SIGNAL_MODEL = "v3.3.0"
POST_REFRESH_LINKAGE_MAX_BYTES = 64 << 10
_SYSTEM_PLATFORM_ALIASES = {
    "微信搜索结果": "wechat-search",
    "微信搜一搜": "wechat-search",
    "公众号": "wechat-mp",
    "小红书": "xiaohongshu",
}


def _bounded_step_result(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, small summary for runtime/audit receipts."""
    if depth >= 3:
        return {"truncated": True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:256] + "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                result["truncated"] = True
                break
            key = str(key)
            # Counts, statuses, ids and timestamps are useful diagnostics;
            # arbitrary nested payloads are not part of the linkage contract.
            if isinstance(item, (dict, list, tuple)):
                result[key] = _bounded_step_result(item, depth=depth + 1)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[key] = _bounded_step_result(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_step_result(item, depth=depth + 1) for item in value[:32]]
    return str(value)[:256]


def _summarize_optional_result(module: str, result: Any) -> dict[str, Any]:
    """Never expose a provider/projection result in a batch receipt."""
    if "wechat_live_projection" in module and isinstance(result, dict):
        counts: dict[str, int] = {}
        generated_at: set[str] = set()
        keys: list[str] = []
        for key, payload in result.items():
            key = str(key)
            keys.append(key)
            kind = key.split(":", 1)[0]
            counts[kind] = counts.get(kind, 0) + 1
            if isinstance(payload, dict) and payload.get("generated_at"):
                generated_at.add(str(payload["generated_at"]))
        return {
            "projection_keys": sorted(keys)[:256],
            "projection_count": len(keys),
            "projection_counts": counts,
            "generated_at": sorted(generated_at)[-1] if generated_at else None,
        }
    return _bounded_step_result(result)


def _summarize_linkage_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_count": len(context.get("snapshot_ids") or []),
        "content_count": len(context.get("content_ids") or []),
        "observed_at": context.get("observed_at"),
        "source_ref": context.get("source_ref"),
    }


def _bound_linkage_receipt(value: dict[str, Any]) -> dict[str, Any]:
    """Keep the HTTP/runtime/audit linkage contract below a hard byte bound."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) <= POST_REFRESH_LINKAGE_MAX_BYTES:
        return value
    return {
        "status": value.get("status"),
        "context": value.get("context"),
        "steps": {
            name: {
                key: step.get(key)
                for key in ("status", "module", "projection_keys", "projection_count",
                            "projection_counts", "generated_at", "error")
                if isinstance(step, dict) and key in step
            }
            for name, step in (value.get("steps") or {}).items()
        },
        "failures": value.get("failures") or {},
        "truncated": True,
    }


def _invoke_optional_service(
    module_names: tuple[str, ...],
    *,
    connection,
    settings,
    job_id: str,
    call_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an optional post-refresh service without making it a hard dependency.

    Newer deployments may provide metric backfill/live projection modules while
    older databases do not.  Missing modules are a deliberate no-op; failures
    from an installed service are propagated so the refresh runtime can record
    them as real failures.
    """
    module = None
    for name in module_names:
        try:
            module = importlib.import_module(name)
            break
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    if module is None:
        return {"status": "skipped", "reason": "module_missing"}

    candidates = (
        "backfill",
        "backfill_metrics",
        "backfill_wechat_article_metrics",
        "backfill_contents",
        "run",
        "project_live",
        "refresh",
        "write",
        "rebuild",
    )
    callable_obj = next(
        (getattr(module, name) for name in candidates if callable(getattr(module, name, None))),
        None,
    )
    if callable_obj is None:
        for name in ("WechatArticleMetricsService", "MetricsBackfillService", "LiveProjectionService", "ProjectionService"):
            cls = getattr(module, name, None)
            if cls is not None:
                cls_params = inspect.signature(cls).parameters
                if "settings" in cls_params:
                    instance = cls(settings)
                else:
                    instance = cls(connection)
                callable_obj = next(
                    (getattr(instance, method) for method in candidates if callable(getattr(instance, method, None))),
                    None,
                )
                if callable_obj is not None:
                    break
    if callable_obj is None:
        return {"status": "skipped", "reason": "entrypoint_missing", "module": module.__name__}

    parameters = inspect.signature(callable_obj).parameters
    kwargs = {
        key: value
        for key, value in {
            "connection": connection,
            "conn": connection,
            "settings": settings,
            "job_id": job_id,
            "refresh_job_id": job_id,
        }.items()
        if key in parameters
    }
    if call_kwargs:
        kwargs.update({key: value for key, value in call_kwargs.items() if key in parameters})
    result = callable_obj(**kwargs)
    return {
        "status": "succeeded",
        "module": module.__name__,
        "result": _summarize_optional_result(module.__name__, result),
    }


def _refresh_snapshot_context(connection, job_id: str) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT DISTINCT s.snapshot_id,s.captured_at,s.source_ref,h.content_id
           FROM search_refresh_items i
           JOIN search_snapshots s ON s.snapshot_id=i.snapshot_id
           LEFT JOIN search_hits h ON h.snapshot_id=s.snapshot_id
          WHERE i.refresh_job_id=? AND i.status='succeeded'
          ORDER BY s.captured_at,s.snapshot_id""",
        (job_id,),
    ).fetchall()
    snapshot_ids = [str(row["snapshot_id"]) for row in rows]
    content_ids = sorted({str(row["content_id"]) for row in rows if row["content_id"]})
    captured_ats = [str(row["captured_at"]) for row in rows if row["captured_at"]]
    source_refs = sorted({str(row["source_ref"]) for row in rows if row["source_ref"]})
    return {
        "snapshot_ids": snapshot_ids,
        "content_ids": content_ids,
        "observed_at": max(captured_ats) if captured_ats else None,
        "source_ref": f"wechat-refresh:{job_id}:snapshots:{','.join(snapshot_ids)}",
        "snapshot_source_refs": source_refs,
    }


def run_post_refresh_linkage(connection, *, settings, job_id: str) -> dict[str, Any]:
    """Run optional backfills/projections, then recompute all signals.

    The caller owns the write transaction.  The returned structure is suitable
    for persisting into job runtime and audit details.
    """
    steps: dict[str, Any] = {}
    failures: dict[str, str] = {}
    context = _refresh_snapshot_context(connection, job_id)
    try:
        metrics_module = importlib.import_module("content_hub.services.wechat_article_metrics")
        metrics_kwargs = {
            "observed_at": context["observed_at"],
            "source_ref": context["source_ref"],
            "content_ids": context["content_ids"],
        }
        steps["metrics_backfill"] = _invoke_optional_service(
            ("content_hub.services.wechat_article_metrics",),
            connection=connection,
            settings=settings,
            job_id=job_id,
            call_kwargs=metrics_kwargs,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "content_hub.services.wechat_article_metrics":
            failures["metrics_backfill"] = f"{type(exc).__name__}: {exc}"
            steps["metrics_backfill"] = {"status": "failed", "error": failures["metrics_backfill"]}
        else:
            steps["metrics_backfill"] = {"status": "skipped", "reason": "module_missing"}
    except Exception as exc:
        failures["metrics_backfill"] = f"{type(exc).__name__}: {exc}"
        steps["metrics_backfill"] = {"status": "failed", "error": failures["metrics_backfill"]}

    optional_metrics_module = "content_hub.services.wechat_article_metrics" in steps["metrics_backfill"].get("module", "")
    for name, modules in (
        ("metrics_backfill", (
            "content_hub.services.metrics_backfill",
            "content_hub.services.metric_backfill",
        )),
        ("live_projection", (
            "content_hub.services.live_projection",
            "content_hub.services.projection",
            "content_hub.services.wechat_live_projection",
        )),
    ):
        if name == "metrics_backfill" and optional_metrics_module:
            continue
        try:
            steps[name] = _invoke_optional_service(modules, connection=connection, settings=settings, job_id=job_id)
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            steps[name] = {"status": "failed", "error": failures[name]}
    try:
        steps["signals"] = {
            "status": "succeeded",
            "result": _bounded_step_result(
                SignalsService(connection).recompute_all(signal_date=None)
            ),
        }
    except Exception as exc:
        failures["signals"] = f"{type(exc).__name__}: {exc}"
        steps["signals"] = {"status": "failed", "error": failures["signals"]}
    linkage = {
        "status": "succeeded" if not failures else "failed",
        "context": _summarize_linkage_context(context),
        "steps": steps,
        "failures": failures,
    }
    return _bound_linkage_receipt(linkage)
def load_platform_rules(path: Path | None) -> dict[str, str]:
    """读取 GEO 已有的平台别名规则；文件不存在时不推断新别名。"""
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases: dict[str, str] = {}
    for canonical, metadata in (payload.get("platforms") or {}).items():
        canonical = str(canonical).strip()
        if not canonical:
            continue
        aliases[canonical.casefold()] = canonical
        for alias in (metadata or {}).get("aliases") or []:
            value = str(alias).strip()
            if value:
                aliases[value.casefold()] = canonical
    return aliases


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date(value: str | None) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _fingerprint(*parts: Any) -> str:
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class SignalsService:
    """平台回填、信号重算和信号查询。连接的事务由调用方负责。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        platform_rules: dict[str, str] | None = None,
    ):
        self._conn = connection
        self._platform_rules = {
            str(key).casefold(): str(value).strip()
            for key, value in (platform_rules or {}).items()
            if str(key).strip() and str(value).strip()
        }

    # ── 平台回填 ────────────────────────────────────────────

    def backfill_platforms(self) -> dict[str, Any]:
        """从已有身份、快照、指标、内容和 GEO 来源安全回填 platforms。"""
        observations: dict[str, dict[str, Any]] = {}

        def observe(raw: Any, source: str) -> None:
            value = str(raw or "").strip()
            if not value:
                return
            canonical = self._canonical_platform(value)
            if not canonical:
                return
            item = observations.setdefault(
                canonical.casefold(),
                {
                    "canonical_name": canonical,
                    "aliases": set(),
                    "sources": Counter(),
                    "raw_values": Counter(),
                },
            )
            item["sources"][source] += 1
            item["raw_values"][value] += 1
            if value.casefold() != canonical.casefold():
                item["aliases"].add(value)

        for row in self._conn.execute("SELECT platform FROM creators WHERE platform IS NOT NULL"):
            observe(row["platform"], "creators.platform")
        for row in self._conn.execute("SELECT platform FROM keywords WHERE platform IS NOT NULL"):
            observe(row["platform"], "keywords.platform")
        for row in self._conn.execute("SELECT platform FROM search_snapshots WHERE platform IS NOT NULL"):
            observe(row["platform"], "search_snapshots.platform")
        for row in self._conn.execute("SELECT platform FROM metric_definitions WHERE platform IS NOT NULL"):
            observe(row["platform"], "metric_definitions.platform")

        # contents.domain 只有在已有 GEO 规则或明确域名/系统名时才进入，避免把分类名伪装成平台。
        for row in self._conn.execute("SELECT domain FROM contents WHERE domain IS NOT NULL"):
            value = str(row["domain"]).strip()
            if value.casefold() in self._platform_rules or value.casefold() in _SYSTEM_PLATFORM_ALIASES:
                observe(value, "contents.domain")
            elif "." in value and " " not in value:
                observe(value, "contents.domain")

        for row in self._conn.execute("SELECT payload_json FROM geo_source_relations"):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            source_fact = payload.get("source_fact") or {}
            for key in ("canonical_platform", "platform", "raw_platform"):
                observe(source_fact.get(key), f"geo_source_relations.source_fact.{key}")

        counts = Counter()
        existing_rows = self._conn.execute("SELECT * FROM platforms").fetchall()
        duplicate_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in existing_rows:
            duplicate_groups[str(row["canonical_name"]).casefold()].append(row)
        for duplicate_rows in duplicate_groups.values():
            if len(duplicate_rows) < 2:
                continue
            keeper = sorted(
                duplicate_rows,
                key=lambda row: (str(row["canonical_name"]).casefold(), str(row["canonical_name"])),
            )[0]
            aliases = set()
            payload: dict[str, Any] = {}
            source_counts: Counter[str] = Counter()
            raw_counts: Counter[str] = Counter()
            for row in duplicate_rows:
                aliases.update(str(value) for value in self._decode_list(row["aliases_json"]))
                row_payload = self._decode_object(row["payload_json"])
                source_counts.update(row_payload.get("source_counts") or {})
                raw_counts.update(row_payload.get("observed_value_counts") or {})
                payload.update(
                    {
                        key: value
                        for key, value in row_payload.items()
                        if key not in {"source_counts", "observed_value_counts"}
                    }
                )
                if str(row["canonical_name"]) != str(keeper["canonical_name"]):
                    aliases.add(str(row["canonical_name"]))
            payload["source_counts"] = dict(sorted(source_counts.items()))
            payload["observed_value_counts"] = dict(
                sorted(raw_counts.items(), key=lambda pair: pair[0].casefold())
            )
            self._conn.execute(
                "UPDATE platforms SET aliases_json = ?, payload_json = ? WHERE platform_key = ?",
                (_json(sorted(aliases, key=str.casefold)), _json(payload), keeper["platform_key"]),
            )
            for row in duplicate_rows:
                if row["platform_key"] != keeper["platform_key"]:
                    self._conn.execute(
                        "DELETE FROM platforms WHERE platform_key = ?",
                        (row["platform_key"],),
                    )
                    counts["deduplicated"] += 1
        existing = {
            str(row["canonical_name"]).casefold(): row
            for row in self._conn.execute("SELECT * FROM platforms")
        }
        used_keys = {
            str(row["platform_key"])
            for row in self._conn.execute("SELECT platform_key FROM platforms")
        }
        for observation_key in sorted(observations):
            item = observations[observation_key]
            canonical = item["canonical_name"]
            aliases = sorted(item["aliases"], key=str.casefold)
            source_counts = dict(sorted(item["sources"].items()))
            raw_counts = dict(sorted(item["raw_values"].items(), key=lambda pair: pair[0].casefold()))
            payload = {
                "backfill_version": SIGNAL_MODEL,
                "source_counts": source_counts,
                "observed_value_counts": raw_counts,
            }
            match = existing.get(canonical.casefold())
            if match:
                old_aliases = self._decode_list(match["aliases_json"])
                merged_aliases = sorted(
                    {str(value).strip() for value in old_aliases + aliases if str(value).strip()},
                    key=str.casefold,
                )
                old_payload = self._decode_object(match["payload_json"])
                old_payload.update(payload)
                changed = (
                    match["aliases_json"] != _json(merged_aliases)
                    or match["payload_json"] != _json(old_payload)
                )
                if changed:
                    self._conn.execute(
                        """
                        UPDATE platforms
                        SET aliases_json = ?, payload_json = ?, active = 1
                        WHERE platform_key = ?
                        """,
                        (_json(merged_aliases), _json(old_payload), match["platform_key"]),
                    )
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                continue

            platform_key = f"plat_{_fingerprint(canonical.casefold())}"
            if platform_key in used_keys:
                # 24 位指纹碰撞极少，但回填不能因碰撞中断；后缀仍由事实名
                # 稳定计算，重复执行会得到同一个 key。
                platform_key = f"{platform_key}_{_fingerprint(canonical)}"
                suffix = 2
                while platform_key in used_keys:
                    platform_key = (
                        f"plat_{_fingerprint(canonical.casefold())}_"
                        f"{_fingerprint(canonical, suffix)}"
                    )
                    suffix += 1
            self._conn.execute(
                """
                INSERT INTO platforms(
                    platform_key, canonical_name, aliases_json, active, payload_json
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (platform_key, canonical, _json(aliases), _json(payload)),
            )
            used_keys.add(platform_key)
            counts["inserted"] += 1

        audit_id = AuditService(self._conn).record(
            action="platforms.backfill",
            subject_type="platforms",
            subject_id="cross-system",
            actor_id="system",
            actor_type="system",
            details={
                "model_version": SIGNAL_MODEL,
                "candidate_platforms": len(observations),
                "inserted": counts["inserted"],
                "updated": counts["updated"],
                "unchanged": counts["unchanged"],
                "deduplicated": counts["deduplicated"],
                "source_counts": {
                    source: sum(item["sources"][source] for item in observations.values())
                    for source in sorted({source for item in observations.values() for source in item["sources"]})
                },
            },
        )
        return {
            "candidate_platforms": len(observations),
            "inserted": counts["inserted"],
            "updated": counts["updated"],
            "unchanged": counts["unchanged"],
            "deduplicated": counts["deduplicated"],
            "audit_id": audit_id,
        }

    def _canonical_platform(self, value: str) -> str:
        folded = value.casefold()
        return (
            self._platform_rules.get(folded)
            or _SYSTEM_PLATFORM_ALIASES.get(folded)
            or value
        )

    @staticmethod
    def _decode_list(value: str | None) -> list[Any]:
        try:
            decoded = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []

    @staticmethod
    def _decode_object(value: str | None) -> dict[str, Any]:
        try:
            decoded = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    # ── 信号重算 ────────────────────────────────────────────

    def recompute_all(
        self,
        *,
        signal_date: str | None = None,
        threshold: float = 0.5,
    ) -> dict[str, Any]:
        results = {
            "read_spike": self._detect_read_spikes_stats(signal_date, threshold=threshold),
            "new_keyword_entry": self._detect_new_keyword_entries_stats(signal_date),
            "rank_change": self._detect_rank_changes_stats(signal_date),
            "new_creator": self._detect_new_creators_stats(signal_date),
            "comment_anomaly": self._detect_comment_anomalies_stats(signal_date),
        }
        totals = Counter()
        for result in results.values():
            for key, value in result.items():
                if key != "signal_type":
                    totals[key] += value
        audit_id = AuditService(self._conn).record(
            action="signals.recompute",
            subject_type="signals",
            subject_id=signal_date or "all-dates",
            actor_id="system",
            actor_type="system",
            details={
                "model_version": SIGNAL_MODEL,
                "signal_date": signal_date,
                "threshold": threshold,
                "by_type": results,
                "totals": dict(totals),
                "comments_present": self._conn.execute(
                    "SELECT COUNT(*) FROM comments"
                ).fetchone()[0],
                "production_jobs_touched": 0,
            },
        )
        return {"by_type": results, "totals": dict(totals), "audit_id": audit_id}

    def detect_all(self) -> dict[str, int]:
        """兼容旧触发器：只检测今天，返回每类实际新增/更新数。"""
        today = utc_now_iso()[:10]
        result = self.recompute_all(signal_date=today)
        return {
            signal_type: int(details["inserted"] + details["updated"])
            for signal_type, details in result["by_type"].items()
        }

    def detect_read_spikes(
        self,
        signal_date: str | None,
        *,
        threshold: float = 0.5,
    ) -> int:
        return self._detect_read_spikes_stats(signal_date, threshold=threshold)["candidates"]

    def _detect_read_spikes_stats(
        self,
        signal_date: str | None,
        *,
        threshold: float = 0.5,
    ) -> dict[str, int]:
        rows = self._conn.execute(
            """
            WITH ordered AS (
                SELECT o.observation_id, o.subject_type, o.subject_id, o.metric_key,
                       o.observed_at, o.numeric_value, o.source_ref,
                       LAG(o.numeric_value) OVER (
                           PARTITION BY o.subject_type, o.subject_id, o.metric_key
                           ORDER BY o.observed_at, o.observation_id
                       ) AS previous_value,
                       LAG(o.observed_at) OVER (
                           PARTITION BY o.subject_type, o.subject_id, o.metric_key
                           ORDER BY o.observed_at, o.observation_id
                       ) AS previous_at,
                       LAG(o.observation_id) OVER (
                           PARTITION BY o.subject_type, o.subject_id, o.metric_key
                           ORDER BY o.observed_at, o.observation_id
                       ) AS previous_observation_id
                FROM metric_observations o
                WHERE o.numeric_value IS NOT NULL
                  AND (
                    o.metric_key IN ('wechat.read_count', 'wechat.article.read_count',
                                     'geo.read_count', 'geo.source.read_count')
                    OR o.metric_key LIKE '%.read_count'
                  )
            )
            SELECT * FROM ordered
            WHERE previous_value > 0
              AND numeric_value > previous_value
              AND (numeric_value - previous_value) * 1.0 / previous_value >= ?
            """,
            (threshold,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            current_date = _date(row["observed_at"])
            if signal_date and current_date != signal_date:
                continue
            grouped[(row["subject_type"], row["subject_id"])].append(
                {
                    "metric_key": row["metric_key"],
                    "value": row["numeric_value"],
                    "baseline_value": row["previous_value"],
                    "current_at": row["observed_at"],
                    "previous_at": row["previous_at"],
                    "observation_id": row["observation_id"],
                    "previous_observation_id": row["previous_observation_id"],
                    "source_ref": row["source_ref"],
                }
            )
        return self._write_grouped(
            "read_spike",
            grouped,
            lambda subject_type, subject_id, changes: {
                "value": max(item["value"] for item in changes),
                "baseline": min(item["baseline_value"] for item in changes),
                "severity": max(
                    self._severity_from_delta(item["value"], item["baseline_value"])
                    for item in changes
                ),
            },
        )

    def detect_new_keyword_entries(self, signal_date: str | None) -> int:
        return self._detect_new_keyword_entries_stats(signal_date)["candidates"]

    def _detect_new_keyword_entries_stats(self, signal_date: str | None) -> dict[str, int]:
        rows = self._conn.execute(
            """
            WITH first_seen AS (
                SELECT h.content_id, s.platform, s.keyword, s.snapshot_id,
                       s.captured_at, h.rank, h.hit_id,
                       MIN(s.captured_at) OVER (
                           PARTITION BY s.platform, s.keyword, h.content_id
                       ) AS first_at
                FROM search_hits h
                JOIN search_snapshots s ON s.snapshot_id = h.snapshot_id
                WHERE h.content_id IS NOT NULL
            )
            SELECT * FROM first_seen WHERE captured_at = first_at
            ORDER BY captured_at, content_id, platform, keyword
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            current_date = _date(row["captured_at"])
            if signal_date and current_date != signal_date:
                continue
            grouped[("content", row["content_id"])].append(
                {
                    "platform": row["platform"],
                    "keyword": row["keyword"],
                    "captured_at": row["captured_at"],
                    "snapshot_id": row["snapshot_id"],
                    "hit_id": row["hit_id"],
                    "rank": row["rank"],
                }
            )
        return self._write_grouped(
            "new_keyword_entry",
            grouped,
            lambda subject_type, subject_id, changes: {
                "value": float(len(changes)),
                "baseline": None,
                "severity": 0.3,
            },
        )

    def detect_rank_changes(self, signal_date: str | None) -> int:
        return self._detect_rank_changes_stats(signal_date)["candidates"]

    def _detect_rank_changes_stats(self, signal_date: str | None) -> dict[str, int]:
        rows = self._conn.execute(
            """
            WITH ordered AS (
                SELECT h.hit_id, h.content_id, h.rank, s.platform, s.keyword,
                       s.snapshot_id, s.captured_at,
                       LAG(h.rank) OVER (
                           PARTITION BY h.content_id, s.platform, s.keyword
                           ORDER BY s.captured_at, s.snapshot_id
                       ) AS previous_rank,
                       LAG(s.snapshot_id) OVER (
                           PARTITION BY h.content_id, s.platform, s.keyword
                           ORDER BY s.captured_at, s.snapshot_id
                       ) AS previous_snapshot_id
                FROM search_hits h
                JOIN search_snapshots s ON s.snapshot_id = h.snapshot_id
                WHERE h.content_id IS NOT NULL
            )
            SELECT * FROM ordered
            WHERE previous_rank IS NOT NULL
              AND ABS(previous_rank - rank) >= 5
            ORDER BY captured_at, content_id, platform, keyword
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            current_date = _date(row["captured_at"])
            if signal_date and current_date != signal_date:
                continue
            grouped[("content", row["content_id"])].append(
                {
                    "platform": row["platform"],
                    "keyword": row["keyword"],
                    "previous_rank": row["previous_rank"],
                    "current_rank": row["rank"],
                    "snapshot_id": row["snapshot_id"],
                    "previous_snapshot_id": row["previous_snapshot_id"],
                    "hit_id": row["hit_id"],
                    "captured_at": row["captured_at"],
                }
            )
        return self._write_grouped(
            "rank_change",
            grouped,
            lambda subject_type, subject_id, changes: {
                "value": float(changes[-1]["current_rank"]),
                "baseline": float(changes[-1]["previous_rank"]),
                "severity": min(
                    1.0,
                    max(abs(item["previous_rank"] - item["current_rank"]) for item in changes) / 20.0,
                ),
            },
        )

    def detect_new_creators(self, signal_date: str | None) -> int:
        return self._detect_new_creators_stats(signal_date)["candidates"]

    def _detect_new_creators_stats(self, signal_date: str | None) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT creator_id, canonical_name, platform, first_seen_at
            FROM creators
            WHERE first_seen_at IS NOT NULL
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if signal_date and _date(row["first_seen_at"]) != signal_date:
                continue
            grouped[("creator", row["creator_id"])].append(
                {
                    "canonical_name": row["canonical_name"],
                    "platform": row["platform"],
                    "first_seen_at": row["first_seen_at"],
                    "creator_id": row["creator_id"],
                }
            )
        return self._write_grouped(
            "new_creator",
            grouped,
            lambda subject_type, subject_id, changes: {
                "value": 1.0,
                "baseline": None,
                "severity": 0.2,
            },
        )

    def detect_comment_anomalies(self, signal_date: str | None) -> int:
        return self._detect_comment_anomalies_stats(signal_date)["candidates"]

    def _detect_comment_anomalies_stats(self, signal_date: str | None) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT comment_id, event_type, current_state, observed_at
            FROM comment_events
            WHERE event_type IN ('missing', 'deleted_confirmed', 'hidden_suspected')
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if signal_date and _date(row["observed_at"]) != signal_date:
                continue
            grouped[("comment", row["comment_id"])].append(dict(row))
        return self._write_grouped(
            "comment_anomaly",
            grouped,
            lambda subject_type, subject_id, changes: {
                "value": float(len(changes)),
                "baseline": None,
                "severity": 0.7,
            },
        )

    def _write_grouped(
        self,
        signal_type: str,
        grouped: dict[tuple[str, str], list[dict[str, Any]]],
        values: Any,
    ) -> dict[str, int]:
        counts = Counter()
        for (subject_type, subject_id), changes in grouped.items():
            # 一个 subject 可能跨多日；按证据日期拆开，保持 signal_date 事实准确。
            by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in changes:
                item_date = _date(
                    item.get("captured_at")
                    or item.get("observed_at")
                    or item.get("current_at")
                    or item.get("first_seen_at")
                )
                if item_date:
                    by_date[item_date].append(item)
            for item_date, dated_changes in by_date.items():
                computed = values(subject_type, subject_id, dated_changes)
                outcome = self._write_signal(
                    signal_type=signal_type,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    signal_date=item_date,
                    value=computed["value"],
                    baseline=computed["baseline"],
                    severity=computed["severity"],
                    details={
                        "model_version": SIGNAL_MODEL,
                        "evidence_count": len(dated_changes),
                        "evidence": dated_changes,
                    },
                )
                counts[outcome] += 1
        return {
            "signal_type": signal_type,
            "candidates": sum(len(items) for items in grouped.values()),
            "inserted": counts["inserted"],
            "updated": counts["updated"],
            "unchanged": counts["unchanged"],
        }

    # ── 查询与持久化 ────────────────────────────────────────

    def list_signals(
        self,
        *,
        signal_date: str | None = None,
        limit: int = 100,
        signal_type: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if signal_date:
            where += " AND signal_date = ?"
            params.append(signal_date)
        if signal_type:
            where += " AND signal_type = ?"
            params.append(signal_type)
        params.append(limit)
        return [
            dict(row)
            for row in self._conn.execute(
                f"SELECT * FROM signals WHERE 1=1 {where} ORDER BY detected_at DESC LIMIT ?",
                params,
            ).fetchall()
        ]

    @staticmethod
    def _severity_from_delta(current: float, previous: float) -> float:
        if previous <= 0:
            return 0.5
        delta = (current - previous) / previous
        return min(1.0, max(0.1, delta))

    def _write_signal(
        self,
        *,
        signal_type: str,
        subject_type: str,
        subject_id: str,
        signal_date: str,
        value: float,
        baseline: float | None,
        severity: float,
        details: dict[str, Any],
    ) -> str:
        details_json = _json(details)
        row = self._conn.execute(
            """
            SELECT signal_id, severity, value, baseline_value, details_json
            FROM signals
            WHERE signal_type = ? AND subject_type = ? AND subject_id = ?
              AND signal_date = ? AND COALESCE(model_version, 'no-model') = ?
            """,
            (signal_type, subject_type, subject_id, signal_date, SIGNAL_MODEL),
        ).fetchone()
        if row is None:
            signal_id = f"sig_{_fingerprint(signal_type, subject_type, subject_id, signal_date, SIGNAL_MODEL)}"
            self._conn.execute(
                """
                INSERT INTO signals(
                    signal_id, signal_type, subject_type, subject_id, detected_at,
                    signal_date, severity, value, baseline_value, model_version,
                    status, details_json, consumed_by_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, '[]')
                """,
                (
                    signal_id,
                    signal_type,
                    subject_type,
                    subject_id,
                    utc_now_iso(),
                    signal_date,
                    severity,
                    value,
                    baseline,
                    SIGNAL_MODEL,
                    details_json,
                ),
            )
            return "inserted"
        changed = (
            row["severity"] != severity
            or row["value"] != value
            or row["baseline_value"] != baseline
            or row["details_json"] != details_json
        )
        if not changed:
            return "unchanged"
        # 状态和消费关系是业务状态，不因重算清除；只更新事实计算字段。
        self._conn.execute(
            """
            UPDATE signals
            SET severity = ?, value = ?, baseline_value = ?, details_json = ?
            WHERE signal_id = ?
            """,
            (severity, value, baseline, details_json, row["signal_id"]),
        )
        return "updated"
