# mcp-imagegen

> 基于 **Provider 抽象** 的图片 / 视频生成 MCP Server —— 可接入任意第三方或自建上游，零默认配置即开箱，配好管理面板才可用，自带管理控制台。

一个 Model Context Protocol（MCP）服务器，把文生图与文生视频封装成标准 MCP 工具，
供 Claude Desktop / DSH / 豆包工作 / WorkBuddy 等任意支持 MCP 的客户端调用。
自带并发槽池、熔断、持久化转存与优雅停机，适合长期挂在低功耗小主机上跑。

---

## 特性

| 能力 | 说明 |
|---|---|
| **四工具** | `generate_image`（文生图/参考图生图，支持批量拆分）· `generate_video`（文生视频、首尾帧/参考图、服务端排队）· `query_video`（查询视频任务状态）· `list_video_tasks`（批量查看后台作业） |
| **图像迭代闭环** | 可上传本机图片，也可把本服务的图片/视频产物作为后续参考输入；图像字节不经第三方图床 |
| **Provider 抽象** | 上游信息全部在管理面板配置（base_url / 模型 / 密钥 / 请求模板 / 限速），不写死任何厂商；零配置启动即告知「无可用提供方」 |
| **并发与排队** | 可配并发槽（默认 3），排队超时返回 `busy` 而非无限等待 |
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

---

## 管理面板（配置上游 / 运行参数）

V5 把「上游配置」从环境变量搬进了 Web 控制台（单文件内联 SPA，零外部依赖）。
首次启动后打开 `http://<host>:8000/admin`，用面板完成初始化与管理：

- **初始化**：首次进入设置管理员口令（弱口令会被拒），之后凭口令登录，会话用 `HttpOnly` + `SameSite=Strict` Cookie 保护，写操作需带 `X-Admin-Request` 头防 CSRF；
- **提供方 CRUD**：新增 / 编辑 / 删除上游，填 `base_url`、模型名、鉴权方式、请求/轮询模板、每秒请求数与突发额度（`limits.rps` / `limits.burst`，默认 10 / 20，脆弱上游可调小）；列表接口对密钥脱敏（只回 `token_preview` + `token_set`）；
- **运行参数热更新**：并发上限、排队等待、视频轮询间隔等在面板改完**立即生效，无需重启**；
- **看板**：总览 / 日志 / 视频任务 / 令牌管理一应俱全。

> 提供方密钥只落在 `data/store.json`（已被 `.gitignore` 排除）。仓库里**没有任何上游地址与密钥**。

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
./venv/bin/python -m pytest -q          # 557 项单测 + 1 跳过
./venv/bin/python scripts/e2e_v5.py     # V5 端到端自检（真起服务+真 mock 上游，约 1 分钟）
./venv/bin/python scripts/stress_mcp.py # 多会话 / 多并发压测
```

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
│   ├── tool.py             三个 MCP 工具的定义与 schema
│   ├── pipeline.py         生图流水线（槽池 / 熔断 / 重试）
│   ├── video_pipeline.py   视频流水线（提交 / 轮询 / 对账）
│   ├── endpoints.py        /healthz、/files、BearerGate 鉴权
│   └── contract.py         对外契约常量
├── engine/
│   ├── errors.py           错误分类与契约行生成
│   ├── router.py           模型路由
│   └── normalizer.py       响应归一化
├── infra/
│   ├── upstream.py            上游服务提供方 客户端（httpx）
│   ├── budget.py           并发槽池
│   ├── circuit.py          熔断器（硬 / 软）
│   ├── persist.py          产物转存服务
│   └── tasks.py            视频任务表（内存态 + TTL）
└── ops/
    ├── liveness.py         sd_notify 与心跳（vendored，无外部依赖）
    ├── shutdown.py         优雅停机协调
    └── logline.py          结构化访问日志
```

---

## 许可

[MIT](LICENSE)

---

## 致谢

上游能力由 [上游服务提供方](https://api.example.com) 提供。本项目仅做协议适配与工程加固。
