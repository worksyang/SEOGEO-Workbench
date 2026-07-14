from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


_WRITE_LOCK = Lock()


class KeywordConfigRepository:
    def __init__(self, keywords_path: Path) -> None:
        self.keywords_path = Path(keywords_path)

    def load(self) -> dict[str, Any]:
        if not self.keywords_path.exists():
            raise FileNotFoundError(f"keywords config not found: {self.keywords_path}")
        return json.loads(self.keywords_path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_payload(payload)
        self.keywords_path.parent.mkdir(parents=True, exist_ok=True)
        self.keywords_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    def list_groups(self) -> list[dict[str, Any]]:
        return self.load().get("groups", [])

    def create_group(self, label: str) -> dict[str, Any]:
        label = self._clean_label(label)
        now = self._now()
        with _WRITE_LOCK:
            payload = self.load()
            groups = payload.setdefault("groups", [])
            if any(g.get("label") == label for g in groups):
                raise ValueError(f"分组已存在：{label}")
            group = {
                "group_id": self._stable_group_id(label),
                "label": label,
                "order": self._next_order(groups),
                "keywords": [],
            }
            groups.append(group)
            payload["updated_at"] = now
            self.save(payload)
            return group

    def update_group(self, group_id: str, label: str | None = None, order: int | None = None) -> dict[str, Any]:
        with _WRITE_LOCK:
            payload = self.load()
            group = self._find_group(payload, group_id)
            if group is None:
                raise FileNotFoundError(f"group not found: {group_id}")
            if label is not None:
                label = self._clean_label(label)
                for item in payload.get("groups", []):
                    if item.get("group_id") != group_id and item.get("label") == label:
                        raise ValueError(f"分组已存在：{label}")
                group["label"] = label
            if order is not None:
                group["order"] = int(order)
            payload["updated_at"] = self._now()
            self.save(payload)
            return group

    def delete_group(self, group_id: str) -> dict[str, Any]:
        with _WRITE_LOCK:
            payload = self.load()
            groups = payload.get("groups", [])
            group = self._find_group(payload, group_id)
            if group is None:
                raise FileNotFoundError(f"group not found: {group_id}")
            if group.get("keywords"):
                raise ValueError("请先清空分组内关键词，再删除分组")
            payload["groups"] = [g for g in groups if g.get("group_id") != group_id]
            payload["updated_at"] = self._now()
            self.save(payload)
            return {"group_id": group_id, "deleted": True}

    def create_keyword(self, group_id: str, keyword_text: str, note: str = "") -> dict[str, Any]:
        keyword_text = self._clean_keyword(keyword_text)
        now = self._now()
        with _WRITE_LOCK:
            payload = self.load()
            if self._find_keyword(payload, keyword_text=keyword_text):
                raise ValueError(f"关键词已存在：{keyword_text}")
            group = self._find_group(payload, group_id)
            if group is None:
                raise FileNotFoundError(f"group not found: {group_id}")
            item = {
                "keyword_id": self._stable_keyword_id(keyword_text),
                "keyword_text": keyword_text,
                "note": str(note or "").strip(),
                "created_at": now,
                "updated_at": now,
            }
            group.setdefault("keywords", []).append(item)
            payload["updated_at"] = now
            self.save(payload)
            return item

    def update_keyword(
        self,
        keyword_id: str,
        keyword_text: str | None = None,
        note: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        with _WRITE_LOCK:
            payload = self.load()
            found = self._find_keyword(payload, keyword_id=keyword_id, with_group=True)
            if found is None:
                raise FileNotFoundError(f"keyword not found: {keyword_id}")
            group, item = found
            original_id = item.get("keyword_id")
            if keyword_text is not None:
                keyword_text = self._clean_keyword(keyword_text)
                duplicate = self._find_keyword(payload, keyword_text=keyword_text)
                if duplicate is not None and duplicate.get("keyword_id") != keyword_id:
                    raise ValueError(f"关键词已存在：{keyword_text}")
                item["keyword_text"] = keyword_text
                item["keyword_id"] = self._stable_keyword_id(keyword_text)
            if note is not None:
                item["note"] = str(note or "").strip()
            item["updated_at"] = self._now()

            if group_id and group_id != group.get("group_id"):
                target = self._find_group(payload, group_id)
                if target is None:
                    raise FileNotFoundError(f"group not found: {group_id}")
                group["keywords"] = [kw for kw in group.get("keywords", []) if kw.get("keyword_id") != original_id]
                target.setdefault("keywords", []).append(item)

            payload["updated_at"] = self._now()
            self.save(payload)
            return item

    def delete_keyword(self, keyword_id: str) -> dict[str, Any]:
        with _WRITE_LOCK:
            payload = self.load()
            found = self._find_keyword(payload, keyword_id=keyword_id, with_group=True)
            if found is None:
                raise FileNotFoundError(f"keyword not found: {keyword_id}")
            group, item = found
            actual_id = item.get("keyword_id")
            group["keywords"] = [kw for kw in group.get("keywords", []) if kw.get("keyword_id") != actual_id]
            payload["updated_at"] = self._now()
            self.save(payload)
            return {"keyword_id": actual_id, "deleted": True}

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _short_hash(text: str, length: int = 10) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]

    @classmethod
    def _stable_keyword_id(cls, text: str) -> str:
        return f"kw_{cls._short_hash(text)}"

    @classmethod
    def _stable_group_id(cls, text: str) -> str:
        return f"grp_{cls._short_hash(text)}"

    @staticmethod
    def _clean_label(label: str) -> str:
        value = str(label or "").strip()
        if not value:
            raise ValueError("分组名称不能为空")
        return value

    @staticmethod
    def _clean_keyword(keyword_text: str) -> str:
        value = str(keyword_text or "").strip()
        if not value:
            raise ValueError("关键词不能为空")
        return value

    @staticmethod
    def _next_order(groups: list[dict[str, Any]]) -> int:
        if not groups:
            return 1
        return max(int(g.get("order") or 0) for g in groups) + 1

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = payload.get("updated_at") or self._now()
        groups = payload.setdefault("groups", [])
        for idx, group in enumerate(groups, 1):
            label = self._clean_label(group.get("label") or f"分组{idx}")
            group["label"] = label
            group["group_id"] = group.get("group_id") or self._stable_group_id(label)
            group["order"] = int(group.get("order") or idx)
            keywords = group.setdefault("keywords", [])
            for item in keywords:
                text = self._clean_keyword(item.get("keyword_text") or item.get("text") or "")
                item["keyword_text"] = text
                item["keyword_id"] = item.get("keyword_id") or self._stable_keyword_id(text)
                item["note"] = str(item.get("note") or "")
                item["created_at"] = item.get("created_at") or now
                item["updated_at"] = item.get("updated_at") or now
        return {
            "version": int(payload.get("version") or 1),
            "updated_at": now,
            "groups": sorted(groups, key=lambda g: (int(g.get("order") or 0), g.get("label") or "")),
        }

    @staticmethod
    def _find_group(payload: dict[str, Any], group_id: str) -> dict[str, Any] | None:
        for group in payload.get("groups", []):
            if group.get("group_id") == group_id:
                return group
        return None

    @staticmethod
    def _find_keyword(
        payload: dict[str, Any],
        keyword_id: str | None = None,
        keyword_text: str | None = None,
        with_group: bool = False,
    ) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]] | None:
        for group in payload.get("groups", []):
            for item in group.get("keywords", []):
                if keyword_id and item.get("keyword_id") == keyword_id:
                    return (group, item) if with_group else item
                if keyword_text and item.get("keyword_text") == keyword_text:
                    return (group, item) if with_group else item
        return None
