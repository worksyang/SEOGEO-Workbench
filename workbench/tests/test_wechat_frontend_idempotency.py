from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MONITOR_MIRROR = ROOT / "workbench/legacy_mirrors/wechat/source/static/js/monitor.js"
MONITOR_PUBLIC = ROOT / "workbench/frontend/public/legacy/wechat/static/js/monitor.js"


def test_wechat_monitor_mirror_is_frozen_reference() -> None:
    relative = MONITOR_MIRROR.relative_to(ROOT)
    expected = subprocess.run(
        ["git", "show", f"HEAD:{relative.as_posix()}"],
        check=True,
        capture_output=True,
    ).stdout
    assert MONITOR_MIRROR.read_bytes() == expected
    assert MONITOR_MIRROR.read_bytes() != MONITOR_PUBLIC.read_bytes()


def test_wechat_monitor_has_twenty_write_calls_and_no_naked_write_fetch() -> None:
    source = MONITOR_PUBLIC.read_text(encoding="utf-8")
    assert len(re.findall(r"""method:\s*['"](POST|PATCH|PUT|DELETE)['"]""", source)) == 20
    assert source.count("return fetch(url, withIdempotencyKey(options));") == 1
    assert not re.search(
        r"\b(?:await\s+)?fetch\s*\([\s\S]{0,260}?method:\s*['\"](?:POST|PATCH|PUT|DELETE)['\"]",
        source,
    )
    assert "fetch(DATA_URL, { cache: 'no-cache' })" in source
    assert "fetch(url, { cache: 'no-cache', signal });" in source
    assert "fetch(REFRESH_HISTORY_URL)" in source


def test_wechat_monitor_idempotency_helper_behavior_and_node_syntax() -> None:
    subprocess.run(
        ["node", "--check", str(MONITOR_PUBLIC)],
        check=True,
        capture_output=True,
        text=True,
    )
    source = MONITOR_PUBLIC.read_text(encoding="utf-8")
    helper_prefix = source.split("marked.setOptions({", 1)[0]
    script = f"""
const vm = require("node:vm");
const {{ webcrypto }} = require("node:crypto");
const calls = [];
const context = {{
  Headers,
  Uint8Array,
  crypto: webcrypto,
  fetch: (url, options) => {{
    calls.push([url, options]);
    return Promise.resolve({{ ok: true }});
  }},
  console,
}};
vm.createContext(context);
vm.runInContext({json.dumps(helper_prefix + '''
globalThis.__helpers = { withIdempotencyKey, idempotentFetch };
''')}, context);

const methods = ["POST", "PATCH", "PUT", "DELETE"];
for (const method of methods) {{
  const options = context.__helpers.withIdempotencyKey({{ method }});
  const key = options.headers.get("Idempotency-Key");
  if (!key) throw new Error(`${{method}} did not receive a key`);
}}
const explicit = context.__helpers.withIdempotencyKey({{
  method: "POST",
  headers: {{ "Idempotency-Key": "caller-key", "X-Test": "ok" }},
}});
if (explicit.headers.get("Idempotency-Key") !== "caller-key") throw new Error("explicit key was replaced");
if (explicit.headers.get("X-Test") !== "ok") throw new Error("existing headers were not merged");
for (const method of ["GET", "HEAD", "OPTIONS"]) {{
  const options = context.__helpers.withIdempotencyKey({{ method }});
  if (options.headers) throw new Error(`${{method}} was forced through the write helper`);
}}
(async () => {{
  await context.__helpers.idempotentFetch("/write", {{ method: "POST" }});
  await context.__helpers.idempotentFetch("/read", {{ method: "GET" }});
  if (calls.length !== 2) throw new Error("unexpected fetch count");
  if (!calls[0][1].headers.get("Idempotency-Key")) throw new Error("write fetch missing key");
  if (calls[1][1].headers) throw new Error("GET fetch changed the read path");
  process.stdout.write(JSON.stringify({{ writeKey: calls[0][1].headers.get("Idempotency-Key") }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_wechat_monitor_performance_overlay_and_detail_guards() -> None:
    source = MONITOR_PUBLIC.read_text(encoding="utf-8")
    assert "const DETAIL_CACHE_LIMIT = 2;" in source
    assert "const DETAIL_CACHE_TTL_MS" in source
    assert "const COVER_CACHE_LIMIT = 500;" in source
    assert "const activeDetailRequests = {" in source
    assert "keyword: null" in source
    assert "account: null" in source
    assert "function touchBoundedCache" in source
    assert "new AbortController()" in source
    assert "signal" in source
    assert "Object.assign(item, detail)" not in source
    assert "Object.assign(item,detail)" not in source
    assert "Object.defineProperty(window, '__WX_PERF__'" in source


def test_wechat_batch_finished_time_keeps_explicit_utc_source_date() -> None:
    source = MONITOR_PUBLIC.read_text(encoding="utf-8")
    start = source.index("function kmSourceDateParts")
    end = source.index("\n\nfunction kmFormatDuration", start)
    helper = source[start:end]
    script = f"""
const vm = require("node:vm");
const context = {{}};
vm.createContext(context);
vm.runInContext({json.dumps(helper + '''
globalThis.__format = kmFormatFinishedTime;
''')}, context);
if (context.__format("2026-07-18T19:43:00Z") !== "7月18日 19:43") {{
  throw new Error("explicit UTC timestamp crossed into the browser local date");
}}
if (context.__format("2026-07-18T19:43:00") !== "7月18日 19:43") {{
  throw new Error("naive legacy timestamp changed its labelled date");
}}
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
