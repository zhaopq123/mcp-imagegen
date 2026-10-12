# MCP 图片生成 Server · 安装说明（install.md）

| 项 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 日期 | 2026-10-10 |
| 作者 | Leo（文档工程师） |
| 依据 | 《技术规格书》第七节环境变量总表、《开发设计方案》S4.3；环境变量名/默认值**逐字取自项目根 `config.py` 与 `config.example.env`**（撰写前已通读）；依赖清单以项目根 `requirements.txt` 为准；关键命令（--check / healthz / 401 / MCP initialize 握手 / py-spy / df 水位脚本）已在干净 venv 实测通过；§7/§11 已与 deploy/ 下 Ethan 交付的 systemd 单元与 Caddyfile 逐字核对一致；**V2 改动依据《MCP图片视频生成Server_增量设计方案V2.md》三节（环境变量）与六节（工具描述）**，变量名/默认值以 `config.py` 为准（`config.example.env` 存在 V1 残留死行，见 §5.1 待确认注），§6 的 V2 自检输出引自 `docs/V2第四轮收尾补测_交付说明.md` 实测记录 |
| 适用 | Linux + systemd（示例命令为 Debian/Ubuntu 系；RHEL 系把 `apt-get` 换成 `dnf` 即可） |

> **部署形态**：`Agent ──(Bearer)──> [Caddy 可选 HTTPS] ──> systemd 守护的 Python 进程（0.0.0.0:8000）──> Agnes AI API（api.agnes-ai.cn，V2 单一上游双模型：agnes-image-2.5-flash / agnes-video-2.5-flash）`
> 组件版本对齐链（勿随意改动）：`read ≤ 分档 ≤ TOTALBUDGETSEC(200) < Agent 建议超时(210) < TimeoutStopSec(220)`。

## 0. 路径与端口总览

| 位置 | 用途 |
|---|---|
| `/opt/mcp-imagegen/` | 代码部署目录（含 `server.py`、`app/`、`engine/`、`infra/`、`ops/`、`config.py`、`deploy/`） |
| `/opt/mcp-imagegen/venv/` | Python 虚拟环境（依赖 + py-spy） |
| `/etc/mcp-imagegen/env` | EnvironmentFile（`root:root`，权限 600） |
| `/var/lib/mcp-imagegen/images/` | 图片落盘目录（`IMAGE_DIR` 默认值） |
| `/var/lib/mcp-imagegen/videos/` | 视频落盘目录（`VIDEO_DIR` 默认值，**V2 新增**；persist 转存视频文件落此处） |
| TCP `8000` | 服务监听端口（`PORT` 默认值；uvicorn 绑定 0.0.0.0） |
| `mcp-imagegen.service` | 主服务单元 |
| `mcp-imagegen-disk-check.service` + `.timer` | 每日磁盘水位巡检（超线只 WARN，不删图） |

**前提条件**：root 或 sudo 权限；Python ≥ 3.10（fastmcp 硬性要求）；服务器可访问 PyPI 与 Agnes AI API（`api.agnes-ai.cn`，V2 唯一上游）。

## 1. 系统依赖安装

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl rsync
python3 --version   # 确认 ≥ 3.10
```

> py-spy（运维三件套之一，验收⑰）不走系统包，随部署 venv 预装，见 §4。

## 2. 创建专用低权限用户与目录

```bash
# 专用系统用户：不可登录、无 root 权限，独占运行本服务
sudo useradd --system --user-group \
     --home-dir /opt/mcp-imagegen --create-home \
     --shell /usr/sbin/nologin \
     mcp-imagegen

# 图片/视频落盘目录（IMAGE_DIR / VIDEO_DIR 默认值，见 §5 变量表）
# VIDEO_DIR 为 V2 新增：persist 转存视频专用，须专用低权限用户 mcp-imagegen
# 可写；应用启动时也会尝试自建（自建失败仅降级 persist，不阻断启动），
# 但部署时预建 + 授权才是正道，勿依赖运行时自愈
sudo mkdir -p /var/lib/mcp-imagegen/images /var/lib/mcp-imagegen/videos
sudo chown -R mcp-imagegen:mcp-imagegen /var/lib/mcp-imagegen
sudo chmod 0750 /var/lib/mcp-imagegen /var/lib/mcp-imagegen/images /var/lib/mcp-imagegen/videos

