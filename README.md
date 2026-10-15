# mcp-imagegen

> 基于 **Provider 抽象** 的图片 / 视频生成 MCP Server —— 可接入任意第三方或自建上游，零默认配置即开箱，配好管理面板才可用，自带管理控制台。

一个 Model Context Protocol（MCP）服务器，把文生图与文生视频封装成标准 MCP 工具，
供 Claude Desktop / DSH / 豆包工作 / WorkBuddy 等任意支持 MCP 的客户端调用。
自带并发槽池、熔断、持久化转存与优雅停机，适合长期挂在低功耗小主机上跑。

---

## 特性

| 能力 | 说明 |
|---|---|
| **六工具** | `generate_image`（文生图/参考图生图，支持批量拆分）· `generate_video`（文生视频、首尾帧/参考图、服务端排队）· `query_video`（查询视频任务状态）· `list_video_tasks`（批量查看后台作业）· `render_image`（**代码生图**：SVG/Python → PNG）· `render_video`（**代码生视频**：SVG/Python → GIF/MP4） |
| **代码直出** | `render_image` / `render_video` 在服务端本地渲染，**不联网、不耗上游额度**；适合图表、示意图、带精确文字的海报、批量模板图——AI 生图画不准的东西。需要容器内有 ImageMagick（见「代码渲染的前置依赖」） |
| **图像迭代闭环** | 可上传本机图片，也可把本服务的图片/视频产物作为后续参考输入；图像字节不经第三方图床 |
| **Provider 抽象** | 上游信息全部在管理面板配置（base_url / 模型 / 密钥 / 请求模板 / 限速），不写死任何厂商；零配置启动即告知「无可用提供方」 |
| **并发与排队** | 生图 / 生视频各自独立泳道（默认 8 / 4，可在面板热调），排队超时返回 `busy` 而非无限等待 |
| **熔断保护** | 硬熔断（429/402）与软熔断（连续失败），避免把上游配额打爆 |
| **持久化转存** | `persist=true` 时把上游临时 URL 的产物转存到本地，经 `/files/<name>` 提供长期可访问链接 |
| **超时预算** | 全链路嵌套不等式自检（read ≤ 分档 ≤ 200s < 210s 建议 < 220s 停止），启动即校验 |
| **优雅停机** | SIGTERM → 停收新请求 → 在途排空（默认 220s）→ 组件收尾 |
| **fail-fast 配置** | 密钥缺失或配置违例时启动即退出（退出码 2），不会带病运行 |

---

## 快速开始

### 1. 准备

需要 Python **≥ 3.10**（实测 3.12）。

```bash
git clone <this-repo>
cd mcp-imagegen
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.env .env
```

**必填一项**（缺失则启动 fail-fast）：

| 变量 | 说明 |
|---|---|
| `MCPACCESS_TOKEN` | 客户端访问本服务所需的 Bearer Token（自己随便定一个强随机串） |
| `STORE_PATH` | 提供方 / 访问令牌 / 管理员口令的持久化文件（默认 `data/store.json`，已在 `.gitignore` 中排除，切勿入库） |

> ⚠️ V5 **不再要求上游密钥写在环境变量里**：上游（第三方或自建）通过管理面板配置，密钥只存在 `data/store.json`（本地文件，不进代码仓库、不进日志）。零上游配置时服务照常启动，但调用工具会明确返回「无可用提供方」而非静默失败。

其余运行参数见 `config.example.env`，全部有默认值。

### 3. 启动前自检

```bash
./venv/bin/python server.py --check
# 退出码 0 = 通过；2 = 配置违例（会列出具体原因）
```

### 4. 运行

```bash
set -a; . ./.env; set +a
./venv/bin/python server.py
```

服务监听 `0.0.0.0:8000`，MCP 端点为 `http://<host>:8000/mcp`。

启动日志会打印两件重要信息：自动生成的 `MCPACCESS_TOKEN`（配到客户端用）、
以及 `data/store.json` 的实际路径。

