from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from content_hub.errors import ValidationAppError


@lru_cache(maxsize=32)
def _validator(schema_path: str) -> Draft202012Validator:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_payload(value: Any, schema_name: str, schema_dir: Path) -> None:
    path = (schema_dir / schema_name).resolve()
    if schema_dir.resolve() not in path.parents:
        raise ValidationAppError("JSON Schema 路径越界。")
    if not path.is_file():
        raise ValidationAppError(f"JSON Schema 不存在：{schema_name}")
    errors = sorted(_validator(str(path)).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.path) or "$"
        raise ValidationAppError(f"JSON 不符合 {schema_name}：{location} {first.message}")