# 配置目录
sudo mkdir -p /etc/mcp-imagegen
```

## 3. 部署代码到 /opt/mcp-imagegen

`SRC` 指向代码检出根目录（即包含 `server.py`、`app/`、`engine/`、`infra/`、`ops/`、`config.py`、`config.example.env`、`requirements.txt`、`deploy/` 的目录）：

```bash
SRC=/path/to/mcp-imagegen     # ← 按实际路径修改

sudo rsync -a --delete --exclude venv "$SRC"/ /opt/mcp-imagegen/
sudo chown -R mcp-imagegen:mcp-imagegen /opt/mcp-imagegen
```

> `--exclude venv` 保护已建好的虚拟环境不被更新覆盖删除（`rsync --delete` 不动 excluded 项）。

## 4. 创建 venv 并安装依赖（requirements.txt 为准）+ py-spy 预装

```bash
sudo python3 -m venv /opt/mcp-imagegen/venv
sudo /opt/mcp-imagegen/venv/bin/pip install --upgrade pip
sudo /opt/mcp-imagegen/venv/bin/pip install -r /opt/mcp-imagegen/requirements.txt
sudo /opt/mcp-imagegen/venv/bin/pip install py-spy
sudo chown -R mcp-imagegen:mcp-imagegen /opt/mcp-imagegen/venv
```

依赖说明（与 `requirements.txt` 一致）：

| 包 | 角色 | 备注 |
|---|---|---|
| `fastmcp` | MCP 工具端点（streamable-http）与工具 schema | 会传递安装 `uvicorn`、`starlette` 等，**无需单列** |
| `httpx` | Agnes API 客户端（生图/生视频/任务查询）+ persist 图片/视频下载（V2） | — |
| `py-spy` | 运维三件套③（调用栈 dump，验收⑰） | **不在 requirements.txt 内**，按设计 S1.1 由部署文档预装（即本步） |

> `requirements.txt` 当前未钉版本；其文件头注释建议生产钉死（示例：`fastmcp==2.14.3 httpx==0.28.1`）。需钉版时直接编辑 `requirements.txt`，重跑本节 `pip install -r` 即可。
> sd_notify 为 vendored 实现（`ops/liveness.py`），**不依赖** sdnotify 包，勿安装。

## 5. EnvironmentFile：/etc/mcp-imagegen/env（600 权限）

```bash
sudo cp /opt/mcp-imagegen/config.example.env /etc/mcp-imagegen/env
sudo chown root:root /etc/mcp-imagegen/env
sudo chmod 600 /etc/mcp-imagegen/env
sudoedit /etc/mcp-imagegen/env      # 填入两个密钥
```

- 文件由 systemd（root）在启动时读取注入进程，服务用户无需直接读权限，故 `root:root 600` 最小暴露。
- **格式注意**：`KEY=value` 一行一条，值不加引号、等号两侧不留空格（systemd EnvironmentFile 语法，非 shell 脚本）。

### 5.1 环境变量总表（V2 重写：逐字对齐 `config.py`，共 25 项）

**密钥（必填 2 项，缺失/为空 → 启动 fail-fast，退出码 2）：**

| 变量 | 填写值 |
|---|---|
| `MCPACCESS_TOKEN` | Agent 客户端 Bearer Token（自拟强随机串，与 agent-config.md 配套） |
| `AGNES_TOKEN` | Agnes AI 上游 API Key（`api.agnes-ai.cn` 控制台 → API Keys 生成；**V2 替代原 `SILICONFLOWTOKEN`**，上线前须换正式 Key，见 §13） |

**其余 23 项（`config.example.env` 已按默认值预填；留空或不写 = 使用默认值）：**

| 变量 | 默认值（config.py） | 分组 |
|---|---|---|
| `AGNES_BASE_URL` | `https://api.agnes-ai.cn` | Agnes 上游（V2 增量） |
| `PORT` | `8000` | 基础 |
| `IMAGE_DIR` | `/var/lib/mcp-imagegen/images` | 基础 |
| `VIDEO_DIR` | `/var/lib/mcp-imagegen/videos` | 基础（V2 增量；须专用低权限用户可写，见 §2） |
| `MAXCONCURRENCY` | `3` | 并发与排队（等待上限 30s，到点返 busy；生图/生视频共用槽池） |
| `QUEUEWAIT_SEC` | `30` | 并发与排队 |
| `TOTALBUDGETSEC` | `200` | 超时预算（请求总预算，图/视频共用） |
| `TIMEOUT_AGNES_IMAGE_SEC` | `60` | Agnes read 档（V2 增量；生图上游读超时；D5 自 30 调整） |
| `VIDEO_SUBMIT_TIMEOUT_SEC` | `30` | Agnes read 档（V2 增量；视频提交读超时） |
| `VIDEO_QUERY_TIMEOUT_SEC` | `10` | Agnes read 档（V2 增量；单次任务查询读超时） |
| `VIDEO_POLL_INTERVAL_SEC` | `2` | 视频轮询间隔（V2 增量；D7 自 5 调整，官方建议 1–2s） |
| `TASK_TTL_SEC` | `7200` | 视频任务表（V2 增量；TTL 2h，内存态重启清零） |
| `TASK_MAX` | `100` | 视频任务表（V2 增量；容量上限，超出逐出旧任务） |
| `PERSIST_TIMEOUT_IMAGE_SEC` | `30` | 超时预算（V2 更名自 PERSISTTIMEOUTSEC，纯改名） |
| `PERSIST_TIMEOUT_VIDEO_SEC` | `120` | 超时预算（V2 增量；视频 persist 下载） |
| `CIRCUITHARDSEC` | `600` | 熔断（V2 起硬=429；402 已随 Provider 删除；内存态，重启清零） |
| `CIRCUITSOFTSEC` | `300` | 熔断（软=连败 2 次） |
| `HTTPXKEEPALIVESEC` | `25` | httpx 连接层 |
| `HTTPXCONNECTSEC` | `5` | httpx 连接层 |
| `HTTPXWRITESEC` | `5` | httpx 连接层 |
| `HTTPXPOOLSEC` | `5` | httpx 连接层 |
| `HTTPXMAXCONNECTIONS` | `10` | httpx 连接层 |
| `HTTPXMAXKEEPALIVECONNECTIONS` | `4` | httpx 连接层 |

