#!/usr/bin/env python3
"""构建、校验、发布或应用微信港险早报 Agent 观察包。

该命令永不发送通知。默认只校验；只有显式 ``--publish`` 才写公开 AUX artifacts。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

WORKBENCH = Path(__file__).resolve().parents[1]
BACKEND = WORKBENCH / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from content_hub.config import Settings  # noqa: E402
from content_hub.db.migrations import migrate  # noqa: E402
from content_hub.services.wechat_agent_projection import (  # noqa: E402
    AgentProjectionValidation,
    apply_decision,
    build_brief_markdown,
    build_projection,
    import_claim_ledger,
    publish_projection,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="微信 Agent 早报观察包生产器（无通知）")
    value.add_argument("--validate", action="store_true", help="生成并校验 staging（默认）")
    value.add_argument("--publish", action="store_true", help="校验后原子发布到 Hub AUX")
    value.add_argument("--import-claims", type=Path, help="幂等导入旧 claim ledger JSON")
    value.add_argument("--apply-decision", type=Path, help="幂等应用 decision JSON")
    value.add_argument("--idempotency-key", help="decision 幂等键")
    value.add_argument("--memory-file", type=Path, help="可选业务 MEMORY，只读用于词表外去重")
    value.add_argument("--database", type=Path, help="覆盖 Hub 数据库路径（测试/影子）")
    value.add_argument("--output-dir", type=Path, help="可选影子输出目录，不影响公开 API")
    return value


def _inside_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    project = WORKBENCH.parent.resolve()
    allowed = (project / "data").resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise AgentProjectionValidation("output-dir must be inside project data/")
    if path.is_symlink():
        raise AgentProjectionValidation("output-dir cannot be a symlink")
    return resolved


def _write_shadow(output: Path, result: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence").mkdir(exist_ok=True)
    (output / "manifest.json").write_text(
        json.dumps(result["manifest"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "daily_brief.json").write_text(
        json.dumps(result["brief"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "daily_brief.md").write_text(build_brief_markdown(result["brief"]), encoding="utf-8")
    for evidence_id, payload in result["evidence"].items():
        (output / "evidence" / f"{evidence_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main() -> int:
    args = parser().parse_args()
    settings = Settings.load()
    if args.database:
        database = args.database.expanduser().resolve()
        settings = replace(settings, database_path=database, lock_path=database.with_suffix(".lock"))
    migrate(settings)

    if args.import_claims:
        claims_path = args.import_claims.expanduser().resolve()
        if claims_path.is_symlink() or not claims_path.is_file():
            raise AgentProjectionValidation("claim ledger is missing or is a symlink")
        payload = json.loads(claims_path.read_text(encoding="utf-8"))
        print(json.dumps(import_claim_ledger(settings, payload), ensure_ascii=False, indent=2))
        return 0

    if args.apply_decision:
        decision_path = args.apply_decision.expanduser().resolve()
        if decision_path.is_symlink() or not decision_path.is_file():
            raise AgentProjectionValidation("decision file is missing or is a symlink")
        allowed = (settings.project_root / "data/agent/morning-brief/decisions").resolve()
        if allowed not in decision_path.parents:
            raise AgentProjectionValidation("decision must be inside data/agent/morning-brief/decisions")
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        if not isinstance(decision, dict):
            raise AgentProjectionValidation("decision must be a JSON object")
        result = apply_decision(
            settings,
            decision,
            idempotency_key=args.idempotency_key or decision.get("idempotency_key", ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    memory_text = ""
    if args.memory_file:
        memory_file = args.memory_file.expanduser().resolve()
        if memory_file.is_symlink() or not memory_file.is_file():
            raise AgentProjectionValidation("memory file is missing or is a symlink")
        memory_text = memory_file.read_text(encoding="utf-8", errors="replace")

    staging = build_projection(settings, memory_text=memory_text)
    if args.output_dir:
        _write_shadow(_inside_output(args.output_dir), staging)
    result = {
        "published": False,
        "source_as_of": staging["manifest"]["source"]["as_of"],
        "source_fingerprint": staging["manifest"]["source"]["source_fingerprint"],
        "brief_id": staging["brief"]["brief_id"],
        "validation": staging["validation"],
    }
    if args.publish:
        result = {"published": True, **publish_projection(settings, staging)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if staging["validation"]["valid"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AgentProjectionValidation, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
