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


@dataclass(slots=True)
class PublishAccount:
    account_id: str
    display_name: str
    profile_dir: str
    cookie_file: str
    token_file: str
    enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "profile_dir": self.profile_dir,
            "cookie_file": self.cookie_file,
            "token_file": self.token_file,
            "enabled": self.enabled,
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
    ):
        self._conn = connection
        self._publish_root = Path(publish_root).resolve()
        self._publish_root.mkdir(parents=True, exist_ok=True)
        self._sensitive_words = sorted({word for word in sensitive_words if word})
        self._accounts: dict[str, PublishAccount] = {acct.account_id: acct for acct in accounts}

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
        cookie_path = Path(acct.cookie_file).expanduser()
        cookie_exists = cookie_path.exists()
        return {
            "account_id": account_id,
            "display_name": acct.display_name,
            "enabled": acct.enabled,
            "cookie_exists": cookie_exists,
            "status": "ready" if cookie_exists else "needs_login",
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
        attempt_id = self._record_attempt(
            account_id=account_id,
            content_md_path=str(target),
            idem_key=digest,
            status="succeeded",
            outcome="draft_saved",
            details={"content_id": content_id, "operator": operator},
        )
        return {"attempt_id": attempt_id, "draft_path": str(target), "content_id": content_id}

    def dry_run(self, *, account_id: str, content_id: str, body: str) -> dict[str, Any]:
        self._ensure_known_account(account_id)
        preview = self.preview(content_id=content_id, body=body)
        target_dir = self._publish_root / account_id / "dry_runs"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{content_id}_preview.html"
        target.write_text(preview.html, encoding="utf-8")
        attempt_id = self._record_attempt(
            account_id=account_id,
            content_md_path=str(target),
            idem_key=hashlib.sha256((preview.html or "").encode("utf-8")).hexdigest()[:16],
            status="succeeded",
            outcome="dry_run",
            details={"sensitive_matches": preview.sensitive_matches, "warnings": preview.warnings},
        )
        return {
            "attempt_id": attempt_id,
            "preview_html": preview.html,
            "preview_path": str(target),
            "sensitive_matches": preview.sensitive_matches,
            "warnings": preview.warnings,
            "status": "dry_run_only",
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
            return {"status": "needs_confirmation", "reason": "真发布要求传入 confirm=True 二次确认。"}
        acct = self._accounts[account_id]
        if not acct.enabled:
            return {"status": "blocked", "reason": "账号未启用"}
        preview = self.preview(content_id=content_id, body=body)
        would_dir = self._publish_root / account_id / "would_publish"
        would_dir.mkdir(parents=True, exist_ok=True)
        attempt_id = self._record_attempt(
            account_id=account_id,
            content_md_path=str(would_dir / f"{content_id}.md"),
            idem_key=hashlib.sha256(f"{account_id}::{content_id}".encode("utf-8")).hexdigest()[:16],
            status="succeeded",
            outcome="would_publish",
            details={"operator": operator, "sensitive_count": len(preview.sensitive_matches)},
        )
        return {
            "status": "succeeded",
            "attempt_id": attempt_id,
            "account": acct.display_name,
            "note": "前端未接真实 cookie 前，会回执 would_publish 标记；审计链路完整。",
        }

    def _ensure_known_account(self, account_id: str) -> None:
        if account_id not in self._accounts:
            raise ValueError(f"未知账号：{account_id}")

    def _record_attempt(
        self,
        *,
        account_id: str,
        content_md_path: str,
        idem_key: str,
        status: str,
        outcome: str,
        details: dict[str, Any],
    ) -> str:
        attempt_id = generate_ulid_like("pub")
        try:
            self._conn.execute(
                """
                INSERT INTO publish_attempts(
                    attempt_id, account_id, content_md_path, idem_key,
                    status, started_at, finished_at, details_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    account_id,
                    content_md_path,
                    idem_key,
                    status,
                    utc_now_iso(),
                    utc_now_iso(),
                    json.dumps({"outcome": outcome, **details}, ensure_ascii=False, sort_keys=True),
                    json.dumps({"automated": False}, ensure_ascii=False, sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT attempt_id FROM publish_attempts WHERE idem_key=?",
                (idem_key,),
            ).fetchone()
            return row["attempt_id"] if row else attempt_id
        return attempt_id


# ── Markdown → 微信编辑器 HTML 的简化实现 ──────────────────

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
