from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.errors import AppError, ConflictError, NotFoundError, ValidationAppError
from content_hub.features.xhs.policy import reject_xhs_write
from content_hub.features.xhs.runtime import PLATFORM, SYSTEM, UNASSIGNED_GROUP_ID, _now, _json, _source_keyword_id


def _internal_keyword_id(source_id: str) -> str:
    return f"xhs_keyword_{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:24]}"


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class XhsStateService:
    def __init__(self, settings: Any, *, actor_id: str = "user") -> None:
        self.settings = settings
        self.actor_id = actor_id or "user"

    def _audit(self, con, action: str, subject_id: str, outcome: str, details: dict[str, Any]) -> None:
        con.execute(
            """INSERT INTO audit_log(
                audit_id,occurred_at,actor_type,actor_id,action,
                subject_type,subject_id,outcome,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                f"audit_xhs_{hashlib.sha256(f'{action}:{subject_id}:{_now()}'.encode()).hexdigest()[:24]}",
                _now(),
                "user",
                self.actor_id,
                action,
                "xiaohongshu",
                subject_id,
                outcome,
                _json(details),
            ),
        )

    def _keyword(self, con, keyword_id: str, *, active_only: bool = False) -> Any:
        row = con.execute(
            """SELECT * FROM keywords
               WHERE platform=?
                 AND (keyword_id=? OR json_extract(payload_json,'$.source_keyword_id')=?)
               ORDER BY CASE WHEN keyword_id=? THEN 0 ELSE 1 END
               LIMIT 1""",
            (PLATFORM, keyword_id, keyword_id, keyword_id),
        ).fetchone()
        if row is None or (active_only and row["status"] != "active"):
            raise NotFoundError("小红书关键词", keyword_id)
        return row

    def _setting(self, con, keyword_id: str) -> Any:
        return con.execute(
            """SELECT s.*,g.group_name
               FROM search_keyword_settings s
               LEFT JOIN search_keyword_groups g ON g.group_id=s.group_id
               WHERE s.system_key=? AND s.platform=? AND s.keyword_id=?""",
            (SYSTEM, PLATFORM, keyword_id),
        ).fetchone()

    def _ensure_setting(self, con, keyword_id: str, *, group_id: str | None = None) -> Any:
        existing = self._setting(con, keyword_id)
        if existing:
            return existing
        now = _now()
        con.execute(
            """INSERT INTO search_keyword_settings(
                setting_id,system_key,platform,keyword_id,group_id,pinned,
                refresh_strategy,refresh_interval_minutes,commercial_value,note,
                archived_at,updated_at,payload_json,refresh_policy_reason,
                commercial_value_source,commercial_value_reason,auto_archive_locked
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{SYSTEM}:{keyword_id}",
                SYSTEM,
                PLATFORM,
                keyword_id,
                group_id,
                0,
                "manual",
                None,
                None,
                "",
                None,
                now,
                "{}",
                "",
                "auto",
                "",
                0,
            ),
        )
        return self._setting(con, keyword_id)

    @staticmethod
    def _group_id_for(row: Any) -> str:
        return str(row["group_id"] or UNASSIGNED_GROUP_ID)

    def keyword_manage(self) -> dict[str, Any]:
        with connect(self.settings, readonly=True) as con:
            rows = con.execute(
                """SELECT k.*,s.pinned,s.pin_order,s.note,s.group_id,s.batch_default_selected,
                          s.refresh_strategy,s.refresh_interval_minutes,g.group_name
                   FROM keywords k
                   LEFT JOIN search_keyword_settings s
                     ON s.keyword_id=k.keyword_id
                    AND s.system_key=? AND s.platform=?
                   LEFT JOIN search_keyword_groups g ON g.group_id=s.group_id
                   WHERE k.platform=? AND k.status='active'
                   ORDER BY COALESCE(g.sort_order,999999),COALESCE(g.group_name,'未分组'),k.keyword,k.keyword_id""",
                (SYSTEM, PLATFORM, PLATFORM),
            ).fetchall()
            groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                group_id = self._group_id_for(row)
                label = str(row["group_name"] or "未分组")
                group = groups.setdefault(
                    group_id,
                    {
                        "group_id": group_id,
                        "label": label,
                        "order": 999999 if group_id == UNASSIGNED_GROUP_ID else 0,
                        "keywords": [],
                    },
                )
                source_id = _source_keyword_id(row)
                latest = con.execute(
                    """SELECT s.snapshot_id,s.captured_at
                       FROM search_snapshots s
                       WHERE s.platform=? AND s.keyword_id=?
                       ORDER BY s.captured_at DESC LIMIT 1""",
                    (PLATFORM, row["keyword_id"]),
                ).fetchone()
                today_best = None
                if latest:
                    rank = con.execute(
                        "SELECT MIN(rank) FROM search_hits WHERE snapshot_id=?",
                        (latest["snapshot_id"],),
                    ).fetchone()[0]
                    today_best = int(rank) if rank is not None else None
                coverage_days = con.execute(
                    "SELECT COUNT(DISTINCT substr(captured_at,1,10)) FROM search_snapshots WHERE platform=? AND keyword_id=?",
                    (PLATFORM, row["keyword_id"]),
                ).fetchone()[0]
                article_count = con.execute(
                    """SELECT COUNT(DISTINCT h.content_id)
                       FROM search_hits h JOIN search_snapshots s ON s.snapshot_id=h.snapshot_id
                       WHERE s.platform=? AND s.keyword_id=?""",
                    (PLATFORM, row["keyword_id"]),
                ).fetchone()[0]
                group["keywords"].append(
                    {
                        "keyword_id": source_id,
                        "internal_keyword_id": row["keyword_id"],
                        "keyword_text": row["keyword"],
                        "note": row["note"] or "",
                        "is_pinned": bool(row["pinned"] or 0),
                        "pin_order": row["pin_order"],
                        "topic": row["topic"],
                        "keyword_bucket": row["keyword_bucket"],
                        "today_best": today_best,
                        "coverage_days": int(coverage_days or 0),
                        "article_count": int(article_count or 0),
                        "batch_default_selected": bool(row["batch_default_selected"]) if row["batch_default_selected"] is not None else True,
                        "refresh_strategy": row["refresh_strategy"] or "manual",
                    }
                )
            for group in groups.values():
                group["total"] = len(group["keywords"])
                group["ranked_count"] = sum(item["today_best"] is not None for item in group["keywords"])
            values = list(groups.values())
            values.sort(key=lambda item: (item["group_id"] == UNASSIGNED_GROUP_ID, item["label"]))
            total = sum(item["total"] for item in values)
            ranked = sum(item["ranked_count"] for item in values)
            return {
                "groups": values,
                "total": total,
                "ranked_total": ranked,
                "not_ranked_total": total - ranked,
                "source": "hub_db",
            }

    def _group(self, con, group_id: str, *, allow_unassigned: bool = False) -> Any:
        if allow_unassigned and group_id == UNASSIGNED_GROUP_ID:
            return None
        row = con.execute(
            """SELECT * FROM search_keyword_groups
               WHERE group_id=? AND system_key=? AND platform=? AND archived_at IS NULL""",
            (group_id, SYSTEM, PLATFORM),
        ).fetchone()
        if row is None:
            raise NotFoundError("小红书关键词分组", group_id)
        return row

    def create_group(self, label: str) -> dict[str, Any]:
        reject_xhs_write("keyword_group_create")
        label = str(label or "").strip()
        if not label:
            raise ValidationAppError("label is required")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    return self._create_group_in_transaction(con, label)

    def _create_group_in_transaction(self, con, label: str) -> dict[str, Any]:
        if con.execute(
            "SELECT 1 FROM search_keyword_groups WHERE system_key=? AND platform=? AND group_name=? AND archived_at IS NULL",
            (SYSTEM, PLATFORM, label),
        ).fetchone():
            raise ValidationAppError(f"分组已存在：{label}")
        group_id = f"xhs_group_{hashlib.sha256(f'{label}:{_now()}'.encode()).hexdigest()[:16]}"
        order = con.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM search_keyword_groups WHERE system_key=? AND platform=? AND archived_at IS NULL",
            (SYSTEM, PLATFORM),
        ).fetchone()[0]
        now = _now()
        con.execute(
            """INSERT INTO search_keyword_groups(
                group_id,system_key,platform,group_name,sort_order,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (group_id, SYSTEM, PLATFORM, label, order, now, now),
        )
        self._audit(con, "xhs.group_create", group_id, "succeeded", {"label": label})
        return {"group_id": group_id, "label": label, "order": order, "keywords": [], "total": 0, "ranked_count": 0}

    def update_group(self, group_id: str, label: str) -> dict[str, Any]:
        reject_xhs_write("keyword_group_update")
        label = str(label or "").strip()
        if not label:
            raise ValidationAppError("label is required")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    if group_id == UNASSIGNED_GROUP_ID:
                        created = self._create_group_in_transaction(con, label)
                        con.execute(
                            """UPDATE search_keyword_settings
                               SET group_id=?,updated_at=?
                               WHERE system_key=? AND platform=? AND group_id IS NULL""",
                            (created["group_id"], _now(), SYSTEM, PLATFORM),
                        )
                        return created
                    group = self._group(con, group_id)
                    duplicate = con.execute(
                        """SELECT 1 FROM search_keyword_groups
                           WHERE system_key=? AND platform=? AND group_name=? AND group_id<>? AND archived_at IS NULL""",
                        (SYSTEM, PLATFORM, label, group_id),
                    ).fetchone()
                    if duplicate:
                        raise ValidationAppError(f"分组已存在：{label}")
                    con.execute(
                        "UPDATE search_keyword_groups SET group_name=?,updated_at=? WHERE group_id=?",
                        (label, _now(), group_id),
                    )
                    self._audit(con, "xhs.group_update", group_id, "succeeded", {"label": label})
                    return {"group_id": group_id, "label": label, "order": group["sort_order"]}

    def delete_group(self, group_id: str) -> dict[str, Any]:
        reject_xhs_write("keyword_group_delete")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    self._group(con, group_id)
                    if con.execute(
                        """SELECT 1 FROM search_keyword_settings s
                           JOIN keywords k ON k.keyword_id=s.keyword_id
                           WHERE s.group_id=? AND s.system_key=? AND s.platform=? AND k.status='active'""",
                        (group_id, SYSTEM, PLATFORM),
                    ).fetchone():
                        raise ValidationAppError("请先清空分组内关键词，再删除分组")
                    con.execute("UPDATE search_keyword_groups SET archived_at=?,updated_at=? WHERE group_id=?", (_now(), _now(), group_id))
                    self._audit(con, "xhs.group_delete", group_id, "succeeded", {})
                    return {"group_id": group_id, "deleted": True}

    def _state(self, con, row: Any) -> dict[str, Any]:
        setting = self._setting(con, row["keyword_id"])
        return {
            "keyword_id": _source_keyword_id(row),
            "internal_keyword_id": row["keyword_id"],
            "keyword_text": row["keyword"],
            "status": row["status"],
            "group_id": setting["group_id"] if setting else UNASSIGNED_GROUP_ID,
            "note": setting["note"] if setting else "",
            "is_pinned": bool(setting["pinned"]) if setting else False,
            "topic": row["topic"],
            "keyword_bucket": row["keyword_bucket"],
        }

    def create_keyword(self, group_id: str, text: str) -> dict[str, Any]:
        reject_xhs_write("keyword_create")
        text = str(text or "").strip()
        if not text:
            raise ValidationAppError("keyword_text is required")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    if group_id != UNASSIGNED_GROUP_ID:
                        self._group(con, group_id)
                    existing = con.execute(
                        "SELECT * FROM keywords WHERE platform=? AND keyword=? AND status='active'",
                        (PLATFORM, text),
                    ).fetchone()
                    if existing:
                        raise ConflictError(f"关键词已存在：{text}")
                    source_id = f"kw_{hashlib.md5(text.encode('utf-8')).hexdigest()[:8]}"
                    old = con.execute(
                        "SELECT * FROM keywords WHERE platform=? AND json_extract(payload_json,'$.source_keyword_id')=?",
                        (PLATFORM, source_id),
                    ).fetchone()
                    now = _now()
                    internal_id = str(old["keyword_id"]) if old else _internal_keyword_id(source_id)
                    payload = {
                        "source_keyword_id": source_id,
                        "keyword_text": text,
                        "source_provider": "hub",
                        "is_active": True,
                    }
                    if old:
                        con.execute(
                            "UPDATE keywords SET keyword=?,status='active',updated_at=?,payload_json=? WHERE keyword_id=?",
                            (text, now, _json(payload), internal_id),
                        )
                    else:
                        con.execute(
                            """INSERT INTO keywords(
                                keyword_id,platform,keyword,status,first_seen_at,updated_at,payload_json
                            ) VALUES(?,?,?,?,?,?,?)""",
                            (internal_id, PLATFORM, text, "active", now, now, _json(payload)),
                        )
                    self._ensure_setting(con, internal_id, group_id=None if group_id == UNASSIGNED_GROUP_ID else group_id)
                    con.execute(
                        "UPDATE search_keyword_settings SET group_id=?,updated_at=? WHERE system_key=? AND platform=? AND keyword_id=?",
                        (None if group_id == UNASSIGNED_GROUP_ID else group_id, now, SYSTEM, PLATFORM, internal_id),
                    )
                    result = self._state(con, con.execute("SELECT * FROM keywords WHERE keyword_id=?", (internal_id,)).fetchone())
                    self._audit(con, "xhs.keyword_create", internal_id, "succeeded", {"keyword_id": source_id, "keyword_text": text})
                    return result

    def update_keyword(self, keyword_id: str, *, text: str | None = None, note: str | None = None) -> dict[str, Any]:
        reject_xhs_write("keyword_update")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = self._keyword(con, keyword_id, active_only=True)
                    current = self._setting(con, row["keyword_id"])
                    next_text = str(text).strip() if text is not None else str(row["keyword"])
                    if not next_text:
                        raise ValidationAppError("关键词不能为空")
                    duplicate = con.execute(
                        """SELECT 1 FROM keywords
                           WHERE platform=? AND keyword=? AND status='active' AND keyword_id<>?""",
                        (PLATFORM, next_text, row["keyword_id"]),
                    ).fetchone()
                    if duplicate:
                        raise ConflictError(f"关键词已存在：{next_text}")
                    payload = _decode(row["payload_json"])
                    payload.update({"keyword_text": next_text, "updated_at": _now()})
                    con.execute(
                        "UPDATE keywords SET keyword=?,updated_at=?,payload_json=? WHERE keyword_id=?",
                        (next_text, _now(), _json(payload), row["keyword_id"]),
                    )
                    self._ensure_setting(con, row["keyword_id"])
                    if note is not None:
                        con.execute(
                            "UPDATE search_keyword_settings SET note=?,updated_at=? WHERE system_key=? AND platform=? AND keyword_id=?",
                            (str(note or "").strip(), _now(), SYSTEM, PLATFORM, row["keyword_id"]),
                        )
                    result = self._state(con, con.execute("SELECT * FROM keywords WHERE keyword_id=?", (row["keyword_id"],)).fetchone())
                    self._audit(con, "xhs.keyword_update", row["keyword_id"], "succeeded", {"keyword_id": _source_keyword_id(row)})
                    return result

    def archive_keyword(self, keyword_id: str) -> dict[str, Any]:
        reject_xhs_write("keyword_archive")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = self._keyword(con, keyword_id, active_only=True)
                    now = _now()
                    payload = _decode(row["payload_json"])
                    payload.update({"is_active": False, "status": "archived", "archived_at": now})
                    con.execute(
                        "UPDATE keywords SET status='archived',updated_at=?,payload_json=? WHERE keyword_id=?",
                        (now, _json(payload), row["keyword_id"]),
                    )
                    self._ensure_setting(con, row["keyword_id"])
                    con.execute(
                        """UPDATE search_keyword_settings
                           SET archived_at=?,pinned=0,pin_order=NULL,refresh_strategy='disabled',updated_at=?
                           WHERE system_key=? AND platform=? AND keyword_id=?""",
                        (now, now, SYSTEM, PLATFORM, row["keyword_id"]),
                    )
                    self._audit(con, "xhs.keyword_archive", row["keyword_id"], "succeeded", {})
                    return {"keyword_id": _source_keyword_id(row), "deleted": True, "status": "archived"}

    def set_flag(self, keyword_id: str, field: str, value: Any) -> dict[str, Any]:
        reject_xhs_write(f"keyword_{field}")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    row = self._keyword(con, keyword_id, active_only=True)
                    setting = self._ensure_setting(con, row["keyword_id"])
                    now = _now()
                    if field == "pinned":
                        pin_order = setting["pin_order"]
                        if value and not setting["pinned"]:
                            pin_order = con.execute(
                                """SELECT COALESCE(MAX(pin_order),0)+1
                                   FROM search_keyword_settings
                                   WHERE system_key=? AND platform=? AND pinned=1""",
                                (SYSTEM, PLATFORM),
                            ).fetchone()[0]
                        if not value:
                            pin_order = None
                        con.execute(
                            """UPDATE search_keyword_settings
                               SET pinned=?,pin_order=?,updated_at=?
                               WHERE system_key=? AND platform=? AND keyword_id=?""",
                            (1 if value else 0, pin_order, now, SYSTEM, PLATFORM, row["keyword_id"]),
                        )
                    elif field == "note":
                        con.execute(
                            "UPDATE search_keyword_settings SET note=?,updated_at=? WHERE system_key=? AND platform=? AND keyword_id=?",
                            (str(value or "").strip(), now, SYSTEM, PLATFORM, row["keyword_id"]),
                        )
                    elif field in {"topic", "keyword_bucket"}:
                        payload = _decode(row["payload_json"])
                        payload[field] = str(value or "").strip() or None
                        con.execute(
                            f"UPDATE keywords SET {field}=?,updated_at=?,payload_json=? WHERE keyword_id=?",
                            (str(value or "").strip() or None, now, _json(payload), row["keyword_id"]),
                        )
                    else:
                        raise ValidationAppError(f"不支持的小红书状态字段：{field}")
                    result = self._state(con, con.execute("SELECT * FROM keywords WHERE keyword_id=?", (row["keyword_id"],)).fetchone())
                    self._audit(con, f"xhs.keyword_{field}", row["keyword_id"], "succeeded", {"value": value})
                    return result