> 开箱即用的边界：服务**不需要任何上游配置就能启动**（不会出现"起不来"），
> 但**要真正生图/生视频，必须先在管理面板接入至少一个上游** →
> 见下方[配置第一个上游](#配置第一个上游约-2-分钟)。
> 想零成本先跑通链路，可用 `scripts/mock_upstream.py` 起一个假上游。

---

## 管理面板（配置上游 / 运行参数）

V5 把「上游配置」从环境变量搬进了 Web 控制台（单文件内联 SPA，零外部依赖）。
首次启动后打开 `http://<host>:8000/admin`，用面板完成初始化与管理：

- **初始化**：首次进入设置管理员口令（弱口令会被拒），之后凭口令登录，会话用 `HttpOnly` + `SameSite=Strict` Cookie 保护，写操作需带 `X-Admin-Request` 头防 CSRF；
- **提供方 CRUD**：新增 / 编辑 / 删除上游，填 `base_url`、模型名、鉴权方式、请求/轮询模板、每秒请求数与突发额度（`limits.rps` / `limits.burst`，默认 10 / 20，脆弱上游可调小）；列表接口对密钥脱敏（只回 `token_preview` + `token_set`）；
- **运行参数热更新**：并发上限、排队等待、视频轮询间隔等在面板改完**立即生效，无需重启**；
- **看板**：总览 / 日志 / 视频任务 / 令牌管理一应俱全。

> 提供方密钥只落在 `data/store.json`（已被 `.gitignore` 排除）。仓库里**没有任何上游地址与密钥**。

### 配置第一个上游（约 2 分钟）

面板初始化完成后，**服务已经起来了但还不能生图**——此时调用工具会返回
「无可用提供方」。按下面三步接上你自己的上游（任何兼容 OpenAI 风格
`POST /v1/images/generations` 的第三方、或自建推理服务都行）：

**① 打开管理面板** → `http://<host>:8000/admin` → 「提供方」→「新增」。

**② 填这几项**（带 ★ 的是必填）：

| 字段 | 填什么 |
|---|---|
| ★ 名称 | 随便起个好认的，如 `my-upstream` |
| ★ Base URL | 上游地址，**带不带 `/v1` 取决于对方**：`https://api.example.com/v1` 或 `https://api.example.com` 都可以，服务会按你填的原文拼路径 |
| ★ 密钥 | 上游的 API Key，存进本地 `data/store.json`，面板只回显脱敏预览 |
| 鉴权方式 | 默认 `bearer`（`Authorization: Bearer <key>`）；若对方要`X-API-Key` 之类，选 `header` 并把头名填对 |
| 生图模型 | 上游的生图模型名，如 `gpt-image-1` |
| 生视频模型 | 上游的视频模型名（不填则该提供方只出图） |
| 每秒请求 / 突发 | `limits.rps` / `limits.burst`，默认 10 / 20。**免费或脆弱上游调小**（如 1 / 2）避免被限流 |

> 高级用户（上游返回结构不标准）才需要碰**请求/轮询模板**——标准 OpenAI
> 风格响应开箱即用，不用动。

**③ 保存 → 看健康状态**。面板列表里该提供方会显示 `enabled` 与最近探测结果。
然后验证：

```bash
# 健康检查里 capabilities.image 应变成 true
curl -s http://<host>:8000/healthz | python3 -m json.tool

# 实际出一张图
curl -s -X POST http://<host>:8000/mcp \
  -H "Authorization: Bearer <你的 MCPACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"generate_image","arguments":{"prompt":"一只柴犬","n":1}}}'
```

**常见坑**

- **配了还是「无可用提供方」** → 多半是忘了把 `enabled` 打开，或改的是另一个
  `STORE_PATH`（服务启动时会把实际路径打进日志）。核对启动日志里的 store 路径。
- **401/403** → 鉴权方式或头名选错了；有些服务用 `api_key` 查询参数而非请求头。
- **超时** → 上游太慢，先把 `limits.rps` 调小、并适当调大
  `TIMEOUT_UPSTREAM_IMAGE_SEC`（见 `config.example.env`）。
- **多上游同时用** → 面板里可以加多个，服务按 `priority` 从高到低选路；把不稳定的
  那个关掉即可，不必删除。

> 想先试手再配？`scripts/mock_upstream.py` 提供一个零依赖假上游，
> 用它可以完整跑通链路而不消耗任何真实额度：

```bash
# 终端 A：起假上游（默认 8001 端口）
./venv/bin/python scripts/mock_upstream.py 8001

# 终端 B：面板里新增提供方，Base URL 填 http://127.0.0.1:8001，密钥随便填非空值
#        生图模型填 mock（模型名不校验，填什么都行，它不做真实推理）
```

配好后 `generate_image` 会返回一张内置的测试 PNG，可用来验证「配通了吗」。

---

## 接入客户端

MCP 端点为 **streamable-http**，需带 Bearer 鉴权。注意各客户端的类型命名不同：

<details>
<summary><b>通用 JSON（多数客户端）</b></summary>

```json
{
  "mcpServers": {
    "mcp-imagegen": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <你的 MCPACCESS_TOKEN>"
      },
      "timeout": 240000
    }
  }
}
```
</details>

<details>
<summary><b>DSH（dsh-mcp-manager）</b></summary>

写 `<DSH_HOME>/@wingsky-1/dsh-mcp-manager/mcp.json`，类型写 `streamable-http`（连字符）：

```json
{
  "version": 1,
  "servers": [
    {
      "name": "mcp-imagegen",
      "transport": "streamable-http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" },
      "toolCallTimeoutMs": 240000
    }
  ]
}
```
</details>

<details>
<summary><b>豆包工作</b></summary>

左侧「插件·技能·伙伴」→ 插件页右上角「+ 添加」→ **自定义连接器**（不是"自定义插件"）：

- 传输类型：`HTTP`
- 服务器 URL：`http://<host>:8000/mcp`
- 自定义 Headers：名称 `Authorization`，值 `Bearer <token>`（Bearer 前缀与空格要一起填）
</details>

<details>
<summary><b>WorkBuddy</b></summary>

类型写 `streamableHttp`（驼峰），`timeout` 建议 240000 毫秒（视频生成耗时较长）。
</details>

> ⚠️ **超时务必设够**。生图约 10 秒，但视频渲染需 5–10 分钟，默认超时短的客户端会提前断开。

---

## 工具说明

### `generate_image`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | **必填** | 提示词，中英文皆可 |
| `size` | string | `1024x1024` | 档位 `1K`/`2K`/`3K`/`4K`，或 `WxH`（如 `1024x768`）|
| `ratio` | string | 上游默认 | `1:1`/`3:4`/`4:3`/`16:9`/`9:16`/`2:3`/`3:2`/`21:9`。**仅可与档位式 size 同传，与 WxH 同传即冲突** |
| `batch_size` | int | `1` | 1–4，服务端拆分串行调用 |
| `persist` | bool | `false` | `true` 则转存为自托管持久 URL |
| `image` | string[] | 不传 | 可选参考图；支持公网 URL、data URI/base64、本服务产物名或 `/files/<name>`。多张图用于图生图/合成 |

本机图片先用 `POST /upload` 上传，再把响应里的 `url` 作为 `image` 参数：

```bash
curl -X POST "http://<host>:8000/upload" \\
  -H "Authorization: Bearer <MCPACCESS_TOKEN>" \\
  --data-binary @reference.png
```

上传成功返回 `201` 和 `/files/<name>`。端点按图片文件签名识别格式；超限返回 `413`，不支持的格式返回 `415`。

### `generate_video`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | **必填** | 视频内容描述 |
| `seconds` | int | `5` | 4–12 秒 |
| `aspect_ratio` | string | `16:9` | `16:9`/`9:16`/`1:1`/`4:3`/`3:4`/`21:9` |
| `seed` | int | 不传 | 随机种子，直传上游 |
| `persist` | bool | `false` | 同上 |
| `first_frame` | string | 不传 | keyframe 模式首帧；让输入图作为成片真实首帧 |
| `last_frame` | string | 不传 | keyframe 模式尾帧；可与首帧配合做过渡 |
| `reference_images` | string[] | 不传 | reference 模式风格/内容参考（最多 5 张，会重新构图）；与首尾帧互斥 |

视频参考图可使用公网 URL、data URI/base64、本服务产物名或 `/files/<name>`。
在时间预算内同步等待；超预算则返回 `task_id`，用 `query_video` 续查。上游队列满时服务端代为排队。

### `query_video`

| 参数 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | **必填**，来自 `generate_video` 的返回 |

### `list_video_tasks`

批量查看内存中跟踪的视频任务，按最新提交优先；返回 task_id、状态与结果 URL。

### `render_image`（代码生图）

用**代码**画图，不调用任何上游、不消耗额度：给一段 SVG，或一段产出 SVG 的 Python，
服务端渲染成 PNG 并返回 `/files/<name>` 链接。AI 生图画不准的东西（图表、
流程图、表格、图标、带精确文字的海报封面、批量模板图）用这个。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` | string | **必填** | SVG 代码，或产出 SVG 的短 Python 脚本 |
| `size` | string | `1024x1024` | `WxH`（如 `1024x768`），单边 16–4096 |
| `lang` | string | 自动 | `svg` / `python`；不传则自动判断（以 `<` 开头当 SVG，否则当 Python）|

Python 代码在受限沙箱里跑：不能读写文件、不能联网、除 `math` / `json` /
`random` / `statistics` / `colorsys` 外不能 import；`width` / `height` 已预置为变量；
把结果 `print` 出来或赋值给 `svg` / `out` / `result` 变量即可。

### `render_video`（代码生视频）

同一套代码通道出**动画**：动画示意图、加载指示、进度条、动效文字。默认出 GIF
（到处都能显示），容器里装了 ffmpeg 就出 MP4（`format=auto`）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` | string | **必填** | SVG（用 `__FRAME__` 占位帧号）或 Python（预置 `frame` / `total` / `width` / `height`）|
| `size` | string | `720x720` | `WxH`，单边 16–1920 |
| `frames` | int | `36` | 1–120；帧越多越慢 |
| `fps` | int | `12` | 1–30 |
| `format` | string | `auto` | `auto` / `mp4` / `gif`；`mp4` 需要容器内有 ffmpeg |

单帧渲染上限 20 秒、整次请求上限 90 秒（防把服务当渲染农场）；超限会返回
`[error]` 行而不是无限跑。

**代码渲染的前置依赖**（与上游无关，纯本地能力）：

```bash
# SVG → PNG/GIF 渲染器（必需；缺了会返回 not_configured 并给出提示）
apt-get install -y imagemagick
# MP4 输出（可选）
apt-get install -y ffmpeg
# 中文字体（强烈建议）：容器里没有中文字体时，图中的中文会被静默丢弃，
# 只剩英文和数字。装了任意一个即可，启动日志会打印实际选中的字体。
apt-get install -y fonts-droid-fallback     # 或 fonts-wqy-microhei / fonts-noto-cjk
# 也可以用环境变量显式指定字体文件
CODERENDER_FONT=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
```

---

## HTTP 端点

| 路径 | 说明 |
|---|---|
| `/mcp` | MCP 协议端点（**需 Bearer 鉴权**）|
| `/healthz` | 健康检查，返回 `{"status":"ok"}` |
| `/files/{name}` | 已转存的图片 / 视频（公开只读，文件名白名单） |
| `/upload` | 本机图片上传（需 Bearer；限制大小，按文件签名识别格式） |

---

## 运行测试

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest -q          # 单测全量
./venv/bin/python scripts/e2e_v5.py     # V5 端到端自检（真起服务+真 mock 上游，约 1 分钟）
./venv/bin/python scripts/stress_mcp.py # 多会话 / 多并发压测
./venv/bin/python scripts/e2e_upstream.py   # 配好上游后的真机自检（图+视频，**会消耗上游额度**）
./venv/bin/python scripts/e2e_coderender.py # 代码渲染自检（纯本地，不耗额度）
```

`e2e_upstream.py` 支持 `--only image|video`、`--poll <秒>`，也可用
`E2E_BASE` / `E2E_STORE` 指向另一个实例（配合 `scripts/mock_upstream.py`
就成了一套零额度的全链路回归）。

---

## 生产部署

`deploy/` 下提供 systemd 单元与安装说明：

- `deploy/install.md` —— 完整安装手册（含变量表、目录规划、Caddy 反代）
- `deploy/mcp-imagegen.service` —— systemd 单元（`Type=notify` + watchdog 自愈）
- `deploy/agent-config.md` —— 各 Agent 客户端的接入配置
- `deploy/Caddyfile` —— 可选反代（注意读超时须 ≥ 210s）

**无 systemd 环境**（如 proot 容器、WSL 无 init）可用 tmux 守护替代：

```bash
tmux new-session -d -s mcp-imagegen \
  "while true; do set -a; . /etc/mcp-imagegen/env; set +a; \
   exec /opt/mcp-imagegen/venv/bin/python /opt/mcp-imagegen/server.py; \
   echo '[$(date)] exited, retry in 10s'; sleep 10; done"
```

---

## 依赖说明

`requirements.txt` 已钉死实测版本：

```
fastmcp==4.1.0
httpx==0.28.1
uvicorn
```

> **关于 fastmcp 版本**：设计文档中示例的 `fastmcp==2.14.3` 在当前 PyPI 依赖树下
> **无法安装**——它导入的 `IdentityAssertionParams` 在 `mcp` 1.x 全系不存在，
> 而它自身声明 `mcp<2.0`，声明与实际不符。`4.1.0` 为实测可用版本（424/424 单测通过）。
> 完整冻结清单见 `requirements.lock`。

---

## 架构

```
server.py                   入口：配置自检 → 组件装配 → FastMCP → BearerGate → uvicorn
├── config.py               配置加载 + 嵌套不等式自检（fail-fast）
├── app/
│   ├── tool.py             六个 MCP 工具的定义与 schema
│   ├── pipeline.py         生图流水线（槽池 / 熔断 / 重试）
│   ├── video_pipeline.py   视频流水线（提交 / 轮询 / 对账）
│   ├── coderender_pipeline.py  代码生图/生视频工具层（参数校验 / 落盘 / 契约行）
│   ├── endpoints.py        /healthz、/files、BearerGate 鉴权
│   └── contract.py         对外契约常量
├── engine/
│   ├── errors.py           错误分类与契约行生成
│   ├── router.py           模型路由
│   └── normalizer.py       响应归一化
├── infra/
│   ├── budget.py           并发槽池（多队列泳道）
│   ├── circuit.py          熔断器（硬 / 软）
│   ├── persist.py          产物转存服务 + /files URL 构造
│   ├── ratelimit.py        上游限速
│   ├── refs.py             参考图/首尾帧取材（URL / data URI / 本服务产物）
│   └── tasks.py            视频任务表（内存态 + TTL）
├── providers/              上游抽象（零厂商硬编码）
│   ├── schema.py           ProviderConfig / CapabilitySpec / RequestSpec …
│   ├── store.py            配置仓库（data/store.json，含令牌与管理员口令）
│   ├── gateway.py          请求执行（模板渲染 / 抽取 / 重试 / 路由）
│   ├── template.py         占位符模板与响应抽取
│   ├── autodetect.py       一键接入：按候选路径探测上游形状并反推模板
│   └── coderender.py       代码生图/生视频的本地渲染与 Python 沙箱
├── admin/                  管理面板（api / auth / page）
└── ops/
    ├── liveness.py         sd_notify 与心跳（vendored，无外部依赖）
    ├── shutdown.py         优雅停机协调
    ├── logline.py          结构化访问日志
    └── metrics.py          运行指标
```

---

## 许可

[MIT](LICENSE)

---

## 致谢

上游能力由 [上游服务提供方](https://api.example.com) 提供。本项目仅做协议适配与工程加固。
