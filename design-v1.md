# MCP 图片生成 Server · 开发设计方案

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 文档版本 | V1.0 |
| 日期 | 2026-10-09 |
| 作者 | 芳（产品负责人） |
| 上位文档 | 《MCP 图片生成 Server · 技术规格书》（**唯一实现依据，所有决策已定案**） |
| 本文档使命 | 让开发**不走样、不遗漏**——只做翻译、拆解、风险前置，**零决策变更** |
| 读者 | Ethan（全栈开发·主力）、Leo（文档工程师·交付物）、芳（验收复核）、用户（3 个语义确认） |

---

## 一、设计总览

### 1.1 目标与场景（一段话）

单 Agent 调用方（自用）通过 MCP 协议调用 `generate_image(prompt, …)` 一个工具，Server 背后接另一家上游两个免费模型（Kolors + FLUX.1-schnell），自动完成模型选择、参数归一、故障兜底、超时预算、僵死自愈。最坏情况是"几张图没生成，重试即可"——一切设计投入与该故障代价成正比（规格书十二节）。

### 1.2 五大支柱 → 模块承接映射

| 支柱 | 职责（规格书原文） | 承接模块（见第二节） |
|---|---|---|
| 归一 | Agent 永不见模型专属参数（steps/guidance 被翻译掉） | `engine/normalizer.py` |
| 路由 | 显式指定 > 熔断绕开 > quality 强制 > 语言检测 > 默认 | `engine/router.py` + `infra/circuit.py` |
| 兜底 | "切模型会好吗？"会→切，不会→不切；降级出图优于报错 | `engine/fallback.py` + `engine/errors.py` |
| 预算 | 200s deadline 贯穿传递，快慢分道，余额不足不兜底 | `infra/budget.py` + `infra/upstream_b.py` |
| 自愈 | watchdog 心跳防僵死 + 熔断三态防撞墙 + 优雅停机防误杀 | `ops/liveness.py` + `ops/shutdown.py` + `infra/circuit.py` |

### 1.3 一条请求的生命周期（主时序，对应规格书 3.3）

```
Agent ──POST /mcp (Bearer MCPACCESS_TOKEN)──> FastMCP
  ① MCP 鉴权失败 → [error] MCP鉴权401 (retry=no)
  ② 参数校验：size 吸附 / batch 1–4 / 枚举 → 参数错 (retry=fixthenretry)
  ③ 创建 Deadline = now + 200s（贯穿后续所有阶段）
  ④ 并发槽 acquire（3 槽，等待上限 30s，等待耗时计入 deadline）→ 到点返 busy (retry=yes)
  ⑤ 路由链：显式① > 熔断② > quality③ > CJK④ > 默认⑤
  ⑥ 归一：quality→steps/guidance（schnell 静默丢 guidance）
  ⑦ 调 API：read 超时 = min(分档上限, deadline 剩余)
       401/403 → 不切换直接报错；400 审核 → 不切换（仅 1 次 API 调用）
       429/402 → 硬熔断 10min → 切换（余额不足则快失败返超时错误）
       5xx/网络 → 退避重试 1 次 → 仍败 → 软熔断计数 → 切换
       切换仅 1 次（两模型各试一遍即止）
  ⑧ 成功且 persist=true → 下载 30s（计入 deadline）→ 失败降级返原 URL（永不报错）
  ⑨ 格式化返回（URL 行 × batch + [meta] 行）+ 一行结构化日志
```

---

## 二、模块设计（对应规格书第三节内核引擎、第六节稳定性）

### 2.1 目录结构（建议，Ethan 可按习惯微调，模块边界不得合并削弱）

```
mcp-imagegen/
├── server.py              # 入口：装配所有模块、事件循环、sd_notify 握手、信号注册
├── config.py              # 环境变量 → 配置对象；默认值=规格书第七节；启动自检嵌套不等式
├── app/                   # ── MCP 接入层 ──
│   ├── tool.py            # generate_image 工具：schema(7参) + description（红线文案，逐字符）
│   ├── contract.py        # 返回契约格式化：URL行/[meta]行/[error]行（唯一出口，防止格式漂移）
│   ├── endpoints.py       # /healthz（人工巡检，不参与僵死判据）、/files/（persist 图片静态服务）
│   └── pipeline.py        # 主流程编排：校验→槽→路由→归一→调用→兜底→persist→格式化→日志
├── engine/                # ── 内核引擎（纯决策，不碰网络）──
│   ├── router.py          # 路由链①–⑤（含 E1/E2 优先级裁决）
│   ├── normalizer.py      # quality 归一表 / size 就近吸附 / CJK≥30% 检测
│   ├── fallback.py        # 兜底决策：切不切、切换标记、仅 1 次约束
│   └── errors.py          # 九类错误分类 + 切换/retry 矩阵（纯查表，无副作用）
├── infra/                 # ── 基础设施（碰网络/状态）──
│   ├── upstream_b.py     # httpx 客户端：Limits 10/4、四段超时、动态 read、每日重建热切换
│   ├── circuit.py         # 熔断三态：硬 10min / 软 5min / 半开探针；内存态不落盘
│   ├── budget.py          # Deadline 对象 + 并发槽封装（finally 归还纪律的唯一实现点）
│   └── persist.py         # 图片下载转存（30s，永不报错）+ 磁盘水位检查
├── ops/                   # ── 稳定性与生命周期 ──
│   ├── liveness.py        # 心跳协程（事件循环内，asyncio.sleep 驱动）+ WATCHDOG=1 通知
│   ├── shutdown.py        # 优雅停机：SIGTERM→停收新请求→在途排空(≤220s)→退出
│   └── logline.py         # 一行结构化日志（12 字段，三个消费者的唯一数据源）
├── tests/                 # 单测 + 契约快照（description 全等断言、返回格式、错误矩阵）
└── deploy/                # systemd 单元 / config.example.env / 安装说明 / Agent 配置片段
```

