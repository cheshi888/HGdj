#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄果短剧播放面板 + 订阅服务
端口默认 8899，改端口只需编辑 panel.conf 的 PANEL_PORT（或用环境变量 PANEL_PORT 覆盖）。

- 网页面板:  http://127.0.0.1:<PORT>/
- 订阅地址:  http://<本机IP>:<PORT>/playlist.m3u8   (NTPlayer/VLC 等订阅)
- 数据接口:  http://127.0.0.1:<PORT>/api/groups
- 实时取流:  http://127.0.0.1:<PORT>/api/stream?url=<视频页>&ep=<集数>
- 订阅代理:  http://127.0.0.1:<PORT>/play?id=<剧集id>&ep=<集数>  (302 跳转到新鲜地址)
- 运行状态:  http://127.0.0.1:<PORT>/health
"""
import json
import os
import re
import socketserver
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import huangguo_parser as h
try:
    import zuzu_parser as zp
except Exception:
    zp = None
try:
    import panel_config as _cfg
    PORT = _cfg.panel_port()
    HOST = _cfg.panel_host()
except Exception:                                    # 配置文件缺失也能跑
    PORT = int(os.environ.get("PANEL_PORT", "8899"))
    HOST = os.environ.get("PANEL_HOST", "0.0.0.0")


def resolve_stream(url, ep, quality="auto"):
    """按 url 协议路由到对应站点解析器：
    zuzu://  -> 新站 APP API（zuzucast/hdmgdj 系列）
    其他(http) -> 旧站 huangguoai.com 页面解析"""
    if zp is not None and str(url).startswith("zuzu://"):
        return zp.get_stream(url, ep, quality=quality)
    return h.get_stream(url, ep, quality=quality)

PROG = os.path.join(BASE, "crawl_progress.json")
ALL = os.path.join(BASE, "all_streams.json")


_items_cache = {"mtime": 0, "items": [], "src": None}


def data_src():
    """all_streams.json 是全量产物，crawl_progress.json 是增量进度。
    取「修改时间较新」的一个，避免爬取过程中面板一直读旧快照。"""
    cands = []
    for f in (ALL, PROG):
        try:
            cands.append((os.path.getmtime(f), f))
        except OSError:
            pass
    return max(cands)[1] if cands else None


def load_items():
    """读取剧集数据（文件未变则用缓存，避免每次请求重新解析大JSON）"""
    src = data_src()
    if not src:
        return _items_cache["items"] or []
    try:
        mtime = os.path.getmtime(src)
        if (_items_cache["src"] == src and _items_cache["mtime"] == mtime
                and _items_cache["items"]):
            return _items_cache["items"]
        with open(src, encoding="utf-8") as f:
            d = json.load(f)
        _items_cache["mtime"] = mtime
        _items_cache["src"] = src
        _items_cache["items"] = d.get("items", [])
        return _items_cache["items"]
    except Exception:
        return _items_cache["items"] or []


def group_items(items):
    groups = OrderedDict()
    for it in items:
        if not (it.get("url") or it.get("stream")):
            continue
        key = it.get("id", "?")
        g = groups.setdefault(key, {"id": key, "title": it.get("title", ""), "eps": []})
        g["eps"].append({
            "ep": it.get("ep"),
            "label": it.get("label"),
            "url": it.get("url"),
            "stream": it.get("stream"),
        })
    return list(groups.values())


# ---------------- 流媒体代理（解决浏览器播放的 CORS 限制） ----------------
# 站点 CDN 不返回 Access-Control-Allow-Origin，hls.js 用 XHR 拉 m3u8/分片会被拦。
# 这里由服务端代取并回写字幕内地址，统一带上宽松 CORS 头 → 浏览器可正常播放。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MAX_PROXY_BYTES = 64 * 1024 * 1024
_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
_host_cache = {"mtime": -1, "hosts": set()}
_learned_hosts = set()   # 仅在「已授权 m3u8」里出现过的 CDN 主机（分片/key 常在其他域名）


def allowed_hosts():
    """允许代理的站点主机（站点线路 + 数据中出现过的流主机），防止变成开放代理"""
    src = data_src()
    try:
        mt = os.path.getmtime(src)
    except (OSError, TypeError):
        mt = 0
    if _host_cache["mtime"] == mt and _host_cache["hosts"]:
        return _host_cache["hosts"]
    hs = set()
    try:
        import zuzu_client as _zc
        for h in getattr(_zc, "HOSTS", []):
            n = urllib.parse.urlparse(h).netloc.lower()
            if n:
                hs.add(n)
    except Exception:
        pass
    for it in load_items():
        for k in ("stream", "url"):
            u = it.get(k) or ""
            if u.startswith("http"):
                n = urllib.parse.urlparse(u).netloc.lower()
                if n:
                    hs.add(n)
    _host_cache["mtime"] = mt
    _host_cache["hosts"] = hs
    return hs


def proxy_allowed(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if host in _learned_hosts:
        return True
    for h in allowed_hosts():
        if host == h or host.endswith("." + h):
            return True
    return False


def proxy_url(u):
    return "/proxy?u=" + urllib.parse.quote(u, safe="")


def rewrite_m3u8(text, base_url):
    """把播放列表里的 key / 分片地址改写成走本代理（相对路径按 base_url 展开）。
    同时把其中出现的主机登记为「已学习」，以便后续分片请求通过白名单。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            def _rep(m):
                absu = urllib.parse.urljoin(base_url, m.group(1))
                _learn_host(absu)
                return 'URI="%s"' % proxy_url(absu)
            out.append(_URI_ATTR_RE.sub(_rep, line))
        else:
            absu = urllib.parse.urljoin(base_url, s)
            _learn_host(absu)
            out.append(proxy_url(absu))
    return "\n".join(out) + "\n"


