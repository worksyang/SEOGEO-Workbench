#!/usr/bin/env python3
"""Read-only baseline and security audit for the legacy system boundary.

The script deliberately writes only the generated evidence file. It never
modifies source/, the demo, or any legacy project directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DEFAULT = ROOT / "docs" / "static_baseline_audit_20260716.json"

SYSTEMS = {
    "wechat_search": Path("/Users/works14/.claude/监控/wechat-ybxhyyh-top3"),
    "wechat_mp": Path("/Users/works14/Documents/zkcode/250626_mpGUI"),
    "xhs": Path("/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor"),
    "geo": Path("/Users/works14/Documents/zkcode/GEOProMax"),
    "wiki": Path("/Users/works14/Documents/output_md/wiki-viewer"),
    "writing_money": Path("/Users/works14/Documents/output_md/wiki-viewer/WritingMoney"),
    "wechat_publish": Path("/Users/works14/Documents/zkcode/YZKcode/1126WritePublish"),
    "mother_library": Path("/Users/works14/Documents/output_md"),
}

SECRET_PATTERNS = {
    "cookie": re.compile(r"(?i)\b(cookie|set-cookie|sessionid|session_id)\b\s*[:=]\s*[\"']?[^\"'\s]{16,}"),
    "token": re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|bearer|tik\s*hub)\b\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}"),
    "api_key": re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|openai[_-]?api|dashscope[_-]?api|siliconflow[_-]?api)\b\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}"),
    "password": re.compile(r"(?i)\b(pass(word)?|pwd)\b\s*[:=]\s*[\"']?[^\"'\s]{8,}"),
    "private_key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "absolute_sensitive_path": re.compile(r"/Users/[^/\s]+/(Library|\.ssh|\.config|\.aws|\.claude|Documents)/"),
}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".md", ".txt", ".log", ".sql", ".html",
    ".css", ".sh", ".vue", ".xml", ".csv",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


def iter_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return
    if path.is_file():
        yield path
        return
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            candidate = Path(base) / name
            if not candidate.is_symlink() and candidate.is_file():
                yield candidate


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def directory_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "file_count": 0, "bytes": 0, "sha256": None, "extensions": {}}
    digest = hashlib.sha256()
    count = 0
    total = 0
    extensions = Counter()
    for file_path in iter_files(path):
        file_hash, size = sha256_file(file_path)
        relative = file_path.relative_to(path).as_posix().encode("utf-8", "surrogateescape")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_hash.encode())
        digest.update(b"\0")
        count += 1
        total += size
        extensions[file_path.suffix.lower() or "<no_extension>"] += 1
    return {
        "exists": True,
        "file_count": count,
        "bytes": total,
        "sha256": digest.hexdigest(),
        "extensions": dict(sorted(extensions.items())),
    }


def text_read(path: Path, limit: int = 2_000_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, UnicodeError):
        return ""


def api_summary(path: Path) -> dict:
    routes: set[tuple[str, str]] = set()
    route_patterns = [
        re.compile(r"@\w+\.(get|post|put|patch|delete|options|head)\(\s*[\"']([^\"']+)"),
        re.compile(r"\b(?:app|router)\.(get|post|put|patch|delete|options|head)\(\s*[\"']([^\"']+)"),
        re.compile(r"@(?:app|router)\.route\(\s*[\"']([^\"']+)[\"'][^)]*methods\s*=\s*\[([^\]]+)\]"),
    ]
    files_scanned = 0
    for file_path in iter_files(path):
        if file_path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        content = text_read(file_path)
        if not content:
            continue
        files_scanned += 1
        for pattern in route_patterns[:2]:
            for match in pattern.finditer(content):
                routes.add((match.group(1).upper(), match.group(2)))
        for match in route_patterns[2].finditer(content):
            methods = re.findall(r"[\"']([A-Za-z]+)[\"']", match.group(2))
            for method in methods:
                routes.add((method.upper(), match.group(1)))
    return {
        "files_scanned": files_scanned,
        "route_count": len(routes),
        "routes": [{"method": method, "path": route} for method, route in sorted(routes)],
        "note": "静态正则摘要；未执行服务、未调用外部 API，不能替代运行态契约验收。",
    }


def sqlite_summary(path: Path) -> list[dict]:
    result = []
    for file_path in iter_files(path):
        if file_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            continue
        try:
            connection = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True, timeout=1)
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            table_rows = []
            for (table,) in tables:
                safe_table = '"' + table.replace('"', '""') + '"'
                try:
                    row_count = connection.execute(f"SELECT COUNT(*) FROM {safe_table}").fetchone()[0]
                except sqlite3.Error:
                    row_count = None
                table_rows.append({"table": table, "row_count": row_count})
            result.append({
                "file": file_path.relative_to(path).as_posix(),
                "tables": table_rows,
            })
            connection.close()
        except (OSError, sqlite3.Error):
            continue
    return result


def data_summary(path: Path) -> dict:
    counts = Counter()
    bytes_by_kind = Counter()
    json_keys = Counter()
    for file_path in iter_files(path):
        suffix = file_path.suffix.lower() or "<no_extension>"
        _, size = sha256_file(file_path)
        counts[suffix] += 1
        bytes_by_kind[suffix] += size
        if suffix == ".json" and size <= 2_000_000:
            try:
                value = json.loads(text_read(file_path))
                if isinstance(value, dict):
                    json_keys.update(value.keys())
            except (json.JSONDecodeError, TypeError):
                pass
    return {
        "file_counts_by_extension": dict(sorted(counts.items())),
        "bytes_by_extension": dict(sorted(bytes_by_kind.items())),
        "sqlite": sqlite_summary(path),
        "json_top_level_keys": dict(json_keys.most_common(40)),
        "note": "仅记录数量、大小、SQLite 表名/行数和 JSON 顶层键；不记录正文、主键值、Cookie、Token 或原始路径。",
    }


def git_tracked_files() -> list[Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, stderr=subprocess.DEVNULL
        )
        return [ROOT / item for item in output.decode("utf-8", "surrogateescape").split("\0") if item]
    except (OSError, subprocess.CalledProcessError):
        return []


def security_scan() -> dict:
    categories = Counter()
    files_by_category: dict[str, set[str]] = {key: set() for key in SECRET_PATTERNS}
    scanned = 0
    candidates = git_tracked_files()
    # Scan tracked repository content plus API/log-like files in the current worktree.
    for candidate in iter_files(ROOT):
        if candidate not in candidates and candidate.suffix.lower() not in {".log", ".json", ".txt", ".md"}:
            continue
        if candidate.name in {".DS_Store"}:
            continue
        content = text_read(candidate)
        if not content:
            continue
        scanned += 1
        label = candidate.relative_to(ROOT).as_posix() if candidate.is_relative_to(ROOT) else "<external>"
        for category, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                categories[category] += 1
                files_by_category[category].add(label)
    git_history = {"status": "NOT RUN", "finding_counts_by_category": {}, "note": ""}
    try:
        # -G asks Git to return only patches whose changed lines match a
        # secret-shaped expression; raw patch content is discarded.
        combined = r"(cookie|set-cookie|sessionid|access[_-]?token|refresh[_-]?token|api[_-]?key|secret[_-]?key|password|pwd)\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,}"
        proc = subprocess.run(
            ["git", "log", "--all", "--no-ext-diff", "--unified=0", "--format=%H", "-G", combined, "--"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
        )
        commits = [line for line in proc.stdout.splitlines() if re.fullmatch(r"[0-9a-f]{40}", line)]
        git_history = {
            "status": "PASS" if not commits else "FAIL",
            "matching_commit_count": len(set(commits)),
            "finding_counts_by_category": {"secret_shaped_history_match": len(set(commits))} if commits else {},
            "raw_matches_omitted": True,
            "note": "扫描所有本地可见 Git 历史的差异行，仅保留数量，不输出提交内容。",
        }
    except (OSError, subprocess.TimeoutExpired):
        git_history = {"status": "NOT RUN", "finding_counts_by_category": {}, "note": "Git 历史扫描超时或不可用。"}
    return {
        "files_scanned": scanned,
        "finding_counts_by_category": dict(sorted(categories.items())),
        "finding_file_counts_by_category": {
            key: len(value) for key, value in sorted(files_by_category.items()) if value
        },
        "raw_matches_omitted": True,
        "scope": "Git tracked files plus current worktree text/log/json/md files; external legacy source is summarized separately and not copied into evidence.",
        "interpretation": "命中可能是变量名、文档说明或真实凭证，需人工复核；本摘要不输出匹配内容。",
        "git_history": git_history,
    }


def legacy_api_log_scan() -> dict:
    """Scan only likely API/log artifacts in legacy trees; never export content."""
    scanned = 0
    categories = Counter()
    candidates = 0
    for path in SYSTEMS.values():
        if not path.exists():
            continue
        for file_path in iter_files(path):
            name = file_path.name.lower()
            suffix = file_path.suffix.lower()
            if suffix not in {".log", ".json", ".txt", ".yaml", ".yml"} and not any(
                marker in name for marker in ("api", "request", "response", "history", "session", "trace")
            ):
                continue
            candidates += 1
            try:
                if file_path.stat().st_size > 20 * 1024 * 1024:
                    continue
            except OSError:
                continue
            content = text_read(file_path, limit=20 * 1024 * 1024)
            if not content:
                continue
            scanned += 1
            for category, pattern in SECRET_PATTERNS.items():
                if pattern.search(content):
                    categories[category] += 1
    return {
        "candidate_file_count": candidates,
        "files_scanned": scanned,
        "finding_counts_by_category": dict(sorted(categories.items())),
        "raw_matches_omitted": True,
        "scope": "七套原系统及母文章库中名称/扩展名疑似 API、请求、响应、历史或日志的文本文件；单文件上限 20 MiB。",
        "note": "只记录命中数量，不复制原始日志/API 响应，也不记录外部绝对路径。",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    args = parser.parse_args()
    now = datetime.now().astimezone()
    snapshots = {
        "repo_source": directory_snapshot(ROOT / "source"),
        "local_full_backup_demo": directory_snapshot(ROOT / "source" / "_local_full_backup" / "demo"),
        "demo_html": directory_snapshot(ROOT / "unified-content-platform-demo.html"),
    }
    systems = {}
    for label, path in SYSTEMS.items():
        systems[label] = {
            "present": path.exists(),
            "snapshot": directory_snapshot(path),
            "data_baseline": data_summary(path) if path.exists() else {},
            "api_contract_summary": api_summary(path) if path.exists() else {},
        }
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
        "purpose": "T003-T010/T018/T019 当前静态基线与安全审计证据",
        "read_only": True,
        "path_policy": "证据文档仅使用仓库内相对路径和系统标签；不写入 Cookie、Token、API Key、密码或绝对敏感路径。",
        "development_before_after": {
            "status": "NOT RUN",
            "reason": "本次任务开始前没有已签名/已保存的同口径前置快照，当前结果只能证明现状，不能证明开发前后不变。",
        },
        "snapshots": snapshots,
        "systems": systems,
        "security_scan": security_scan(),
        "legacy_api_log_scan": legacy_api_log_scan(),
        "write_boundary": {
            "allowed": ["workbench/scripts", "tests", "docs", "data", "asset_store"],
            "observed_commit_scope": "由提交前 git diff --name-only 人工复核",
            "status": "NOT RUN",
            "reason": "脚本只能提供当前快照；最终目录边界需结合本次提交 diff 判定。",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    print(json.dumps({
        "repo_source_sha256": snapshots["repo_source"]["sha256"],
        "backup_demo_exists": snapshots["local_full_backup_demo"]["exists"],
        "demo_html_sha256": snapshots["demo_html"]["sha256"],
        "security_findings": payload["security_scan"]["finding_counts_by_category"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