### 2.2 职责边界表（"不负责"列 = 防越界镀金，规格书十二节精神）

| 模块 | 唯一职责 | 明确不负责 | 规格书依据 |
|---|---|---|---|
| `server.py` | 装配、启动、sd_notify READY、事件循环托管 | 任何业务逻辑 | 一、六 |
| `config.py` | 环境变量加载、默认值、启动校验（必填缺失 fail-fast、不等式自检） | 热更新（改完 restart 生效） | 五、七 |
| `app/tool.py` | 工具注册、schema、description 固定文案 | 路由/兜底决策 | 二 |
| `app/contract.py` | 成功/兜底/persist/错误四种返回形态的**格式化** | 决定内容（内容来自 engine） | 二 |
| `app/endpoints.py` | /healthz 200 响应、/files/ 图片服务 | 僵死判据（已降级人工巡检） | 一、6.1 |
| `app/pipeline.py` | 按序编排、阶段耗时打点、整体 asyncio 超时 | 单步实现细节 | 3.3 |
| `engine/router.py` | 5 步路由链决策，输出模型+决策来源（供日志） | 熔断状态维护、API 调用 | 3.1 |
| `engine/normalizer.py` | quality→steps/guidance 映射、size 吸附、CJK 比例计算 | 模型选择 | 3.2 |
| `engine/fallback.py` | 收到错误后"切不切"的裁决与切换计数 | 重试（重试在 另一家上游 层） | 3.3、4.1 |
| `engine/errors.py` | HTTP 状态/异常 → 九类之一 + retry 字段 | 任何处理动作 | 4.1 |
| `infra/upstream_b.py` | API 调用、连接池纪律、动态超时、退避重试 1 次、每日重建 | 错误分类（交 errors.py） | 三、五、6.2 |
| `infra/circuit.py` | 三态流转、硬/软计时、半开探针标记 | 路由决策（只被查询/写入） | 4.2 |
| `infra/budget.py` | Deadline 剩余计算、信号量 acquire/finally-release | 分档上限数值（来自 config） | 3.3、五 |
| `infra/persist.py` | 下载落盘、/files URL、水位检查 | **报错（永不）**、自动删图 | 3.3、6.2 |
| `ops/liveness.py` | 15s 心跳 + watchdog 通知 | **独立线程（铁律禁止）** | 6.1 |
| `ops/shutdown.py` | 停收→排空→退出、每日重建的旧客户端延迟关闭调度 | 强杀（systemd 的事） | 6.2 |
| `ops/logline.py` | 12 字段一行日志组装 | 多行、缺字段 | 八 |

### 2.3 依赖关系（DAG，箭头=依赖）

```
config.py ──────────────→ （所有模块，只读）
ops/logline.py ─────────→ （所有模块，横切）

app/tool.py → app/pipeline.py → engine/router.py   → infra/circuit.py（查询）
                            → engine/normalizer.py
                            → engine/fallback.py   → infra/circuit.py（写入）
                            → engine/errors.py
                            → infra/upstream_b.py → infra/budget.py（读剩余）
                            → infra/persist.py
                            → infra/budget.py（acquire/release）
          → app/contract.py（被 pipeline 调用做格式化）
app/endpoints.py（独立于 pipeline，仅依赖 config + 文件系统）

ops/liveness.py、ops/shutdown.py → server.py 事件循环注册（心跳协程与请求路径共循环）
```

**依赖纪律**：
- `engine/*` 是纯决策层，**禁止 import httpx**（可测性 + 边界清晰）；
- `infra/circuit.py` 不 import router（避免环）——router 查询它，fallback 写入它；
- 所有超时数值从 `config` 注入，**任何模块不得硬编码超时常量**（规格书五节校准闭环的前提）；
- 返回字符串只能从 `app/contract.py` 出去——全项目唯一格式化出口。

### 2.4 模块间关键契约（内部接口要点）