def _learn_host(u):
    try:
        h = (urllib.parse.urlparse(u).hostname or "").lower()
        if h:
            _learned_hosts.add(h)
    except Exception:
        pass


PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>黄果短剧播放面板</title>
<style>
:root{
  --bg:#0a0d14;--panel:#121827;--panel2:#1a2134;--line:#232c42;
  --text:#e9edf5;--muted:#8b96ad;--accent:#ff6a3d;--accent2:#ff8a5c;
  --r:14px;--shadow:0 10px 40px rgba(0,0,0,.5);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{margin:0;font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  background:radial-gradient(1100px 520px at 15% -8%,#182340 0%,var(--bg) 55%) fixed;color:var(--text);min-height:100vh}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#2a3348;border-radius:8px}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer}

/* 顶栏 */
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
  padding:12px 18px;background:rgba(10,13,20,.82);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line)}
.logo{font-size:18px;font-weight:800;white-space:nowrap;letter-spacing:.5px}
.logo .dot{color:var(--accent)}
#kw{flex:1;min-width:120px;max-width:460px;padding:10px 14px;border-radius:10px;
  border:1px solid var(--line);background:var(--panel);color:var(--text);outline:none;font-size:14px}
#kw:focus{border-color:var(--accent)}
#sort{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;
  color:var(--text);font-size:13px;max-width:130px}
.stat{color:var(--muted);font-size:12px;white-space:nowrap}
#delta{color:var(--accent);font-weight:600}

