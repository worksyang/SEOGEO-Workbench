#!/usr/bin/env zsh
# 公众号抓取控制台 — 一键启动脚本
set -u
ROOT="/Users/works14/Documents/zkcode/250626_mpGUI"
LOG_DIR="$ROOT/.web_console/logs"
BACKEND_PORT="${MPGUI_BACKEND_PORT:-28765}"
mkdir -p "$LOG_DIR"

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

pids_on_port() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

backend_ok() {
  curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" 2>/dev/null | grep -q '"service"[[:space:]]*:[[:space:]]*"mpgui-web"'
}

frontend_ok() {
  curl -fsS http://127.0.0.1:5173/ 2>/dev/null | grep -q '<title>公众号抓取控制台</title>'
}

stop_port() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(pids_on_port "$port")"
  if [[ -z "$pids" ]]; then
    return 0
  fi

  echo "[$label] :$port 被非本项目服务占用，停止 PID：${pids//$'\n'/, }"
  kill $=pids 2>/dev/null || true

  for _ in {1..20}; do
    if ! listening "$port"; then
      return 0
    fi
    sleep 0.2
  done

  echo "[$label] 普通停止失败，强制停止 PID：${pids//$'\n'/, }"
  kill -9 $=pids 2>/dev/null || true
}

if backend_ok; then
  echo "[backend] 已确认 MP GUI 后端运行在 :${BACKEND_PORT}"
else
  if listening "$BACKEND_PORT"; then
    stop_port "$BACKEND_PORT" backend
  fi
  echo "[backend] 启动 uvicorn → http://127.0.0.1:${BACKEND_PORT}"
  cd "$ROOT"
  nohup python3 -m uvicorn server.app:app --host 127.0.0.1 --port "$BACKEND_PORT" --no-access-log \
    >"$LOG_DIR/backend.log" 2>&1 &
  disown
fi

if frontend_ok; then
  echo "[frontend] 已确认公众号抓取控制台前端运行在 :5173"
else
  if listening 5173; then
    stop_port 5173 frontend
  fi
  echo "[frontend] 启动 vite → http://127.0.0.1:5173"
  cd "$ROOT/client"
  nohup npm run dev >"$LOG_DIR/frontend.log" 2>&1 &
  disown
fi

echo "[wait] 等待前后端健康检查..."
for _ in {1..60}; do
  if backend_ok && frontend_ok; then
    open http://127.0.0.1:5173
    echo "✅ 公众号抓取控制台 已就绪：http://127.0.0.1:5173"
    echo "   日志目录：$LOG_DIR"
    exit 0
  fi
  sleep 1
done
echo "❌ 启动超时，请查看 $LOG_DIR 下的日志"
exit 1
