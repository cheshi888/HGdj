#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄豆短剧 APP API 客户端（zuzucast / hdmgdj 系列站点）

完整复刻 Flutter Web 端（main.dart.js）的加密协议，纯标准库实现：

【请求】
  payload = {"token": "", "deviceId": <uuid>, "data": <业务数据>}
  json    = compact_json(payload)
  gz      = gzip(json)
  key     = HMAC-SHA256(key=utf8(WEB_SECRET), msg=utf8(requestId.replace("-","")))
  body    = random_iv16 || AES-256-CBC(key, iv).encrypt(gz)      # PKCS7

  请求头：
    version/deviceType/time/requestId/sessionId/sign/X-Lang/...
    sign = md5_hex("Dart|{sessionId}|{requestId}|{time}|{url去掉scheme}") + "-{time}"

【响应】
  iv  = body[:16]; ct = body[16:]
  gz  = AES-256-CBC(key, iv).decrypt(ct)      # 同一 requestId 派生同一把 key
  json = gunzip(gz) -> utf8 -> json.loads

逆向来源：.probe_main.txt (main.5421ff9d68dd.js) 中
  dHS(请求主函数) / dcq(会话密钥) / b7x(平台密钥) / d5S+d5L(签名头) /
  a1V(AES/CBC/PKCS7) / aKa(gzip) / aJu(gunzip) / aoW(hex) / ug(MD5)

用法：
  python zuzu_client.py test                      # 连通性 + 协议自测
  python zuzu_client.py call /drama/list '{"page":"1","page_size":"20"}'
  python zuzu_client.py drama-list [--page N]
  python zuzu_client.py drama-detail <id>
  python zuzu_client.py drama-play <id> <seq>
