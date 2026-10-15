#!/usr/bin/env bash
# ============================================================
#  黄果短剧 — 一键安装 / 更新脚本（Linux VPS）
#
#  推荐用法（一条命令，全新安装或更新都适用）:
#    curl -fsSL https://raw.githubusercontent.com/cheshi888/HGdj/main/install.sh | sudo bash
#
#  可选环境变量:
#    REPO         仓库（默认 cheshi888/HGdj）
#    BRANCH       分支（默认 main）
#    APP_DIR      安装目录（默认 /opt/HD）
#    GITHUB_PROXY 镜像前缀，GitHub 直连失败时使用（例: https://ghfast.top/）
#    PANEL_PORT   面板端口（传给 deploy.sh）
#    DRY_RUN=1    只打印将执行的动作，不改动系统
# ============================================================
set -euo pipefail

REPO="${REPO:-cheshi888/HGdj}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/HD}"
GITHUB_PROXY="${GITHUB_PROXY:-}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# ---------- 0) root 检查 ----------
SKIP_ROOT="${SKIP_ROOT:-0}"
if [ "${DRY_RUN}" != "1" ] && [ "${SKIP_ROOT}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
  warn "[错误] 需要 root 权限。请这样运行："
  warn "  curl -fsSL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh | sudo bash"
  exit 1
fi

log "=============================================="
log "  黄果短剧 — 一键安装"
log "=============================================="

# ---------- 1) 安装依赖 git / python3 ----------
log "[1/4] 安装依赖 git / python3 ..."
if [ "${DRY_RUN}" != "1" ]; then
  export DEBIAN_FRONTEND=noninteractive
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y git python3 ca-certificates >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git python3 >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git python3 >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git python3 >/dev/null 2>&1 || true
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm git python >/dev/null 2>&1 || true
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install git python3 >/dev/null 2>&1 || true
  else
    warn "[警告] 未识别的包管理器，跳过自动安装，请确保已装 git 与 python3"
  fi
fi

command -v git >/dev/null 2>&1 || { warn "[错误] 未找到 git，请手动安装后重试"; exit 1; }
PY=""
for p in python3 python; do command -v "$p" >/dev/null 2>&1 && { PY="$(command -v "$p")"; break; }; done
[ -n "${PY}" ] || { warn "[错误] 未找到 python3，请手动安装后重试"; exit 1; }
log "      依赖就绪：git $(git --version 2>&1 | awk '{print $3}') / $("${PY}" --version 2>&1)"

# ---------- 2) 拉取 / 更新代码 ----------
log "[2/4] 拉取代码 ${REPO}@${BRANCH} -> ${APP_DIR} ..."
GIT_URL="https://github.com/${REPO}.git"

try_urls() {
  printf '%s\n' "${GIT_URL}"
  [ -n "${GITHUB_PROXY}" ] && printf '%s\n' "${GITHUB_PROXY}${GIT_URL}"
  printf '%s\n' "https://ghfast.top/${GIT_URL}"
  printf '%s\n' "https://ghproxy.net/${GIT_URL}"
}

if [ "${DRY_RUN}" = "1" ]; then
  log "      [dry-run] git clone/update ${REPO}@${BRANCH} -> ${APP_DIR}"
else
  if [ -d "${APP_DIR}/.git" ]; then
    log "      已存在仓库，更新到最新 ..."
    git -C "${APP_DIR}" remote set-url origin "${GIT_URL}" 2>/dev/null || true
    if ! git -C "${APP_DIR}" fetch --depth=1 origin "${BRANCH}"; then
      warn "[警告] git fetch 失败（网络？），沿用本地现有代码继续部署"
    else
      git -C "${APP_DIR}" checkout -B "${BRANCH}" "origin/${BRANCH}" >/dev/null 2>&1 \
        || git -C "${APP_DIR}" reset --hard "origin/${BRANCH}"
      git -C "${APP_DIR}" clean -fd >/dev/null 2>&1 || true
    fi
  else
    rm -rf "${APP_DIR}"
    mkdir -p "$(dirname "${APP_DIR}")"
    ok=0
    for u in $(try_urls); do
      log "      git clone --depth=1 -b ${BRANCH} ${u}"
      if git clone --depth=1 --branch "${BRANCH}" "${u}" "${APP_DIR}"; then ok=1; break; fi
      warn "      该地址失败，尝试下一个 ..."
      rm -rf "${APP_DIR}"
    done
    if [ "${ok}" != "1" ]; then
      warn "[错误] 代码拉取失败。可指定镜像后重试，例如："
      warn "  GITHUB_PROXY=https://ghfast.top/ curl -fsSL .../install.sh | sudo bash"
      exit 1
    fi
  fi
fi

# ---------- 3) 部署 ----------
log "[3/4] 执行部署 ..."
if [ "${DRY_RUN}" = "1" ]; then
  DRY_RUN=1 bash "${APP_DIR}/deploy.sh"
else
  bash "${APP_DIR}/deploy.sh"
fi

# ---------- 4) 完成 ----------
log "[4/4] 全部完成。"