**V2 已删除变量（`config.py` 不再读取，随硅基流动 Provider 一并删除）**：`SILICONFLOWTOKEN`、`TIMEOUTSCHNELL`、`TIMEOUTKOLORSSMALL`、`TIMEOUTKOLORS_LARGE`。

> ⚠ **待确认（文档核对发现，本轮不动代码文件）**：项目根 `config.example.env` 中 `TIMEOUTSCHNELL` / `TIMEOUTKOLORSSMALL` / `TIMEOUTKOLORS_LARGE` 三行仍残留（死配置，不影响启动），且 `AGNES_TOKEN` 在"密钥段"与"Agnes 段"出现两行（同值无害但重复）——建议开发侧下轮清理该样例文件；本表以 `config.py` 为准。

**改动须知**：

- 改配置需 `systemctl restart` 生效（只读配置，无热更新）；
- 启动自检嵌套不等式（红线 B6，违例即 fail-fast）：`read ≤ 分档 ≤ TOTALBUDGETSEC(200) < Agent 建议(210) < TimeoutStop(220)`；V2 另校验 Agnes read 档（`生图/单查/提交 read ≤ 单查 ≤ 提交 ≤ 总预算`）与视频任务表参数（见 §6 输出第三行）；故调大 `TOTALBUDGETSEC` 前须同步核对 Agent 客户端超时与 systemd `TimeoutStopSec`。

## 6. 启动前配置自检（一次性，退出码 0=通过 / 2=失败）

```bash
sudo sh -c 'cd /opt/mcp-imagegen && set -a && . /etc/mcp-imagegen/env && set +a && venv/bin/python server.py --check'
```

实测通过时的输出（节选）：

```
[config] 嵌套不等式自检: read ≤ 分档(max=60s) ≤ deadline(200s) < Agent建议(210s) < TimeoutStop(220s) ✓
[config] 队列等待 30s ≤ 总预算 ✓ | persist 下载 图30s/视频120s ≤ 总预算 ✓ | 并发槽 3 | 熔断 硬600s/软300s
[config] V2 Agnes read 档自检: 生图 60s / 提交 30s / 单查 10s ≤ 单查 ≤ 提交 ≤ deadline (200s) ✓ | 轮询 2s | 任务表 TTL 7200s / 容量 100 ✓
[startup] 配置自检通过（--check）
```

