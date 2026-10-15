#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄果短剧常驻守护进程
- 后台启动播放/订阅服务 (player_server.py)
- 周期性：快速扫描新剧（前几页）+ 刷新已超时的播放地址（防签名过期）
- 播放健康巡检：滚动探测 m3u8 真实可播性，失效的剧集**立即重抓补全**
- 对前几页「老剧」做集数增量检测（老剧更新集数也能抓到）
- 每隔若干轮做一次全站全量扫描，确保不漏掉排在后面的新剧
- 单实例锁 + 每轮数量上限 + 硬超时，保证 7x24 稳定运行

环境变量:
  REFRESH_MINUTES      全量刷新间隔（分钟，默认 30）
  REFRESH_AGE_SECONDS  播放地址剩余有效期不足多少秒就重新抓（默认 1800，即剩 30 分钟）
  HEALTH_ENABLED       是否开启播放健康巡检（默认 1）
  HEALTH_MINUTES       播放巡检间隔（分钟，默认 10；比全量刷新更高频）
  HEALTH_BUDGET        每轮巡检时间预算秒数（默认 120）
  HEALTH_WORKERS       巡检并发数（默认 10）
  HEALTH_TIMEOUT       单个地址探测超时秒数（默认 8）
  DAEMON_TICK          调度轮询间隔秒数（默认 30）
  SCAN_LIMIT           每轮快速扫描最多翻多少页（默认 3）
  FULL_SCAN_EVERY      每多少轮做一次全量扫描（默认 12，约 6 小时）
  NEW_LIMIT            每轮最多爬多少部新剧（默认 20）
  ROUND_TIMEOUT        每轮硬超时（秒，默认 600，超时则跳过本轮）
  INCR_CHECK           每轮检查多少部老剧的新增集（默认 10）
  PANEL_PORT           播放服务端口（默认读 panel.conf，内置默认 8899）
  CRAWL_WORKERS        并发线程数（默认 12）
