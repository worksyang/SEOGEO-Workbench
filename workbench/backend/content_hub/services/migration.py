"""BFF 迁移开关与契约对账的共享实现。"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.domain.ids import generate_ulid_like
from content_hub.errors import ConflictError


MODES = {"legacy", "compare", "hub"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class MigrationResolver:
    """按 module/contract 读取 migration_switches，并执行三模式读取。"""

    def __init__(self, settings: Any, *, module_key: str, contract_key: str) -> None:
        self.settings = settings
        self.module_key = module_key
        self.contract_key = contract_key

    def mode(self) -> str:
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                """
                SELECT data_mode FROM migration_switches
                WHERE module_key=? AND contract_key=? AND enabled=1
                """,
                (self.module_key, self.contract_key),
            ).fetchone()
        return str(row["data_mode"]) if row and row["data_mode"] in MODES else "legacy"

    def describe(self) -> dict[str, str]:
        return {
            "module_key": self.module_key,
            "contract_key": self.contract_key,
            "data_mode": self.mode(),
        }

    def compare(
        self,
        *,
        request_fingerprint: str,
        legacy: Callable[[], Any],
        hub: Callable[[], Any],
    ) -> tuple[Any, dict[str, Any]]:
        legacy_value = hub_value = None
        legacy_error = hub_error = None
        try:
            legacy_value = legacy()
        except Exception as exc:  # 对账必须保留真实错误，不能吞成成功
            legacy_error = {"type": type(exc).__name__, "message": str(exc)}
        try:
            hub_value = hub()
        except Exception as exc:
            hub_error = {"type": type(exc).__name__, "message": str(exc)}

        if legacy_error:
            status = "legacy_error"
        elif hub_error:
            status = "hub_error"
        else:
            status = "matched" if payload_hash(legacy_value) == payload_hash(hub_value) else "different"
        comparison = self._record_comparison(
            request_fingerprint=request_fingerprint,
            legacy_value=legacy_value,
            hub_value=hub_value,
            status=status,
            legacy_error=legacy_error,
            hub_error=hub_error,
        )
        if legacy_error:
            raise ConflictError(f"{self.module_key}/{self.contract_key} legacy 读取失败：{legacy_error['message']}")
        return legacy_value, {"mode": "compare", "comparison": comparison}

    def read(
        self,
        *,
        request_fingerprint: str,
        legacy: Callable[[], Any],
        hub: Callable[[], Any],
    ) -> tuple[Any, dict[str, Any]]:
        selected = self.mode()
        if selected == "legacy":
            return legacy(), {"mode": "legacy"}
        if selected == "hub":
            try:
                value = hub()
            except NotImplementedError as exc:
                raise ConflictError(
                    f"{self.module_key}/{self.contract_key} 的 hub 模式尚未实现：{exc}"
                ) from exc
            return value, {"mode": "hub"}
        return self.compare(request_fingerprint=request_fingerprint, legacy=legacy, hub=hub)

    def require_mode(self, expected: str) -> None:
        selected = self.mode()
        if selected != expected:
            raise ConflictError(
                f"{self.module_key}/{self.contract_key} 当前为 {selected} 模式，不能执行 {expected} 专用操作。"
            )

    def _record_comparison(
        self,
        *,
        request_fingerprint: str,
        legacy_value: Any,
        hub_value: Any,
        status: str,
        legacy_error: dict[str, Any] | None,
        hub_error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = utc_now()
        legacy_hash = None if legacy_error else payload_hash(legacy_value)
        hub_hash = None if hub_error else payload_hash(hub_value)
        diff = {"legacy_error": legacy_error, "hub_error": hub_error}
        if not legacy_error and not hub_error and legacy_hash != hub_hash:
            diff["legacy"] = legacy_value
            diff["hub"] = hub_value
        comparison_id = generate_ulid_like("cmp")
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    con.execute(
                        """
                        INSERT INTO contract_comparisons(
                            comparison_id,module_key,contract_key,request_fingerprint,
                            legacy_hash,hub_hash,status,diff_json,compared_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            comparison_id,
                            self.module_key,
                            self.contract_key,
                            request_fingerprint[:200],
                            legacy_hash,
                            hub_hash,
                            status,
                            json.dumps(diff, ensure_ascii=False, sort_keys=True, default=str),
                            now,
                        ),
                    )
        return {
            "comparison_id": comparison_id,
            "status": status,
            "legacy_hash": legacy_hash,
            "hub_hash": hub_hash,
            "compared_at": now,
        }
