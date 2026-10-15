#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄豆短剧 APP 版增量爬虫（zuzucast / hdmgdj 系列站点）

替代已失效的 huangguoai.com 旧站爬虫（crawl_update.py），接口完全一致：
  load_progress / save_progress / acquire_lock / release_lock /
  collect_all_ids(limit) / check_ep_increments(ids, prog, limit) /
  crawl_one(did, title, items) / refresh_streams(items, max_age) / export(items)

数据流：
  1) 分页扫描 /drama/list（order=new/hot/update）收集全站剧 id
  2) 与 crawl_progress.json 对比：只爬「新增剧」+「失败重试」
  3) 每部剧：详情(1次) -> 免费集逐个取流(签名 m3u8)
  4) 播放健康巡检：滚动探测 m3u8 真实可播性，失效立即重抓补全
  5) 导出 all_playlist.m3u8 + all_streams.json（原子写）

首次运行会自动把旧站数据备份为 *.old-site.bak 再重建进度文件。

环境变量:
  ZUZU_JOBS          剧级并发数（默认 6，仅全量爬取时生效）
  ZUZU_PAGE_SIZE     扫描每页条数（默认 100）
  ZUZU_MAX_PAGES     全量扫描页数上限（默认 80）
  SCAN_CACHE_TTL     复用扫描结果的有效秒数（默认 1800，0=关闭）
  INCR_CHECK         每轮检查多少部老剧的新增集数
  HEALTH_BUDGET      每轮播放巡检的时间预算秒数（默认 120，0=关闭）
  HEALTH_WORKERS     巡检并发数（默认 10）
  HEALTH_TIMEOUT     单个地址探测超时秒数（默认 8）
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import zuzu_parser as zp
import error_handler as eh

ID_FILE = os.path.join(BASE, "all_ids.json")
PROG_FILE = os.path.join(BASE, "crawl_progress.json")
OUT_M3U = os.path.join(BASE, "all_playlist.m3u8")
OUT_JSON = os.path.join(BASE, "all_streams.json")
HEALTH_CURSOR = os.path.join(BASE, ".health_cursor.json")

WORKERS = int(os.environ.get("CRAWL_WORKERS", "8"))
PAGE_SIZE = int(os.environ.get("ZUZU_PAGE_SIZE", "100"))
MAX_PAGES = int(os.environ.get("ZUZU_MAX_PAGES", "80"))   # 全量扫描安全上限
LOCK_FILE = os.path.join(BASE, ".crawl.lock")
_lock_fd = None

# 扫描用的排序（new=最新, hot=热门, update=最近更新；多路扫描防漏）
SCAN_ORDERS = ["new", "hot", "update"]


# ---------------- 单实例锁（与旧爬虫共用 .crawl.lock） ----------------
def acquire_lock(blocking=False):
    global _lock_fd
    try:
        fd = open(LOCK_FILE, "a+")
        if fd.tell() == 0:
            fd.write("\0")
            fd.flush()
        fd.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fd.close()
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fd.close()
                return False
        _lock_fd = fd
        return True
    except Exception:
        try:
            if _lock_fd:
                _lock_fd.close()
        except Exception:
            pass
        return False


def release_lock():
    global _lock_fd
    try:
        if _lock_fd is not None:
            if os.name == "nt":
                import msvcrt
                _lock_fd.seek(0)
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
    except Exception:
        pass


# 首次切换到新站的初始化标记（防止重复启动时把进度反复备份掉）
INIT_MARK = os.path.join(BASE, ".zuzu_crawl_init")