> 密钥缺失/为空时输出 `[startup] 配置自检失败: 必填环境变量缺失: ...`，退出码 2——先回 §5 补齐再继续。
> systemd 单元已内置 `ExecStartPre=... server.py --check`（§7.1），此后每次 start/restart 都会自动自检、违例阻止启动；本步是 systemd 装载**前**的提前验证。

## 7. systemd 装载与启用

单元文件随代码在 `/opt/mcp-imagegen/deploy/` 下（`mcp-imagegen.service`、`mcp-imagegen-disk-check.service`、`mcp-imagegen-disk-check.timer`）：

```bash
sudo install -m 0644 /opt/mcp-imagegen/deploy/mcp-imagegen.service /etc/systemd/system/
sudo install -m 0644 /opt/mcp-imagegen/deploy/mcp-imagegen-disk-check.service /etc/systemd/system/
sudo install -m 0644 /opt/mcp-imagegen/deploy/mcp-imagegen-disk-check.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now mcp-imagegen.service
sudo systemctl enable --now mcp-imagegen-disk-check.timer
```

### 7.1 单元关键参数核对表（已定案参数；装载前 diff 实际文件核对）

`mcp-imagegen.service`：

| 参数 | 应为 | 对应事实 |
|---|---|---|
| `Type=notify` + `WatchdogSec=30` | — | 心跳 15s（`ops/liveness.py`），僵死 30s 判死重启（验收⑬） |
| `Restart=always` + `RestartSec=5` | — | 崩溃自愈 |
| `StartLimitBurst=5` + `StartLimitIntervalSec=60` | — | 连崩风暴保护（验收⑭） |
| `TimeoutStopSec=220` | — | 优雅排空窗口（= uvicorn `timeout_graceful_shutdown`；对齐 §0 版本链） |
| `MemoryMax=512M` | — | 内存上限 |
| `User=mcp-imagegen` + `Group=mcp-imagegen` | — | 专用低权限用户（§2） |
| `WorkingDirectory=/opt/mcp-imagegen` | — | `server.py` 同级导入 `config`/`app`/`engine` 等模块 |
| `ExecStartPre=/opt/mcp-imagegen/venv/bin/python server.py --check` | — | 每次启动前自动配置自检，违例（退出码 2）阻止启动 |
| `ExecStart=/opt/mcp-imagegen/venv/bin/python server.py` | — | 前台运行，交 systemd 守护 |
| `EnvironmentFile=/etc/mcp-imagegen/env`（root:root 600，systemd 读取注入，服务用户无需可读） | — | §5；V2 全部新变量（`AGNES_TOKEN`、`VIDEO_DIR`、视频三超时、任务表两项等）经此同一文件注入，单元文件无变量硬编码，**无需改动** |
| `After=network.target` | — | 依赖网络就绪 |
| `SyslogIdentifier=mcp-imagegen` | — | journal 中的日志标识 |
| `NoNewPrivileges/PrivateTmp/ProtectHome/ProtectSystem=full` | — | 轻量加固（V2：应用写 `IMAGE_DIR` / `VIDEO_DIR`，两目录均落 `/var/lib`，`ProtectSystem=full` 不禁止，兼容） |
| `WantedBy=multi-user.target` | — | 开机自启挂钩 |

`mcp-imagegen-disk-check.service` / `.timer`：**每日 04:30（本地时间）**巡检磁盘水位——复用应用代码取数（`config.DISK_WATERMARK_RATIO=0.90` 与 `PersistService.disk_usage_ratio()`，单一出处无新魔法数值）；**V2 起水位覆盖 `IMAGE_DIR` 与 `VIDEO_DIR` 双目录**（`disk_usage_ratio()` 取两目录所在文件系统使用率的**最大值**，最保守判定；典型部署两目录同盘）；触发时刻刻意错开应用内 04:00 每日客户端重建；`Persistent=true`（停机错过则下次启动补跑）；超线**只记 WARN 日志 + meta 提示，绝不自动删图/视频**（C10），退出码恒 0（不把 timer 打红，处置留给人工）。

