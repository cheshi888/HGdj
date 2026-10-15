#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻看门狗：监视 daemon.py 与 player_server.py，崩溃自动重启。
- 每 15 秒检查两个子进程存活
- daemon 退出 → 自动重启（记录日志）
- player_server 端口失联 → 自动重启
保证 7x24 小时连续运行。

用法:
  python watchdog.py          # 前台运行（通常由 start.py 后台拉起）
  python watchdog.py --once   # 只启动/修复一次就退出（便于手动修复）
"""
import os
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
try:
    import panel_config as _cfg
    PORT = _cfg.panel_port()
except Exception:
    PORT = int(os.environ.get("PANEL_PORT", "8899"))
CHECK_SECONDS = 15
LOG_FILE = os.path.join(BASE, "watchdog.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def spawn(name, script):
    log("启动 %s ..." % name)
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE, script)],
        cwd=BASE, creationflags=CREATE_NO_WINDOW,
        env=dict(os.environ, PANEL_PORT=str(PORT)))


def main():
    once = len(sys.argv) > 1 and sys.argv[1] in ("--once", "-1")
    log("看门狗启动 (检查周期 %ds，端口 %d)" % (CHECK_SECONDS, PORT))
    daemon = None
    counter = 0

    while True:
        # --- 守护进程检查 ---
        if daemon is None or daemon.poll() is not None:
            if daemon is not None:
                log("daemon 已退出 (code=%s)，自动重启…" % daemon.poll())
            daemon = spawn("daemon", "daemon.py")
            time.sleep(3)

        # --- 播放服务器检查（端口兜底）---
        if not port_in_use(PORT):
            log("播放服务端口 %d 无响应，重启 player_server…" % PORT)
            spawn("player_server", "player_server.py")
            time.sleep(3)

        counter += 1
        if once and counter >= 1:
            break
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
