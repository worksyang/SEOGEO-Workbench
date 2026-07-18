from __future__ import annotations

from content_hub.errors import AppError


XHS_FREEZE_CODE = "XHS_MIGRATION_FROZEN"
XHS_FREEZE_MESSAGE = (
    "小红书迁移期间已全部冻结：旧刷新、批量刷新、恢复、调度和设置写入均已停用；"
    "当前只允许读取已迁移数据和执行独立的搜索级影子刷新。"
)


def frozen_payload(operation: str) -> dict[str, object]:
    message = f"{XHS_FREEZE_MESSAGE}（操作：{operation}）"
    return {
        "ok": False,
        "blocked": True,
        "upstream_called": False,
        "freeze_state": "all_frozen",
        "operation": operation,
        "error": {
            "code": XHS_FREEZE_CODE,
            "message": message,
        },
        "message": message,
    }


class XhsMigrationFrozenError(AppError):
    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"{XHS_FREEZE_MESSAGE}（操作：{operation}）",
            XHS_FREEZE_CODE,
            409,
        )


def reject_xhs_write(operation: str) -> None:
    raise XhsMigrationFrozenError(operation)