> **核对结论（2026-10-10）**：deploy/ 下三个单元文件已由 Ethan 交付并落盘，上表与交付文件逐字核对一致（含 `ExecStartPre`、加固项与 `SyslogIdentifier`）。
>
> **V2 文档层面核对（2026-10-10）**：三单元与 Caddyfile **功能层无需改动**——V2 新变量均经同一 `EnvironmentFile` 注入（见上表）；`/files/*` 视频（`.mp4`/`.webm`）与图片同由后端按扩展名分流，Caddy 路由原样透传即可；水位检查经应用代码已覆盖双目录。**注释层 4 处滞后**（纯注释、不影响功能；按任务纪律本轮不动文件，列清单交开发确认后下轮统一改）：
> 1. `mcp-imagegen.service` 头部安装注释：仅示范创建 images 目录，缺 videos（与本文 §2 对齐即可）；
> 2. `mcp-imagegen.service` 注释「（含 SILICONFLOWTOKEN / MCPACCESS_TOKEN）」→ 应改「（含 MCPACCESS_TOKEN / AGNES_TOKEN）」（`SILICONFLOWTOKEN` 已删除）；
> 3. `mcp-imagegen.service` 注释「应用仅写 IMAGE_DIR」→ 应改「应用写 IMAGE_DIR / VIDEO_DIR」；
> 4. `mcp-imagegen-disk-check.service` 头部注释两处「IMAGE_DIR 所在文件系统」→ 应改双目录表述；
> 另（可选）：`Caddyfile` 注释「图片静态服务」可顺手改「图片/视频静态服务」，同为纯注释。

## 8. 安装验证（命令已实测，逐项应通过）

```bash
systemctl is-active mcp-imagegen                      # → active
sudo journalctl -u mcp-imagegen -n 20 --no-pager      # 应见 [config] 自检行 + [startup] 就绪 port=8000
curl -s http://127.0.0.1:8000/healthz                 # → {"status":"ok"}
curl -s -i http://127.0.0.1:8000/mcp                  # → HTTP 401 + [error] category=mcp_auth retry=no ...（无 token 亦应 401，证明鉴权在位）
systemctl list-timers 'mcp-imagegen*'                 # 磁盘水位 timer 应在列（每日 04:30）
sudo systemctl start mcp-imagegen-disk-check.service  # 手动触发一次水位巡检（oneshot）
sudo journalctl -u mcp-imagegen-disk-check -n 5 --no-pager
sudo journalctl -u mcp-imagegen-disk-check -p warning --no-pager   # 只看超线/不可得告警
```

> healthz 为人工巡检探活（不参与僵死判据）；工具调用请走 `/mcp` + Bearer，完整客户端侧自检见 `agent-config.md`。

## 9. 更新操作流程（先确认无在途，再 restart）

```bash
# ① 确认无在途请求：看最近日志——一行 15 字段（ts/req_id/.../total=，V2 起 +task_id/poll_count/split_calls）即一个请求收尾；
#    最后一行之后无新行、无进行中的调用方任务，才算"无在途"
sudo journalctl -u mcp-imagegen --since "-10 min" --no-pager
# 辅助：观察 8000 口连接平稳无增长（注意：Caddy 前置时其常驻连接池会计入，属正常）
ss -tan state established '( sport = :8000 )'

# ② 部署新代码（venv 原地保留）
SRC=/path/to/mcp-imagegen
sudo rsync -a --delete --exclude venv "$SRC"/ /opt/mcp-imagegen/
sudo chown -R mcp-imagegen:mcp-imagegen /opt/mcp-imagegen

# ③ 依赖有变时重装（requirements.txt 为准）
sudo /opt/mcp-imagegen/venv/bin/pip install -r /opt/mcp-imagegen/requirements.txt

# ④ 启动前自检（退出码必须为 0）
sudo sh -c 'cd /opt/mcp-imagegen && set -a && . /etc/mcp-imagegen/env && set +a && venv/bin/python server.py --check'

# ⑤ 重启并复核
sudo systemctl restart mcp-imagegen
systemctl is-active mcp-imagegen && curl -s http://127.0.0.1:8000/healthz
sudo journalctl -u mcp-imagegen -n 10 --no-pager    # 应见 [shutdown] 在途清零（或"在途 0 个"）+ 新一轮 [startup] 就绪
```

> 兜底说明（R9）：即便 ① 漏判了在途请求，restart 的 SIGTERM 也会**先停收新请求、排空在途**（上限 220s = TimeoutStopSec，覆盖排队 30s + 请求预算 200s 的最坏叠加；V2 视频 persist 下载读超时取 `min(PERSIST_TIMEOUT_VIDEO_SEC, 预算剩余)`，不越预算），排空日志见 `[shutdown]` 行；仅超 220s 才由 systemd SIGKILL 强杀。