| 交接口 | 契约 |
|---|---|
| Deadline 对象 | `remaining()` 单调递减；每阶段耗时动作前必须检查；<0 → 立即转超时错误，不再发起 IO |
| 并发槽 | 只暴露 async context manager 形式（`async with slot(deadline)`），acquire 等待上限 30s，超时抛 busy；**release 在 finally 内部封装，调用方无法绕过** |
| 熔断表 | `check(model) -> CLOSED/OPEN(硬)/OPEN(软)/HALF_OPEN(探针资格)`；`record_success/record_failure(model, kind)`；软熔断连败计数**成功即清零** |
| 路由决策 | 返回 `(model, reason_tag)`，reason_tag 必须是有限枚举（explicit/bypass_circuit/quality_high/cjk/default/draft_speed），直接进日志"路由决策来源"字段 |
| 错误分类 | 返回 `(category, retry, switchable, reason, suggestion)` 五元组，与规格书 4.1 矩阵一一对应 |
| persist | 永远返回 `(url, status)`，status ∈ {persisted, persist_failed, skipped}；**任何内部异常就地吞掉转 persist_failed + WARN 日志** |

---

## 三、开发任务清单（按规格书十一节实施顺序，映射十节 22 项验收）

### 3.0 里程碑总览

| 阶段 | 内容 | 负责人 | 建议工期 | 里程碑出口 |
|---|---|---|---|---|
| S1 | 项目骨架 + 依赖清单 | Ethan | 0.5d | 空服务可启动，配置自检通过 |
| S2 | 另一家上游客户端内核 | Ethan | 2d | 内核单测全绿（mock 上游） |
| S3 | MCP 层 | Ethan | 1.5d | Inspector 可连通调通 |
| S4 | 部署交付物 | Ethan(单元/env) + Leo(文档) | 0.5d | 五件套齐备 |
| S5 | 22 项验收 | Ethan 执行 + 芳复核 | 1d | 验收报告（22/22） |

> 工期为纯开发估计；S2/S3 可在 Q1–Q3 澄清确认前先行（澄清只影响路由⑤与 fallback 两处小逻辑）。

### 3.1 阶段 S1：项目骨架 + 依赖清单（Ethan）

| 任务 | 内容 | 产出物 | 验收映射 |
|---|---|---|---|
| S1.1 | 仓库结构（2.1 节）、依赖清单（fastmcp / httpx / sdnotify 或等价 / pytest / py-spy 列入部署文档预装） | 可 git 管理的项目骨架 | 基础（支撑全部） |
| S1.2 | `config.py`：第七节全部变量（名与默认值**原样**）、必填缺失 fail-fast、启动打印嵌套不等式自检（read ≤ 分档 ≤ 200 < 210 建议 < 220，违例拒绝启动） | 配置模块 + config.example.env 雏形 | 支撑⑳⑲ |

### 3.2 阶段 S2：另一家上游客户端内核（Ethan，对应规格书实施顺序第 2 步）

| 任务 | 内容 | 产出物 | 验收映射 |
|---|---|---|---|
| S2.1 | `budget.py`：Deadline 对象 + 并发槽 context manager（finally 归还、等待 30s 计入预算、到点返 busy） | 模块 + 单测 | ⑲㉑ E4 E5 |
| S2.2 | `upstream_b.py`：httpx 客户端（Limits 10/4；connect/write/pool=5s；read=动态 min(分档, 剩余)；keep-alive 25s）；5xx/网络退避重试 1 次（0.25–1s 指数+全抖动，尊重 Retry-After）；每日重建指针热切换 | 模块 + 单测 | ⑳⑯ E6 |
| S2.3 | `normalizer.py`：quality 归一表（draft 15步 / standard 20步+guidance7.5 / high 25步+guidance7.5；schnell 恒 4 步）；size 就近吸附档位表；CJK≥30% 检测；schnell 静默丢 guidance | 模块 + 单测（表驱动） | ②③⑤ 间接 |
| S2.4 | `circuit.py`：三态流转（硬 600s/软 300s 可配）；半开仅放行第一个请求带探针标记；软熔断连败 2 次、成功清零；纯内存态 | 模块 + 单测（状态机全路径） | ⑨⑪ E8 4.2 |
| S2.5 | `router.py`：路由链①显式→②熔断→③high→④CJK→⑤默认（含 E1：熔断优先于 quality；E2：显式绕过熔断、失败刷新熔断；draft+中文按第六节 Q1 确认结果实现） | 模块 + 单测（路由链真值表） | ②③④⑤ E1 E2 |
| S2.6 | `errors.py`：九类错误分类矩阵（4.1 表逐行实现）+ retry 字段（no/yes/fixthenretry） | 模块 + 参数化单测（9 类 × 期望矩阵） | ⑧⑩⑫ |
| S2.7 | `fallback.py`：切换裁决（可切换类错误才切；切换计数 ≤1，两模型各试一遍即止；余额不足快失败返超时错误，按第六节 Q2 确认结果实现） | 模块 + 单测 | ⑧ 3.3 |

### 3.3 阶段 S3：MCP 层（Ethan，对应规格书实施顺序第 3 步）

