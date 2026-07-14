#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.geopromax.web"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$PLIST_DIR"

python3 - "$PLIST_PATH" "$ROOT" "$PYTHON_BIN" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
root = sys.argv[2]
python_bin = sys.argv[3]
payload = {
    "Label": "com.geopromax.web",
    "ProgramArguments": [python_bin, f"{root}/run.py"],
    "WorkingDirectory": "/tmp",
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Interactive",
    "ThrottleInterval": 10,
    "EnvironmentVariables": {
        "PYTHONUNBUFFERED": "1",
    },
    "StandardOutPath": "/tmp/geopromax_web_8790.launchd.log",
    "StandardErrorPath": "/tmp/geopromax_web_8790.launchd.error.log",
}
plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
PY

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
for _ in {1..20}; do
  if ! launchctl print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if lsof -nP -iTCP:8790 -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 "安装失败：8790 已被其他进程占用，请先停止该进程。"
  exit 1
fi

bootstrap_error=""
for _ in {1..5}; do
  if bootstrap_error="$(launchctl bootstrap "$GUI_DOMAIN" "$PLIST_PATH" 2>&1)"; then
    bootstrap_error=""
    break
  fi
  sleep 1
done
if [[ -n "$bootstrap_error" ]]; then
  print -u2 "$bootstrap_error"
  exit 1
fi
launchctl enable "$GUI_DOMAIN/$LABEL"
launchctl kickstart -k "$GUI_DOMAIN/$LABEL"

for _ in {1..40}; do
  if curl -fsS http://127.0.0.1:8790/health >/dev/null 2>&1; then
    echo "已安装并启动 $LABEL"
    echo "配置文件：$PLIST_PATH"
    echo "服务地址：http://127.0.0.1:8790/"
    exit 0
  fi
  sleep 0.25
done

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
print -u2 "安装失败：8790 未通过健康检查。如项目位于 OneDrive，请先为 Python 授予文件访问权限，或直接运行 python3 run.py。"
exit 1
