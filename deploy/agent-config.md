# MCP 图片生成 Server · Agent 客户端配置说明（agent-config.md）

| 项 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 日期 | 2026-10-10 |
| 作者 | Leo（文档工程师） |
| 依据 | 《技术规格书》二、4.1 节与 `config.py` 部署层常量；服务端行为经实测（healthz / 401 / MCP initialize 握手）；**V2：三工具 schema/返回契约/错误分类逐字取自项目 `app/tool.py`、`app/contract.py`、`engine/errors.py` 与《增量设计方案V2》六节（撰写前通读）** |
| 读者 | 配置 MCP 客户端（Claude Code、Cursor、自研 Agent 等）的维护者 |

## 1. 连接要素

| 项 | 值 |
|---|---|
| 端点 URL | `http://<服务器IP>:8000/mcp`（内网直连）或 `http://<服务器>/mcp`（Caddy 前置，默认 `:80`）或 `https://<你的域名>/mcp`（Caddy 换域名后自动 HTTPS，推荐公网场景） |
| 鉴权 | 请求头 `Authorization: Bearer <MCPACCESS_TOKEN>`（即服务端 `/etc/mcp-imagegen/env` 中的 `MCPACCESS_TOKEN`） |
| 传输 | MCP streamable HTTP（fastmcp 实现） |
| 工具 | 3 个（V2）：`generate_image` / `generate_video` / `query_video`——单一 Agnes 上游双模型（agnes-image-2.5-flash / agnes-video-2.5-flash）；参数归一/校验/视频轮询全部在服务端完成，客户端不感知模型专属参数（参数表见 §3） |
| 无鉴权端点 | `/healthz`（探活）、`/files/<name>`（取图/取视频，视频为 .mp4/.webm；persist 落盘文件**长期有效**、服务端不自动清理——V2 核对更正：V1 版"24h 内有效"表述与 `infra/persist.py` 不符，以代码为准） |
| 客户端超时 | **≥ 210s（硬性建议，见 §4）** |

## 2. mcpServers 配置条目

通用 JSON 写法（绝大多数支持远程 MCP 服务器的客户端可直接套用）：

```json
{
  "mcpServers": {
    "mcp-imagegen": {
      "url": "https://<你的域名>/mcp",
      "headers": {
        "Authorization": "Bearer <MCPACCESS_TOKEN>"
      }
    }
  }
}
```

- 内网直连把 url 换成 `http://<服务器IP>:8000/mcp`；
- 部分客户端要求显式传输类型（如 `"type": "http"` 或 `"type": "streamable-http"`），字段名以所用客户端官方文档为准，`url` + `Authorization` 头两要素不变；
- CLI 快捷方式示例（Claude Code）：

```bash
claude mcp add --transport http mcp-imagegen https://<你的域名>/mcp \
  --header "Authorization: Bearer <MCPACCESS_TOKEN>"
```

## 3. 三工具能力与参数（V2：1 个 → 3 个；单一 Agnes 上游双模型）

| 工具 | 上游模型 | 一句话 |
|---|---|---|
| `generate_image` | agnes-image-2.5-flash | 文生图（prompt 中英文皆可） |
| `generate_video` | agnes-video-2.5-flash | 文生视频（服务端轮询阻塞至完成或预算尽，见 §3.4） |
| `query_video` | ——（视频任务表 + 上游实时查询） | 凭 task_id 续查视频任务 |

### 3.1 generate_image

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `prompt` | string | 是 | — | 提示词，中英文皆可 |
| `size` | string | 否 | `1024x1024` | 档位 `1K/2K/3K/4K` 或 `WxH`（如 `1024x768`）双格式直传（D2；格式校验在服务端） |
| `ratio` | string | 否 | 不传 | `1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9` 八档；**仅与档位式 size 同传**，与 `WxH` 同传即 `invalid_params`；不传=上游默认 `1:1`（D2） |
| `batch_size` | integer | 否 | `1` | 1–4（服务端拆分串行；部分成功时 meta 追加 `requested/delivered`） |
| `persist` | boolean | 否 | `false` | **建议开启**：true=转存返自托管 `/files` 持久 URL（长期有效） |

### 3.2 generate_video

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `prompt` | string | 是 | — | 视频内容描述 |
| `seconds` | integer | 否 | `5` | 4–12（范围校验在服务端） |
| `aspect_ratio` | string | 否 | `16:9` | `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9` 六档（D4） |
| `seed` | integer | 否 | 不传 | 随机种子，直传上游（D3）；不传=不注入请求体 |
| `persist` | boolean | 否 | `false` | **建议开启**：同生图；不转存则上游 URL 短效（meta 标 `url_expires=1h`） |

### 3.3 query_video

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | string | 是 | `generate_video` 返回的任务号 |

不占用生图/生视频的并发槽（3 槽池），可独立调用；单次查询读超时 10s，稍后重查即可。

### 3.4 返回契约（Agent 只读文本行，无 JSON）

- **成功**：每行一个 URL + 末行 `[meta] ...`——生图含 `model/size`（显式传 ratio 时含 `ratio=<v>`；±`requested/delivered`）、视频含 `model/seconds/aspect_ratio/elapsed`（显式传 seed 时含 `seed=<v>`），后接 persist 三态标记（`persisted` / `persist_failed` / 未开 persist 时 `url_expires=1h` 短效警示）；
- **视频异步语义**：`generate_video` 服务端轮询阻塞至完成；200s 总预算内仍未完成时返回 `[meta] task_id=<id> status=running ... 请稍后调用 query_video 查询`——**这不是错误行**，Agent 应凭 task_id 稍后续查，勿当失败重提；
- **任务表边界**：TTL 7200s（2h）、容量 100、内存态**服务重启即清零**——超时/逐出/重启后查询返回 `task_not_found`（retry=fixthenretry，重新提交生成即可，勿原样重查）；
- **错误**：单行 `[error] category=<码> retry=<值> reason=... suggestion=...`（§5 表）。