| 任务 | 内容 | 产出物 | 验收映射 |
|---|---|---|---|
| S3.1 | `tool.py`：7 参数 schema（名/型/默认/枚举照抄第二节）+ description 逐字符复制 + 单测全等断言；MCP 层 Bearer 鉴权 | 工具定义 | ①⑥⑫ |
| S3.2 | `contract.py`：四种返回形态格式化（正常/兜底 meta 追加/persist 成败/error 行）；快照测试锁定格式 | 契约模块 + 快照 | ⑥⑧㉒ |
| S3.3 | `pipeline.py`：主流程编排 + 每请求整体 asyncio 超时 + 阶段耗时打点 + 兜底预算检查（固有特性落地：切换前 remaining ≥ 目标分档） | 可运行 Server | ⑲②③④⑤⑥⑧ |
| S3.4 | `persist.py` + `endpoints.py`：下载 30s（计入 deadline）、/files/ 静态服务、/healthz；磁盘水位检查接口（超线返回提示，**不删图**） | 模块 + 端到端测试 | ⑱㉒ 6.2 |
| S3.5 | `liveness.py` + `shutdown.py`：心跳协程（事件循环内 15s）+ sd_notify WATCHDOG=1；SIGTERM 优雅停机（停收→排空→退出）；每日客户端重建调度（旧客户端排空后 aclose） | 模块 | ⑬⑮ E6 6.1 6.2 |
| S3.6 | `logline.py`：12 字段一行结构化日志（ts/req_id/prompt_hash/路由决策来源/model/参数摘要/各阶段耗时/结果或错误类别/fallback标记+原因/超时触发标记/persist状态/总耗时） | 日志模块 | ⑨（日志验证）⑯ |

### 3.4 阶段 S4：部署交付（对应规格书实施顺序第 4 步）

| 任务 | 内容 | 负责人 | 产出物 | 验收映射 |
|---|---|---|---|---|
| S4.1 | systemd 单元：Type=notify / WatchdogSec=30 / Restart=always / RestartSec=5 / StartLimitBurst=5 + StartLimitIntervalSec=60 / MemoryMax=512M / TimeoutStopSec=220 / EnvironmentFile(600) / 专用低权限用户 / After=network.target；磁盘水位 systemd timer | Ethan | mcp-imagegen.service + timer | ⑬⑭ |
| S4.2 | config.example.env：第七节变量总表全覆盖，密钥项占位 | Ethan | config.example.env | ⑫ |
| S4.3 | 安装说明：依赖安装、py-spy 预装、systemd 装载步骤、更新操作流程（先 journalctl 确认无在途再 restart）、运维三件套使用（journalctl -f / ss close-wait / py-spy dump）、Caddy 可选前置（反代读超时 ≥210s） | Leo | install.md | ⑮⑰ |
| S4.4 | Agent 配置片段：mcpServers 条目（url: http(s)://<服务器>/mcp + headers Authorization: Bearer <token>），含客户端超时 ≥210s 建议 | Leo | agent-config.md | ⑦ |

### 3.5 阶段 S5：22 项验收（对应规格书实施顺序第 5 步）

| 任务 | 内容 | 执行人 | 验收映射 |
|---|---|---|---|
| S5.1 | A 功能 7 项：Inspector 连接、中/英路由、显式覆盖、high 强制、batch=4、真实 Agent 端到端 | Ethan | ①–⑦ |
| S5.2 | B 兜底熔断 4 项：额度耗尽切换+meta、429 后 10min 直路由（查日志）、审核 400 仅 1 次 API 调用、探针失败续期 | Ethan | ⑧–⑪ |
| S5.3 | C 稳定性 7 项：错 Token、SIGSTOP 模拟僵死→watchdog 重启、连崩 5 次风暴保护、更新期在途完成、24h CLOSE-WAIT≈0、py-spy 可用、/healthz | Ethan（⑯需挂 24h 观察，可用定时采集） | ⑫–⑱ |
| S5.4 | D 超时预算 3 项：慢死路径 ≤200s 截断返结构化错误、read 动态放大不误杀 batch=4、连续 10 次异常后第 11 次仍拿得到信号量 | Ethan | ⑲㉑ |
| S5.5 | E persist 2 项：下载失败仍成功返原 URL+persist_failed、/files URL 24h 后仍可访问 | Ethan | ㉒ |
| S5.6 | 汇总 22 项结论 + 证据（日志/截图/命令输出）+ 校准闭环基线数据（上线后 7 天首报） | 芳 | 交付闭环 |

---

## 四、高风险点与易错点（开发红线前置，逐项给出防错手段）

> 排序按"走样后果严重度"。R1–R5 为规格书点名的高危项，**对应单测不过不得进入 S3**。

### R1 信号量 finally 归还 —— 本项目最高危根因（规格书 6.1 原文）