## 10. 日常运维三件套

| # | 命令 | 用途 |
|---|---|---|
| ① | `sudo journalctl -u mcp-imagegen -f` | 实时日志。一行 = 一个请求，15 字段（V2 自 12 扩）：`ts / req_id / prompt_hash / route / model / params / stages / result / fallback / timeout_hit / persist / task_id / poll_count / split_calls / total`——视频请求 `task_id`/`poll_count` 有值、`split_calls=null`，生图相反；`result` 新增 `running`（视频超预算收条，非错误）（journald 单行可 grep，如 `journalctl -u mcp-imagegen \| grep 'result=error'`） |
| ② | `ss -tan state close-wait | wc -l` | CLOSE-WAIT 计数。健康基线 ≈ 0（验收⑯，24h 观察）；持续 >0 说明上游/代理侧半死连接堆积，结合每日 04:00 客户端重建与 keep-alive 25s 观察，必要时 restart 排空 |
| ③ | `sudo /opt/mcp-imagegen/venv/bin/py-spy dump --pid $(systemctl show -p MainPID --value mcp-imagegen)` | Python 调用栈快照（验收⑰）。长请求疑似卡死时先 dump 再决定是否 restart；需 sudo（ptrace 权限） |

## 11. Caddy 可选前置（反代 + 可选 HTTPS；读超时 ≥210s）

**何时需要**：服务器暴露公网 / 需要统一入口。内网自用可跳过（Agent 直连 `http://<服务器IP>:8000/mcp`）。交付 Caddyfile 默认 `:80` 明文，换域名即自动升级 HTTPS。

### 11.1 安装 Caddy（官方稳定源，Debian/Ubuntu）

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
```

### 11.2 配置

正式配置使用随代码交付的 `/opt/mcp-imagegen/deploy/Caddyfile`（已落盘，与下述行为核对一致）：

```bash
sudo install -m 0644 /opt/mcp-imagegen/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

交付 Caddyfile 要点（装载后按此核对）：

- 站点地址默认 `:80`（明文 HTTP，本机/内网**即装即用**）；把 `:80` 换成真实域名后 Caddy 自动签发 HTTPS 并 301 跳转，无需其他改动；
- 三条路由均反代 `127.0.0.1:8000`（改 `PORT` 需同步改 Caddyfile）：
  | 路由 | 读超时 | 说明 |
  |---|---|---|
  | `/mcp*` | `response_header_timeout 220s` | ≥210s 达标；超时链 `总预算 200 < Agent 210 ≤ Caddy 220 = TimeoutStopSec 220`，代理不先于客户端断开；`flush_interval -1` SSE 即时透传不攒包 |
  | `/files/*` | `response_header_timeout 220s` | 图片/视频（V2）走后端白名单校验与防穿越，不在 Caddy 侧直读磁盘；视频文件大，下载读满窗口属正常 |
  | `/healthz*` | `response_header_timeout 30s` | 探活应快速失败，不必等满 220s |
- Caddy server 层超时保持默认（读 body 无限制、写无超时），长请求与 SSE 流安全；
- Caddy 反代默认注入 `X-Forwarded-Proto` / `X-Forwarded-Host`，服务端据此拼 `/files` 完整 URL——换域名启用 HTTPS 后自动变为 `https://<域名>/files/...`，勿在 Caddy 侧剥离这些头。

### 11.3 前置条件与验证

- 仅 `:80` 明文使用：无前置条件，装好即用；
- 启用 HTTPS（推荐公网场景）：域名 A 记录指向服务器，防火墙/安全组放行 80、443，Caddyfile 站点地址换成域名即可；证书自动签发续期；
- 验证：`curl -s http://127.0.0.1/healthz`（或配好域名后 `curl -s https://<你的域名>/healthz`）→ `{"status":"ok"}`；
- 启用 Caddy 后，Agent 客户端相应改用 `http://<服务器>/mcp` 或 `https://<你的域名>/mcp`（见 `agent-config.md`）。

## 12. 常见问题速查

