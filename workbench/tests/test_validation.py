from __future__ import annotations

from pathlib import Path

import pytest

from content_hub.errors import ValidationAppError
from content_hub.validation.json_schema import validate_payload
from content_hub.validation.paths import resolve_allowed_path
from content_hub.validation.timestamps import parse_utc
from content_hub.validation.urls import canonicalize_url


def test_url_canonicalization_removes_tracking_but_keeps_identity_parameters() -> None:
    assert canonicalize_url(
        "HTTPS://Example.COM/article/?id=123&utm_source=test#part"
    ) == "https://example.com/article?id=123"


def test_invalid_url_is_rejected() -> None:
    with pytest.raises(ValidationAppError):
        canonicalize_url("javascript:alert(1)")


def test_only_utc_z_timestamps_are_accepted() -> None:
    assert parse_utc("2026-07-14T12:30:00Z").isoformat() == "2026-07-14T12:30:00+00:00"
    with pytest.raises(ValidationAppError):
        parse_utc("2026-07-14 12:30:00")


def test_path_must_stay_inside_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    assert resolve_allowed_path(root / "file.md", (root,)) == root / "file.md"
    with pytest.raises(ValidationAppError):
        resolve_allowed_path(tmp_path.parent / "outside.md", (root,))


def test_json_schema_rejects_wrong_shape(settings) -> None:
    validate_payload(["a", "b"], "string-array.schema.json", settings.schema_dir)
    with pytest.raises(ValidationAppError):
        validate_payload({"not": "an array"}, "string-array.schema.json", settings.schema_dir)