"""
import os
import socket
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import crawl_update_zuzu as cu

# 确保日志实时输出（systemd 下 stdout 默认块缓冲）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

try:
    import panel_config as _cfg
    PORT = _cfg.panel_port()
except Exception:
    PORT = int(os.environ.get("PANEL_PORT", "8899"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
REFRESH_AGE_SECONDS = int(os.environ.get("REFRESH_AGE_SECONDS", "1800"))
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "3"))
FULL_SCAN_EVERY = int(os.environ.get("FULL_SCAN_EVERY", "12"))
NEW_LIMIT = int(os.environ.get("NEW_LIMIT", "20"))
ROUND_TIMEOUT = int(os.environ.get("ROUND_TIMEOUT", "600"))
INCR_CHECK = int(os.environ.get("INCR_CHECK", "10"))
HEALTH_ENABLED = os.environ.get("HEALTH_ENABLED", "1") not in ("0", "false", "no")
HEALTH_MINUTES = int(os.environ.get("HEALTH_MINUTES", "10"))
HEALTH_BUDGET = int(os.environ.get("HEALTH_BUDGET", "120"))
HEALTH_WORKERS = int(os.environ.get("HEALTH_WORKERS", "10"))
HEALTH_TIMEOUT = int(os.environ.get("HEALTH_TIMEOUT", "8"))
TICK = int(os.environ.get("DAEMON_TICK", "30"))
LOCK_RETRY = int(os.environ.get("LOCK_RETRY", "180"))   # 抢不到锁时的退避秒数
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def refresh_once(limit=None):
    """一轮刷新：1)刷新过期流 2)老剧增量 3)新增剧（限制数量）。
    任何一步失败都不影响后续轮次。"""
    mode = "全量" if limit is None else ("前%d页" % limit)
    print("[%s] 开始刷新（%s扫描）…" % (time.strftime("%Y-%m-%d %H:%M:%S"), mode))

    # 1) 刷新过期播放地址（防签名过期，最优先）
    prog = cu.load_progress()
    items = prog.get("items", [])
    try:
        ok, err, skip = cu.refresh_streams(items, max_age=REFRESH_AGE_SECONDS)
        print("  [刷新] 成功 %d 集，失败 %d 集，跳过(未过期) %d 集" % (ok, err, skip))
    except Exception as e:
        print("  [刷新] 出错: %s" % str(e)[:100])
        ok = err = skip = 0
    cu.save_progress(prog)

    # 2) 收集当前站点剧集 id
    current, order = cu.collect_all_ids(limit)
    done = prog.get("done", {})

    # 3) 老剧集数增量检测（网站把更新过的剧排前面）
    if order:
        try:
            inc = cu.check_ep_increments(order, prog, limit=INCR_CHECK)
            if inc:
                print("  [增量] 老剧新增 %d 集" % inc)
            done = prog.get("done", {})
            items = prog.get("items", [])
        except Exception as e:
            print("  [增量] 出错: %s" % str(e)[:100])

    # 4) 爬新增剧（每轮限制 NEW_LIMIT 部，防止一轮跑几小时）
    todo_new = [d for d in order if d not in done]
    todo_retry = [d for d in order if d in done and "error" in done[d]]
    for it in items[:]:
        if "error" in it and it.get("id") in todo_retry:
            items.remove(it)
    todo = (todo_new + todo_retry)[:NEW_LIMIT]
    if todo:
        print("  [新增] 待爬 %d 部（本页发现 %d 部新剧，本轮限 %d 部）"
              % (len(todo), len(todo_new), NEW_LIMIT))
        for i, did in enumerate(todo, 1):
            try:
                done[did] = cu.crawl_one(did, current.get(did, ""), items)
                print("  [新增 %d/%d] %s 完成" % (i, len(todo), did))
            except cu.eh.ParseError as e:
                if "404" in str(e) or "Not Found" in str(e):
                    done[did] = {"gone": True}
                    print("  [新增 %d/%d] %s 已下架" % (i, len(todo), did))
                else:
                    done[did] = {"error": str(e)}
                    print("  [新增 %d/%d] %s 失败: %s" % (i, len(todo), did, str(e)[:60]))
            except Exception as e:
                done[did] = {"error": str(e)}
                print("  [新增 %d/%d] %s 失败: %s" % (i, len(todo), did, str(e)[:60]))
            cu.save_progress(prog)

    cu.save_progress(prog)
    cu.export(items)
    print("[%s] 本轮完成：新增 %d 部，刷新 %d 集，失败 %d 集"
          % (time.strftime("%H:%M:%S"), len(todo), ok, err))


def health_once():
    """一轮播放健康巡检：滚动探测真实可播性，失效的剧集立即重抓补全。
    独立于全量刷新、以更高频率运行，保证「随时检测、失效即补」。"""
    prog = cu.load_progress()
    items = prog.get("items", [])
    hr = cu.health_check(items, budget=HEALTH_BUDGET,
                         workers=HEALTH_WORKERS, timeout=HEALTH_TIMEOUT)
    if hr["checked"]:
        cu.save_progress(prog)
        if hr["repaired"] or hr["unresolved"]:
            cu.export(items)        # 有修复才重写订阅/数据文件
        print("[%s] [巡检] 抽查 %d 集，失效 %d，已补全 %d，仍失败 %d"
              % (time.strftime("%H:%M:%S"), hr["checked"], hr["bad"],
                 hr["repaired"], hr["unresolved"]))
    return hr


def run_round(full):
    """在线程中执行一轮并硬超时；超时只跳过本轮，daemon 不退出。
    返回：仍在运行的线程（本轮超时）或 None（已正常结束）。
    调用方据此决定爬虫锁的释放时机，避免超时线程仍在写进度时锁被提前释放。"""
    result = {}
    def worker():
        try:
            refresh_once(limit=None if full else SCAN_LIMIT)
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=ROUND_TIMEOUT)
    if t.is_alive():
        print("[%s] 本轮超时(>%ds)，跳过本轮，等待下个周期…"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), ROUND_TIMEOUT))
        return t
    if "err" in result:
        print("[%s] 本轮出错: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), result["err"][:150]))
    return None


def main():
    print("=" * 56)
    print("  黄果短剧 - 常驻守护 + 播放服务")
    print("=" * 56)
    if not port_in_use(PORT):
        print("[启动] 播放/订阅服务 (端口 %d)..." % PORT)
        _env = dict(os.environ, PANEL_PORT=str(PORT))
        subprocess.Popen([sys.executable, os.path.join(BASE, "player_server.py")],
                         cwd=BASE, creationflags=CREATE_NO_WINDOW, env=_env)
        time.sleep(2)
    else:
        print("[提示] 播放服务已在运行 (端口 %d)" % PORT)
    print("[守护] 每 %d 分钟全量刷新一次，签发超过 %d 秒的地址会重新抓取"
          % (REFRESH_MINUTES, REFRESH_AGE_SECONDS))
    if HEALTH_ENABLED and HEALTH_BUDGET > 0:
        print("[守护] 每 %d 分钟播放巡检一次（每轮 %d 秒 / %d 并发），失效剧集立即重抓补全"
              % (HEALTH_MINUTES, HEALTH_BUDGET, HEALTH_WORKERS))
    else:
        print("[守护] 播放健康巡检已关闭")
    print("[守护] 每轮上限 %d 部新剧，硬超时 %d 秒" % (NEW_LIMIT, ROUND_TIMEOUT))
    print("[面板] http://127.0.0.1:%d/" % PORT)
    print("[订阅] http://<本机IP>:%d/playlist.m3u8" % PORT)
    print("常驻运行中，按 Ctrl+C 退出。")
    print()
    counter = 0
    try:
        with open(os.path.join(BASE, ".daemon.pid"), "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    # 启动即做一次全量刷新与一次巡检（不必等第一个周期）
    last_round = 0.0
    last_health = 0.0
    backoff_until = 0.0
    while True:
        now = time.time()
        if now < backoff_until:
            time.sleep(TICK)
            continue
        due_round = (now - last_round) >= REFRESH_MINUTES * 60
        due_health = (HEALTH_ENABLED and HEALTH_BUDGET > 0
                      and (now - last_health) >= HEALTH_MINUTES * 60)
        if due_round or due_health:
            # 单实例锁：抢不到说明有手动爬虫在跑，跳过本次避免数据竞争
            if not cu.acquire_lock():
                print("[%s] 有其他爬虫实例运行中，%d 秒后重试"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), LOCK_RETRY))
                backoff_until = now + LOCK_RETRY
            else:
                stuck = None
                if due_health:
                    try:
                        health_once()
                    except Exception as e:
                        print("  [巡检] 出错: %s" % str(e)[:120])
                    last_health = time.time()
                if due_round:
                    counter += 1
                    full = (FULL_SCAN_EVERY > 0 and counter % FULL_SCAN_EVERY == 0)
                    stuck = run_round(full)
                    last_round = time.time()
                    last_health = last_round
                if stuck is None:
                    cu.release_lock()
                else:
                    # 超时线程仍在写进度：把锁交给它，跑完自行释放。
                    # 期间 acquire_lock 会失败 → 后续轮次自动跳过，避免数据竞争。
                    threading.Thread(target=lambda: (stuck.join(), cu.release_lock()),
                                     daemon=True).start()
        time.sleep(TICK)


if __name__ == "__main__":
    main()
