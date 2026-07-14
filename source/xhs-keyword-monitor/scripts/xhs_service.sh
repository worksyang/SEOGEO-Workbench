#!/bin/zsh
set -euo pipefail

LABEL="com.zk.xhs-keyword-monitor"
PROJECT_ROOT="/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor"
SOURCE_PLIST="${PROJECT_ROOT}/scripts/${LABEL}.plist"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${LAUNCH_AGENTS}/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
SERVICE="${DOMAIN}/${LABEL}"
LOG_DIR="${PROJECT_ROOT}/data/state/launchd"

usage() {
  cat <<'EOF'
Usage: scripts/xhs_service.sh <command>

Commands:
  install    Install/update the macOS launchd user service and start it
  start      Start the installed service
  stop       Stop the service without removing its plist
  restart    Restart the service
  status     Show launchd state and port 8766 listener
  logs       Tail service stdout/stderr logs
  uninstall  Stop and remove the service
EOF
}

ensure_dirs() {
  mkdir -p "${LAUNCH_AGENTS}" "${LOG_DIR}"
}

bootout_if_loaded() {
  launchctl bootout "${SERVICE}" >/dev/null 2>&1 || true
  # bootout 是异步的；若立刻 bootstrap，偶发会出现“命令成功但 job 没真正
  # 拉起”的假启动。等 launchd 确认旧 job 已释放后再继续。
  local attempt
  for attempt in {1..10}; do
    if ! launchctl print "${SERVICE}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Warning: ${LABEL} is still unloading; continuing with guarded bootstrap." >&2
}

bootstrap_service() {
  local attempt
  for attempt in 1 2 3; do
    if launchctl bootstrap "${DOMAIN}" "${TARGET_PLIST}"; then
      return 0
    fi
    # launchd 在 bootout 后偶发尚未释放旧 job，短暂退避后重试，避免把
    # 已安装的系统服务误判为“启动失败”。
    sleep "${attempt}"
    bootout_if_loaded
  done
  echo "Failed to bootstrap ${LABEL}; inspect: ${LOG_DIR}/xhs-keyword-monitor.err.log" >&2
  return 1
}

wait_for_port() {
  local attempt
  for attempt in {1..10}; do
    if lsof -nP -iTCP:8766 -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${LABEL} was loaded but port 8766 did not become ready; inspect: ${LOG_DIR}/xhs-keyword-monitor.err.log" >&2
  return 1
}

install_service() {
  ensure_dirs
  /usr/bin/plutil -lint "${SOURCE_PLIST}" >/dev/null
  /bin/cp "${SOURCE_PLIST}" "${TARGET_PLIST}"
  /usr/bin/plutil -lint "${TARGET_PLIST}" >/dev/null
  bootout_if_loaded
  bootstrap_service
  # 某些 macOS 版本 bootstrap 后不会立刻兑现 RunAtLoad；普通 kickstart
  # 只会唤起未运行的 job，不会像 -k 那样杀掉刚启动的进程。
  launchctl kickstart "${SERVICE}" >/dev/null 2>&1 || true
  launchctl enable "${SERVICE}" >/dev/null 2>&1 || true
  wait_for_port
  echo "Installed and started ${LABEL}"
}

start_service() {
  local bootstrapped=0
  ensure_dirs
  if ! launchctl print "${SERVICE}" >/dev/null 2>&1; then
    if [[ ! -f "${TARGET_PLIST}" ]]; then
      install_service
      return
    fi
    bootstrap_service
    bootstrapped=1
  fi
  launchctl enable "${SERVICE}" >/dev/null 2>&1 || true
  # RunAtLoad 在 bootstrap 后偶尔不会即时启动；此时使用普通 kickstart
  # 确保唤起即可。只有已存在的 job 重启才使用 -k。
  if [[ "${bootstrapped}" -eq 1 ]]; then
    launchctl kickstart "${SERVICE}" >/dev/null 2>&1 || true
  else
    launchctl kickstart -k "${SERVICE}" >/dev/null 2>&1 || true
  fi
  wait_for_port
  echo "Started ${LABEL}"
}

stop_service() {
  bootout_if_loaded
  echo "Stopped ${LABEL}"
}

restart_service() {
  stop_service
  start_service
}

status_service() {
  if launchctl print "${SERVICE}" >/dev/null 2>&1; then
    echo "launchd: loaded (${SERVICE})"
    launchctl print "${SERVICE}" | sed -n '1,45p'
  else
    echo "launchd: not loaded (${SERVICE})"
  fi
  echo
  if lsof -nP -iTCP:8766 -sTCP:LISTEN; then
    :
  else
    echo "port 8766: not listening"
  fi
}

logs_service() {
  ensure_dirs
  touch "${LOG_DIR}/xhs-keyword-monitor.out.log" "${LOG_DIR}/xhs-keyword-monitor.err.log"
  tail -n 80 -f "${LOG_DIR}/xhs-keyword-monitor.out.log" "${LOG_DIR}/xhs-keyword-monitor.err.log"
}

uninstall_service() {
  stop_service
  rm -f "${TARGET_PLIST}"
  echo "Removed ${TARGET_PLIST}"
}

case "${1:-}" in
  install) install_service ;;
  start) start_service ;;
  stop) stop_service ;;
  restart) restart_service ;;
  status) status_service ;;
  logs) logs_service ;;
  uninstall) uninstall_service ;;
  *) usage; exit 2 ;;
esac
