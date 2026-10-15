#!/usr/bin/env bash
# ============================================================
#  黄果短剧 — Linux VPS 部署脚本
#
#  用法:
#    sudo bash deploy.sh              # 一键部署（systemd 优先，无 systemd 自动降级 nohup）
#    sudo bash deploy.sh restart      # 重启服务
#    sudo bash deploy.sh stop         # 停止服务
#    sudo bash deploy.sh status       # 查看运行状态
#    bash deploy.sh info              # 只打印访问地址（无需 root）
#
#  可选环境变量:
#    PANEL_PORT   面板端口（默认读 panel.conf，再默认 8899）
#    FORCE_INIT   systemd | nohup    强制指定托管方式（默认自动探测）
#    DRY_RUN=1    只打印将要执行的动作，不改动系统（用于自检）
#    SKIP_ROOT=1  跳过 root 检查（仅测试用）
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="huangguo"
DEFAULT_PORT=8899
PYTHON_BIN=""
FORCE_INIT="${FORCE_INIT:-auto}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_ROOT="${SKIP_ROOT:-0}"
LOGDIR="${APP_DIR}/logs"

# ---------- 端口：环境变量 > panel.conf > 默认 ----------
CONF_PORT=""
if [ -f "${APP_DIR}/panel.conf" ]; then
  CONF_PORT="$(sed -n 's/^[[:space:]]*PANEL_PORT[[:space:]]*=[[:space:]]*\([0-9]\{1,5\}\).*/\1/p' \
    "${APP_DIR}/panel.conf" 2>/dev/null | tail -n1 || true)"
fi
PORT="${PANEL_PORT:-${CONF_PORT:-$DEFAULT_PORT}}"
case "${PORT}" in (*[!0-9]*|"") PORT="${DEFAULT_PORT}" ;; esac

log()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# ---------- 通用工具 ----------
detect_ip() {
  local ip=""
  if command -v curl >/dev/null 2>&1; then
    for u in "https://api.ipify.org" "https://ifconfig.me" "https://ip.sb"; do
      ip="$(curl -4 -fsS --max-time 5 "$u" 2>/dev/null || true)"
      case "${ip}" in
        *[!0-9.]*|"") ip="" ;;
        *) break ;;
      esac
    done
  fi
  if [ -z "${ip}" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  [ -z "${ip}" ] && ip="<服务器公网IP>"
  printf '%s' "${ip}"
}

print_info() {
  local ip; ip="$(detect_ip)"
  log ""
  log "  ================= 黄果短剧 ==================="
  log "   播放面板 : http://${ip}:${PORT}/"
  log "   订阅地址 : http://${ip}:${PORT}/playlist.m3u8"
  log "  ============================================="
  log ""
  log "   常用指令："
  log "     查看状态 : systemctl status ${SERVICE_NAME}"
  log "     实时日志 : journalctl -u ${SERVICE_NAME} -f"
  log "     重启服务 : sudo bash ${APP_DIR}/deploy.sh restart"
  log "     停止服务 : sudo bash ${APP_DIR}/deploy.sh stop"
  log "     更新代码 : curl -fsSL https://raw.githubusercontent.com/cheshi888/HGdj/main/install.sh | sudo bash"
  log "     改端口   : 编辑 ${APP_DIR}/panel.conf 的 PANEL_PORT 后 restart"
  log ""
}

need_root() {
  [ "${SKIP_ROOT}" = "1" ] && return 0
  [ "${DRY_RUN}" = "1" ] && return 0
  if [ "$(id -u)" -ne 0 ]; then
    warn "[错误] 需要 root 权限，请用: sudo bash deploy.sh"
    exit 1
  fi
}

find_python() {
  for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$p")"; return 0; fi
  done
  warn "[错误] 未找到 Python3，请先安装："
  warn "  Debian/Ubuntu: apt-get update && apt-get install -y python3"
  warn "  CentOS/RHEL  : yum install -y python3"
  warn "  Alpine       : apk add python3"
  exit 1
}

detect_init() {
  if [ "${FORCE_INIT}" != "auto" ]; then printf '%s' "${FORCE_INIT}"; return 0; fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf 'systemd'; return 0
  fi
  printf 'nohup'
}

port_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -lptnH "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:${PORT}" 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser "${PORT}/tcp" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  fi
}

stop_service() {
  log "[停止] 停止服务 ..."
  if command -v systemctl >/dev/null 2>&1 && [ "$(detect_init)" = "systemd" ]; then
    systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
  for pf in .watchdog.pid .daemon.pid .player.pid; do
    if [ -f "${APP_DIR}/${pf}" ]; then
      local pid; pid="$(cat "${APP_DIR}/${pf}" 2>/dev/null || true)"
      case "${pid}" in (*[!0-9]*|"") ;; *) kill "${pid}" >/dev/null 2>&1 || true ;; esac
      rm -f "${APP_DIR}/${pf}"
    fi
  done
  sleep 1
  local left; left="$(port_pids || true)"
  if [ -n "${left}" ]; then
    for pid in ${left}; do kill "${pid}" >/dev/null 2>&1 || true; done
    sleep 1
    left="$(port_pids || true)"
    [ -n "${left}" ] && { for pid in ${left}; do kill -9 "${pid}" >/dev/null 2>&1 || true; done; }
  fi
  log "  已停止。"
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
    log "  [防火墙] 已放行 ufw 端口 ${PORT}/tcp"
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --add-port="${PORT}/tcp" --permanent >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    log "  [防火墙] 已放行 firewalld 端口 ${PORT}/tcp"
  fi
  if command -v iptables >/dev/null 2>&1 && ! command -v ufw >/dev/null 2>&1 \
     && ! command -v firewall-cmd >/dev/null 2>&1; then
    iptables -C INPUT -p tcp --dport "${PORT}" -j ACCEPT >/dev/null 2>&1 \
      || iptables -I INPUT -p tcp --dport "${PORT}" -j ACCEPT >/dev/null 2>&1 || true
    log "  [防火墙] 已放行 iptables 端口 ${PORT}/tcp"
  fi
}

