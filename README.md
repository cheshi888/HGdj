# 黄果短剧解析与播放

爬取黄豆短剧（新版 APP 站 zuzucast.top / hdmgdj.com 系列线路），提供本地播放面板 + 订阅服务，常驻后台定时刷新播放地址、自动抓取新剧，并保证 7×24 小时稳定运行。

> 旧站 huangguoai.com 已失效（502），现全面切换至新版 APP 加密 API。
> 加密协议为纯 Python 逆向实现（无第三方依赖），详见下方「加密协议」一节。

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 🎬 全站抓取 | 增量抓取新剧，已爬不重复 |
| 🔄 老剧增量 | 已存在剧更新集数时（如 4 集变 9 集），自动补抓新集 |
| 🔑 防地址过期 | 每轮刷新签发超时的播放地址（`force_refresh` 绕过缓存） |
| 🩺 播放健康巡检 | 滚动探测每个播放地址的**真实可播性**，一旦失效**立即重抓补全**（10 分钟一轮，跨轮次覆盖全量） |
| 🛡️ 7×24 看门狗 | `watchdog.py` 崩溃自动拉起 daemon 与播放服务 |
| 🔒 单实例锁 | OS 级文件锁，防止重复爬取损坏进度文件 |
| 📱 自适应播放器 | 手机 / 平板 / 电脑全视口自适应，`contain` 等比缩放不拉伸不变形不裁切 |
| 🔄 旋转播放 | 左/右旋转 90°，竖屏剧横屏看；旋转后自动交换基准宽高，画面**完整铺满且不变形** |
| 🔍 缩放平移 | 40%–500% 无级缩放（按钮 / 滚轮 / 双指捏合），放大或旋转后可拖拽平移 |
| 🖥️ 全屏自适应 | 进入/退出全屏、窗口缩放、屏幕旋转都自动重算画面尺寸 |
| 🔗 流代理 | 内置 `/proxy` 代取 m3u8/分片/key 并回写地址，解决浏览器 CORS 无法播放的问题 |
| 📊 增量统计 | 网页顶栏显示距上次浏览新增多少集/部 |
| 🔀 列表排序 | 默认 / 标题 / 集数多→少 / ID 新旧 |
| 📡 M3U 订阅 | 输出 `playlist.m3u8` 供 NTPlayer / VLC 等订阅 |

## 文件说明

| 文件 | 作用 |
|------|------|
| `zuzu_client.py` | 新站 APP API 加密客户端（复刻 Flutter 端协议：HMAC 会话密钥 + AES-256-CBC + gzip + MD5 签名） |
| `zuzu_aes.py` | 纯 Python AES 实现（ECB/CBC/PKCS7，通过 FIPS-197 / NIST 测试向量） |
| `zuzu_parser.py` | 新站解析器：列表/搜索/详情/取流/批量导出（接口对齐 `huangguo_parser.py`） |
| `crawl_update_zuzu.py` | 新站增量爬虫（接口对齐 `crawl_update.py`，daemon/start 已切换） |
| `huangguo_parser.py` | 旧站 huangguoai.com 页面解析（站点已失效，保留兼容） |
| `daemon.py` | 常驻守护：定时全量刷新 + **播放健康巡检（失效即时补全）** + 自动抓新剧（限速 / 硬超时防卡死） |
| `watchdog.py` | 看门狗：daemon / player 崩溃自动重启（7×24 保障） |
| `player_server.py` | 播放面板（旋转/缩放/全屏自适应的播放器）+ 订阅服务（m3u8）+ `/proxy` 流代理；按 url 协议路由新旧解析器 |
| `video_cache.py` | 视频流缓存（LRU / 过期 / 原子写） |
| `error_handler.py` | 错误分类 / 重试 / 日志（写文件，不打屏） |
| `start.py` / `start.bat` | 一键启动 / 更新 / 停止 |
| `install.sh` / `deploy.sh` | Linux VPS 一键安装（systemd 开机自启） |

## 加密协议（逆向自 main.dart.js）

新版 APP（Flutter Web）所有 `/api/*` 请求均为二进制加密体，协议如下：

```
请求：  body = iv(16B) || AES-256-CBC( gzip( compact_json({"token","deviceId","data"}) ) )
       密钥 = HMAC-SHA256( key=utf8("7961beb44246e3012ce228d6b5ced05a"),
                            msg=hexDecode(requestId去掉横线) )     # UUID 去横线=32 hex=16B
       请求头：version=2.0.0 / deviceType=web / time / requestId / sessionId /
               sign = md5_hex("Dart|sessionId|requestId|time|url去掉scheme") + "-" + time
               X-Lang=zh-CN，Content-Type: application/x-www-form-urlencoded
响应：  iv = body[:16], ct = body[16:]
        plain = AES-256-CBC-Decrypt(ct, iv) -> gunzip -> json（同一 requestId 派生同一密钥）
```