- **走样后果**：某条异常路径忘记归还 → 3 个额度渐进泄漏 → 数日后所有请求返 busy，且症状伪装成"并发繁忙"，极难排查。
- **防错手段**：
  1. 并发槽只以 `async with` context manager 形式暴露（S2.1），release 封装在 `__aexit__`/finally 内部，**调用方在语法上无法绕过**；
  2. 覆盖全部异常路径：参数校验后所有步骤、`asyncio.CancelledError`（Agent 断连）、`BaseException`；
  3. 每次 acquire/release 打含 req_id 的 debug 日志，泄漏时可对账；
  4. 验收㉑（连续 10 次异常后第 11 次仍能拿到信号量）作为回归门禁。

### R2 心跳绝不放独立线程 —— 铁律（规格书 6.1 原文）

- **走样后果**：独立线程里跳心跳 = 假活——事件循环死了线程照跳，watchdog 永不触发，僵死变成永久僵死。
- **防错手段**：
  1. 心跳协程 = 事件循环内的普通 task，`asyncio.sleep(15)` 驱动，每醒一次即循环转动的证明；
  2. 每次心跳发 `sd_notify("WATCHDOG=1")`，间隔 15s < WatchdogSec 30s 的一半，留足裕量；
  3. `Type=notify` 要求启动时发 `READY=1`，否则 systemd 判启动失败；
  4. code review 明确检查：liveness.py 中**不得出现 threading / to_thread / run_in_executor**。

### R3 deadline 贯穿传递断裂（规格书 3.3、五节）

- **走样后果**：任一阶段用固定超时而非"min(自身上限, 剩余)" → 总耗时冲破 200s → Agent 侧 210s 先断连，用户看到的是断连而非结构化错误，整个预算体系失效。
- **防错手段**：
  1. Deadline 对象在 pipeline 入口创建，**作为参数传入每一个异步阶段**（槽等待、API 调用、重试退避、persist 下载）；
  2. 排队等待耗时计入预算（E4）：acquire 返回后立即重算 remaining；
  3. 兜底切换前检查 remaining ≥ 目标模型分档上限（规格书指出这是固有特性无需特判，但实现须验证不等式成立：Kolors 失败常剩 ~80s ≥ schnell 30s；schnell 失败剩 ~170s ≥ Kolors 60/120s）；
  4. 任何阶段入口检查 remaining ≤ 0 → 立即返超时错误，不再发起 IO；
  5. 启动自检嵌套不等式（S1.2），违例拒绝启动。

### R4 熔断三态流转不完整（规格书 4.2）

- **走样后果**：半开状态泄洪（多个请求同时当探针）、软熔断计数不清零（一次成功后仍熔断）、显式请求不刷新熔断（E2 破坏）。
- **防错手段**：
  1. 状态机三态：CLOSED → OPEN(硬600s/软300s) → 到期 HALF_OPEN → **仅第一个请求**获探针资格（E8：就是普通请求，带探针标记、正常超时预算），成功→CLOSED，失败→续期一轮 OPEN；
  2. 半开期间的后续请求仍按 OPEN 处理（绕开该模型）；
  3. 软熔断"连续 2 次"超时/5xx——**成功一次即清零计数**；
  4. 显式 model 绕过熔断表，但其失败结果照样写入熔断状态（E2：显式请求=新鲜探针，刷新熔断）；
  5. 熔断只作用于路由链②，quality③、CJK④等步骤照常评估（E1：high+Kolors 熔断 → 落 schnell + meta 注明降级）；
  6. 纯内存态，重启清零——**不做落盘**（规格书明确：重启重学代价 = 1 个失败请求，不值得持久化）；
  7. 单测覆盖状态机全路径（含 E1/E2/E8 三条边界）。

### R5 persist 永不报错（规格书二、3.3、4.1）

- **走样后果**：图已生成、额度已花，persist 内部异常（磁盘满/网络/权限）若冒泡到顶层 → 整个请求变 [error]，白花钱且违背"降级出图优于报错"。
- **防错手段**：
  1. persist 整体 try/except Exception，就地转：WARN 日志 + 另一家上游原始 URL + meta 加 `persist_failed`；
  2. 下载超时独立 30s 且取 min(30s, deadline 剩余)（E7）；
  3. 注意停机交互：优雅停机排空窗口内，在途请求的 persist 应被允许完成（排空 ≤220s 覆盖）；
  4. 磁盘水位超线 → WARN 日志 + meta 提示（**不自动删图**，删图是危险动作）。

### R6 重试与切换纪律走样（规格书 4.3、3.3）

- **走样后果**：对 429 重试 = 撞墙上加撞墙；重试多次 = 预算失控；无界切换 = 死循环烧免费额度。
- **防错手段**：
  1. **仅** 5xx/网络/超时重试 1 次；退避 0.25–1s 指数 + 全抖动，尊重 Retry-After 头；
  2. 429 **零重试**，直接硬熔断 + 切换；400 审核 / 401 / 403 / 422 零重试零切换；
  3. 切换仅 1 次：A 失败 → 试 B；B 也失败 → 返回错误。**绝不回到 A**；
  4. 审核 400 只发生 1 次 API 调用（验收⑩的语义：不重试、不切换、不兜底）。

