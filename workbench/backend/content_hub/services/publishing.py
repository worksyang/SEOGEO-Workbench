"""发布服务：账号状态、Markdown→HTML 预览、敏感词检查、保存草稿、dry-run、真发布入口。

依据 dev-plan §5.7：
- 所有路径由本机配置提供，Cookie 不进入前端 / 日志；
- publish_attempts 走幂等键；
- 自动化测试仅走 dry-run 与草稿箱，真发布需二次确认。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..domain.ids import generate_ulid_like
from ..validation.timestamps import utc_now_iso
from .audit import AuditService
from .safety import public_asset_ref, scrub_public_payload


@dataclass(slots=True)
class PublishAccount:
    account_id: str
    display_name: str
    profile_dir: str
    cookie_file: str
    token_file: str
    enabled: bool
    publishable: bool = False
    bridge_kind: str = "disabled"
    bridge_status: str = "unconfigured"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "publishable": False,
            "bridge_kind": self.bridge_kind,
            "bridge_status": self.bridge_status,
            "status": "unavailable",
            "reason_code": "publish.bridge_unavailable",
        }


@dataclass(slots=True)
class PublishPreview:
    content_id: str
    html: str
    sensitive_matches: list[dict[str, Any]]
    warnings: list[str]


class PublishingService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        publish_root: Path,
        sensitive_words: Iterable[str] = (),
        accounts: Iterable[PublishAccount] = (),
        bridge_kind: str = "disabled",
        bridge_status: str = "unconfigured",
    ):
        self._conn = connection
        self._publish_root = Path(publish_root).resolve()
        self._publish_root.mkdir(parents=True, exist_ok=True)
        self._sensitive_words = sorted({word for word in sensitive_words if word})
        self._accounts: dict[str, PublishAccount] = {acct.account_id: acct for acct in accounts}
        self._bridge_kind = bridge_kind
        self._bridge_status = bridge_status
        self._audit = AuditService(connection)

    def list_accounts(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for acct in self._accounts.values():
            items.append(acct.to_dict())
        for row in self._conn.execute(
            "SELECT account_key, MAX(attempted_at) AS last_at FROM publish_attempts GROUP BY account_key"
        ).fetchall():
            for item in items:
                if item["account_id"] == row["account_key"]:
                    item["last_attempt_at"] = row["last_at"]
        return items

    def status(self, account_id: str) -> dict[str, Any]:
        acct = self._accounts.get(account_id)
        if not acct:
            return {"account_id": account_id, "status": "unknown", "enabled": False}
        return {
            "account_id": account_id,
            "display_name": acct.display_name,
            "enabled": acct.enabled,
            "publishable": False,
            "bridge_kind": self._bridge_kind,
            "bridge_status": self._bridge_status,
            "status": "unavailable",
            "reason_code": "publish.bridge_unavailable",
        }

    def preview(
        self,
        *,
        content_id: str,
        body: str,
        extra_sensitive_words: Iterable[str] = (),
    ) -> PublishPreview:
        sensitive_set = set(self._sensitive_words)
        sensitive_set.update(extra_sensitive_words)
        matches: list[dict[str, Any]] = []
        for word in sorted(sensitive_set, key=lambda x: -len(x)):
            if not word:
                continue
            start = 0
            while True:
                pos = body.find(word, start)
                if pos < 0:
                    break
                line_no = body.count("\n", 0, pos) + 1
                matches.append({"word": word, "position": pos, "line": line_no})
                start = pos + len(word)
        html = _markdown_to_wechat_html(body)
        warnings: list[str] = []
        if len(body) > 20000:
            warnings.append("正文长度超过 20000 字，微信编辑器可能截断。")
        if "<img" not in html and not html.startswith("<h"):
            warnings.append("未检测到任何标题或图片，公众号会显示空白摘要。")
        return PublishPreview(
            content_id=content_id,
            html=html,
            sensitive_matches=matches,
            warnings=warnings,
        )

    def save_draft(self, *, account_id: str, content_id: str, body: str, operator: str = "user") -> dict[str, Any]:
        self._ensure_known_account(account_id)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        target_dir = self._publish_root / account_id / "drafts"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{content_id}_{digest}.md"
        target.write_text(body, encoding="utf-8")
        draft_ref = self._publish_ref(target)
        attempt_id = self._record_attempt(
            account_id=account_id,
            idem_key=digest,
            status="succeeded",
            outcome="draft_saved",
            details={"content_id": content_id, "operator": operator, "asset_ref": draft_ref},
        )
        # 仅在 Publishing 自身需要时新建 job；下面调用 _record_attempt 时已经包含 job_id 备用
        self._audit.record(
            action="publishing.draft",
            subject_type="publish_attempt",
            subject_id=attempt_id,
            outcome="succeeded",
            details={"bridge_kind": self._bridge_kind, "bridge_status": self._bridge_status, "reason_code": "publish.draft_only"},
        )
        return {
            "attempt_id": attempt_id,
            "draft_ref": draft_ref,
            "content_id": content_id,
            "status": "draft_only",
            "draft_only": True,
        }

    def dry_run(self, *, account_id: str, content_id: str, body: str) -> dict[str, Any]:
        self._ensure_known_account(account_id)
        preview = self.preview(content_id=content_id, body=body)
        target_dir = self._publish_root / account_id / "dry_runs"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{content_id}_preview.html"
        target.write_text(preview.html, encoding="utf-8")
        preview_ref = self._publish_ref(target)
        attempt_id = self._record_attempt(
            account_id=account_id,
            idem_key=hashlib.sha256((preview.html or "").encode("utf-8")).hexdigest()[:16],
            status="succeeded",
            outcome="dry_run",
            details={
                "content_id": content_id,
                "sensitive_matches": preview.sensitive_matches,
                "warnings": preview.warnings,
                "asset_ref": preview_ref,
            },
        )
        self._audit.record(
            action="publishing.dry_run",
            subject_type="publish_attempt",
            subject_id=attempt_id,
            outcome="succeeded",
            details={"bridge_kind": self._bridge_kind, "bridge_status": self._bridge_status, "reason_code": "publish.preview_only"},
        )
        return {
            "attempt_id": attempt_id,
            "preview_html": preview.html,
            "preview_ref": preview_ref,
            "sensitive_matches": preview.sensitive_matches,
            "warnings": preview.warnings,
            "status": "dry_run_only",
            "preview_only": True,
        }

    def publish(
        self,
        *,
        account_id: str,
        content_id: str,
        body: str,
        confirm: bool = False,
        operator: str = "user",
    ) -> dict[str, Any]:
        self._ensure_known_account(account_id)
        if not confirm:
            result = {"status": "needs_confirmation", "ok": False, "reason_code": "publish.confirmation_required",
                      "reason": "真发布要求传入 confirm=True 二次确认。"}
            self._audit.record(
                action="publishing.publish",
                subject_type="publish_account",
                subject_id=account_id,
                outcome="blocked",
                details={"bridge_kind": self._bridge_kind, "bridge_status": self._bridge_status,
                         "reason_code": result["reason_code"]},
            )
            return result
        attempt_id = self._record_attempt(
            account_id=account_id,
            idem_key=hashlib.sha256(f"{account_id}::{content_id}".encode("utf-8")).hexdigest()[:16],
            status="blocked",
            outcome="blocked",
            details={"operator": operator, "reason_code": "publish.bridge_unavailable"},
        )
        result = {"status": "blocked", "ok": False, "attempt_id": attempt_id,
                  "reason_code": "publish.bridge_unavailable",
                  "reason": "未配置真实发布桥，未执行发布。"}
        self._audit.record(
            action="publishing.publish",
            subject_type="publish_account",
            subject_id=account_id,
            outcome="blocked",
            details={"bridge_kind": self._bridge_kind, "bridge_status": self._bridge_status,
                     "reason_code": result["reason_code"]},
        )
        return result

    def _ensure_known_account(self, account_id: str) -> None:
        if account_id not in self._accounts:
            raise ValueError(f"未知账号：{account_id}")

    def _publish_ref(self, target: Path) -> str:
        ref = public_asset_ref(target, self._publish_root.parent)
        if not ref:
            raise ValueError("发布产物路径不在受控 asset_store 内")
        return ref

    def _record_attempt(
        self,
        *,
        account_id: str,
        idem_key: str,
        status: str,
        outcome: str,
        details: dict[str, Any],
    ) -> str:
        payload = scrub_public_payload(
            {"automated": False, "outcome": outcome, **details},
            asset_root=self._publish_root.parent,
        )
        if "dry" in outcome:
            mode = "dry_run"
        elif "draft" in outcome:
            mode = "draft"
        else:
            mode = "publish"
        # 幂等先查
        existing = self._conn.execute(
            "SELECT attempt_id FROM publish_attempts WHERE idempotency_key=?",
            (idem_key,),
        ).fetchone()
        if existing:
            return existing["attempt_id"] if isinstance(existing, sqlite3.Row) else existing[0]
        # job_id 派生自 idem_key 以满足 FK，且幂等时复用同一行
        job_id = f"job_pub_{idem_key}"
        job_status = "blocked" if status == "blocked" else "succeeded"
        try:
            self._conn.execute(
                """INSERT OR IGNORE INTO production_jobs(
                       job_id, job_type, status, input_signal_ids_json,
                       source_content_ids_json, created_at, updated_at, payload_json
                   ) VALUES (?, ?, ?, '[]', '[]', ?, ?, '{}')""",
                (job_id, f"publish_{mode}", job_status, utc_now_iso(), utc_now_iso()),
            )
        except sqlite3.OperationalError:
            # 单独的 OperationalError（非完整性错误），重抛
            raise
        attempt_id = generate_ulid_like("pub")
        self._conn.execute(
            """INSERT OR IGNORE INTO publish_attempts(
                   attempt_id, job_id, account_key, idempotency_key,
                   mode, status, attempted_at, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                job_id,
                account_id,
                idem_key,
                mode,
                status,
                utc_now_iso(),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        # 任何情况都返回本次要返回的 attempt_id（已有则取旧的）
        row = self._conn.execute(
            "SELECT attempt_id FROM publish_attempts WHERE idempotency_key=?",
            (idem_key,),
        ).fetchone()
        if row:
            return row["attempt_id"] if isinstance(row, sqlite3.Row) else row[0]
        return attempt_id# ── Markdown → 微信编辑器 HTML 的简化实现 ──────────────────

_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_EM = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _markdown_to_wechat_html(body: str) -> str:
    lines = body.splitlines()
    output: list[str] = []
    in_list = False
    for raw in lines:
        line = raw.rstrip()
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append("<p></p>")
            continue
        if line.startswith("### "):
            output.append(f"<h3>{_escape(line[4:])}</h3>")
        elif line.startswith("## "):
            output.append(f"<h2>{_escape(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h1>{_escape(line[2:])}</h1>")
        elif line.startswith("> "):
            output.append(f"<blockquote>{_escape(line[2:])}</blockquote>")
        elif line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_inline(line[2:])}</li>")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{_inline(line)}</p>")
    if in_list:
        output.append("</ul>")
    return "\n".join(output)


def _inline(text: str) -> str:
    escaped = _escape(text)
    escaped = _INLINE_IMG.sub(lambda m: f'<img src="{_escape_attr(m.group(2))}" alt="{_escape_attr(m.group(1))}" />', escaped)
    escaped = _INLINE_LINK.sub(lambda m: f'<a href="{_escape_attr(m.group(2))}">{_escape(m.group(1))}</a>', escaped)
    escaped = _INLINE_CODE.sub(lambda m: f"<code>{_escape(m.group(1))}</code>", escaped)
    escaped = _INLINE_BOLD.sub(lambda m: f"<strong>{_escape(m.group(1))}</strong>", escaped)
    escaped = _INLINE_EM.sub(lambda m: f"<em>{_escape(m.group(1))}</em>", escaped)
    return escaped


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")
