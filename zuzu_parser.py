#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄豆短剧 APP API 解析器（zuzucast / hdmgdj 系列站点）

基于 zuzu_client.py 的加密协议客户端，提供与 huangguo_parser.py 对齐的
归一化接口（列表/详情/取流/批量导出），供 crawl_update / player_server 复用。

用法示例：
  python zuzu_parser.py list                       # 最新剧集
  python zuzu_parser.py list --order hot --page 2  # 热门第2页
  python zuzu_parser.py search 重生                 # 搜索
  python zuzu_parser.py detail 6a583d66b2283e06d44f5f5b   # 详情+集数
  python zuzu_parser.py play 6a583d66b2283e06d44f5f5b 1   # 单集取流
  python zuzu_parser.py dump 6a583d66b2283e06d44f5f5b --out E:\\m3u
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import zuzu_client as zc

SITE_NAME = "黄豆短剧"
WORKERS = int(os.environ.get("ZUZU_WORKERS", "8"))


# ---------------- 归一化 ----------------
def _cover(item):
    return item.get("img_x") or item.get("img") or item.get("img_y") or ""


def norm_list_item(x):
    """列表项 -> 统一卡片格式"""
    return {
        "id": str(x.get("id") or ""),
        "title": x.get("name") or "",
        "cover": _cover(x),
        "score": "",
        "episode": x.get("episode_count") or "",
        "desc": x.get("description") or "",
        "tags": [],
        "url": drama_url(x.get("id") or ""),
        "pay_type": x.get("pay_type") or "",
        "update_label": x.get("update_label") or "",
        "corner": x.get("corner") or "",
        "category": x.get("category") or "",
        "hot_rate": x.get("hot_rate") or "",
        "love": x.get("love") or "",
    }


def drama_url(drama_id):
    return "zuzu://drama/%s" % drama_id


def parse_drama_id(url_or_id):
    """从 url（zuzu://drama/xxx）或裸 id 中取剧 id"""
    s = str(url_or_id or "")
    m = re.search(r"([0-9a-f]{16,24})", s)
    return m.group(1) if m else s


def list_dramas(page=1, page_size=18, order="new", cat_id="", tag_id="",
                keywords="", update_status=""):
    """剧集列表（归一化卡片）"""
    data = zc.drama_list(page=page, page_size=page_size, order=order,
                         cat_id=cat_id, tag_id=tag_id, keywords=keywords,
                         update_status=update_status)
    return [norm_list_item(x) for x in (data.get("list") or [])]


def get_detail(drama_id):
    """详情 + 全部集数（归一化）"""
    drama_id = parse_drama_id(drama_id)
    d = zc.drama_detail(drama_id) or {}
    eps = []
    for e in (d.get("episodes") or []):
        seq = e.get("seq")
        etype = e.get("type") or "free"
        eps.append({
            "ep": str(seq),
            "label": e.get("name") or ("第%s集" % seq),
            "url": drama_url(drama_id),
            "type": etype,
            "locked": etype != "free",
            "duration": e.get("duration") or 0,
            "cover": e.get("cover") or "",
        })
    return {
        "id": drama_id,
        "title": d.get("name") or "",
        "desc": d.get("description") or "",
        "cover": _cover(d),
        "tags": d.get("tags") or [],
        "category": d.get("category") or "",
        "episode_count": d.get("episode_count") or len(eps),
        "update_label": d.get("update_label") or "",
        "pay_type": d.get("pay_type") or "",
        "episodes": eps,
    }


def pick_line(play, quality="auto"):
    """从播放响应中选线路：auto=free 优先；否则按名字匹配"""
    lines = [ln for ln in (play.get("lines") or []) if ln.get("url")]
    if not lines:
        return play.get("m3u8") or ""
    if quality in ("auto", ""):
        for ln in lines:
            if ln.get("name") == "free":
                return ln["url"]
        return lines[0]["url"]
    for ln in lines:
        if ln.get("name") == quality:
            return ln["url"]
    return lines[0]["url"]


def get_stream(drama_id, seq, quality="auto", force_refresh=False):
    """单集取流：返回 m3u8 地址（签名 URL，带 exp 过期时间）"""
    drama_id = parse_drama_id(drama_id)
    play = zc.drama_play(drama_id, seq) or {}
    url = pick_line(play, quality)
    if not url:
        raise ValueError("未取得可播放地址 (drama=%s seq=%s preview=%s)"
                         % (drama_id, seq, play.get("is_preview")))
    return url


