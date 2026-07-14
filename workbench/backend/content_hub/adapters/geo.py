from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE = re.compile(
    r"(token|api[_-]?key|secret|password|cookie|session|authorization|credential)",
    re.I,
)


def _scrub_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = [
            (key, "[REDACTED]" if SENSITIVE.search(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        return value


def scrub(value: Any) -> Any:
    """递归脱敏；不截断事实列表或正文。"""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE.search(str(key)) else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _scrub_url(value) if "://" in value else value
    return value


def stable_source_id(raw_platform: str, url: str) -> str:
    return hashlib.sha256(f"{raw_platform}\n{url}".encode("utf-8")).hexdigest()[:20]


def _safe_read(path: Path, root: Path) -> tuple[str | None, str | None, bytes | None]:
    try:
        root_path = Path(root)
        root = root_path.resolve()
        resolved = Path(path).resolve(strict=True)
        if root not in resolved.parents and resolved != root:
            return None, "outside_root", None
        current = Path(path)
        while current != root_path and current != current.parent:
            if current.is_symlink():
                return None, "symlink", None
            current = current.parent
        if root_path.is_symlink() or Path(path).is_symlink():
            return None, "symlink", None
        raw = resolved.read_bytes()
        return raw.decode("utf-8"), None, raw
    except FileNotFoundError:
        return None, "missing", None
    except (OSError, UnicodeError) as exc:
        return None, f"read_error:{type(exc).__name__}", None


def parse_markdown(path: Path, root: Path) -> dict[str, Any]:
    text, error, raw_bytes = _safe_read(path, root)
    result: dict[str, Any] = {"path": str(path), "exists": error is None, "error": error}
    if text is None:
        return result
    result["file_hash"] = hashlib.sha256(raw_bytes or b"").hexdigest()
    result["sha256"] = result["file_hash"]  # 兼容旧调用方；语义是原始字节 hash
    result["content"] = text
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) != 3:
            result["error"] = "bad_frontmatter"
        else:
            front: dict[str, Any] = {}
            for line in parts[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    front[key.strip()] = value.strip().strip("'\"")
            result["frontmatter"] = scrub(front)
            body = parts[2]
    normalized = "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines()).strip()
    result["content_hash"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    result["answer_hash"] = result["content_hash"]
    return result


class GeoSourceError(RuntimeError):
    pass


class GeoAdapter:
    adapter_key = "geopromax-sqlite"

    def __init__(self, settings: Any):
        self.settings = settings

    def _connect(self) -> sqlite3.Connection:
        if not self.settings.geo_database_path.is_file():
            raise GeoSourceError(f"GEOProMax SQLite 不存在：{self.settings.geo_database_path}")
        con = sqlite3.connect(
            f"file:{self.settings.geo_database_path}?mode=ro", uri=True, timeout=5
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    def platform_rules(self) -> dict[str, Any]:
        if not self.settings.geo_platforms_path.is_file():
            return {"platforms": {}, "aliases": {}}
        data = json.loads(self.settings.geo_platforms_path.read_text(encoding="utf-8"))
        platforms = data.get("platforms") or {}
        aliases: dict[str, str] = {}
        for canonical, meta in platforms.items():
            for alias in [canonical, *(meta.get("aliases") or [])]:
                aliases[str(alias).strip()] = canonical
        return {"platforms": platforms, "aliases": aliases}

    def canonical_platform(self, raw: str | None) -> tuple[str, bool]:
        value = str(raw or "").strip()
        rules = self.platform_rules()
        if value in rules["aliases"]:
            return rules["aliases"][value], True
        return value or "其他网页", False

    @staticmethod
    def _in_clause(values: list[Any]) -> tuple[str, tuple[Any, ...]]:
        return ("NULL", ()) if not values else (",".join("?" for _ in values), tuple(values))

    def _rows(self, con: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in con.execute(f"SELECT * FROM {table} {where}", params)]

    def source_counts(self) -> dict[str, int]:
        with self._connect() as con:
            counts = {
                table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "batches", "answers", "tools", "tool_search_keywords", "sources",
                    "source_relations", "suggested_questions", "source_metrics",
                )
            }
            counts["source_url_distinct"] = con.execute("SELECT COUNT(DISTINCT url) FROM sources").fetchone()[0]
            counts["source_duplicate_groups"] = con.execute(
                "SELECT COUNT(*) FROM (SELECT url FROM sources GROUP BY url HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            counts["source_duplicate_rows"] = con.execute(
                "SELECT COALESCE(SUM(n), 0) FROM (SELECT COUNT(*) AS n FROM sources GROUP BY url HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            return counts

    def snapshot(self, *, limit: int | None = None) -> dict[str, Any]:
        with self._connect() as con:
            counts = self.source_counts()
            suffix = " LIMIT ?" if limit else ""
            params = (limit,) if limit else ()
            return {
                "counts": counts,
                "batches": self._rows(con, "batches", f"ORDER BY id DESC{suffix}", params),
                "answers": self._rows(con, "answers", f"ORDER BY id DESC{suffix}", params),
            }

    def records(self, *, limit: int | None = None) -> dict[str, Any]:
        """按答案先选集，再只读取其关联事实，避免 limit 泄漏成全量导入。"""
        with self._connect() as con:
            answers = self._rows(
                con, "answers",
                "ORDER BY id" + (" LIMIT ?" if limit else ""),
                (limit,) if limit else (),
            )
            answer_ids = [row["id"] for row in answers]
            answer_sql, answer_params = self._in_clause(answer_ids)
            batches = self._rows(
                con, "batches",
                f"WHERE id IN (SELECT DISTINCT batch_id FROM answers WHERE id IN ({answer_sql})) ORDER BY id",
                answer_params,
            ) if answer_ids else []
            tool_rows = self._rows(
                con, "tools", f"WHERE answer_id IN ({answer_sql}) ORDER BY answer_id,position", answer_params
            ) if answer_ids else []
            tool_ids = [row["id"] for row in tool_rows]
            tool_sql, tool_params = self._in_clause(tool_ids)
            keywords = self._rows(
                con, "tool_search_keywords", f"WHERE tool_id IN ({tool_sql}) ORDER BY tool_id,position", tool_params
            ) if tool_ids else []
            relations = self._rows(
                con, "source_relations", f"WHERE answer_id IN ({answer_sql}) ORDER BY answer_id,position,id", answer_params
            ) if answer_ids else []
            suggestions = self._rows(
                con, "suggested_questions", f"WHERE answer_id IN ({answer_sql}) ORDER BY answer_id,position", answer_params
            ) if answer_ids else []
            metrics = self._rows(
                con, "source_metrics", f"WHERE answer_id IN ({answer_sql}) ORDER BY answer_id,source_id", answer_params
            ) if answer_ids else []
            source_ids = sorted({row["source_id"] for row in relations if row.get("source_id")})
            source_sql, source_params = self._in_clause(source_ids)
            sources = self._rows(
                con, "sources", f"WHERE id IN ({source_sql}) ORDER BY id", source_params
            ) if source_ids else []
        for source in sources:
            canonical, mapped = self.canonical_platform(source.get("platform"))
            source.update({
                "raw_platform": source.get("platform"),
                "canonical_platform": canonical,
                "platform_mapped": mapped,
                "identity_expected": stable_source_id(str(source.get("platform") or ""), source["url"]),
                "identity_valid": stable_source_id(str(source.get("platform") or ""), source["url"]) == source["id"],
            })
        batch_by_id = {row["id"]: row for row in batches}
        for answer in answers:
            batch = batch_by_id.get(answer["batch_id"], {})
            answer["batch"] = batch
            for key in ("app", "channel"):
                answer[key] = batch.get(key)
            answer["raw_batch"] = {
                key: batch.get(key)
                for key in ("app", "channel", "mode", "new_context", "status", "started_at",
                            "finished_at", "duration_seconds", "raw_file", "input_file", "output_file")
            }
        return {
            "batches": batches, "answers": answers, "tools": tool_rows, "keywords": keywords,
            "sources": sources, "relations": relations, "suggestions": suggestions,
            "metrics": metrics, "source_totals": self.source_counts(),
        }

    def markdown(self, relative: str | None, *, redfox: bool = False) -> dict[str, Any]:
        if not relative:
            return {"path": None, "exists": False, "error": "missing_path"}
        root = self.settings.geo_redfox_root if redfox else self.settings.geo_source_root
        path = Path(relative)
        if not path.is_absolute():
            path = root / relative
        return parse_markdown(path, root)


class RedfoxAdapter:
    """RedFox 本轮明确只读历史快照，不混入 SQLite 导入，也不调用刷新。"""

    adapter_key = "geopromax-redfox"

    def __init__(self, settings: Any):
        self.settings = settings

    def scan(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        root = self.settings.geo_redfox_root
        paths = sorted(root.rglob("*.md")) if root.is_dir() else []
        if limit is not None:
            paths = paths[:limit]
        return [{
            "adapter": self.adapter_key,
            "path": str(path),
            "question": path.stem,
            "markdown": parse_markdown(path, root),
            "lineage": "redfox",
        } for path in paths]

    def batch_refresh(self, *args: Any, **kwargs: Any) -> None:
        raise GeoSourceError("RedFox 禁止全局、批量或自动刷新；只能人工确认单问题。")
