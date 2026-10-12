# mcp-imagegen

> 基于 **Agnes AI** 的图片 / 视频生成 MCP Server —— 单上游、三工具、可自托管。

一个 Model Context Protocol（MCP）服务器，把文生图与文生视频封装成标准 MCP 工具，
供 Claude Desktop / DSH / 豆包工作 / WorkBuddy 等任意支持 MCP 的客户端调用。
自带并发槽池、熔断、持久化转存与优雅停机，适合长期挂在低功耗小主机上跑。

---

## 特性

| 能力 | 说明 |
|---|---|
| **三工具** | `generate_image`（文生图，支持批量拆分）· `generate_video`（文生视频 4–12 秒）· `query_video`（查询视频任务状态） |
| **单上游** | 仅依赖 Agnes AI（`agnes-image-2.5-flash` / `agnes-video-2.5-flash`），配置面简单 |
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

**必填两项**（缺失则启动 fail-fast）：

| 变量 | 说明 |
|---|---|
| `AGNES_TOKEN` | Agnes AI 的 API Key |
| `MCPACCESS_TOKEN` | 客户端访问本服务所需的 Bearer Token（自己随便定一个强随机串） |

其余 20+ 项见 `config.example.env`，全部有默认值。

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

### `generate_video`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | **必填** | 视频内容描述 |
| `seconds` | int | `5` | 4–12 秒 |
| `aspect_ratio` | string | `16:9` | `16:9`/`9:16`/`1:1`/`4:3`/`3:4`/`21:9` |
| `seed` | int | 不传 | 随机种子，直传上游 |
| `persist` | bool | `false` | 同上 |

在时间预算内同步等待；超预算则返回 `task_id`，用 `query_video` 续查。

### `query_video`

| 参数 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | **必填**，来自 `generate_video` 的返回 |

---

## HTTP 端点

| 路径 | 说明 |
|---|---|
| `/mcp` | MCP 协议端点（**需 Bearer 鉴权**）|
| `/healthz` | 健康检查，返回 `{"status":"ok"}` |
| `/files/{name}` | 已转存的图片 / 视频 |

---

## 运行测试

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest -q          # 424 项单测
./venv/bin/python acceptance/e2e_acceptance.py        # e2e 全量（含慢死路径，约 3 分钟）
./venv/bin/python acceptance/e2e_acceptance.py --fast # 快速档
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
│   ├── agnes.py            Agnes AI 客户端（httpx）
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

上游能力由 [Agnes AI](https://api.agnes-ai.cn) 提供。本项目仅做协议适配与工程加固。


---

## 📚 完整文档

更详细的说明在 [docs/wiki/](docs/wiki/Home.md)：快速开始 · 配置项详解 · 接入客户端 · 架构说明 · 常见问题