def _backup_legacy():
    """首次切换到新站时，把旧站遗留数据备份为 *.old-site.bak（只做一次）"""
    if os.path.exists(INIT_MARK):
        return
    backed = False
    for f in (PROG_FILE, OUT_M3U, OUT_JSON):
        bak = f + ".old-site.bak"
        if os.path.exists(f) and not os.path.exists(bak):
            try:
                os.replace(f, bak)
                print("已备份旧站数据: %s -> %s" % (os.path.basename(f), os.path.basename(bak)))
                backed = True
            except Exception:
                pass
    try:
        with open(INIT_MARK, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    except Exception:
        pass


# ---------------- 进度存取 ----------------
def load_progress():
    for src in (PROG_FILE, PROG_FILE + ".bak"):
        if os.path.exists(src):
            try:
                with open(src, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return {"done": {}, "items": []}


def _replace_retry(src, dst, attempts=12, base_delay=0.25):
    """Windows 下 os.replace 可能因目标文件被瞬时占用（杀软扫描 / 索引 / 其它进程读取）
    抛 WinError 5「拒绝访问」。带指数退避重试 + 兜底直写，保证 7x24 常驻不因一次写失败崩掉。"""
    last = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except OSError as e:
            last = e
            time.sleep(base_delay * (1 + i))
    try:
        with open(src, encoding="utf-8") as f:
            data = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(data)
        try:
            os.remove(src)
        except OSError:
            pass
        return True
    except OSError as e:
        print("警告: 无法写入 %s（%s / %s），本轮跳过该写操作" % (dst, last, e))
        return False


def save_progress(prog):
    tmp = PROG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False)
    # 保留上一版为 .bak：写入中断/文件损坏时可由 load_progress 自动恢复
    if os.path.exists(PROG_FILE):
        try:
            with open(PROG_FILE, "rb") as fr, open(PROG_FILE + ".bak", "wb") as fw:
                fw.write(fr.read())
        except OSError:
            pass
    _replace_retry(tmp, PROG_FILE)


def export(items):
    lines = ["#EXTM3U"]
    for it in items:
        if "stream" in it:
            lines.append('#EXTINF:-1 group-title="黄豆短剧",%s 第%s集'
                         % (it.get("title", ""), it.get("ep", "")))
            lines.append(it["stream"])
    with open(OUT_M3U + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _replace_retry(OUT_M3U + ".tmp", OUT_M3U)
    with open(OUT_JSON + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
    _replace_retry(OUT_JSON + ".tmp", OUT_JSON)
    ok = sum(1 for i in items if "stream" in i)
    print("已导出: %s / %s (%d 条流)" % (os.path.basename(OUT_M3U),
                                        os.path.basename(OUT_JSON), ok))


def parallel_map(fn, args, workers=None):
    args = list(args)
    if not args:
        return []
    w = workers or WORKERS
    w = max(1, min(w, len(args)))
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, args))


# ---------------- 扫描 ----------------
def _scan_pages(order, max_pages, page_size=PAGE_SIZE):
    """顺序翻页拉列表，返回 [(id, title), ...]（遇空页/重复页即停）"""
    out = []
    seen_pages = set()
    for pg in range(1, max_pages + 1):
        try:
            cards = zp.list_dramas(page=pg, page_size=page_size, order=order)
        except Exception as e:
            print("  扫描 %s 第%d页异常，停止该排序: %s" % (order, pg, str(e)[:60]))
            break
        if not cards:
            break
        sig = tuple(c["id"] for c in cards)
        if sig in seen_pages:   # 到底了（服务端页码上限后返回重复内容）
            break
        seen_pages.add(sig)
        out.extend((c["id"], c["title"]) for c in cards if c["id"])
        print("  扫描 order=%s 第%d页: +%d (累计 %d)" % (order, pg, len(cards), len(out)))
        if len(cards) < page_size:
            break
    return out


def _load_scan_cache(ttl):
    """复用最近的扫描结果，避免重启后重复扫描（默认 30 分钟内有效）"""
    if ttl <= 0:
        return None, None
    try:
        with open(ID_FILE, encoding="utf-8") as f:
            d = json.load(f)
        age = time.time() - float(d.get("ts", 0))
        ids = d.get("ids") or []
        titles = d.get("titles") or {}
        if ids and 0 <= age < ttl:
            print("复用 %.1f 分钟前的扫描缓存（跳过全站扫描）" % (age / 60.0))
            return titles, ids
    except Exception:
        pass
    return None, None


def _save_scan_cache(titles, ids):
    try:
        with open(ID_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"ids": ids, "titles": titles, "ts": time.time()},
                      f, ensure_ascii=False)
        _replace_retry(ID_FILE + ".tmp", ID_FILE)
    except OSError as e:
        print("警告: 写入扫描缓存失败: %s" % e)


def collect_all_ids(limit=None):
    """扫描全站收集剧 id。
    limit：每个排序最多扫多少页（None=全量；快速检查传 1~3）。"""
    orders = ["new"] if limit else SCAN_ORDERS
    max_pages = max(1, limit) if limit else MAX_PAGES
    seen, order = {}, []
    for od in orders:
        for did, title in _scan_pages(od, max_pages):
            if did and did not in seen:
                seen[did] = title
                order.append(did)
    return seen, order


