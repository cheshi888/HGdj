#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄果短剧 - 通用一键启动脚本

用法:
  python start.py           启动常驻守护（看门狗 + 播放服务 + 定时刷新 + 自动抓新剧）+ 打开浏览器
  python start.py update    手动增量更新（只爬新增，不重复）
  python start.py stop      停止所有后台进程（watchdog/daemon/player）
"""
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    import panel_config as _cfg
    PORT = _cfg.panel_port()
except Exception:
    PORT = int(os.environ.get("PANEL_PORT", "8899"))
WATCHDOG_PID_FILE = os.path.join(BASE, ".watchdog.pid")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def watchdog_alive():
    """通过 PID 文件 + 端口双检查 watchdog 是否在跑"""
    try:
        with open(WATCHDOG_PID_FILE) as f:
            pid = int(f.read().strip() or "0")
        if pid:
            os.kill(pid, 0)  # 抛异常说明进程不存在
            return True
    except (OSError, ValueError):
        pass
    return False


def start_daemon():
    os.chdir(BASE)
    print("=" * 56)
    print("  黄果短剧 - 一键启动（常驻守护）")
    print("=" * 56)
    if watchdog_alive():
        print("[提示] 看门狗已在运行，服务保持常驻（端口 %d）" % PORT)
    else:
        print("[启动] 看门狗（自动拉起播放服务+守护进程，崩溃自动重启）...")
        proc = subprocess.Popen([sys.executable, "watchdog.py"],
                                cwd=BASE, creationflags=CREATE_NO_WINDOW,
                                env=dict(os.environ, PANEL_PORT=str(PORT)))
        try:
            with open(WATCHDOG_PID_FILE, "w") as f:
                f.write(str(proc.pid))
        except Exception:
            pass
        time.sleep(4)
        if port_in_use(PORT):
            print("[启动] 播放/订阅服务已就绪 (端口 %d)" % PORT)
        else:
            print("[警告] 播放服务暂未就绪，看门狗将自动拉起，稍后刷新即可")
    url = "http://127.0.0.1:%d/" % PORT
    print("[打开] 浏览器 -> %s" % url)
    webbrowser.open(url)
    print()
    print("  播放面板  : %s" % url)
    print("  订阅地址  : http://%s:%d/playlist.m3u8" % (lan_ip(), PORT))
    print("  端口配置  : panel.conf 里的 PANEL_PORT（当前 %d），改完重启生效" % PORT)
    print("  看门狗    : 崩溃自动重启，保证 7x24 运行")
    print("  手动更新  : python start.py update")
    print("  全部停止  : python start.py stop")
    print()


def kill_port(port):
    """兜底：干掉仍占用该端口的进程（PID 文件丢失/换端口后的残留）"""
    killed = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 errors="ignore").stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    if parts[1].endswith(":%d" % port):
                        pid = int(parts[4])
                        if pid > 0:
                            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                           capture_output=True)
                            killed.append(pid)
        else:
            out = subprocess.run(["bash", "-c",
                                  "lsof -ti tcp:%d 2>/dev/null || fuser %d/tcp 2>/dev/null"
                                  % (port, port)], capture_output=True, text=True).stdout
            for tok in out.split():
                pid = int("".join(ch for ch in tok if ch.isdigit()) or 0)
                if pid > 0:
                    os.kill(pid, signal.SIGTERM)
                    killed.append(pid)
    except Exception:
        pass
    if killed:
        print("  已释放端口 %d (pid %s)" % (port, ",".join(map(str, killed))))


def stop_all():
    """停止 watchdog/daemon/player_server（通过 PID 文件精确定位）"""
    print("正在停止全部后台进程...")
    pid_files = [WATCHDOG_PID_FILE,
                 os.path.join(BASE, ".daemon.pid"),
                 os.path.join(BASE, ".player.pid")]
    # 先停 watchdog（防止它自动拉起下面的进程）
    if os.path.exists(pid_files[0]):
        try:
            with open(pid_files[0]) as f:
                pid = int(f.read().strip() or "0")
            if pid:
                os.kill(pid, signal.SIGTERM)
                print("  已停止 watchdog (pid %d)" % pid)
        except (OSError, ValueError):
            pass
    time.sleep(1)
    for pf in pid_files[1:]:
        try:
            if os.path.exists(pf):
                with open(pf) as f:
                    pid = int(f.read().strip() or "0")
                if pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        print("  已停止 %s (pid %d)" % (os.path.basename(pf), pid))
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass
    for pf in pid_files:
        try:
            if os.path.exists(pf):
                os.remove(pf)
        except Exception:
            pass
    time.sleep(1)
    # 兜底：PID 文件可能已丢失，或进程还占着端口（例如刚换过端口）
    kill_port(PORT)
    if PORT != 8788:
        kill_port(8788)   # 清掉换端口前遗留的旧面板
    print("完成。")


def run_update():
    os.chdir(BASE)
    print("正在增量更新（重新扫描全站，只爬新增，不重复）...")
    print()
    subprocess.call([sys.executable, "crawl_update_zuzu.py"], cwd=BASE)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("update", "-u", "--update"):
        run_update()
        input("\n更新完成，按回车键关闭...")
    elif len(sys.argv) > 1 and sys.argv[1] in ("stop", "-s", "--stop"):
        stop_all()
        input("按回车键关闭...")
    else:
        start_daemon()
        input("按回车键退出（后台服务保持常驻）...")
