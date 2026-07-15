"""对外响应与持久事件的安全清洗。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_SECRET_KEYS = {
    "cookie", "cookie_file", "cookie_blob", "raw_cookie",
    "token", "token_file", "token_blob", "raw_token",
    "profile", "profile_dir", "body", "content", "html", "preview_html",
}
_PATH_KEYS = {"path", "md_path", "content_md_path", "draft_path", "preview_path"}


def public_asset_ref(raw: str | Path | None, asset_root: Path) -> str | None:
    """把内部路径转换成稳定相对 ref；越界/不可识别路径不回显。"""
    if not raw:
        return None
    root = Path(asset_root).resolve()
    candidate = Path(str(raw)).expanduser()
    try:
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        return resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def scrub_public_payload(value: Any, *, asset_root: Path | None = None) -> Any:
    """递归清洗历史 payload，不把内部凭据、正文或绝对路径带出。"""
    root = Path(asset_root).resolve() if asset_root else None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _SECRET_KEYS:
                continue
            if normalized in _PATH_KEYS:
                ref = public_asset_ref(item, root) if root else None
                if ref:
                    out["md_path" if normalized == "md_path" else "asset_ref"] = ref
                continue
            out[key] = scrub_public_payload(item, asset_root=root)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_public_payload(item, asset_root=root) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        ref = public_asset_ref(value, root) if root else None
        return ref or "[REDACTED_PATH]"
    return value