# ---------------- 爬取 ----------------
def crawl_one(did, title, items, delay=0.2):
    return zp.crawl_one(did, title, items, delay=delay)


def check_ep_increments(ids, prog, limit=15):
    """检查已爬过的剧是否有新集（只查排在前面的，通常是最近更新的）"""
    if not ids:
        return 0
    done = prog.get("done", {})
    items = prog.get("items", [])
    candidates = [d for d in ids
                  if d in done and isinstance(done[d], dict)
                  and "error" not in done[d] and "gone" not in done[d]
                  and done[d].get("eps", 0) > 0][:limit]
    if not candidates:
        return 0

    def probe(did):
        try:
            d = zp.get_detail(did)
            eps = d.get("episodes") or []
            existing = {str(it["ep"]) for it in items
                        if str(it.get("id")) == str(did) and it.get("stream")}
            new_eps = [e for e in eps
                       if str(e["ep"]) not in existing and not e.get("locked")]
            if not new_eps:
                return None
            title = d.get("title") or done[did].get("title") or did
            return (did, title, new_eps, d.get("episode_count") or len(eps))
        except Exception:
            return None

    added = 0
    for r in parallel_map(probe, candidates, workers=min(len(candidates), 6)):
        if not r:
            continue
        did, title, new_eps, total_eps = r

        def fetch_ep(e):
            try:
                s = zp.get_stream(did, e["ep"])
                return {"id": did, "title": title, "ep": e["ep"],
                        "label": e["label"], "url": e["url"], "stream": s}
            except Exception as ex:
                return {"id": did, "title": title, "ep": e["ep"],
                        "label": e["label"], "url": e["url"], "error": str(ex)}

        items.extend(parallel_map(fetch_ep, new_eps, workers=min(len(new_eps), 6)))
        done[did] = {"title": title, "eps": total_eps}
        added += len(new_eps)
        print("  [增量] %s: +%d 新集" % (did, len(new_eps)))
        time.sleep(0.2)
    if added:
        save_progress(prog)
    return added


# ---------------- 刷新 ----------------
def refresh_streams(items, max_age=1800):
    """并行重新抓取即将过期的播放地址（签名 URL 带 exp，约6小时有效期）。
    max_age：剩余有效期低于该秒数就刷新（daemon 默认 1800=剩半小时内刷新）"""
    todo = []
    for it in items:
        if not it.get("url") or not it.get("stream"):
            continue
        remain = zp.stream_age(it.get("stream"))
        if remain is not None and remain >= max_age:
            continue
        todo.append(it)

    def do(it):
        try:
            it["stream"] = zp.get_stream(it["id"], it["ep"], force_refresh=True)
            it.pop("error", None)
            return "ok"
        except Exception as ex:
            it["error"] = str(ex)
            return "err"

    results = parallel_map(do, todo)
    return results.count("ok"), results.count("err"), len(items) - len(todo)


# ---------------- 播放健康巡检（失效即时补全） ----------------
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