## 4. 客户端超时必须 ≥ 210s

**原因（数值链，勿破坏）**：服务端请求预算 `TOTALBUDGETSEC=200` < Agent 客户端建议 210 < systemd 排空 220。客户端必须**最后**断开——否则最坏情况是：图/视频已生成、Agnes 额度已花，客户端却因超时报错把"慢成功"误判为"失败"。视频同样受保护：200s 预算内服务端必有返回（完成 URL 或 `task_id` 收条，见 §3.4），210s 客户端超时足够收到收条，不会悬挂。

- 建议：工具调用超时（含连接）统一设 **≥ 210s**；
- 各客户端设置位置不同（配置项 / 环境变量 / 客户端默认值），若提供毫秒制环境变量，对应 **210000**（具体变量名以客户端文档为准，常见如 `MCP_TIMEOUT` / `MCP_TOOL_TIMEOUT`）；
- 未提供超时配置项的客户端：注意其默认值若小于 210s 会出现上述误判，属客户端侧限制，需评估更换或调大。

## 5. 错误处理约定（读 `[error]` 行的 retry 字段，勿盲目重试）

工具失败时返回体是纯文本一行：`[error] category=<码> retry=<值> reason=... suggestion=...`。Agent 按 `retry` 字段行动（红线 A4）：

| category | retry | Agent 应做 |
|---|---|---|
| `invalid_params` | `fixthenretry` | 按 reason 指明的参数修正后重试 |
| `mcp_auth` | `no` | 客户端 Bearer 与服务端 `MCPACCESS_TOKEN` 不一致，找管理员 |
| `upstream_auth` | `no` | 服务端 `AGNES_TOKEN` 配错或 Key 无效/过期（provider=Agnes），找管理员，勿重试 |
| `rate_limit` | `yes` | 上游限流 429 或服务端熔断 OPEN 快失败（硬熔断 600s/10min），稍后重试 |
| `upstream_error` | `yes` | 上游 5xx/超时/网络/预算耗尽/视频上游 failed，稍后重试 |
| `content_policy` | `no` | 内容审核未过，改 prompt 再发（等同新请求） |
| `upstream_422` | `no` | 归一化层 bug，把错误码反馈给开发者 |
| `task_not_found` | `fixthenretry` | 视频任务号不存在（TTL 过期/容量逐出/服务重启清空），**重新提交生成**（新 task_id），勿原样重查 |
| `busy` | `yes` | 并发占满（等待 30s 超限），稍后重调 |

> `persist` 失败不是错误行：服务端自动降级返回原上游 URL + `persist_failed` 标记（图/视频同语义），客户端无感知处理。

## 6. 连通性自检（curl 三连，服务端已实测通过）

```bash
# ① 服务在线（无鉴权探活）
curl -s https://<你的域名>/healthz
# → {"status":"ok"}

# ② 端点与鉴权边界（无 token 应得 401 + [error] 行，证明 URL 正确且鉴权在位）
curl -s -i https://<你的域名>/mcp
# → HTTP/1.1 401 + [error] category=mcp_auth retry=no ...

# ③ 正式 MCP initialize 握手（带 token；直连时换 http://<服务器IP>:8000）
curl -s -m 10 -X POST https://<你的域名>/mcp \
  -H "Authorization: Bearer <MCPACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# → HTTP 200；响应头含 mcp-session-id；SSE data 中 serverInfo.name 为 "mcp-imagegen"
```

三连全过，即可在客户端里挂载 `mcpServers` 条目（§2）正常调用三工具（§3）。

## 7. 注意事项

- token 泄露面：`MCPACCESS_TOKEN` 是唯一客户端凭证，公网使用建议让服务端把 Caddyfile 站点地址换成域名启用 HTTPS；直连 `http://` 或 Caddy 默认 `:80` 明文场景下 token 会明文过网，仅限内网/临时使用；
- 服务重启/更新窗口（≤220s 排空 + 秒级重启）内调用可能失败，`retry=yes` 类错误稍后重调即可；**视频任务表随重启清零**（内存态），在途任务的 task_id 失效后再查得 `task_not_found` 属预期——需重新提交生成；
- 服务端每日 04:00（本地时间）做客户端热重建（`rotate`），不影响在途请求，无需客户端配合。

---

## 版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| V1.0 | 2026-10-10 | 初版：mcpServers 条目 / Bearer 鉴权 / 超时 ≥210s / retry 语义 / curl 三连自检 |
| V2.0 | 2026-10-10 | Agnes 双模型三工具：新增 §3 三工具参数表与返回契约（视频异步语义/task_id 收条/任务表 TTL 与重启清零/persist 建议开启）；错误表增 `task_not_found`、`upstream_auth` 指向 `AGNES_TOKEN`、删 402 语义；`/files` 更正为图/视频且长期有效；原 §3–§6 顺延为 §4–§7 |

下一步建议：正式 Key 落位后，用 §6 的 ③（initialize 握手）做端到端验收，再在客户端挂载条目跑一次真实 `generate_image`（persist=true）与一次 `generate_video`（顺带验证 ≥210s 超时设置与 task_id 收条语义），并按《增量设计方案V2》九节 9.3 清单回填附录 A 实测项。
