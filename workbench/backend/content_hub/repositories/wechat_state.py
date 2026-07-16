from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from content_hub.errors import AppError, ValidationAppError


PLATFORM = "wechat-search"
SYSTEM = "wechat-search"
SUPPORTED_REFRESH_DAYS = {1, 3, 7, 15}
LEGACY_UNHANDLED_NOT_FOUND = "LEGACY_UNHANDLED_NOT_FOUND"
LEGACY_UNHANDLED_INTERNAL_ERROR = "LEGACY_UNHANDLED_INTERNAL_ERROR"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def keyword_id_for(text: str) -> str:
    return f"kw_{hashlib.md5(text.encode('utf-8')).hexdigest()[:8]}"


def canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class WechatStateRepository:
    def __init__(self, connection):
        self.con = connection

    def _keyword(
        self,
        keyword_id: str,
        *,
        active_only: bool = False,
        legacy_unhandled_missing: bool = False,
    ):
        query = "SELECT * FROM keywords WHERE keyword_id=? AND platform=?"
        params: list[Any] = [keyword_id, PLATFORM]
        if active_only:
            query += " AND status='active'"
        row = self.con.execute(query, params).fetchone()
        if row is None:
            if legacy_unhandled_missing:
                raise AppError(
                    f"keyword not found: {keyword_id}",
                    LEGACY_UNHANDLED_NOT_FOUND,
                    500,
                )
            raise AppError(f"keyword not found: {keyword_id}", "NOT_FOUND", 404)
        return row

    def _setting(self, keyword_id: str):
        return self.con.execute(
            """SELECT s.*, g.group_name
               FROM search_keyword_settings s
               LEFT JOIN search_keyword_groups g ON g.group_id=s.group_id
               WHERE s.system_key=? AND s.platform=? AND s.keyword_id=?""",
            (SYSTEM, PLATFORM, keyword_id),
        ).fetchone()

    def require_keyword_text(self, keyword_id: str, expected: str | None = None, *, active_only: bool = True):
        row = self._keyword(keyword_id, active_only=active_only)
        if expected is not None and str(expected).strip() != row["keyword"]:
            raise ValidationAppError("keyword 与 keyword_id 不匹配")
        return row

    def ensure_setting(self, keyword_id: str, *, group_id: str | None = None, note: str = ""):
        row = self._setting(keyword_id)
        if row:
            return row
        now = now_iso()
        sid = f"{SYSTEM}:{keyword_id}"
        self.con.execute(
            """INSERT INTO search_keyword_settings(
                setting_id,system_key,platform,keyword_id,group_id,pinned,
                refresh_strategy,refresh_interval_minutes,commercial_value,note,
                archived_at,updated_at,payload_json,refresh_policy_reason,
                commercial_value_source,commercial_value_reason,auto_archive_locked
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, SYSTEM, PLATFORM, keyword_id, group_id, 0, "manual", 1440, 5,
             note, None, now, "{}", "新词：观察期每日刷新", "auto", "", 0),
        )
        return self._setting(keyword_id)

    @staticmethod
    def _default_setting() -> dict[str, Any]:
        """Read-only fallback; GET/projection paths must never INSERT."""
        return {
            "group_id": None,
            "group_name": None,
            "keyword_order": None,
            "note": "",
            "updated_at": None,
            "archived_at": None,
            "pinned": 0,
            "pin_order": None,
            "batch_default_selected": 1,
            "refresh_interval_minutes": 1440,
            "refresh_strategy": "manual",
            "refresh_policy_reason": "新词：观察期每日刷新",
            "commercial_value": 5,
            "commercial_value_source": "auto",
            "commercial_value_reason": "",
            "auto_archive_locked": 0,
            "payload_json": "{}",
        }

    @staticmethod
    def _decode_payload(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _update_setting_payload(self, keyword_id: str, **values: Any) -> None:
        setting = self.ensure_setting(keyword_id)
        payload = self._decode_payload(setting["payload_json"])
        payload.update(values)
        self.con.execute(
            """UPDATE search_keyword_settings
               SET payload_json=?
               WHERE keyword_id=? AND system_key=? AND platform=?""",
            (canonical_payload(payload), keyword_id, SYSTEM, PLATFORM),
        )

    @staticmethod
    def _payload_value(payload: dict[str, Any], key: str, default: Any = None) -> Any:
        return payload[key] if key in payload else default

    @staticmethod
    def _legacy_refresh_times(
        last_refresh_at: Any,
        effective_interval_hours: float,
    ) -> tuple[str | None, int | None, bool]:
        if not last_refresh_at:
            return None, None, True
        try:
            refreshed = datetime.fromisoformat(str(last_refresh_at).replace("Z", "+00:00"))
            now = datetime.now(refreshed.tzinfo) if refreshed.tzinfo else datetime.now()
            next_refresh = refreshed + timedelta(hours=effective_interval_hours)
            age_days = max(0, int((now - refreshed).total_seconds() // 86400))
            return next_refresh.isoformat(timespec="seconds"), age_days, now >= next_refresh
        except (TypeError, ValueError):
            return None, None, True

    def _state(self, keyword_id: str) -> dict[str, Any]:
        key = self._keyword(keyword_id, active_only=False)
        setting = self._setting(keyword_id) or self._default_setting()
        payload = self._decode_payload(key["payload_json"])
        setting_payload = self._decode_payload(setting["payload_json"])
        configured_frequency = self._payload_value(
            setting_payload, "refresh_frequency_days", None
        )
        if configured_frequency is None:
            configured_frequency = max(
                1, int((setting["refresh_interval_minutes"] or 1440) / 1440)
            )
        frequency = int(configured_frequency or 1)
        refresh_source = str(
            self._payload_value(setting_payload, "refresh_frequency_source", "auto")
            or "auto"
        )
        lifecycle_stage = str(
            self._payload_value(
                setting_payload,
                "lifecycle_stage",
                self._payload_value(payload, "lifecycle_stage", "established"),
            )
            or "established"
        )
        effective_interval = (
            3.0
            if lifecycle_stage == "observing" and refresh_source != "manual"
            else float(max(1, frequency) * 24)
        )
        last_refresh = self._payload_value(
            setting_payload,
            "last_refresh_at",
            self._payload_value(payload, "last_refresh_at"),
        )
        next_refresh, refresh_age, refresh_due = self._legacy_refresh_times(
            last_refresh, effective_interval
        )
        topic = self._payload_value(setting_payload, "topic", key["topic"])
        bucket = self._payload_value(
            setting_payload, "keyword_bucket", key["keyword_bucket"]
        )
        created_at = self._payload_value(
            setting_payload,
            "created_at",
            self._payload_value(payload, "created_at", key["first_seen_at"]),
        )
        first_seen_at = self._payload_value(
            setting_payload,
            "first_seen_at",
            self._payload_value(payload, "first_seen_at", key["first_seen_at"]),
        )
        group_id = self._payload_value(
            setting_payload, "group_id", setting["group_id"]
        )
        keyword_order = self._payload_value(
            setting_payload, "keyword_order", setting["keyword_order"]
        )
        note = self._payload_value(setting_payload, "note", setting["note"]) or ""
        updated_at = self._payload_value(
            setting_payload, "updated_at", setting["updated_at"]
        )
        archived_at = self._payload_value(
            setting_payload, "archived_at", setting["archived_at"]
        )
        is_pinned = bool(
            self._payload_value(setting_payload, "is_pinned", setting["pinned"])
        )
        pin_order = self._payload_value(
            setting_payload, "pin_order", setting["pin_order"]
        )
        batch_default_selected = bool(
            self._payload_value(
                setting_payload,
                "batch_default_selected",
                setting["batch_default_selected"],
            )
        )
        refresh_policy_reason = (
            self._payload_value(
                setting_payload,
                "refresh_policy_reason",
                setting["refresh_policy_reason"],
            )
            or ""
        )
        commercial_value = self._payload_value(
            setting_payload,
            "commercial_value_score",
            setting["commercial_value"],
        )
        commercial_source = (
            self._payload_value(
                setting_payload,
                "commercial_value_source",
                setting["commercial_value_source"],
            )
            or "auto"
        )
        commercial_reason = (
            self._payload_value(
                setting_payload,
                "commercial_value_reason",
                setting["commercial_value_reason"],
            )
            or ""
        )
        archive_locked = bool(
            self._payload_value(
                setting_payload,
                "auto_archive_locked",
                setting["auto_archive_locked"],
            )
        )
        result = {
            "keyword_id": key["keyword_id"], "keyword_text": key["keyword"],
            "status": key["status"],
            "enabled": key["status"] == "active", "is_active": key["status"] == "active",
            "source": self._payload_value(
                setting_payload,
                "source",
                self._payload_value(payload, "source", "manual"),
            ),
            "group_id": group_id, "keyword_order": keyword_order,
            "note": note, "created_at": created_at,
            "updated_at": updated_at, "archived_at": archived_at,
            "is_pinned": is_pinned, "pin_order": pin_order,
            "topic": topic, "keyword_bucket": bucket,
            "batch_default_selected": batch_default_selected,
            "first_seen_at": first_seen_at,
            "last_seen_at": self._payload_value(
                setting_payload,
                "last_seen_at",
                self._payload_value(payload, "last_seen_at"),
            ),
            "snapshot_count": int(
                self._payload_value(
                    setting_payload,
                    "snapshot_count",
                    self._payload_value(payload, "snapshot_count", 0),
                )
                or 0
            ),
            "refresh_frequency_days": frequency,
            "effective_refresh_interval_hours": effective_interval,
            "refresh_frequency_source": refresh_source,
            "refresh_policy_reason": refresh_policy_reason,
            "last_refresh_at": last_refresh,
            "last_refresh_attempt_at": self._payload_value(
                setting_payload,
                "last_refresh_attempt_at",
                self._payload_value(payload, "last_refresh_attempt_at"),
            ),
            "last_refresh_status": self._payload_value(
                setting_payload,
                "last_refresh_status",
                self._payload_value(payload, "last_refresh_status"),
            ),
            "next_refresh_at": next_refresh, "refresh_age_days": refresh_age,
            "is_refresh_due": refresh_due,
            "commercial_value_score": int(
                commercial_value if commercial_value is not None else 5
            ),
            "commercial_value_source": commercial_source,
            "commercial_value_reason": commercial_reason,
            "lifecycle_stage": lifecycle_stage,
            "observation_started_at": self._payload_value(
                setting_payload,
                "observation_started_at",
                self._payload_value(payload, "observation_started_at"),
            ),
            "observation_deadline_at": self._payload_value(
                setting_payload,
                "observation_deadline_at",
                self._payload_value(payload, "observation_deadline_at"),
            ),
            "discovery_candidate_id": self._payload_value(
                setting_payload,
                "discovery_candidate_id",
                self._payload_value(payload, "discovery_candidate_id"),
            ),
            "auto_archive_locked": archive_locked,
            "archive_reason_code": self._payload_value(
                setting_payload,
                "archive_reason_code",
                self._payload_value(payload, "archive_reason_code"),
            ),
            "archive_reason_detail": self._payload_value(
                setting_payload,
                "archive_reason_detail",
                self._payload_value(payload, "archive_reason_detail"),
            ),
        }
        return result

    def _update_payload(self, keyword_id: str, **values: Any) -> None:
        row = self._keyword(keyword_id, active_only=False)
        payload = self._decode_payload(row["payload_json"])
        payload.update({k: v for k, v in values.items() if k not in {"topic", "keyword_bucket"}})
        if "topic" in values:
            self.con.execute(
                "UPDATE keywords SET topic=?,updated_at=? WHERE keyword_id=?",
                (values["topic"], now_iso(), keyword_id),
            )
        if "keyword_bucket" in values:
            self.con.execute(
                "UPDATE keywords SET keyword_bucket=?,updated_at=? WHERE keyword_id=?",
                (values["keyword_bucket"], now_iso(), keyword_id),
            )
        self.con.execute("UPDATE keywords SET payload_json=? WHERE keyword_id=?", (canonical_payload(payload), keyword_id))

    def set_flag(self, keyword_id: str, expected: str, field: str, value: Any) -> dict[str, Any]:
        # 旧 W02-W06 只校验请求内 keyword 非空；存在性只按 ID 判断，
        # 也不会因为显示文本不同而拒绝请求。
        row = self._keyword(
            keyword_id,
            active_only=False,
        )
        setting = self.ensure_setting(keyword_id)
        now = now_iso()
        if field in {"topic", "keyword_bucket"}:
            self._update_payload(keyword_id, **{field: value})
            self._update_setting_payload(
                keyword_id, **{field: value, "updated_at": now}
            )
        elif field == "note":
            self.con.execute("UPDATE search_keyword_settings SET note=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?",
                             (value, now, keyword_id, SYSTEM, PLATFORM))
            self._update_setting_payload(keyword_id, note=value, updated_at=now)
        elif field == "pinned":
            pin_order = setting["pin_order"]
            if value and not setting["pinned"]:
                pin_order = self.con.execute("SELECT COALESCE(MAX(pin_order),0)+1 FROM search_keyword_settings WHERE system_key=? AND platform=? AND pinned=1", (SYSTEM, PLATFORM)).fetchone()[0]
            if not value:
                pin_order = None
            self.con.execute("UPDATE search_keyword_settings SET pinned=?,pin_order=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?",
                             (1 if value else 0, pin_order, now, keyword_id, SYSTEM, PLATFORM))
            self._update_setting_payload(
                keyword_id,
                is_pinned=bool(value),
                pin_order=pin_order,
                updated_at=now,
            )
        else:
            raise ValidationAppError("unsupported state field")
        return self._state(row["keyword_id"])

    def groups(self) -> list[dict[str, Any]]:
        rows = self.con.execute("SELECT group_id,group_name,sort_order,created_at,updated_at FROM search_keyword_groups WHERE system_key=? AND platform=? AND archived_at IS NULL ORDER BY sort_order,group_name,group_id", (SYSTEM, PLATFORM)).fetchall()
        return [{"group_id": r["group_id"], "label": r["group_name"], "order": r["sort_order"], "keywords": []} for r in rows]

    def group(self, group_id: str):
        row = self.con.execute("SELECT * FROM search_keyword_groups WHERE group_id=? AND system_key=? AND platform=? AND archived_at IS NULL", (group_id, SYSTEM, PLATFORM)).fetchone()
        if row is None:
            raise AppError(f"group not found: {group_id}", "NOT_FOUND", 404)
        return row

    def create_group(self, label: str) -> dict[str, Any]:
        label = str(label or "").strip()
        if not label:
            raise ValidationAppError("label is required")
        existing = self.con.execute(
            """SELECT * FROM search_keyword_groups
               WHERE system_key=? AND platform=? AND group_name=?
               ORDER BY archived_at IS NULL DESC, updated_at DESC LIMIT 1""",
            (SYSTEM, PLATFORM, label),
        ).fetchone()
        if existing and existing["archived_at"] is None:
            raise ValidationAppError(f"分组已存在：{label}")
        if existing:
            raise ValidationAppError(f"分组已存在：{label}")
        gid = f"grp_{hashlib.md5(label.encode()).hexdigest()[:8]}"
        order = self.con.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM search_keyword_groups WHERE system_key=? AND platform=? AND archived_at IS NULL", (SYSTEM, PLATFORM)).fetchone()[0]
        now = now_iso()
        self.con.execute("INSERT INTO search_keyword_groups(group_id,system_key,platform,group_name,sort_order,created_at,updated_at,archived_at) VALUES(?,?,?,?,?,?,?,NULL)", (gid, SYSTEM, PLATFORM, label, order, now, now))
        return {"group_id": gid, "label": label, "order": order, "keywords": []}

    def update_group(self, group_id: str, label: str | None, order: int | None) -> dict[str, Any]:
        row = self.group(group_id)
        new_label = row["group_name"] if label is None else str(label or "").strip()
        if not new_label:
            raise ValidationAppError("分组名称不能为空")
        if self.con.execute("SELECT 1 FROM search_keyword_groups WHERE group_name=? AND group_id!=? AND system_key=? AND platform=? AND archived_at IS NULL", (new_label, group_id, SYSTEM, PLATFORM)).fetchone():
            raise ValidationAppError(f"分组已存在：{new_label}")
        self.con.execute("UPDATE search_keyword_groups SET group_name=?,sort_order=?,updated_at=? WHERE group_id=?", (new_label, row["sort_order"] if order is None else int(order), now_iso(), group_id))
        return {"group_id": group_id, "label": new_label, "order": row["sort_order"] if order is None else int(order)}

    def delete_group(self, group_id: str) -> dict[str, Any]:
        self.group(group_id)
        if self.con.execute("SELECT 1 FROM search_keyword_settings s JOIN keywords k ON k.keyword_id=s.keyword_id WHERE s.group_id=? AND k.platform=? AND k.status='active'", (group_id, PLATFORM)).fetchone():
            raise ValidationAppError("请先清空分组内关键词，再删除分组")
        self.con.execute("UPDATE search_keyword_groups SET archived_at=?,updated_at=? WHERE group_id=?", (now_iso(), now_iso(), group_id))
        return {"group_id": group_id, "deleted": True}

    def create_keyword(self, group_id: str, text: str, note: str) -> dict[str, Any]:
        self.group(group_id)
        text = str(text or "").strip()
        if not text:
            raise ValidationAppError("keyword_text is required")
        kid = keyword_id_for(text)
        old = self.con.execute("SELECT * FROM keywords WHERE keyword_id=? OR (platform=? AND keyword=?)", (kid, PLATFORM, text)).fetchone()
        now = now_iso()
        if old and old["status"] == "active":
            raise ValidationAppError(f"关键词已存在：{text}")
        runtime_payload = {
            "keyword_id": kid,
            "keyword_text": text,
            "status": "active",
            "enabled": True,
            "is_active": True,
            "source": "manual",
            "group_id": group_id,
            "keyword_order": None,
            "note": str(note or "").strip(),
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
            "is_pinned": False,
            "pin_order": None,
            "topic": None,
            "keyword_bucket": None,
            "batch_default_selected": True,
            "first_seen_at": None,
            "last_seen_at": None,
            "snapshot_count": 0,
            "refresh_frequency_days": 1,
            "refresh_frequency_source": "auto",
            "refresh_policy_reason": "新词：观察期每日刷新",
            "last_refresh_at": None,
            "last_refresh_attempt_at": None,
            "last_refresh_status": None,
            "commercial_value_score": 5,
            "commercial_value_source": "auto",
            "commercial_value_reason": "",
            "lifecycle_stage": "established",
            "observation_started_at": None,
            "observation_deadline_at": None,
            "discovery_candidate_id": None,
            "auto_archive_locked": False,
            "archive_reason_code": None,
            "archive_reason_detail": None,
        }
        if old:
            self.con.execute(
                """UPDATE keywords
                   SET status='active',topic=NULL,keyword_bucket=NULL,updated_at=?,
                       payload_json=?
                   WHERE keyword_id=?""",
                (now, canonical_payload(runtime_payload), old["keyword_id"]),
            )
            kid = old["keyword_id"]
            runtime_payload["keyword_id"] = kid
        else:
            self.con.execute(
                """INSERT INTO keywords(
                   keyword_id,platform,keyword,status,topic,keyword_bucket,
                   first_seen_at,updated_at,payload_json
                ) VALUES(?,?,?,?,NULL,NULL,?,?,?)""",
                (
                    kid, PLATFORM, text, "active", now, now,
                    canonical_payload(runtime_payload),
                ),
            )
        order = self.con.execute(
            """SELECT COALESCE(MAX(s.keyword_order),0)+1
               FROM search_keyword_settings s
               JOIN keywords k ON k.keyword_id=s.keyword_id
               WHERE s.group_id=? AND k.platform=? AND k.status='active'
                 AND k.keyword_id<>?""",
            (group_id, PLATFORM, kid),
        ).fetchone()[0]
        runtime_payload["keyword_order"] = order
        self.con.execute(
            """INSERT INTO search_keyword_settings(
               setting_id,system_key,platform,keyword_id,group_id,pinned,
               refresh_strategy,refresh_interval_minutes,commercial_value,note,
               archived_at,updated_at,payload_json,keyword_order,
               refresh_policy_reason,commercial_value_source,
               commercial_value_reason,auto_archive_locked
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(setting_id) DO UPDATE SET
               group_id=excluded.group_id,pinned=0,refresh_strategy='scheduled',
               refresh_interval_minutes=1440,commercial_value=5,
               note=excluded.note,archived_at=NULL,updated_at=excluded.updated_at,
               payload_json=excluded.payload_json,keyword_order=excluded.keyword_order,
               refresh_policy_reason=excluded.refresh_policy_reason,
               commercial_value_source='auto',commercial_value_reason='',
               auto_archive_locked=0""",
            (
                f"{SYSTEM}:{kid}", SYSTEM, PLATFORM, kid, group_id, 0,
                "scheduled", 1440, 5, str(note or "").strip(), None, now,
                canonical_payload(runtime_payload), order,
                "新词：观察期每日刷新", "auto", "", 0,
            ),
        )
        return self._state(kid)

    def update_keyword(self, keyword_id: str, text: str | None, note: str | None, group_id: str | None) -> dict[str, Any]:
        row = self.require_keyword_text(keyword_id, active_only=True)
        target_group = group_id if group_id is not None else self.ensure_setting(keyword_id)["group_id"]
        if target_group:
            self.group(target_group)
        normalized_text = str(text or "").strip() if text is not None else None
        if text is not None and not normalized_text:
            raise ValidationAppError("关键词不能为空")
        if normalized_text is not None and normalized_text != row["keyword"]:
            created = self.create_keyword(target_group or "", normalized_text, note if note is not None else self.ensure_setting(keyword_id)["note"])
            self.archive_keyword(keyword_id)
            return created
        now = now_iso()
        stored_note = (
            self.ensure_setting(keyword_id)["note"]
            if note is None
            else str(note or "").strip()
        )
        self.con.execute("UPDATE search_keyword_settings SET group_id=?,note=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?", (target_group, stored_note, now, keyword_id, SYSTEM, PLATFORM))
        self._update_setting_payload(
            keyword_id,
            group_id=target_group,
            note=stored_note,
            updated_at=now,
        )
        return self._state(keyword_id)

    def archive_keyword(self, keyword_id: str) -> dict[str, Any]:
        self.require_keyword_text(keyword_id, active_only=True)
        now = now_iso()
        self.con.execute("UPDATE keywords SET status='archived',updated_at=? WHERE keyword_id=?", (now, keyword_id))
        self.con.execute("UPDATE search_keyword_settings SET archived_at=?,pinned=0,pin_order=NULL,refresh_strategy='disabled',updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?", (now, now, keyword_id, SYSTEM, PLATFORM))
        self._update_setting_payload(
            keyword_id,
            status="archived",
            enabled=False,
            is_active=False,
            archived_at=now,
            updated_at=now,
            is_pinned=False,
            pin_order=None,
            lifecycle_stage="archived",
            archive_reason_code=None,
            archive_reason_detail=None,
        )
        return {"keyword_id": keyword_id, "deleted": True, "status": "archived", "archive_reason_code": None}

    def set_policy(self, keyword_id: str, days: int | None, source: str) -> dict[str, Any]:
        self.require_keyword_text(keyword_id, active_only=True)
        source = str(source or "").strip().lower()
        if source not in {"auto", "manual"}:
            raise ValidationAppError("refresh policy source must be auto or manual")
        if source == "manual" and days is None:
            raise ValidationAppError(
                "refresh_frequency_days is required for manual policy"
            )
        if source == "manual" and days not in SUPPORTED_REFRESH_DAYS:
            raise ValidationAppError("refresh_frequency_days must be one of 1, 3, 7, 15")
        actual = 1 if source == "auto" else int(days)
        reason = (
            "自动策略：等待下一次评估"
            if source == "auto"
            else f"人工设定：每 {actual} 天刷新"
        )
        now = now_iso()
        self.con.execute("UPDATE search_keyword_settings SET refresh_strategy=?,refresh_interval_minutes=?,refresh_policy_reason=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?", ("scheduled", actual * 1440, reason, now, keyword_id, SYSTEM, PLATFORM))
        self._update_setting_payload(
            keyword_id,
            refresh_frequency_days=actual,
            refresh_frequency_source=source,
            refresh_policy_reason=reason,
            updated_at=now,
        )
        return self._state(keyword_id)

    def set_commercial(self, keyword_id: str, score: int, reason: str) -> dict[str, Any]:
        self.require_keyword_text(keyword_id, active_only=False)
        if score < 1 or score > 10:
            raise ValidationAppError("commercial value score must be between 1 and 10")
        actual_reason = str(reason or "").strip() or "人工设定商业价值"
        now = now_iso()
        self.con.execute("UPDATE search_keyword_settings SET commercial_value=?,commercial_value_source='manual',commercial_value_reason=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?", (score, actual_reason, now, keyword_id, SYSTEM, PLATFORM))
        self._update_setting_payload(
            keyword_id,
            commercial_value_score=score,
            commercial_value_source="manual",
            commercial_value_reason=actual_reason,
            updated_at=now,
        )
        return self._state(keyword_id)

    def set_archive_lock(self, keyword_id: str, locked: bool) -> dict[str, Any]:
        self.require_keyword_text(keyword_id, active_only=False)
        now = now_iso()
        self.con.execute("UPDATE search_keyword_settings SET auto_archive_locked=?,updated_at=? WHERE keyword_id=? AND system_key=? AND platform=?", (1 if locked else 0, now, keyword_id, SYSTEM, PLATFORM))
        self._update_setting_payload(
            keyword_id,
            auto_archive_locked=bool(locked),
            updated_at=now,
        )
        return self._state(keyword_id)
