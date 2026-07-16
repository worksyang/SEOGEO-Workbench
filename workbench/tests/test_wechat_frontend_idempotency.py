from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MONITOR_MIRROR = ROOT / "workbench/legacy_mirrors/wechat/source/static/js/monitor.js"
MONITOR_PUBLIC = ROOT / "workbench/frontend/public/legacy/wechat/static/js/monitor.js"


def test_wechat_monitor_mirrors_are_byte_identical() -> None:
    assert MONITOR_MIRROR.read_bytes() == MONITOR_PUBLIC.read_bytes()


def test_wechat_monitor_has_twenty_write_calls_and_no_naked_write_fetch() -> None:
    source = MONITOR_MIRROR.read_text(encoding="utf-8")
    assert len(re.findall(r"""method:\s*['"](POST|PATCH|PUT|DELETE)['"]""", source)) == 20
    assert source.count("return fetch(url, withIdempotencyKey(options));") == 1
    assert not re.search(
        r"\b(?:await\s+)?fetch\s*\([\s\S]{0,260}?method:\s*['\"](?:POST|PATCH|PUT|DELETE)['\"]",
        source,
    )
    assert "fetch(DATA_URL, { cache: 'no-cache' })" in source
    assert "fetch(url, { cache: 'no-cache' })" in source
    assert "fetch(REFRESH_HISTORY_URL)" in source


def test_wechat_monitor_idempotency_helper_behavior_and_node_syntax() -> None:
    subprocess.run(
        ["node", "--check", str(MONITOR_MIRROR)],
        check=True,
        capture_output=True,
        text=True,
    )
    source = MONITOR_MIRROR.read_text(encoding="utf-8")
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