### R7 嵌套超时不等式与动态 read（规格书五节）

- **走样后果**：read 写死 → batch=4 Kolors（档 120s）被 30s read 误杀（违背⑳）；或 read > 分档 → 分档形同虚设。
- **防错手段**：
  1. 每次 API 调用动态构造 `httpx.Timeout(connect=5, write=5, pool=5, read=min(分档, remaining))`；
  2. 分档表：schnell 任意 batch 30s；Kolors batch 1–2 60s；Kolors batch 3–4 120s；
  3. 全部来自 config（TIMEOUTSCHNELL / TIMEOUTKOLORSSMALL / TIMEOUTKOLORS_LARGE），校准闭环靠它；
  4. 不等式链 read ≤ 档 ≤ 200 < Agent 210 < TimeoutStop 220，启动自检 + 文档双重锁定。

### R8 httpx 每日重建砍断在途请求（规格书 6.2、E6）

- **走样后果**：重建时直接 `await old.aclose()` → 在途请求连接被抽走，正常请求莫名失败。
- **防错手段**：指针热切换——新客户端即刻接管新请求；旧客户端挂后台任务，等其上在途请求全部完成后才 aclose。keep-alive 25s（保守值，防半死连接积累，配合⑯ CLOSE-WAIT≈0）。

### R9 优雅停机误杀在途（规格书 6.2）

- **走样后果**：SIGTERM 即退 → 更新窗口期在途请求全灭（验收⑮直接失败）。
- **防错手段**：SIGTERM → 停收新请求（/mcp 返回服务不可用）→ 等待在途排空（上限 220s，覆盖：排队者 30s 自然 busy + API 档 120s + persist 30s 的最坏叠加）→ 主动退出。systemd TimeoutStopSec=220 与之对齐，超时未退由 systemd SIGKILL 兜底。更新操作流程写入 install.md：先 `journalctl` 确认无在途再 restart。

### R10 错误分类矩阵走样（规格书 4.1，九类）

- **走样后果**：Agent 读 retry 字段决定动作——分类错一个，Agent 行为就错一个（如把上游 401 当可重试 → 无限撞墙）。
- **防错手段**：
  1. **双层鉴权严格区分**：MCP 层 401（MCPACCESS_TOKEN，找管理员）≠ 上游 401/403（UPSTREAM_B_TOKEN，服务器配置错）——两者都 retry=no，但 category 与 suggestion 不同；
  2. 422 = 归一层 bug 的信号通道（报错码反馈给开发者，不切换不重试）；
  3. errors.py 做成纯查表 + 参数化单测逐行对照 4.1 矩阵（9 类 × 切换 × retry 全断言）。

### R11 返回契约与 meta 完整性（规格书二）

- **走样后果**：meta 缺字段/格式漂移 → Agent 解析失败或误判。契约是 Agent 唯一感知面。
- **防错手段**：
  1. 格式化统一走 contract.py（唯一出口）+ 快照测试锁定四种形态；
  2. meta 必报**实际值**：size 是吸附后的（不是请求值）、model 是真实出图模型；
  3. 五类 meta 追加标记不得遗漏：兜底 `fallback: 原因, 已切换至 …`、persist 成功 `persisted`、persist 失败 `persist_failed`、E1 降级注明、draft+中文换速度注明；
  4. error 行格式逐字段：`[error] category=<码> retry=no|yes|fixthenretry reason=… suggestion=…`。

### R12 description 与 schema 被顺手优化（规格书二）

- **走样后果**：文案改动会实际影响 Agent 侧工具选择行为——这是对外契约，不是注释。
- **防错手段**：description 逐字符复制规格书第二节；单测做**字符串全等断言**；7 参数的名称/类型/默认值/枚举值（model: kolors/flux-schnell/auto；quality: draft/standard/high；batch 1–4）同样锁定。任何"优化冲动" → 一律拒绝。

### R13 日志字段缺失（规格书八）

- **走样后果**：一行日志喂三个消费者（校准闭环 / 故障定位 / 审计）——缺字段 = 对应能力失明。如缺"超时触发标记"，7 天后校准闭环无从谈起。
- **防错手段**：logline.py 强制 12 字段 schema，缺失显式写 null 而非省略；S3.6 用单测断言字段全集。prompt 只记 hash 不记原文（审计够用 + 不泄内容）。

---

## 五、不得改动的约束（防走样红线清单）

> **原则：以下任何一项与规格书冲突时，以规格书原文为准。改动 = 走样，不接受"等价改写"。**

### A. 对外契约类（Agent 可见，最严格）