播放地址为签名 URL：`https://<线路>/api/drama/hls/{id}/{集}/play.m3u8?line=free&exp={过期戳}&sig=...`（有效期约 6 小时，daemon 会在剩余不足 30 分钟时自动重签）。多条线路（free/line2/line3）自动优选，付费集仅返回 10 秒预览。

## 🪟 Windows 使用

```bat
start.bat                 # 一键启动：看门狗 + 播放面板 http://127.0.0.1:8899/
python start.py update    # 手动增量更新（全站扫描补新剧）
python start.py stop      # 停止全部后台进程（watchdog/daemon/player）
```

后台保持 7×24 常驻：`start.py` 会启动 `watchdog.py`，即使守护进程意外退出也会在 15 秒内自动拉起。

## 🔌 改端口（只需改一处）

端口写在项目根目录的 **`panel.conf`** 里，面板 / 守护进程 / 看门狗 / 启动脚本 / 部署脚本全部读它：

```ini
PANEL_PORT=8899
PANEL_HOST=0.0.0.0     # 0.0.0.0=局域网可访问，127.0.0.1=仅本机
```

> `panel.conf` 是本机配置、不随代码提交（不会被 `git pull` 覆盖）。文件不存在时用内置默认 **8899**，
> 需要改端口就新建/编辑这个文件即可。

改完重启生效：

```bat
python start.py stop
start.bat
```

不想改文件也可以临时覆盖（不改动配置）：

```bat
set PANEL_PORT=9000 && start.bat        :: Windows
PANEL_PORT=9000 python start.py         # Linux/macOS
```

> 端口被占用时面板会直接提示「端口 xxx 启动失败，请修改 panel.conf 后重启」，不会再静默失败。

## 🐧 Linux VPS 一键部署

在 VPS 上执行**一条命令**，自动装依赖、拉取代码、部署并开机自启（全新安装和后续更新都用这同一条）：

```bash
curl -fsSL https://raw.githubusercontent.com/cheshi888/HGdj/main/install.sh | sudo bash
```

流程：检测包管理器安装 `git`/`python3` → 拉取/更新代码到默认目录 `/opt/HD` → 调用 `deploy.sh` 部署。

部署后访问：
- 播放面板：`http://<VPS公网IP>:8899/`
- 订阅地址：`http://<VPS公网IP>:8899/playlist.m3u8`

**部署脚本特性**
- 自动识别 **systemd**（写入 `huangguo.service` 开机自启）；无 systemd 的机器自动降级为 **nohup 常驻 + crontab @reboot**
- 自动放行 **ufw / firewalld / iptables** 对应端口
- 部署后**自动自检** `http://127.0.0.1:<端口>/health`，就绪才打印访问地址
- GitHub 直连失败自动**回退镜像**（`ghfast.top` / `ghproxy.net`），也可用 `GITHUB_PROXY=...` 指定

**常用运维指令**

```bash
sudo bash /opt/HD/deploy.sh status     # 查看运行状态
sudo bash /opt/HD/deploy.sh restart    # 重启
sudo bash /opt/HD/deploy.sh stop       # 停止
bash /opt/HD/deploy.sh info            # 打印访问地址（无需 root）
systemctl status huangguo                          # systemd 方式：状态
journalctl -u huangguo -f                          # systemd 方式：实时日志
```

> 仅需 Python3，无任何第三方依赖。更新代码 = 重新执行上面那条 `curl` 命令。
> 端口以 `panel.conf` 的 `PANEL_PORT` 为准（默认 8899），记得在云厂商「安全组」放行该端口。

