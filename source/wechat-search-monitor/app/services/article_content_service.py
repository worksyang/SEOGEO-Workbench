from __future__ import annotations

from pathlib import Path


def resolve_article_markdown_payload(project_root: Path, content_path: str) -> dict:
    if not content_path:
        raise FileNotFoundError("empty content path")

    project_root = Path(project_root).resolve()
    requested = Path(content_path)
    if requested.is_absolute():
        raise ValueError("absolute path is not allowed")

    candidate = (project_root / requested).resolve()
    if candidate != project_root and project_root not in candidate.parents:
        raise ValueError("path escapes project root")
    if candidate.suffix.lower() != ".md":
        raise ValueError("only markdown files are allowed")
    if not candidate.exists():
        raise FileNotFoundError(f"content file not found: {content_path}")

    return {
        "path": content_path,
        "markdown": candidate.read_text(encoding="utf-8"),
    }