| # | 红线 | 锁定手段 |
|---|---|---|
| A1 | 工具 description 文案逐字符：`Generate images from a text prompt. Auto-selects model by prompt language: Chinese → Kolors (quality), English → FLUX.1-schnell (fast). Returns image URL(s).` | 单测全等断言 |
| A2 | 7 参数 schema：prompt(必填) / model(auto) / quality(standard) / size(1024x1024) / batch_size(1–4) / seed(随机) / persist(false) | 单测 |
| A3 | 返回契约四形态 + meta 字段（model/quality/size实际值/fallback/persisted/persist_failed）+ error 行（category/retry/reason/suggestion） | 快照测试 |
| A4 | retry 三值语义：`no` / `yes` / `fixthenretry`——Agent 据此决定动作 | 矩阵单测 |

### B. 数值参数类（全部配置化，默认值=规格值）

| # | 红线 |
|---|---|
| B1 | 预算体系：deadline 200s、3 槽、等待 30s（计入 deadline） |
| B2 | API 分档：schnell 30s（任意 batch）/ Kolors 60s（batch 1–2）/ Kolors 120s（batch 3–4） |
| B3 | httpx 四段：connect/write/pool 5s、read 动态、keep-alive 25s、Limits 10/4 |
| B4 | persist 下载 30s、心跳 15s、watchdog 30s、TimeoutStop 220s、硬熔断 600s、软熔断 300s |
| B5 | 配置变量名与默认值 = 第七节总表原样（UPSTREAM_B_TOKEN / MCPACCESS_TOKEN / PORT 8000 / IMAGE_DIR / MAXCONCURRENCY / QUEUEWAIT_SEC / TOTALBUDGETSEC / TIMEOUTSCHNELL / TIMEOUTKOLORSSMALL / TIMEOUTKOLORS_LARGE / PERSISTTIMEOUTSEC / CIRCUITHARDSEC / CIRCUITSOFTSEC / HTTPXKEEPALIVESEC） |
| B6 | 嵌套不等式：read ≤ 档 ≤ 200 < Agent 建议 210 < TimeoutStop 220 |

### C. 行为语义类

| # | 红线 |
|---|---|
| C1 | 路由链顺序：①显式 → ②熔断 → ③high 强制 Kolors → ④CJK≥30% → ⑤默认 schnell（E1：②优先于③） |
| C2 | quality 归一表数值：draft 15 步 / standard 20+guidance7.5 / high 25+guidance7.5；schnell 恒 4 步、静默丢 guidance 不报错；high 落 schnell 仅熔断兜底时可能且 meta 注明降级 |
| C3 | 九类错误的切换/retry 矩阵（4.1 表逐行） |
| C4 | 重试纪律：仅 5xx/网络 1 次，退避 0.25–1s 指数+全抖动、尊重 Retry-After；429 零重试；超时重试的重复计费风险已接受，不需规避 |
| C5 | 切换仅 1 次（两模型各试一遍即止）；降级出图优于报错 |
| C6 | persist 永不报错（失败→原 URL + persist_failed + WARN） |
| C7 | 心跳在事件循环内，绝不开独立线程；/healthz 不参与僵死判据 |
| C8 | 熔断内存态不落盘；半开探针=普通请求带标记 |
| C9 | E1–E8 边界裁决全部按第九节速查表执行 |
| C10 | 磁盘水位超线只 WARN + meta 提示，不自动删图 |

### D. 范围类（规格书十二节 + 镀金禁令）

多实例负载均衡 / K8s / 外部 APM / 双机热备 / 消息队列 / 熔断落盘 / 自动删图——**明确不做**。开发过程中任何人提出"顺手加上 X"→ 默认拒绝，需用户明示才可评估。

---

## 六、实现语义澄清（3 问，请用户一句话确认；确认前 S2.5/S2.7 可先按推荐解读开发）

规格书决策已定案，以下 3 处是**实现层面的语义解读确认**，不构成决策变更：

**Q1 · 路由⑤"draft + 中文允许落 schnell"如何落地？**
规格书 3.1 路由链：④"CJK≥30% → Kolors"，⑤"默认 → schnell；draft+中文允许落 schnell（meta 注明换速度）"。
**推荐解读**：中文 + quality=draft 时，④放行、穿透到⑤落 schnell（快模型换速度），meta 注明换速度——即"允许"在实现上表现为默认路由结果。若理解为"仍落 Kolors、仅在 Kolors 熔断时落 schnell"，则⑤该句无行为意义。
**确认**：是否按推荐解读执行？

**Q2 · "余额够则切换 / 余额不足快失败"的判定依据？**
规格书 3.3："429/402 → 记硬熔断 10min → 余额够则切换"；"切换仅 1 次；余额不足 → 快失败返超时错误"；4.1 表：429/402 标 ✅切换。
**推荐解读**：不引入独立余额查询（规格书未定义该接口）——收到 429/402 即硬熔断并切换，由另一个模型的真实调用裁决（与"显式请求=新鲜探针"哲学一致）；切换后仍余额类失败 → 两模型各试一遍即止 → 快失败返超时类错误（retry=yes，Agent 稍后再来）。
**确认**：是否按推荐解读执行？

