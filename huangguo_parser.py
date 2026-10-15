#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄果短剧 huangguoai.com 完整解析工具

用法示例：
  python huangguo_parser.py list                       # 首页剧集
  python huangguo_parser.py category ai-duanju         # 分类剧集
  python huangguo_parser.py search 校园                 # 搜索
  python huangguo_parser.py detail 12                  # 详情 + 全部集数
  python huangguo_parser.py play https://huangguoai.com/video/12/ --ep 1
  python huangguo_parser.py dump 12 --out E:\\m3u       # 批量解析全剧生成播放列表
"""
import argparse
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import video_cache
import error_handler as eh

SITE = "https://huangguoai.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _headers(referer=None):
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    return h


def fetch(url, referer=None, timeout=20, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_headers(referer))
            with urllib.request.urlopen(req, timeout=timeout) as res:
                charset = res.headers.get_content_charset() or "utf-8"
                return res.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            # 4xx（除429）是永久错误，不重试
            if e.code < 500 and e.code != 429:
                raise eh.ParseError(f"HTTP {e.code}: {e.reason} (url={url})")
            last = eh.NetworkError(f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            last = eh.NetworkError(f"Network error: {str(e)}")
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))
    raise last


def fix(u):
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return SITE + u
    return u


def strip_tags(s):
    return re.sub(r"<[^>]*>", "", str(s or "")).strip()


def img_clean(u):
    u = fix(u or "")
    if u.startswith("http") and "?" in u:
        u = u.split("?", 1)[0]
    return u


def parse_cards(html):
    """解析 .hg-drama-card 卡片（首页/分类/搜索通用）"""
    cards = []
    parts = re.split(r'(?=<div\s+class="[^"]*\bhg-drama-card\b[^"]*")', html)
    for blk in parts[1:]:
        m_id = re.search(r'data-track-id="(\d+)"', blk)
        m_ti = re.search(r'data-track-title="([^"]*)"', blk)
        m_hr = re.search(r'href="(/detail/\d+/)"', blk)
        m_cv = re.search(r'data-src="([^"]+)"', blk)
        m_sc = re.search(r'hg-drama-card__score">([^<]*)<', blk)
        m_ep = re.search(r'hg-drama-card__episode">([^<]*)<', blk)
        m_de = re.search(r'hg-drama-card__desc">([\s\S]*?)</p>', blk)
        m_ti2 = re.search(r'hg-drama-card__title"[^>]*>[\s\S]*?<a[^>]*>([^<]*)</a>', blk)
        tags = re.findall(r'hg-tag"[^>]*>([^<]*)</a>', blk)
        cid = m_id.group(1) if m_id else ""
        if not cid and m_hr:
            cid = re.sub(r"\D", "", m_hr.group(1))
        title = htmlmod.unescape(m_ti.group(1)) if m_ti else (strip_tags(m_ti2.group(1)) if m_ti2 else "")
        cards.append({
            "id": cid,
            "title": title,
            "href": m_hr.group(1) if m_hr else ("/detail/%s/" % cid if cid else ""),
            "url": fix(m_hr.group(1)) if m_hr else "",
            "cover": img_clean(m_cv.group(1)) if m_cv else "",
            "score": m_sc.group(1) if m_sc else "",
            "episode": m_ep.group(1) if m_ep else "",
            "desc": strip_tags(m_de.group(1)) if m_de else "",
            "tags": tags,
        })
    return cards


def parse_detail(html):
    """解析详情页：data-history JSON + 集数列表"""
    info = {}
    m = re.search(r'data-history="([^"]*)"', html)
    if m:
        try:
            info = json.loads(htmlmod.unescape(m.group(1)))
        except json.JSONDecodeError:
            info = {}
    eps = []
    grid = re.search(r'class="[^"]*\bhg-web-detail__ep-grid\b[^"]*"[^>]*>([\s\S]*?)</div>', html)
    if grid:
        for a in re.finditer(r'<a\b[^>]*href="(/video/[^"]+)"[^>]*data-ep-id="(\d+)"[^>]*>([\s\S]*?)</a>', grid.group(1)):
            eps.append({
                "ep": a.group(2),
                "label": strip_tags(a.group(3)),
                "href": a.group(1),
                "url": fix(a.group(1)),
            })
    info["episodes"] = eps
    if not eps:
        play = re.search(r'class="[^"]*\bhg-web-detail__play\b[^"]*"[^>]*href="(/video/[^"]+)"', html)
        if play:
            eps.append({"ep": "1", "label": "01", "href": play.group(1), "url": fix(play.group(1))})
    info["episodes"] = eps
    return info


@eh.handle_errors(max_retries=2, retry_delay=1)
@eh.safe_parse
def parse_play(html, ep="1"):
    """解析播放页：videoInitialData JSON -> m3u8 地址"""
    try:
        m = re.search(r'<script\b[^>]*\bid=["\']videoInitialData["\'][^>]*>([\s\S]*?)</script>', html, re.I)
        if not m:
            raise eh.ParseError("页面未找到 videoInitialData")
        
        raw = htmlmod.unescape(m.group(1)).strip()
        data = json.loads(raw)
        
        # 优先尝试获取无水印版本
        watermark_key = "videoSrcNoWatermark"
        stream = data.get(watermark_key) or ""
        
        # 如果没有无水印版本，尝试获取高清版本
        if not stream:
            # 尝试获取高清版本
            hd_key = "videoSrcHD" if "HD" in data else "videoSrc1080"
            stream = data.get(hd_key) or ""
        
        # 如果没有高清版本，尝试获取普通版本
        if not stream:
            streams = data.get("epPlaySrcs") or {}
            stream = streams.get(str(ep)) or data.get("videoSrc") or ""
        
        stream = str(stream).replace("\\u0026", "&")
        if not stream.startswith(("http://", "https://")):
            mm = re.search(r'https?://[^\s"\']+', stream)
            stream = mm.group(0) if mm else ""
        
        if not stream:
            raise eh.ParseError("未取得可播放地址")
        
        return stream
    except json.JSONDecodeError as e:
        raise eh.ParseError(f"JSON解析失败: {str(e)}")
    except Exception as e:
        raise eh.ParseError(f"解析播放页失败: {str(e)}")


@eh.handle_errors(max_retries=1, retry_delay=1)
def get_stream(url, ep="1", referer=None, quality="auto", force_refresh=False):
    """
    获取视频流地址，支持质量选择和缓存
    quality: "auto"(自动选择最高清), "hd"(高清), "normal"(标清), "low"(低清), "nowatermark"(无水印)
    force_refresh: True 时绕过缓存强制重新抓取（用于守护进程刷新过期地址）
    """
    if not url:
        raise eh.ParseError("url 为空")

    # 首先检查缓存（force_refresh 时跳过）
    if not force_refresh:
        cached_stream = video_cache.get_cached_stream(url, ep, quality)
        if cached_stream:
            return cached_stream

    # 缓存未命中，获取新的流
    html = fetch(url, referer or (SITE + "/"))
    data = parse_play(html, ep)

    # 根据质量参数选择合适的流
    if quality == "nowatermark":
        m = re.search(r'<script\b[^>]*\bid=["\']videoInitialData["\'][^>]*>([\s\S]*?)</script>', html, re.I)
        if m:
            raw = htmlmod.unescape(m.group(1)).strip()
            parsed_data = json.loads(raw)
            stream = parsed_data.get("videoSrcNoWatermark")
            if stream:
                result = str(stream).replace("\\u0026", "&")
                video_cache.set_cached_stream(url, ep, quality, result)
                return result

    if quality in ("hd", "auto"):
        m = re.search(r'<script\b[^>]*\bid=["\']videoInitialData["\'][^>]*>([\s\S]*?)</script>', html, re.I)
        if m:
            raw = htmlmod.unescape(m.group(1)).strip()
            parsed_data = json.loads(raw)
            hd_key = "videoSrcHD" if "HD" in parsed_data else "videoSrc1080"
            stream = parsed_data.get(hd_key)
            if stream:
                result = str(stream).replace("\\u0026", "&")
                video_cache.set_cached_stream(url, ep, quality, result)
                return result

    # 默认返回解析到的流
    if data:
        video_cache.set_cached_stream(url, ep, quality, data)
    return data


def dump(detail_id, out_dir=None, start=1, end=None):
    """批量解析一部剧的所有集，生成 JSON + M3U 播放列表"""
    url = SITE + "/detail/%s/" % detail_id
    html = fetch(url, SITE + "/")
    info = parse_detail(html)
    eps = info.get("episodes") or []
    if not eps:
        raise ValueError("未找到集数")
    if end is not None:
        eps = [e for e in eps if int(e["ep"]) <= end]
    eps = [e for e in eps if int(e["ep"]) >= start]
    title = info.get("title") or ("剧集%s" % detail_id)
    out_dir = out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    m3u_path = os.path.join(out_dir, safe + ".m3u8")
    json_path = os.path.join(out_dir, safe + ".json")
    items = []
    for e in eps:
        try:
            s = get_stream(e["url"], e["ep"])
            items.append({"ep": e["ep"], "label": e["label"], "stream": s})
            print("  [%s] %s -> %s" % (e["ep"], e["label"], s[:70]))
        except Exception as ex:
            items.append({"ep": e["ep"], "label": e["label"], "error": str(ex)})
            print("  [%s] %s 失败: %s" % (e["ep"], e["label"], ex))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"title": title, "id": detail_id, "items": items},
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
    p = argparse.ArgumentParser(description="黄果短剧 huangguoai.com 完整解析工具")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="首页剧集列表")

    s_cat = sub.add_parser("category", help="分类剧集")
    s_cat.add_argument("cat", help="分类 id，如 ai-duanju / ai-manju / ai-huanlian / ai-mogai / ranks/hot / chigua")
    s_cat.add_argument("--page", type=int, default=1)

    s_search = sub.add_parser("search", help="搜索")
    s_search.add_argument("kw")

    s_detail = sub.add_parser("detail", help="详情 + 全部集数")
    s_detail.add_argument("id")

    s_play = sub.add_parser("play", help="单集取流")
    s_play.add_argument("url")
    s_play.add_argument("--ep", default="1")

    s_dump = sub.add_parser("dump", help="批量解析全剧，生成播放列表")
    s_dump.add_argument("id")
    s_dump.add_argument("--out", default=".")
    s_dump.add_argument("--start", type=int, default=1)
    s_dump.add_argument("--end", type=int, default=None)

    args = p.parse_args()

    try:
        if args.cmd == "list":
            _print_json(parse_cards(fetch(SITE + "/")))
        elif args.cmd == "category":
            cid = args.cat.strip("/")
            url = SITE + "/" + cid + "/" + ("%d/" % args.page if args.page > 1 else "")
            _print_json(parse_cards(fetch(url)))
        elif args.cmd == "search":
            url = SITE + "/search/video/" + urllib.parse.quote(args.kw) + "/"
            _print_json(parse_cards(fetch(url)))
        elif args.cmd == "detail":
            _print_json(parse_detail(fetch(SITE + "/detail/%s/" % args.id)))
        elif args.cmd == "play":
            print(get_stream(args.url, args.ep))
        elif args.cmd == "dump":
            dump(args.id, args.out, args.start, args.end)
        else:
            p.print_help()
    except Exception as e:
        print("错误: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
