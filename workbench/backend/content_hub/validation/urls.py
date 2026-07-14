from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from content_hub.errors import ValidationAppError

TRACKING_PARAMETERS = {
    "from",
    "from_source",
    "flow_extra",
    "scene",
    "share_from_user_hidden",
    "share_red_id",
    "source",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def canonicalize_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationAppError(f"仅接受完整 HTTP(S) URL：{raw}")
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMETERS
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.lower(), host, path, query, ""))