**Q3 · persist 返回的 /files URL 如何绝对化？**
规格书二："persist 成功 → URL 为 /files/…"。Agent 需要可直接访问的完整 URL，但第七节配置表无对外地址变量。
**推荐解读**：Server 按请求的 Host / X-Forwarded-Proto / X-Forwarded-Host（Caddy 前置场景）推断完整 URL（如 `https://<域名>/files/xxx.png`），不新增配置变量。
**确认**：是否按推荐解读执行？（若部署环境 Host 头不可靠，再评估追加一个可选变量，届时另行确认）

---

## 七、验收执行方案（22 项，第十节映射）

| # | 验收项 | 测试方法要点 | 执行 | 证据 |
|---|---|---|---|---|
| ① | Inspector 连接/单工具 | MCP Inspector 连 /mcp，列出 generate_image | Ethan | 截图 |
| ② | 中文→kolors | 中文 prompt 调用，查日志路由来源=cjk，meta model=kolors | Ethan | 日志行 |
| ③ | 英文→schnell | 英文 prompt，meta model=flux-schnell | Ethan | 日志行 |
| ④ | 显式覆盖 | model=kolors + 英文 prompt → kolors | Ethan | 日志行 |
| ⑤ | high+英文→kolors | quality=high + 英文 → 强制 Kolors | Ethan | 日志行 |
| ⑥ | batch=4 返 4 URL | batch_size=4，返回 4 行 URL + meta | Ethan | 返回体 |
| ⑦ | 真实 Agent 端到端 | 按 agent-config.md 配置真实 Agent 出图 | Ethan+用户 | Agent 对话记录 |
| ⑧ | 额度耗尽→切换+meta | 模拟（改错 key / mock 429）后调中文 prompt → 切换至 schnell + meta fallback | Ethan | 返回体+日志 |
| ⑨ | 429 后 10min 直路由 | 触发 429 后（可临时把 CIRCUITHARDSEC 调短验证逻辑），期间非显式请求绕开该模型；到期半开探针 | Ethan | 日志（路由来源含 bypass_circuit/探针标记） |
| ⑩ | 审核 400 仅 1 次 API 调用 | 构造审核敏感 prompt，日志统计上游调用次数=1 | Ethan | 日志计数 |
| ⑪ | 探针失败熔断续期 | 半开态发探针（mock 失败）→ 状态回 OPEN 续期一轮 | Ethan | 单测+日志 |
| ⑫ | 错 Token 401 | 错误 MCPACCESS_TOKEN 调用 → [error] MCP鉴权 retry=no | Ethan | 返回体 |
| ⑬ | STOP 模拟僵死→watchdog 重启 | `kill -STOP <pid>` → 30s 后 systemd 判死并重启 | Ethan | journalctl |
| ⑭ | 连崩 5 次风暴保护 | 连续 5 次人为崩溃 → StartLimit 触发停止重启 | Ethan | journalctl |
| ⑮ | 更新期在途请求完成 | 发起慢请求（Kolors batch=4）期间执行 restart 流程 → 在途请求正常返回 | Ethan | 返回体+journalctl |
| ⑯ | 24h CLOSE-WAIT≈0 | 持续运行 24h，`ss -tan state close-wait \| wc -l` ≈0 | Ethan（定时采集） | 命令输出 |
| ⑰ | py-spy 可用 | `py-spy dump --pid <pid>` 输出调用栈 | Ethan | 栈输出 |
| ⑱ | /healthz | curl 返回 200 | Ethan | curl 输出 |
| ⑲ | 慢死路径 ≤200s 截断 | mock 上游挂起 → ≤200s 返回结构化超时错误（非断连） | Ethan | 返回体+耗时 |
| ⑳ | read 动态不误杀 batch=4 | Kolors batch=4 真实慢出图不被 30s 误杀 | Ethan | 日志 read 值 |
| ㉑ | 连续 10 次异常后第 11 次仍得槽 | 构造 10 次中途异常（如取消/上游异常），第 11 次正常获槽执行 | Ethan | 日志对账 |
| ㉒ | persist 降级 + 24h 有效 | mock 下载失败 → 原URL+persist_failed；persist 成功的 /files URL 24h 后仍可访问 | Ethan | 返回体+24h 后 curl |

**验收结论**：由芳汇总输出《验收报告》（22/22 逐项结论 + 证据索引 + 遗留项）；上线后 7 天输出首期校准数据（按模型×batch 的 P99 与超时触发率，对照规格书五节目标区间 <1%）。

---

## 八、下一步

1. 用户确认第六节 Q1–Q3（一句话即可，不阻塞 S1/S3/S4 开工）；
2. Ethan 按第三节顺序开工，R1–R5 对应单测为进入 S3 的门禁；
3. Leo 在 S3 完成后接手 S4.3/S4.4 交付文档；
4. 芳在 S5 全程跟踪验收并出具报告。

下一步建议：用户回复 Q1–Q3 确认后，Ethan 即按 S1→S5 顺序开工，预计 5–6 个工作日交付全部 22 项验收。