def stream_age(stream):
    """返回 stream 的剩余有效秒数（exp - now）；无法解析返回 None。
    注意：新站签名 URL 的 exp 是【过期时间戳】（有效期约 6 小时），
    与旧站 auth_key=签发时间 语义相反，这里统一换算成剩余秒数，
    <=0 表示已过期，调用方按“剩余 < 阈值就刷新”使用。"""
    m = re.search(r"exp=(\d+)", stream or "")
    if not m:
        # 兼容旧站格式：auth_key=签发时间-签名
        m = re.search(r"auth_key=(\d+)-", stream or "")
        if m:
            return int(time.time()) - int(m.group(1))
        return None
    return int(m.group(1)) - int(time.time())


def parallel_map(fn, args, workers=None):
    args = list(args)
    if not args:
        return []
    w = workers or WORKERS
    w = max(1, min(w, len(args)))
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, args))


def crawl_one(drama_id, title, items, delay=0.2):
    """爬取一部剧：详情 -> 逐集取流（只取免费集，付费集标记锁定）"""
    drama_id = parse_drama_id(drama_id)
    d = get_detail(drama_id)
    eps = d.get("episodes") or []
    title = d.get("title") or title or drama_id

    def fetch_ep(e):
        if e.get("locked"):
            return {"id": drama_id, "title": title, "ep": e["ep"],
                    "label": e["label"], "url": e["url"],
                    "error": "付费集(%s)" % e.get("type")}
        try:
            s = get_stream(drama_id, e["ep"])
            return {"id": drama_id, "title": title, "ep": e["ep"],
                    "label": e["label"], "url": e["url"], "stream": s}
        except Exception as ex:
            return {"id": drama_id, "title": title, "ep": e["ep"],
                    "label": e["label"], "url": e["url"], "error": str(ex)}

    items.extend(parallel_map(fetch_ep, eps))
    time.sleep(delay)
    return {"title": title, "eps": len(eps)}


def dump(drama_id, out_dir=None, start=1, end=None):
    """批量解析一部剧的所有集，生成 JSON + M3U 播放列表"""
    drama_id = parse_drama_id(drama_id)
    d = get_detail(drama_id)
    eps = d.get("episodes") or []
    if not eps:
        raise ValueError("未找到集数")
    if end is not None:
        eps = [e for e in eps if int(e["ep"]) <= end]
    eps = [e for e in eps if int(e["ep"]) >= start]
    title = d.get("title") or ("剧集%s" % drama_id)
    out_dir = out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    m3u_path = os.path.join(out_dir, safe + ".m3u8")
    json_path = os.path.join(out_dir, safe + ".json")

    def fetch_ep(e):
        if e.get("locked"):
            return {"ep": e["ep"], "label": e["label"], "error": "付费集(%s)" % e.get("type")}
        try:
            s = get_stream(drama_id, e["ep"])
            return {"ep": e["ep"], "label": e["label"], "stream": s}
        except Exception as ex:
            return {"ep": e["ep"], "label": e["label"], "error": str(ex)}

    items = parallel_map(fetch_ep, eps)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"title": title, "id": drama_id, "items": items},
                  f, ensure_ascii=False, indent=2)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    ok = 0
    for it in items:
        if "stream" in it:
            lines.append("#EXTINF:-1,第%s集 %s" % (it["ep"], it["label"]))
            lines.append(it["stream"])
            ok += 1
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("已生成 M3U: %s （%d/%d 集成功）" % (m3u_path, ok, len(items)))
    print("已生成 JSON: %s" % json_path)
    return {"title": title, "m3u": m3u_path, "json": json_path, "items": items}


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description="黄豆短剧 API 解析器")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("list", help="剧集列表")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--size", type=int, default=18)
    s.add_argument("--order", default="new", help="new=最新 hot=热门 update=最近更新")
    s.add_argument("--cat", default="")
    s.add_argument("--tag", default="")

    s = sub.add_parser("search", help="搜索")
    s.add_argument("kw")

    s = sub.add_parser("detail", help="详情 + 全部集数")
    s.add_argument("id")

    s = sub.add_parser("play", help="单集取流")
    s.add_argument("id")
    s.add_argument("seq")
    s.add_argument("--quality", default="auto", help="auto / free / line2 / line3")

    s = sub.add_parser("dump", help="批量解析全剧，生成播放列表")
    s.add_argument("id")
    s.add_argument("--out", default=".")
    s.add_argument("--start", type=int, default=1)
    s.add_argument("--end", type=int, default=None)

    args = p.parse_args()

    try:
        if args.cmd == "list":
            _print_json(list_dramas(page=args.page, page_size=args.size,
                                    order=args.order, cat_id=args.cat, tag_id=args.tag))
        elif args.cmd == "search":
            _print_json(list_dramas(keywords=args.kw))
        elif args.cmd == "detail":
            _print_json(get_detail(args.id))
        elif args.cmd == "play":
            print(get_stream(args.id, args.seq, quality=args.quality))
        elif args.cmd == "dump":
            dump(args.id, args.out, args.start, args.end)
        else:
            p.print_help()
    except Exception as e:
        print("错误: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