health_check() {
  if [ "${DRY_RUN}" = "1" ]; then
    log "[自检] dry-run 模式：跳过实际健康检查（未真正启动服务）"
    return 0
  fi
  log "[自检] 等待面板就绪 (http://127.0.0.1:${PORT}/health) ..."
  local i=0 body=""
  while [ "${i}" -lt 30 ]; do
    if command -v curl >/dev/null 2>&1; then
      body="$(curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
    elif [ -n "${PYTHON_BIN}" ]; then
      body="$("${PYTHON_BIN}" - "$PORT" <<'PY' 2>/dev/null || true
import sys, urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:%s/health" % sys.argv[1], timeout=3).read().decode())
except Exception:
    pass
PY
)"
    fi
    if [ -n "${body}" ]; then log "  [OK] 面板已就绪：${body}"; return 0; fi
    i=$((i+1)); sleep 1
  done
  warn "  [警告] 面板暂未就绪。排查：systemctl status ${SERVICE_NAME} && journalctl -u ${SERVICE_NAME} -f"
  return 1
}

install_systemd() {
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  log "[部署] 托管方式：systemd（${unit}）"
  if [ "${DRY_RUN}" = "1" ]; then
    log "  [dry-run] 写入 ${unit} 并 systemctl daemon-reload/enable/restart"
    return 0
  fi
  cat > "${unit}" <<EOF
[Unit]
Description=Huangguo Drama Player & Daemon (watchdog)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/watchdog.py
Restart=always
RestartSec=10
Environment=PANEL_PORT=${PORT}
Environment=PYTHONUNBUFFERED=1
Environment=REFRESH_MINUTES=30
Environment=REFRESH_AGE_SECONDS=1800
Environment=HEALTH_ENABLED=1
Environment=HEALTH_MINUTES=10
Environment=SCAN_LIMIT=3
Environment=FULL_SCAN_EVERY=12
Environment=NEW_LIMIT=20
Environment=ROUND_TIMEOUT=600
Environment=INCR_CHECK=10
Environment=CRAWL_WORKERS=12

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${SERVICE_NAME}"
}

install_nohup() {
  log "[部署] 托管方式：nohup 常驻（未检测到 systemd）"
  if [ "${DRY_RUN}" = "1" ]; then
    log "  [dry-run] nohup ${PYTHON_BIN} watchdog.py & 并写入 cron @reboot"
    return 0
  fi
  mkdir -p "${LOGDIR}"
  ( cd "${APP_DIR}" && PANEL_PORT="${PORT}" nohup "${PYTHON_BIN}" watchdog.py \
      >> "${LOGDIR}/watchdog.out" 2>&1 & echo $! > "${APP_DIR}/.watchdog.pid" ) || true
  if command -v crontab >/dev/null 2>&1; then
    local line="@reboot cd ${APP_DIR} && PANEL_PORT=${PORT} ${PYTHON_BIN} ${APP_DIR}/watchdog.py >> ${LOGDIR}/watchdog.out 2>&1"
    ( crontab -l 2>/dev/null | grep -v "${APP_DIR}/watchdog.py" || true ; printf '%s\n' "${line}" ) \
      | crontab - 2>/dev/null || true
    log "  [自启] 已写入 crontab @reboot"
  else
    warn "  [提示] 无 crontab，重启服务器后请手动执行 deploy.sh restart"
  fi
}

# ============================ 主流程 ============================
SUB="${1:-deploy}"

case "${SUB}" in
  info)    print_info; exit 0 ;;
  stop)    need_root; stop_service; exit 0 ;;
  status)
    if command -v systemctl >/dev/null 2>&1; then systemctl status "${SERVICE_NAME}" --no-pager || true; fi
    log "监听端口 ${PORT}："; netstat -lntp 2>/dev/null | grep ":${PORT}" || ss -lntp 2>/dev/null | grep ":${PORT}" || log "  （无监听）"
    exit 0 ;;
  restart)
    need_root; find_python; stop_service; sleep 1
    log "[重启] 重新拉起 ..."
    if [ "$(detect_init)" = "systemd" ]; then
      systemctl restart "${SERVICE_NAME}" 2>/dev/null || { install_systemd; }
    else
      install_nohup
    fi
    health_check || true; print_info; exit 0 ;;
esac

need_root
find_python
log "=============================================="
log "  黄果短剧 — VPS 部署"
log "=============================================="
log "[1/5] Python: $("${PYTHON_BIN}" --version 2>&1)"
log "[2/5] 工作目录: ${APP_DIR}"
if [ -n "${PANEL_PORT:-}" ]; then _src="环境变量 PANEL_PORT"; else _src="panel.conf 或默认值"; fi
log "[3/5] 端口: ${PORT}  (来源: ${_src})"

if [ ! -f "${APP_DIR}/player_server.py" ] || [ ! -f "${APP_DIR}/watchdog.py" ]; then
  warn "[错误] 项目文件不完整（缺少 player_server.py / watchdog.py），请确认在项目目录内运行。"
  exit 1
fi

log "[4/5] 启动服务 ..."
stop_service >/dev/null 2>&1 || true
case "$(detect_init)" in
  systemd) install_systemd ;;
  *)       install_nohup ;;
esac
open_firewall

log "[5/5] 部署完成，开始自检 ..."
health_check || true
print_info
log "  提示：端口 ${PORT} 需在云厂商「安全组」放行后方可公网访问。"
log ""