| 现象 | 排查 |
|---|---|
| 启动即退出，退出码 2 | `sudo journalctl -xeu mcp-imagegen`：密钥为空 / 嵌套不等式违例（§5、§6） |
| 客户端 401 `[error] category=mcp_auth` | `/etc/mcp-imagegen/env` 的 `MCPACCESS_TOKEN` 与客户端 Bearer 不一致 |
| 上游 401/403 `[error] category=upstream_auth` | `AGNES_TOKEN` 配错或 Key 无效/过期（provider=Agnes；上线前按 §13 ② 在 Agnes 控制台核对；服务器侧问题，客户端勿重试） |
| 服务反复重启后停 | `StartLimit` 5 次/60s 风暴保护触发；`journalctl -u mcp-imagegen` 看崩溃根因 |
| 日志出现磁盘水位 WARN | `df -h /var/lib/mcp-imagegen/images /var/lib/mcp-imagegen/videos`（V2 水位覆盖双目录）；**人工**清理旧文件（服务永不自动删图/视频，C10） |
| 工具调用慢但成功 | 正常：视频轮询受 200s 总预算约束（超预算返 `task_id` 收条非错误，稍后 `query_video` 续查）；客户端超时须 ≥210s（详见 agent-config.md §4） |
| `query_video` 返回 `task_not_found` | 预期行为：任务超 TTL（默认 2h）/容量逐出/服务重启（任务表内存态清零）——按 retry=fixthenretry 重新提交生成即可，勿原样重查 |

---

## 13. 上线前检查清单（V2 新增；普通安装验证见 §8，此处是上线闸门）

首次部署/正式上线前逐项确认：

| # | 检查项 | 要点 |
|---|---|---|
| ① | **换正式 Agnes Key** | 群内发过的测试 Key 已随消息暴露且实测 401 无效——**不得用于上线**。在 Agnes 控制台重新生成正式 Key，填入 `/etc/mcp-imagegen/env` 的 `AGNES_TOKEN`（600 权限文件；Key 不入代码仓库/文档/日志，纪律 B7） |
| ② | **Agnes 控制台核对 Key** | `api.agnes-ai.cn` 后台 → API Keys：确认 Key 在有效期内/未吊销；完整复制整串（`sk-` 开头，无空格换行截断）；确认是 API 专用 Key（聊天应用 Token 与 API Key 不通用）。此前实测：无效 Key 返回 401 `Invalid token`——端点可达、属 Key 校验层拒绝，换 Key 即过 |
| ③ | **首次部署附录 A 实测项** | 按项目内方案《MCP图片视频生成Server_增量设计方案V2.md》九节 9.3「Key 到位后 30 分钟首轮联调清单」五项执行并回填：生图默认档（响应字段/耗时校 30s 档）、生图 2K 档（耗时校 `TIMEOUT_AGNES_IMAGE_SEC`）、非法 size（400 错误体 message 透传）、视频提交→轮询至完成（状态枚举/总耗时校 A-5）、连发 3 次生图观察 429 与 Retry-After——对应附录 A-2/A-3/A-4/A-5 实测回填 |
| ④ | 换 Key 后重启与复核 | `systemctl restart mcp-imagegen`（EnvironmentFile 改动须 restart 生效）→ `journalctl` 确认 §6 三行自检 + `[startup]` 就绪；再跑一遍 §8 安装验证 |

---

## 版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| V1.0 | 2026-10-10 | 初版：安装/配置/systemd 装载/更新流程/运维三件套/Caddy 前置；环境变量与 `config.py`、`config.example.env` 逐字核对；关键命令实测通过；与 Ethan 交付的 systemd 单元、Caddyfile 逐字核对一致（含 `ExecStartPre`、水位 timer 04:30、Caddy `response_header_timeout 220s`） |
| V2.0 | 2026-10-10 | V2 部署文档同步（Agnes 双模型三工具）：§5.1 变量表重写（19→25 项，硅基四变量删除、Agnes/视频/任务表九项新增）；§2 增 `VIDEO_DIR` 创建与权限；§6 自检输出更新为 V2 三行；§7.1 双目录水位 + 单元注释 4 处滞后清单（功能层无需改）；§9/§10 日志 12→15 字段；§12 排查表适配；新增 §13 上线前检查清单 |

下一步建议：正式 Key 落位后先过 §13 上线闸门（换 Key → 控制台核对 → 附录 A 实测回填），再按 §1–§8 真机完整演练并逐项对账，进入 24h 稳定性观察（验收⑯）；开发确认 §7.1 注释滞后清单后，下轮统一改单元文件注释（连同 `config.example.env` 残留清理）。