**可选环境变量**

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO` | `cheshi888/HGdj` | 仓库 |
| `BRANCH` | `main` | 分支 |
| `APP_DIR` | `/opt/HD` | 安装目录 |
| `GITHUB_PROXY` | 空 | GitHub 镜像前缀，直连失败时使用 |
| `PANEL_PORT` | `panel.conf` | 面板端口 |
| `FORCE_INIT` | 自动 | `systemd` / `nohup` 强制指定托管方式 |
| `DRY_RUN` | 0 | `1` = 只打印将要执行的动作，不改动系统（自检用） |


## 📡 M3U 订阅（播放器如 NTPlayer / VLC）

```
http://<本机IP>:8899/playlist.m3u8
```

## 🧭 面板可用地址

| 地址 | 说明 |
|------|------|
| `/`、`/index.html`、`/player`、`/panel`、`/home`、`/app`、`/drama`、`/web` | 播放面板（等价入口，手输/旧收藏都不会 404） |
| `/playlist.m3u8`（别名 `/subscribe`、`/playlist.m3u`） | M3U 订阅 |
| `/api/groups` | 面板数据接口（JSON） |
| `/api/stream?url=&ep=&quality=` | 取单集新鲜播放地址（JSON） |
| `/proxy?u=<编码后的地址>` | 流代理：代取 m3u8/分片/AES key 并回写地址、补 CORS 头 |
| `/health` | 运行状态：剧集数、数据来源文件、代理白名单主机数 |
| `/favicon.ico`、`/robots.txt` | 图标 / 爬虫声明 |

> 未知路径不再返回裸文本 `not found`：浏览器访问会看到带跳转链接的 404 页面，`/api/*` 未知路径返回 `{"error":"not found"}`。
> 面板数据取自 `all_streams.json` 与 `crawl_progress.json` 中**修改时间较新的一个**，爬取过程中也能看到最新数据。

## ⚙️ 守护进程参数（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `REFRESH_MINUTES` | 30 | 全量刷新间隔（分钟） |
| `REFRESH_AGE_SECONDS` | 1800 | 播放地址剩余有效期不足该秒数即重新抓取（约剩 30 分钟） |
| `HEALTH_ENABLED` | 1 | 是否开启播放健康巡检（设 0 关闭） |
| `HEALTH_MINUTES` | 10 | 播放巡检间隔（分钟），比全量刷新更高频 |
| `HEALTH_BUDGET` | 120 | 每轮巡检时间预算（秒），预算内尽量多查，游标跨轮次连续覆盖全量 |
| `HEALTH_WORKERS` | 10 | 巡检并发数 |
| `HEALTH_TIMEOUT` | 8 | 单个播放地址探测超时（秒） |
| `DAEMON_TICK` | 30 | 守护进程调度轮询间隔（秒） |
| `SCAN_LIMIT` | 3 | 每轮快速扫描最多翻多少页（新站为扁平列表，无分类） |
| `FULL_SCAN_EVERY` | 12 | 每多少轮做一次全站全量扫描 |
| `NEW_LIMIT` | 20 | 每轮最多爬取新剧部数（防一轮跑几小时） |
| `ROUND_TIMEOUT` | 600 | 每轮硬超时秒数（超时跳过本轮不卡死） |
| `INCR_CHECK` | 10 | 每轮检查多少部老剧的新增集数 |
| `CRAWL_WORKERS` | 12 | 抓取并发线程数 |
| `PANEL_PORT` | panel.conf（默认 8899） | 播放服务端口，改端口只需编辑 `panel.conf` |
| `SCAN_CACHE_TTL` | 1800 | 爬虫复用扫描结果的有效秒数（重启免重复全站扫描；设 0 关闭） |
| `ZUZU_JOBS` | 6 | 全量爬取时剧级并发数 |

Linux 下通过编辑 `/etc/systemd/system/huangguo.service` 的 `Environment=` 行调整。

## 🩺 后台巡检机制（常驻自动补全）

守护进程交错执行两条独立时间线，互不阻塞：

| 周期 | 任务 | 作用 |
|------|------|------|
| 每 10 分钟 | 播放健康巡检（`health_check`） | 按持久化游标滚动抽查播放地址，实际 GET m3u8 校验返回 `200 + #EXTM3U`；**发现失效立即重新取流补全**，补全后再复核一次；仍失败则标记 `error` 下轮继续重试 |
| 每 30 分钟 | 全量刷新（`refresh_once`） | ① 重签即将过期的地址 ② 老剧新增集数增量抓取 ③ 抓取新剧（每轮限 `NEW_LIMIT` 部） |

巡检游标存在 `.health_cursor.json`，因此**跨轮次连续覆盖全量**、不会反复扫同一批；单轮在时间预算内尽量多查（约 12 条/秒），全量 2 万余条流约数小时轮完一圈，此后持续循环。

任何一步异常都只跳过该步，不会中断常驻进程。

## 🖥️ 播放面板

- 顶部搜索框实时过滤剧集
- 排序下拉：默认 / 标题 / 集数多→少 / 集数少→多 / ID 新→旧
- 顶栏橙色文字显示距上次浏览的增量（如 `+12集 / +3部`）

**播放器操作**

| 操作 | 按钮 | 快捷键 |
|------|------|--------|
| 上一集 / 下一集 | ⏮ ⏭ | `←` / `→` |
| 向左 / 向右旋转 90° | ⟲ ⟳ | `R`（Shift+R 反向） |
| 缩小 / 放大 | − ＋ | `-` / `+` |
| 重置画面 | ⤢ | `0` |
| 全屏 / 退出全屏 | ⛶ | `F` |
| 关闭播放器 | ✕ | `Esc` |

- 缩放锚点：按钮以画面中心缩放；滚轮以鼠标位置、双指以两指中点缩放
- 放大或旋转后，按住画面可拖拽平移（避开底部进度条区域）
- 画面始终 `object-fit: contain` 等比缩放，任何旋转/缩放/全屏组合都**不会变形**
- 播放失败（地址失效）时前端会自动向后端索取新地址重播一次

> 站点的 CDN 不返回 CORS 头，浏览器直连播放会被拦截。面板内置 `/proxy?u=<编码后的地址>`
> 代取播放列表/分片/AES key 并回写内部地址、统一补上 CORS 头；代理仅放行站点线路域
> 及在其播放列表中出现过的 CDN 主机（防开放代理），其他域名返回 403。

## ⚠️ 免责声明

本项目仅用于个人学习与技术交流。爬取内容版权归原网站所有，请勿用于商业用途或传播侵权内容。