"""
import argparse
import gzip
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

from zuzu_aes import AesCipher

BASE = os.path.dirname(os.path.abspath(__file__))

# ---- 平台固定密钥（b7x(): deviceType=="web" 分支，a4y() 恒返回 "web"）----
WEB_SECRET = "7961beb44246e3012ce228d6b5ced05a"
VERSION = "2.0.0"          # $.a2h 默认值
DEVICE_TYPE = "web"        # A.a4y() 恒为 "web"
X_LANG = "zh-CN"           # $.er().gbKN() 默认分支

# ---- 可用线路（site-config releasePage + 探针池，故障自动切换）----
HOSTS = [
    "https://zuzucast.top",
    "https://hdmgdj.com",
    "https://jaorexkn.cc",
    "https://duynmkkc.cc",
    "https://svlyibwt.cc",
    "https://qoyetjob.cc",
    "https://djxlqqzc.cc",
    "https://hddj01.com",
    "https://hddj02.com",
    "https://seiopcc.icu",
    "https://halobloom.cc",
    "https://mintgate.top",
]

DEVICE_FILE = os.path.join(BASE, ".zuzu_device.json")
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ---------------- 设备标识（对应 a4x(): localStorage 持久化 uuid） ----------------
def get_device_id():
    did = None
    try:
        if os.path.exists(DEVICE_FILE):
            with open(DEVICE_FILE, encoding="utf-8") as f:
                did = json.load(f).get("deviceId")
    except Exception:
        did = None
    if not did:
        did = str(uuid.uuid4())
        try:
            with open(DEVICE_FILE, "w", encoding="utf-8") as f:
                json.dump({"deviceId": did}, f)
        except Exception:
            pass
    return did


# ---------------- 协议原语 ----------------
def session_key(request_id):
    """dcq(requestId, b7x())：HMAC-SHA256 -> 32 字节 AES-256 密钥

    注意（逆向要点）：
      Dart 端 r = B.AW.gDm().c2(s)，其中 B.AW 是 Hex 编解码器（ayc.c2 按
      16*o+n 解析 hex 对），s = requestId.replace("-","")。
      UUID 去 dash 后恰为 32 个 hex 字符 -> 解码为 16 原始字节作为 HMAC 消息。
    """
    msg = bytes.fromhex(request_id.replace("-", ""))
    return hmac.new(WEB_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()


def make_sign(session_id, request_id, ts, url_no_scheme):
    """d5L(x) = hex(md5(utf8(x)))；sign = d5L("Dart|sid|rid|ts|url") + "-" + ts"""
    raw = "Dart|%s|%s|%s|%s" % (session_id, request_id, ts, url_no_scheme)
    return hashlib.md5(raw.encode("utf-8")).hexdigest() + "-" + str(ts)


def _strip_scheme(url):
    """B.c.dJ(url, RegExp('https?://'), '')"""
    for p in ("https://", "http://"):
        if url.startswith(p):
            return url[len(p):]
    return url


def build_request(path, data, token="", device_id=None, session_id=None, request_id=None):
    """组装加密请求体与请求头。返回 (body_bytes, headers, request_id)"""
    request_id = request_id or str(uuid.uuid4())
    session_id = session_id or str(uuid.uuid4()).replace("-", "")
    device_id = device_id or get_device_id()
    ts = int(time.time())

    # 1) payload -> compact json（Dart jsonEncode 风格：无空格）
    payload = {"token": token, "deviceId": device_id, "data": data}
    js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    # 2) gzip（对齐 Dart：mtime=当前秒, OS=255）
    buf = __import__("io").BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=ts) as gz:
        gz.write(js)
    gz_bytes = buf.getvalue()

    # 3) AES-256-CBC(PKCS7) 加密，随机 16 字节 IV
    key = session_key(request_id)
    iv = os.urandom(16)
    cipher = AesCipher(key)
    ct = cipher.encrypt_cbc(gz_bytes, iv)
    body = iv + ct

    url_no_scheme = _strip_scheme(_api_url(path))
    headers = {
        "version": VERSION,
        "deviceType": DEVICE_TYPE,
        "time": str(ts),
        "sign": make_sign(session_id, request_id, ts, url_no_scheme),
        "requestId": request_id,
        "sessionId": session_id,
        "deviceBrand": "",
        "deviceModel": "",
        "systemName": "",
        "systemVersion": "",
        "X-Lang": X_LANG,
        # dio 实例 $.daK 的 BaseOptions 默认 Content-Type（$.daK = SO("",...,"application/x-www-form-urlencoded",...)）
        # 服务端按此校验，必须是 x-www-form-urlencoded
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Origin": HOSTS[0],
        "Referer": HOSTS[0] + "/",
    }
    return body, headers, request_id


def _api_url(path):
    return HOSTS[0] + "/api" + path


def parse_response(body, request_id):
    """解密响应：iv16 || AES-CBC(gz) -> gunzip -> json"""
    if len(body) < 32:
        raise ValueError("响应过短: %d bytes" % len(body))
    iv, ct = body[:16], body[16:]
    key = session_key(request_id)
    cipher = AesCipher(key)
    gz_bytes = cipher.decrypt_cbc(ct, iv)
    if gz_bytes[:2] == b"\x1f\x8b":
        gz_bytes = gzip.decompress(gz_bytes)
    return json.loads(gz_bytes.decode("utf-8"))


def api_post(path, data=None, host=None, timeout=20, retries=2, token=""):
    """加密 POST /api/<path>，返回响应 JSON 的 data 字段（失败抛异常）"""
    last_err = None
    hosts = [host] if host else HOSTS
    for attempt in range(retries):
        for h in hosts:
            body, headers, request_id = build_request(path, data, token=token)
            url = h + "/api" + path
            headers = dict(headers)
            headers["Origin"] = h
            headers["Referer"] = h + "/"
            # sign 与 URL 绑定，换 host 需重算
            ts = headers["time"]
            headers["sign"] = make_sign(headers["sessionId"], request_id, ts,
                                        _strip_scheme(url))
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                    raw = r.read()
                resp = parse_response(raw, request_id)
                status = resp.get("status")
                # status: "y"=成功, "n"=失败（errorCode 2002=token过期需刷新重试）
                if status != "y":
                    err = resp.get("error") or resp.get("errorCode") or resp
                    raise RuntimeError("API 错误(%s): %s" % (resp.get("errorCode"), err))
                return resp.get("data", resp)
            except urllib.error.HTTPError as e:
                last_err = "HTTP %s %s" % (e.code, e.reason)
            except Exception as e:
                last_err = repr(e)[:200]
            # 换下一条线路
        if attempt < retries - 1:
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("所有线路请求失败: %s" % last_err)


# ---------------- 业务封装 ----------------
def drama_list(page=1, page_size=18, order="", cat_id="", tag_id="", source="",
               canvas="", keywords="", update_status="", host=None):
    data = {"page": str(page), "page_size": str(page_size)}
    if order:
        data["order"] = order
    if cat_id:
        data["cat_id"] = cat_id
    if tag_id:
        data["tag_id"] = tag_id
    if source:
        data["source"] = source
    if canvas:
        data["canvas"] = canvas
    if keywords:
        data["keywords"] = keywords
    if update_status:
        data["update_status"] = update_status
    return api_post("/drama/list", data, host=host)


def drama_detail(drama_id, host=None):
    return api_post("/drama/detail", {"id": str(drama_id)}, host=host)


def drama_play(drama_id, seq, host=None):
    return api_post("/drama/play", {"id": str(drama_id), "seq": str(seq)}, host=host)


def drama_search_conf(host=None):
    return api_post("/drama/searchConf", None, host=host)


def drama_rank(tab, page=1, host=None):
    return api_post("/drama/rank", {"tab": tab, "page": str(page)}, host=host)


def drama_nav_list(position="", host=None):
    data = {}
    if position:
        data["position"] = position
    data["with_filters"] = "1"
    return api_post("/drama/navList", data, host=host)


def drama_nav_block(code, tab, page=1, host=None):
    return api_post("/drama/navBlock", {"code": code, "tab": tab, "page": str(page)}, host=host)


# ---------------- CLI ----------------
def _pj(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description="黄豆短剧 API 加密客户端")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("test", help="协议自测 + 连通性测试")

    s = sub.add_parser("call", help="原始调用")
    s.add_argument("path")
    s.add_argument("data", nargs="?", default="{}")

    s = sub.add_parser("drama-list", help="剧集列表")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--size", type=int, default=18)
    s.add_argument("--order", default="")
    s.add_argument("--cat", default="")
    s.add_argument("--tag", default="")
    s.add_argument("--kw", default="")
    s.add_argument("--host", default=None)

    s = sub.add_parser("drama-detail", help="剧集详情")
    s.add_argument("id")
    s.add_argument("--host", default=None)

    s = sub.add_parser("drama-play", help="单集播放地址")
    s.add_argument("id")
    s.add_argument("seq")
    s.add_argument("--host", default=None)

    sub.add_parser("search-conf", help="搜索配置（热搜/分类/标签）")
    sub.add_parser("nav-list", help="导航块列表")

    args = p.parse_args()

    try:
        if args.cmd == "test":
            from zuzu_client_test import run_test
            run_test()
        elif args.cmd == "call":
            data = json.loads(args.data) if args.data else None
            _pj(api_post(args.path, data))
        elif args.cmd == "drama-list":
            _pj(drama_list(page=args.page, page_size=args.size, order=args.order,
                           cat_id=args.cat, tag_id=args.tag, keywords=args.kw,
                           host=args.host))
        elif args.cmd == "drama-detail":
            _pj(drama_detail(args.id, host=args.host))
        elif args.cmd == "drama-play":
            _pj(drama_play(args.id, args.seq, host=args.host))
        elif args.cmd == "search-conf":
            _pj(drama_search_conf())
        elif args.cmd == "nav-list":
            _pj(drama_nav_list())
        else:
            p.print_help()
    except Exception as e:
        print("错误: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