/* 主体 */
main{max-width:1400px;margin:0 auto;padding:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.drama{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
  transition:transform .15s,box-shadow .15s,border-color .15s;display:flex;flex-direction:column}
.drama:hover{transform:translateY(-3px);border-color:#34405f;box-shadow:var(--shadow)}
.drama-head{padding:13px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:8px}
.drama-title{font-weight:700;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drama-meta{color:var(--muted);font-size:12px;white-space:nowrap}
.eps{display:none;padding:0 12px 12px;flex-wrap:wrap;gap:7px}
.drama.open .eps{display:flex}
.drama.open{border-color:var(--accent)}
.ep{padding:7px 11px;border-radius:9px;background:var(--panel2);cursor:pointer;font-size:13px;
  border:1px solid transparent;transition:.12s;color:var(--text)}
.ep:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.ep.active{background:var(--accent);color:#fff}
.empty{color:var(--muted);text-align:center;padding:60px 10px}

/* 播放器：全视口自适应，桌面/手机/平板统一体验
   视频永远 contain（等比缩放 → 不拉伸、不裁切、不变形）；
   旋转/缩放由 JS 按容器真实像素精算，全屏/窗口尺寸变化自动重算 */
.overlay{position:fixed;inset:0;z-index:50;background:#000;
  display:flex;flex-direction:column;animation:fade .18s}
.overlay.hidden{display:none}
@keyframes fade{from{opacity:0}to{opacity:1}}
.player{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden;background:#000}
.phead{display:flex;align-items:center;gap:8px;padding:8px 12px;flex-wrap:wrap;
  background:rgba(20,24,34,.92);border-bottom:1px solid rgba(255,255,255,.08);flex-shrink:0}
.ptitle{font-weight:700;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:80px}
.pbtns{display:flex;gap:5px;flex-shrink:0;align-items:center;flex-wrap:wrap}
.pbtn{width:34px;height:34px;border-radius:8px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);
  color:#fff;font-size:14px;display:flex;align-items:center;justify-content:center;transition:.12s;cursor:pointer;
  user-select:none;flex-shrink:0}
.pbtn:hover{border-color:var(--accent);color:#fff;background:rgba(255,106,61,.15)}
.pbtn.close:hover{background:#e5484d;border-color:#e5484d}
.pbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.pzoom{min-width:46px;text-align:center;font-size:12px;color:#cfd6e4;
  font-variant-numeric:tabular-nums;user-select:none}
#qsel{background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.15);
  border-radius:6px;padding:4px 8px;font-size:12px;max-width:120px}
#qsel option{background:#1a2134;color:#fff}
/* 视频区：绝对定位居中，宽高 + 旋转角由 JS 精算 → 旋转/缩放/全屏均不变形 */
.vwrap{flex:1;min-height:0;position:relative;overflow:hidden;background:#000;touch-action:none}
.vwrap video{position:absolute;left:50%;top:50%;transform-origin:center center;
  object-fit:contain;background:#000;display:block;max-width:none;max-height:none}
.vwrap.pannable{cursor:grab}
.vwrap.grabbing{cursor:grabbing}
/* 集数栏 */
.peps{display:flex;gap:6px;overflow-x:auto;padding:8px 12px;flex-wrap:nowrap;flex-shrink:0;
  background:rgba(20,24,34,.92);border-top:1px solid rgba(255,255,255,.08)}
.peps .ep{flex-shrink:0}

/* 桌面端微调：字号略大、搜索栏保留 */
@media(min-width:761px){
  .overlay{padding:0}
}

/* 手机/平板 - 顶栏紧凑 */
@media(max-width:760px){
  .topbar{padding:8px 10px;gap:8px}
  .logo{font-size:15px}
  .stat{display:none}
  main{padding:10px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
  .drama-head{padding:10px}
  .drama-title{font-size:13px}
  .pbtn{width:38px;height:38px}
}
/* 手机横屏：保证视频高度撑满 */
@media(max-width:760px) and (orientation:landscape){
  .overlay{flex-direction:column}
  .player{height:100%}
  .vwrap{flex:1;min-height:0}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="logo">🍿 黄果短剧<span class="dot">·</span>播放器</div>
  <input id="kw" placeholder="搜索剧集标题…">
  <select id="sort" title="排序">
    <option value="default">默认</option>
    <option value="title">按标题</option>
    <option value="eps_desc">集数多→少</option>
    <option value="eps_asc">集数少→多</option>
    <option value="id_desc">ID新→旧</option>
    <option value="id_asc">ID旧→新</option>
  </select>
  <span class="stat" id="stat">加载中…</span>
  <span class="stat" id="delta"></span>
</header>
<main>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty">加载中…</div>
</main>

<div id="overlay" class="overlay hidden">
  <div class="player">
    <div class="phead">
      <div class="ptitle" id="ptitle"></div>
      <div class="pbtns">
        <button class="pbtn" id="bprev" title="上一集 (←)">⏮</button>
        <button class="pbtn" id="bnext" title="下一集 (→)">⏭</button>
        <button class="pbtn" id="brotl" title="向左旋转 90°">⟲</button>
        <button class="pbtn" id="brot" title="向右旋转 90° (R)">⟳</button>
        <button class="pbtn" id="bzout" title="缩小 (-)">−</button>
        <span class="pzoom" id="pzoom">100%</span>
        <button class="pbtn" id="bzin" title="放大 (+)">＋</button>
        <button class="pbtn" id="bfit" title="重置画面 (0)">⤢</button>
        <button class="pbtn" id="bfull" title="全屏 / 退出全屏 (F)">⛶</button>
        <button class="pbtn close" id="bclose" title="关闭 (Esc)">✕</button>
      </div>
      <select id="qsel">
        <option value="auto">自动</option>
        <option value="hd">高清</option>
        <option value="normal">标清</option>
        <option value="low">低清</option>
        <option value="nowatermark">无水印</option>
      </select>
    </div>
    <div class="vwrap" id="vwrap">
      <video id="v" controls playsinline webkit-playsinline></video>
    </div>
    <div class="peps" id="peps"></div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js" onerror="var s=document.createElement('script');s.src='https://unpkg.com/hls.js@1.5.17/dist/hls.min.js';document.head.appendChild(s)"></script>
<script>
let hls=null, groups=[], cur={g:-1,i:-1};
const $=id=>document.getElementById(id);
const grid=$('grid'), empty=$('empty'), stat=$('stat'), kw=$('kw'), sortEl=$('sort'), deltaEl=$('delta');
const overlay=$('overlay'), video=$('v'), ptitle=$('ptitle'), peps=$('peps');
const player=document.querySelector('.player');

/* ============ 画面视图：旋转 / 缩放 / 平移（等比缩放，永不变形） ============
   核心：旋转 90/270 时把视频元素的“基准宽高”互换，再整体旋转 →
        旋转后正好铺满可视区；配合 object-fit:contain 保证画面完整、不变形。
   全屏/窗口/屏幕方向变化时统一重算，故任意尺寸都自适应。 */
const view={rot:0,zoom:1,px:0,py:0};
const ZMIN=0.4,ZMAX=5,ZSTEP=1.25;
const vwrap=$('vwrap');

function _viewGeom(){
  // 返回 {W,H,swap,w,h,visW,visH}（visW/visH = 旋转后的视觉占用尺寸）
  const W=vwrap.clientWidth||1, H=vwrap.clientHeight||1;
  const r=((view.rot%360)+360)%360;
  const swap=(r===90||r===270);
  const w=Math.max(1,(swap?H:W)*view.zoom);
  const h=Math.max(1,(swap?W:H)*view.zoom);
  return {W,H,swap,w,h,visW:swap?h:w,visH:swap?w:h};
}
function applyView(){
  const W=vwrap.clientWidth, H=vwrap.clientHeight;
  if(!W||!H)return;                       // 面板隐藏/尺寸为0时不重算
  let r=((view.rot%360)+360)%360;
  view.rot=r;
  view.zoom=Math.max(ZMIN,Math.min(ZMAX,view.zoom));
  const g=_viewGeom();
  // 平移量限制在溢出范围内（不放大/未旋转时自动归零）
  const mx=Math.max(0,(g.visW-g.W)/2), my=Math.max(0,(g.visH-g.H)/2);
  view.px=Math.max(-mx,Math.min(mx,view.px));
  view.py=Math.max(-my,Math.min(my,view.py));
  video.style.width=Math.round(g.w)+'px';
  video.style.height=Math.round(g.h)+'px';
  video.style.transform='translate(-50%,-50%) translate('+Math.round(view.px)+'px,'
    +Math.round(view.py)+'px) rotate('+r+'deg)';
  const z=$('pzoom'); if(z)z.textContent=Math.round(view.zoom*100)+'%';
  $('brot').classList.toggle('on',r!==0);
  $('brotl').classList.toggle('on',r!==0);
  vwrap.classList.toggle('pannable',view.zoom>1.02||r!==0);
}
function rotateBy(d){
  view.rot=(((view.rot+d)%360)+360)%360;
  view.px=0; view.py=0;                   // 旋转后回到居中
  applyView();
}
function zoomAt(f,cx,cy){                 // 以 (cx,cy) 为锚点缩放（默认容器中心）
  const W=vwrap.clientWidth||1, H=vwrap.clientHeight||1;
  const nz=Math.max(ZMIN,Math.min(ZMAX,view.zoom*f));
  if(Math.abs(nz-view.zoom)<1e-6)return;
  const k=nz/view.zoom;
  const dx=(cx==null?W/2:cx)-W/2, dy=(cy==null?H/2:cy)-H/2;
  view.px=view.px*k+dx*(1-k);
  view.py=view.py*k+dy*(1-k);
  view.zoom=nz;
  applyView();
}
function resetView(){view.rot=0;view.zoom=1;view.px=0;view.py=0;applyView();}
function onViewportChange(){requestAnimationFrame(applyView);}

/* 缩放：按钮 / 滚轮 / 双指捏合 */
$('bzin').onclick=()=>zoomAt(ZSTEP);
$('bzout').onclick=()=>zoomAt(1/ZSTEP);
$('bfit').onclick=resetView;
$('brot').onclick=()=>rotateBy(90);
$('brotl').onclick=()=>rotateBy(-90);
vwrap.addEventListener('wheel',e=>{
  if(overlay.classList.contains('hidden'))return;
  e.preventDefault();
  const r=vwrap.getBoundingClientRect();
  zoomAt(e.deltaY<0?1.12:1/1.12,e.clientX-r.left,e.clientY-r.top);
},{passive:false});

/* 平移：放大/旋转后按住拖动（避开视频原生控件条） */
let _drag=null, _pinch=null;
vwrap.addEventListener('pointerdown',e=>{
  if(overlay.classList.contains('hidden')||_pinch)return;
  if(view.zoom<=1.02&&view.rot===0)return;
  const r=vwrap.getBoundingClientRect();
  if(view.rot===0&&e.clientY>r.bottom-56)return;   // 不抢原生进度条
  _drag={x:e.clientX,y:e.clientY,px:view.px,py:view.py,id:e.pointerId};
  vwrap.classList.add('grabbing');
  try{vwrap.setPointerCapture&&vwrap.setPointerCapture(e.pointerId);}catch(_){}
});
vwrap.addEventListener('pointermove',e=>{
  if(!_drag)return;
  view.px=_drag.px+(e.clientX-_drag.x);
  view.py=_drag.py+(e.clientY-_drag.y);
  applyView();
});
function endDrag(){
  if(!_drag)return;
  try{vwrap.releasePointerCapture&&vwrap.releasePointerCapture(_drag.id);}catch(_){}
  _drag=null; vwrap.classList.remove('grabbing');
}
vwrap.addEventListener('pointerup',endDrag);
vwrap.addEventListener('pointercancel',endDrag);

/* 双指捏合缩放（移动端） */
const _tdist=t=>Math.hypot(t[0].clientX-t[1].clientX,t[0].clientY-t[1].clientY);
vwrap.addEventListener('touchstart',e=>{
  if(e.touches.length===2){
    _pinch={d:_tdist(e.touches)||1,z:view.zoom};
    _drag=null; vwrap.classList.remove('grabbing');
  }
},{passive:true});
vwrap.addEventListener('touchmove',e=>{
  if(_pinch&&e.touches.length===2){
    e.preventDefault();
    const r=vwrap.getBoundingClientRect(), t=e.touches;
    const target=Math.max(ZMIN,Math.min(ZMAX,_pinch.z*_tdist(t)/_pinch.d));
    const cx=(t[0].clientX+t[1].clientX)/2-r.left;
    const cy=(t[0].clientY+t[1].clientY)/2-r.top;
    zoomAt(target/view.zoom,cx,cy);
  }
},{passive:false});
vwrap.addEventListener('touchend',e=>{if(e.touches.length<2)_pinch=null;},{passive:true});

/* 尺寸自适应：窗口 / 全屏 / 屏幕方向 / 容器尺寸变化 */
window.addEventListener('resize',onViewportChange);
window.addEventListener('orientationchange',()=>setTimeout(applyView,260));
document.addEventListener('fullscreenchange',onViewportChange);
document.addEventListener('webkitfullscreenchange',onViewportChange);
if(window.ResizeObserver){try{new ResizeObserver(onViewportChange).observe(vwrap);}catch(_){}}

let _curEp=null;  // 当前播放的 eps 条目（用于失败恢复）

/* 站点 CDN 无 CORS 头，浏览器直连会被拦 → 一律走本机代理播放 */
const PROXY=u=>'/proxy?u='+encodeURIComponent(u);

function startPlay(url,onReady){
  if(hls){hls.destroy();hls=null}
  video.removeAttribute('src');
  video.innerHTML='';
  let hlsRetry=0;
  const src=PROXY(url);
  const orig=ptitle.textContent;
  ptitle.textContent=orig+' (加载中…)';
  const doFallback=()=>{
    // 播放失败(地址过期等) → 自动拉新地址重播一次
    if(_curEp&&_curEp.url){
      ptitle.textContent=orig+' (换新地址…)';
      fetchStream(_curEp.url,_curEp.ep,_curEp,true);
    }else{
      ptitle.textContent='加载失败，请重试';
      if(hls){hls.destroy();hls=null;}
    }
  };
  if(window.Hls && Hls.isSupported()){
    hls=new Hls({
      enableWorker:true,
      lowLatencyMode:false,
      backBufferLength:60,
      maxBufferLength:45,
      maxMaxBufferLength:90,
      fragLoadingTimeOut:20000,
      manifestLoadingTimeOut:15000,
      levelLoadingTimeOut:15000,
      fragLoadingMaxRetry:6,
      fragLoadingRetryDelay:300,
      startLevel:-1,           // 自动从合适的码率起播
      autoStartLoad:true,
      smoothChange:true,
      capLevelToPlayerSize:false
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{
      ptitle.textContent=orig;
      video.play().catch(()=>{ptitle.textContent=orig+' (点击播放)';});
      if(onReady)onReady();
    });
    hls.on(Hls.Events.ERROR,(_,d)=>{
      if(d.fatal){
        if(d.type==='networkError'&&hlsRetry<2){
          // 短网络抖动 → 先原地重试
          hlsRetry++;
          ptitle.textContent=orig+' (缓冲重试 '+hlsRetry+'/2)';
          setTimeout(()=>{if(hls){hls.startLoad();}},1000);
        }else if(d.type==='mediaError'){
          // 媒体错误 → 恢复
          if(hls){hls.recoverMediaError();}
        }else{
          // 彻底失败(含URL签名过期) → 拉新地址重播
          doFallback();
        }
      }
    });
  }else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src=src;video.load();
    video.play().catch(()=>{ptitle.textContent=orig+' (点击播放)';});
    if(onReady)onReady();
  }else{
    ptitle.textContent='浏览器不支持 HLS 播放';
  }
}

function fetchStream(url,ep,epItem,force){
  // 经 /api/stream 取新鲜播放地址（缓存命中秒回）
  const q=document.getElementById('qsel')?.value||'auto';
  return fetch('/api/stream?url='+encodeURIComponent(url)+'&ep='+encodeURIComponent(ep||'1')+'&quality='+encodeURIComponent(q))
    .then(r=>r.json())
    .then(d=>{
      if(d.error)throw new Error(d.error);
      if(epItem){epItem.stream=d.stream;}
      return d.stream;
    });
}

function playEp(gi,ei){
  const g=groups[gi];if(!g)return;
  const e=g.eps[ei];if(!e)return;
  cur={g:gi,i:ei};
  _curEp=e;
  overlay.classList.remove('hidden');
  renderPeps();
  applyView();          // 面板刚显示 → 立即按容器尺寸排布画面
  if(!e.url&&!e.stream){ptitle.textContent=g.title+' · 第'+e.ep+'集 (无地址)';return;}
  ptitle.textContent=g.title+' · 第'+e.ep+'集';
  // 优先：数据里已有流地址 → 立即播放（秒开），后台静默刷新新签名备用
  if(e.stream){
    startPlay(e.stream,()=>{
      // 后台拉一次新地址更新 e.stream，供下一集/失败恢复使用（不打断当前播放）
      fetchStream(e.url,e.ep,e,false).catch(()=>{});
    });
    return;
  }
  // 无本地流 → 实时取流
  ptitle.textContent=g.title+' · 第'+e.ep+'集 (获取中…)';
  fetchStream(e.url,e.ep,e,true)
    .then(s=>{startPlay(s);ptitle.textContent=g.title+' · 第'+e.ep+'集';})
    .catch(err=>{ptitle.textContent='获取失败: '+(err.message||err);});
}
function renderPeps(){
  const g=groups[cur.g];if(!g){peps.innerHTML='';return;}
  peps.innerHTML='';
  g.eps.forEach((e,ei)=>{
    const b=document.createElement('span');
    b.className='ep'+(ei===cur.i?' active':'');
    b.textContent='第'+e.ep+'集';
    b.onclick=()=>playEp(cur.g,ei);
    peps.appendChild(b);
  });
  const act=peps.querySelector('.ep.active');
  if(act)act.scrollIntoView({inline:'center',block:'nearest'});
}
function prev(){if(cur.i>0)playEp(cur.g,cur.i-1);}
function next(){const g=groups[cur.g];if(g&&cur.i<g.eps.length-1)playEp(cur.g,cur.i+1);}
function closePlayer(){
  if(hls){hls.destroy();hls=null;}
  video.pause();video.removeAttribute('src');video.load();
  resetView();
  overlay.classList.add('hidden');cur={g:-1,i:-1};
}
function toggleFull(){
  if(document.fullscreenElement){
    document.exitFullscreen().catch(()=>{});
  }else{
    (overlay.requestFullscreen?overlay:video).requestFullscreen().catch(()=>{});
  }
}
function render(data,noDelta){
  groups=data;
  const totalEps=groups.reduce((a,g)=>a+g.eps.length,0);
  // 增量显示（仅页面首次加载/数据刷新时计算，切换排序不重算）
  if(!noDelta){
    const lastEps=parseInt(localStorage.getItem('hg_last_eps')||'0');
    const lastCount=parseInt(localStorage.getItem('hg_last_count')||'0');
    if(lastEps>0){
      const dEps=totalEps-lastEps, dCount=groups.length-lastCount;
      if(dEps>0) deltaEl.textContent='+'+dEps+'集 / +'+dCount+'部';
      else if(dCount>0) deltaEl.textContent='+'+dCount+'部';
      else deltaEl.textContent='无新增';
    } else {
      deltaEl.textContent='';
    }
    localStorage.setItem('hg_last_eps',String(totalEps));
    localStorage.setItem('hg_last_count',String(groups.length));
  }

  // 排序
  const mode=sortEl.value||'default';
  if(mode==='title') groups.sort((a,b)=>(a.title||'').localeCompare(b.title||'','zh'));
  else if(mode==='eps_desc') groups.sort((a,b)=>b.eps.length-a.eps.length);
  else if(mode==='eps_asc') groups.sort((a,b)=>a.eps.length-b.eps.length);
  else if(mode==='id_desc') groups.sort((a,b)=>parseInt(b.id||0)-parseInt(a.id||0));
  else if(mode==='id_asc') groups.sort((a,b)=>parseInt(a.id||0)-parseInt(b.id||0));

  stat.textContent='共 '+groups.length+' 部 / '+totalEps+' 集';
  grid.innerHTML='';
  groups.forEach((g,gi)=>{
    const d=document.createElement('div');d.className='drama';d.dataset.title=(g.title||'').toLowerCase();
    const head=document.createElement('div');head.className='drama-head';
    head.innerHTML='<span class="drama-title"></span><span class="drama-meta">'+g.eps.length+'集</span>';
    head.querySelector('.drama-title').textContent=g.title;
    const box=document.createElement('div');box.className='eps';
    g.eps.forEach((e,ei)=>{
      const b=document.createElement('span');b.className='ep';b.textContent='第'+e.ep+'集';
      b.onclick=()=>playEp(gi,ei);
      box.appendChild(b);
    });
    head.onclick=()=>d.classList.toggle('open');
    d.appendChild(head);d.appendChild(box);grid.appendChild(d);
  });
  empty.style.display=groups.length?'none':'block';
}
$('bprev').onclick=prev;
$('bnext').onclick=next;
$('bfull').onclick=toggleFull;
$('bclose').onclick=closePlayer;
sortEl.addEventListener('change',()=>{if(groups.length)render(groups,true);});
overlay.addEventListener('click',e=>{if(e.target===overlay)closePlayer();});

document.addEventListener('keydown',e=>{
  if(overlay.classList.contains('hidden'))return;
  const k=e.key;
  if(k==='Escape')closePlayer();
  else if(k==='ArrowLeft')prev();
  else if(k==='ArrowRight')next();
  else if(k==='r'||k==='R')rotateBy(e.shiftKey?-90:90);
  else if(k==='+'||k==='=')zoomAt(ZSTEP);
  else if(k==='-'||k==='_')zoomAt(1/ZSTEP);
  else if(k==='0')resetView();
  else if(k==='f'||k==='F')toggleFull();
});
kw.addEventListener('input',()=>{
  const q=kw.value.trim().toLowerCase();
  document.querySelectorAll('.drama').forEach(d=>{d.style.display=(!q||d.dataset.title.includes(q))?'':'none'});
});
fetch('/api/groups').then(r=>r.json()).then(render).catch(e=>{empty.textContent='加载失败: '+e.message;empty.style.display='block';});

// 预加载下一集（防堆积：切换后先取消旧定时器再延迟触发）
let _preloadTimer=null;
function preloadNextEp(){
  if(_preloadTimer){clearTimeout(_preloadTimer);_preloadTimer=null;}
  _preloadTimer=setTimeout(()=>{
    if(cur.g>=0&&cur.i>=0&&cur.i<groups[cur.g].eps.length-1){
      const ne=groups[cur.g].eps[cur.i+1];
      if(ne&&ne.url){
        // 优先用已带地址；没有才请求（只预热缓存，不打断当前播放）
        if(ne.stream)return;
        const q=document.getElementById('qsel')?.value||'auto';
        fetch('/api/stream?url='+encodeURIComponent(ne.url)+'&ep='+encodeURIComponent(ne.ep||'1')+'&quality='+encodeURIComponent(q))
          .then(r=>r.json()).then(d=>{if(!d.error)ne.stream=d.stream;}).catch(()=>{});
      }
    }
  },3000);
}
const _origPlayEp=playEp;
playEp=function(gi,ei){_origPlayEp(gi,ei);preloadNextEp();};
</script>
</body>
</html>'''


def build_m3u(items):
    lines = ["#EXTM3U"]
    for it in items:
        if "stream" in it:
            lines.append('#EXTINF:-1 group-title="黄果短剧",%s 第%s集' % (it.get("title", ""), it.get("ep", "")))
            lines.append(it["stream"])
    return "\n".join(lines) + "\n"


# ---------------- 静态资源 / 兜底 ----------------
# 面板允许从多个等价路径进入，避免手输或旧收藏的地址出现裸「not found」
PAGE_PATHS = {"/", "/index.html", "/index.htm", "/index.php", "/player",
              "/panel", "/home", "/app", "/drama", "/web"}

# 浏览器会自动请求 /favicon.ico，之前会 404 并在控制台刷「Not Found」
ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#ff6a3d"/>'
    '<path d="M16 24h32v18a4 4 0 0 1-4 4H20a4 4 0 0 1-4-4z" fill="#fff"/>'
    '<path d="M20 24l5-9 7 9 7-9 5 9z" fill="#ffd9c9"/>'
    '</svg>').encode("utf-8")
ROBOTS = b"User-agent: *\nDisallow: /\n"


def notfound_page(path, err=None):
    """友好 404：列出可用入口，而不是丢一行裸文本 not found"""
    links = [("/", "播放面板"), ("/playlist.m3u8", "订阅地址（m3u8）"),
             ("/api/groups", "数据接口 /api/groups"), ("/health", "运行状态 /health")]
    rows = "".join('<li><a href="%s">%s</a></li>' % (u, t) for u, t in links)
    safe = (path or "/").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    tip = ('<p class="p" style="color:#ff8a5c">' + err + "</p>") if err else ""
    tpl = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/favicon.ico">
<title>404 · 黄果短剧面板</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:radial-gradient(900px 460px at 20% -10%,#182340 0%,#0a0d14 60%) fixed;
color:#e9edf5;font:15px/1.7 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.box{max-width:540px;margin:20px;padding:30px 34px;background:#121827;
border:1px solid #232c42;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
h1{margin:0 0 4px;font-size:40px;color:#ff6a3d}.p{color:#8b96ad;margin:0 0 16px;word-break:break-all}
code{background:#1a2134;padding:2px 7px;border-radius:6px;color:#ff8a5c}
ul{margin:0;padding-left:20px}a{color:#ff8a5c;text-decoration:none}a:hover{text-decoration:underline}</style>
</head><body><div class="box">
<h1>404</h1>
<p class="p">没有这个地址：<code>__PATH__</code><br>链接可能写错了，从这里进去试试：</p>
__TIP__<ul>__ROWS__</ul></div></body></html>'''
    return tpl.replace("__PATH__", safe).replace("__TIP__", tip).replace("__ROWS__", rows)


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _head(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_HEAD(self):
        """支持 HEAD 请求（curl -I 健康检查）"""
        p = urllib.parse.urlparse(self.path)
        if p.path in PAGE_PATHS:
            self._head(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif p.path in ("/favicon.ico", "/favicon.svg"):
            self._head(ICON, "image/svg+xml; charset=utf-8")
        elif p.path == "/api/groups":
            self._head(json.dumps(group_items(load_items()), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif p.path in ("/playlist.m3u8", "/subscribe", "/playlist.m3u"):
            self._head(build_m3u(load_items()).encode("utf-8"),
                       "application/vnd.apple.mpegurl; charset=utf-8")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path in PAGE_PATHS:
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p.path in ("/favicon.ico", "/favicon.svg"):
            self._send(200, ICON.decode("utf-8"), "image/svg+xml; charset=utf-8")
        elif p.path == "/robots.txt":
            self._send(200, ROBOTS.decode(), "text/plain; charset=utf-8")
        elif p.path == "/health":
            its = load_items()
            self._send(200, json.dumps({
                "ok": True,
                "dramas": len({i.get("id") for i in its}),
                "episodes": len(its),
                "port": PORT,
                "data_src": os.path.basename(data_src() or ""),
                "proxy_hosts": len(allowed_hosts()),
            }, ensure_ascii=False), "application/json; charset=utf-8")
        elif p.path == "/api/groups":
            self._send(200, json.dumps(group_items(load_items()), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p.path == "/api/stream":
            qs = urllib.parse.parse_qs(p.query)
            url = (qs.get("url") or [""])[0]
            ep = (qs.get("ep") or ["1"])[0]
            quality = (qs.get("quality") or ["auto"])[0]
            try:
                stream = resolve_stream(url, ep, quality=quality)
                self._send(200, json.dumps({"stream": stream}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as ex:
                self._send(500, json.dumps({"error": str(ex)}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        elif p.path == "/play":
            qs = urllib.parse.parse_qs(p.query)
            did = (qs.get("id") or [""])[0]
            ep = (qs.get("ep") or ["1"])[0]
            target = next((it.get("url") for it in load_items()
                           if it.get("url") and str(it.get("id")) == did
                           and str(it.get("ep")) == str(ep)), None)
            if not target:
                self._send(404, json.dumps(
                    {"error": "episode not found", "id": did, "ep": ep},
                    ensure_ascii=False), "application/json; charset=utf-8")
                return
            try:
                stream = resolve_stream(target, ep)
                self.send_response(302)
                self.send_header("Location", stream)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            except Exception as ex:
                self._send(500, "resolve failed: %s" % ex, "text/plain; charset=utf-8")
        elif p.path in ("/playlist.m3u8", "/subscribe", "/playlist.m3u"):
            self._send(200, build_m3u(load_items()),
                       "application/vnd.apple.mpegurl; charset=utf-8")
        elif p.path == "/proxy":
            self._proxy()
        elif p.path.startswith("/api/"):
            self._send(404, json.dumps({"error": "not found", "path": p.path},
                                       ensure_ascii=False), "application/json; charset=utf-8")
        else:
            # 未知路径 → 友好 404（列出可用入口），不再返回裸文本 not found
            self._send(404, notfound_page(p.path), "text/html; charset=utf-8")

    def _proxy(self):
        """代取远端 m3u8 / 分片 / AES key，回写地址并补齐 CORS 头，供浏览器播放"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = (qs.get("u") or [""])[0]
        if not target or not proxy_allowed(target):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        origin = "%s://%s" % (urllib.parse.urlparse(target).scheme,
                              urllib.parse.urlparse(target).netloc)
        headers = {"User-Agent": PROXY_UA, "Referer": origin + "/", "Origin": origin,
                   "Accept": "*/*", "Accept-Encoding": "identity"}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(req, timeout=25, context=_SSL_CTX) as r:
                raw = r.read(MAX_PROXY_BYTES)
                ctype = r.headers.get("Content-Type", "") or "application/octet-stream"
        except urllib.error.HTTPError as e:
            self._send(e.code, "upstream http %d" % e.code, "text/plain; charset=utf-8")
            return
        except Exception as e:
            self._send(502, "proxy error: %s" % e, "text/plain; charset=utf-8")
            return

        path_only = target.split("?")[0].lower()
        if "mpegurl" in ctype.lower() or path_only.endswith(".m3u8") or raw[:7] == b"#EXTM3U":
            body = rewrite_m3u8(raw.decode("utf-8", "replace"), target).encode("utf-8")
            ctype = "application/vnd.apple.mpegurl; charset=utf-8"
        else:
            body = raw
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.ThreadingTCPServer):
    # 重启换端口/立刻重开时不会被 TIME_WAIT 上的旧连接卡住
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    try:
        with open(os.path.join(BASE, ".player.pid"), "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    print("播放面板: http://127.0.0.1:%d/" % PORT)
    print("订阅地址: http://<本机IP>:%d/playlist.m3u8" % PORT)
    print("监听地址: %s:%d  (改端口请编辑 panel.conf)" % (HOST, PORT))
    try:
        with _Server((HOST, PORT), App) as srv:
            srv.serve_forever()
    except OSError as e:
        print("[错误] 端口 %d 启动失败：%s" % (PORT, e))
        print("       端口可能被占用，请修改 panel.conf 里的 PANEL_PORT 后重启。")
        raise SystemExit(1)