PROBE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def probe_stream(url, timeout=8):
    """探测播放地址是否真能播：GET m3u8，要求 HTTP 200 且响应含 #EXTM3U。
    返回 (ok: bool, detail: str)。"""
    if not url:
        return False, "empty"
    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(url, headers={
        "User-Agent": PROBE_UA,
        "Referer": "https://%s/" % host if host else "",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            if r.status != 200:
                return False, "http%d" % r.status
            head = r.read(512).decode("utf-8", "replace")
            if "#EXTM3U" not in head:
                return False, "not-hls"
            return True, "ok"
    except urllib.error.HTTPError as e:
        return False, "http%d" % e.code
    except Exception as e:
        return False, type(e).__name__


def _load_health_cursor():
    try:
        with open(HEALTH_CURSOR, encoding="utf-8") as f:
            return int(json.load(f).get("i", 0))
    except Exception:
        return 0


def _save_health_cursor(i):
    try:
        with open(HEALTH_CURSOR + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"i": int(i), "ts": time.time()}, f)
        _replace_retry(HEALTH_CURSOR + ".tmp", HEALTH_CURSOR)
    except OSError:
        pass


def health_check(items, budget=120, workers=10, timeout=8, repair=True):
    """在时间预算内滚动巡检播放地址，游标持久化 → 跨轮次连续覆盖全量（不重复扫同一批）。
    - 探测到失效的 → **立即**重新取流补全（force_refresh）
    - 补全后复核真实可播性；仍不通 → 标记 error，下轮继续尝试
    返回 dict(checked, bad, repaired, unresolved)"""
    pool = [it for it in items if it.get("stream")]
    if not pool:
        return {"checked": 0, "bad": 0, "repaired": 0, "unresolved": 0}
    n = len(pool)
    idx = _load_health_cursor() % n
    t0 = time.time()
    checked = bad = repaired = unresolved = 0
    chunk = max(workers * 4, 40)

    while checked < n:
        take = min(chunk, n - checked)
        batch = [pool[(idx + k) % n] for k in range(take)]

        def _check(it):
            ok, _ = probe_stream(it.get("stream"), timeout=timeout)
            return ok

        flags = parallel_map(_check, batch, workers=workers)
        broken = [it for it, ok in zip(batch, flags) if not ok]
        checked += take
        idx = (idx + take) % n

        if broken:
            bad += len(broken)
            if repair:
                def _fix(it):
                    try:
                        s = zp.get_stream(it["id"], it["ep"], force_refresh=True)
                    except Exception as ex:
                        it["error"] = str(ex)
                        return False
                    if not s:
                        it["error"] = "no-stream"
                        return False
                    it["stream"] = s
                    ok, detail = probe_stream(s, timeout=timeout)
                    if ok:
                        it.pop("error", None)
                        return True
                    it["error"] = "unplayable:" + detail
                    return False

                res = parallel_map(_fix, broken, workers=min(workers, len(broken)))
                repaired += sum(1 for x in res if x)
                unresolved += sum(1 for x in res if not x)
            else:
                unresolved += len(broken)

        if time.time() - t0 >= budget:
            break

    _save_health_cursor(idx)
    return {"checked": checked, "bad": bad, "repaired": repaired,
            "unresolved": unresolved}


# ---------------- 主流程 ----------------
def _main_inner():
    _backup_legacy()
    ttl = int(os.environ.get("SCAN_CACHE_TTL", "1800"))
    current, order = _load_scan_cache(ttl)
    if not order:
        print("扫描全站列表页，收集当前剧集…")
        current, order = collect_all_ids()
        _save_scan_cache(current, order)
    print("当前全站剧集总数: %d" % len(order))

    prog = load_progress()
    done = prog.get("done", {})
    items = prog.get("items", [])

    todo_new, todo_retry = [], []
    for did in order:
        if did not in done:
            todo_new.append(did)
        elif "error" in done[did]:
            todo_retry.append(did)
        elif "gone" in done[did]:
            pass
    todo = todo_new + todo_retry

    print("已成功爬取: %d 部" % sum(1 for v in done.values() if "error" not in v))
    print("待爬取: 新增 %d 部, 失败重试 %d 部" % (len(todo_new), len(todo_retry)))

    if order:
        inc = check_ep_increments(order, prog, limit=int(os.environ.get("INCR_CHECK", "15")))
        if inc:
            print("老剧增量: 新增 %d 集" % inc)
        done = prog.get("done", {})
        items = prog.get("items", [])

    if not todo:
        print("无新增内容，全部已是最新。仅刷新导出。")
        export(items)
        return

    for it in items[:]:
        if "error" in it and it.get("id") in todo_retry:
            items.remove(it)

    total = len(todo)
    jobs = max(1, int(os.environ.get("ZUZU_JOBS", "6")))
    print("开始爬取 %d 部（剧级并发 %d）…" % (total, jobs))
    counter = {"i": 0}

    def work(did):
        try:
            r = crawl_one(did, current.get(did, ""), items)
            return did, r, None
        except Exception as e:
            return did, None, str(e)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for did, r, err in ex.map(work, todo):
            counter["i"] += 1
            i = counter["i"]
            if err:
                done[did] = {"error": err}
                print("[%d/%d] %s 失败: %s" % (i, total, did, err[:60]))
            else:
                done[did] = r
                print("[%d/%d] %s 完成" % (i, total, did))
            try:
                save_progress(prog)
                # 周期性导出：长任务中途面板/订阅也有数据可用
                if i % 200 == 0:
                    export(items)
            except Exception as e:
                # 存档/导出失败不中断整轮爬取（数据仍在内存，下轮会重写）
                print("警告: 存档/导出异常已忽略: %s" % (str(e)[:120]))

    _save_scan_cache(current, order)
    export(items)
    print("增量更新完成。")


def main():
    if not acquire_lock():
        print("已有另一个爬虫实例在运行（锁: %s），本次退出。" % LOCK_FILE)
        return
    try:
        _main_inner()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
