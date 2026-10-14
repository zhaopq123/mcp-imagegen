# MCP 图片/视频生成 Server · 增量设计方案 V2（纯 上游服务提供方 双工具版）

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 日期 | 2026-10-10 |
| 作者 | 芳（产品负责人） |
| 上位文档 | 《MCP 图片/视频生成 Server · 技术规格书》（最终交付版）＋ 用户 2026-10-10 需求变更两条指示 |
| 本文档使命 | 增量 diff：只写"删什么、留什么、加什么、怎么验"，零决策变更、零重写 |
| 读者 | Ethan（全栈开发·主力）、芳（验收复核）、用户（3 个确认点，其中 1 项阻塞） |

---

## 〇、需求变更记录（本文档的增量基线）

**变更指示①**（用户，2026-10-10 10:01，群聊原文）：
> 需求变了 另一家上游的api删掉，只留 上游的这两个 一个生视频 一个生图

**变更指示②**（用户，2026-10-10 10:23，群聊原文）：
> 把api配置好 bug都测试好 测试key：sk-LGVj…（测试凭据，仅入部署 EnvironmentFile，不入本文档/git/日志）

**变更性质**：推翻规格书"双 Provider 三工具"架构中的 Provider A（另一家上游）。系统简化为**单 Provider（上游服务提供方）双模型三工具**：`generate_image`（example-image-1）+ `generate_video`（example-video-1）+ `query_video`。

**规格书条款处置**：
- **作废**（涉及另一家上游/kolors/flux-schnell/双 Provider）：设计哲学中"路由/兜底"两支柱的双模型语义；3.1 路由链③④⑤（quality 强制/CJK/默认落另一家上游模型）；3.2 中 Kolors/schnell 两列归一表与吸附档位表；4.1 中 402 行（仅另一家上游）；4.2"四模型"表述；七节 UPSTREAM_B\_\* 变量；十节 ②③⑤⑧㉚㊲ 等另一家上游专属验收项；E15（GET /v1/models 核对）；附录 A-1/A-7。
- **沿用**（与 Provider 无关或纯 上游服务提供方 侧）：四层架构与装配链；预算体系全部数值（deadline 200s、3 槽/等待 30s）；上游服务提供方 三端点与参数；视频流水线与任务表全部设计；persist；错误分类框架；熔断三态；心跳/停机/每日重建；日志字段集；全部超时数值（见六节配置全集）。

**复杂度净变化**：删一个 Provider → 路由退化为直通、跨模型兜底消失、双客户端互扰风险消失；整体风险面收窄，但**唯一生图模型 + 熔断**的可用性语义成为新边界（见 E24、R18）。

---

## 一、变更总览：删 / 留 / 加

### 1.1 删除清单（代码与配置彻底移除，不留开关——历史由 git 承载；彻底性待 Q2 确认）

| 层 | 删除项 | 理由 |
|---|---|---|
| infra | `upstream_b.py` 整文件（另一家上游Client） | 唯一上游已换 |
| engine | `router.py` 路由链①–⑤全链（quality_high/cjk/draft_speed/default 判定）、MODELS 双模型常量 | 唯一生图模型，无路由可选 |
| engine | `fallback.py` 的 `decide_switch`（切换裁决）、tried_models 循环 | 唯一模型无切换（对齐视频侧 E19 语义）；`finalize_error` 保留语义并入 pipeline |
| engine | `normalizer.py` 的 `_STEPS`/`_GUIDANCE` 归一表、Kolors/schnell 吸附档位表、`cjk_ratio`/`is_cjk` | image 无 steps/guidance；size 直传；语言检测失去消费者 |
| engine | `errors.py` 的 402 分类分支 | 上游服务提供方 无限期间免费，402 不可能出现（规格书 4.1"仅另一家上游侧"） |
| config | `UPSTREAM_B_BASE_URL`、`UPSTREAM_B_TOKEN`、`TIMEOUTSCHNELL`、`TIMEOUTKOLORS_SMALL`、`TIMEOUTKOLORS_LARGE` | 随 Provider 删除 |
| tool | `generate_image` 旧 schema 的 model 枚举值 kolors/flux-schnell、旧 description（含另一家上游措辞） | 接口面重裁（见八节 Q1） |
| tests | `test_upstream_b.py`、`test_router.py`（路由链部分）、`test_fallback.py`（切换部分）、`test_normalizer.py`（归一表/吸附/CJK 部分） | 对应实现删除 |
| 根目录 | 上轮《开发设计方案》中 S 系列另一家上游任务 | 已被本方案 T 系列替代 |

### 1.2 保留清单（V1 已验证资产，回归即可）

| 资产 | 说明 |
|---|---|
| 四层结构 app/engine/infra/ops + server.py 装配链 | 骨架不动，仅换组件 |
| `budget.py`（Deadline/SlotPool，finally 归还语义） | 视频侧复用同一槽池 |
| `circuit.py` 三态状态机 | 模型集从"按需扩展"固定为 {image, video}，代码零改动（本就不限模型数） |
| `persist.py`（永不报错/降级原 URL/水位只 WARN） | 扩展视频下载 |
| `endpoints.py`（BearerGate/healthz/files 防穿越） | files 白名单加视频扩展名 |
| `liveness.py`（心跳铁律/每日重建 rotate）+ `shutdown.py`（排空协调） | 单客户端后更简单 |
| `logline.py` 12 字段 + `contract.py` 四形态契约 | 扩展新字段/新 meta 标记 |
| V1 全部 225 单测中与 Provider 无关者（budget/circuit/no_threads/config 骨架/errors 骨架/tool 骨架） | 全量回归门禁 |

### 1.3 新增清单

| 层 | 新增项 |
|---|---|
| infra | `upstream.py`（上游服务提供方 客户端：生图/视频提交/视频查询 + rotate） |
| infra | `tasks.py`（任务表：登记/TTL/容量逐出） |
| app | `video_pipeline.py`（视频流水线：提交→轮询→persist；query_video 入口） |
| app | tool 层 `generate_video`/`query_video` 两工具注册 |
| config | 上游服务提供方/视频/任务表/视频 persist 全部变量（见六节） |
| tests | `test_upstream.py`、`test_tasks.py`、`test_video_pipeline.py` + e2e 视频场景 |

---

## 二、新增模块设计（含四层落位）

### 2.1 上游服务提供方 客户端（infra/upstream.py，替代 upstream_b.py 的生态位）

```
UpstreamClient(cfg, transport=None)          # transport 注入口供单测（沿用 V1 模式）
├─ generate_image(model, prompt, size, deadline)        → ApiCallResult
│    POST {UPSTREAM_BASE_URL}/v1/images/generations
│    请求体: {"model": "example-image-1", "prompt": …, "size": "WxH"}
├─ submit_video(model, prompt, seconds, aspect_ratio, deadline) → video_id
│    POST {UPSTREAM_BASE_URL}/v1/videos
│    请求体: {"model": "example-video-1", "prompt": …,
│             "seconds": "5"(str), "mode": "text", "size": "720P",
│             "aspect_ratio": "16:9"}
├─ query_video(video_id, model, deadline) → {status, url?}
│    GET {UPSTREAM_BASE_URL}/v1/videos/query?video_id=…&model_name=example-video-1
└─ rotate() / aclose()                    # 每日 04:00 指针热切换，排空后关闭
```

**纪律**：
- 独立 httpx 池：Limits(max_connections=10, max_keepalive_connections=4)；四段超时 connect/write/pool = `HTTPX_CONNECT_TIMEOUT_SEC`(5s) 全显式，keep-alive 25s；**read 动态**：生图 = min(`TIMEOUT_UPSTREAM_IMAGE_SEC`, deadline 剩余)，提交 = min(`VIDEO_SUBMIT_TIMEOUT_SEC`, 剩余)，单查 = min(`VIDEO_QUERY_TIMEOUT_SEC`, 剩余)。
- 重试纪律沿用 C4：仅 5xx/网络/超时退避重试 1 次（0.25–1s 全抖动、尊重 Retry-After、计入 deadline）；429/4xx 零重试。
- 响应解析双兼容（E14）：先查 `images` 再查 `data`，首次实测后定死（附录 A-2）。
- 别名常量映射（E15 精神延续）：`image → example-image-1`、`video → example-video-1`，禁止自由文本 model。
- 每日重建：复用 `liveness.daily_rebuild_loop(client)`——纯 上游服务提供方 后**仅一个**重建协程（原"双客户端互不干扰"风险点随之消灭）。

### 2.2 视频流水线（app/video_pipeline.py）

```
run(prompt, seconds, aspect_ratio, seed, persist)
  → 参数校验（seconds 4–12 int、aspect_ratio 三档枚举、seed 视 Q1）
  → deadline = now + 200s
  → 槽：async with pool.slot(deadline)        # 一请求一槽，含轮询期；finally 归还
  → 提交 submit_video（read ≤30s）→ video_id → 登记任务表
  → 轮询循环：
       while True:
         await sleep(VIDEO_POLL_INTERVAL_SEC=5s)     # 协程 sleep，不碰线程
         r = query_video(read ≤10s)
         poll_count += 1
         ├─ failed    → 错误分类返回（E19：唯一模型无切换）
         ├─ completed → break
         └ deadline 到仍 running → 返 [meta] task_id=… status=running,
            已等待 …s, 请稍后调用 query_video（非错误；槽随 with 块自然释放）
  → persist？→ 下载视频（min(120s, 剩余)，VIDEO_DIR）→ 失败降级原 URL 永不报错
  → 契约行 + 一行日志（task_id、poll_count 字段）

query_video(task_id)
  → 校验非空 → 查任务表：
     ├─ 未命中 → [error] task_not_found retry=fixthenretry（E18）
     └─ 命中 → 实时查上游（read ≤10s，不占信号量）→ completed→URL / running→状态行
```

**关键语义**：
- **槽持有期 = 提交 + 全部轮询 + persist**（规格书 3.4"一请求一槽，含轮询等待期"）。超预算返 running 时，`async with` 退出即释放——与 V1 budget.py 的 finally 语义天然一致，无需新代码路径，只需单测锁定。
- 轮询是状态查询、幂等，不计入重试纪律（4.3）。
- 停机排空：视频请求在途时 `ShutdownCoordinator` 计数照常 +1，优雅停机 ≤220s 覆盖轮询中视频（验收⑯新增子场景）。

### 2.3 任务表（infra/tasks.py）

```
TaskTable(ttl_sec=TASK_TTL_SEC=7200, max_entries=TASK_MAX=100)
├─ register(task_id, model, prompt_hash) → TaskEntry{status="running", submitted_at}
├─ update(task_id, status, url=None)      # 轮询/查询时实时打上游回写
├─ get(task_id) → TaskEntry | None        # 过期视为未命中
├─ cleanup_loop()                         # 事件循环协程：周期清 TTL 过期项（禁线程）
└─ 逐出策略：容量超 100 → 按 submitted_at 逐出最旧（含未完成——上游任务不受影响，
   Agent 端表现为 task_not_found，重新提交即可，与 E18 同哲学）
```

- 纯内存 dict，不落盘（重启丢失 = task_not_found，重学代价 1 次提交）。
- 清理协程与心跳同为事件循环 sleep 驱动，不阻塞 watchdog。
- `generate_video` 的轮询每次查询后顺手 `update`，`query_video` 命中后同样 update——表内 status 永远是最近一次上游实况（"状态实时打上游"）。

### 2.4 四层落位总图（增量后）

```
app/      tool.py(三工具) pipeline.py(生图·瘦身) video_pipeline.py(新)
          contract.py(+5 类 meta 标记) endpoints.py(files 白名单+视频扩展名)
engine/   router.py(退化为别名常量+熔断门) normalizer.py(瘦身)
          errors.py(改造) fallback.py(删除)
infra/    upstream.py(新) tasks.py(新) budget.py circuit.py persist.py(+视频)
ops/      logline.py(+3 字段) liveness.py shutdown.py
```

---

## 三、现有模块改造点清单（逐文件 diff）

| 文件 | 改造点 |
|---|---|
| `config.py` | **删** 5 个另一家上游变量；**加**：`UPSTREAM_BASE_URL`(默认 https://api.example.com，必填 Token `UPSTREAM_TOKEN`)、`VIDEO_DIR`(/var/lib/mcp-imagegen/videos)、`TIMEOUT_UPSTREAM_IMAGE_SEC`(30)、`VIDEO_SUBMIT_TIMEOUT_SEC`(30)、`VIDEO_POLL_INTERVAL_SEC`(5)、`VIDEO_QUERY_TIMEOUT_SEC`(10)、`PERSIST_TIMEOUT_IMAGE_SEC`(30，更名自 PERSISTTIMEOUTSEC)、`PERSIST_TIMEOUT_VIDEO_SEC`(120)、`TASK_TTL_SEC`(7200)、`TASK_MAX`(100)；**补** `HTTPX_CONNECT_TIMEOUT_SEC`(5，write/pool 同值——规格书补漏项)。自检不等式更新：read(10/30) ≤ 各调用档 ≤ deadline 200 < 210 < TimeoutStop 220，新增提交 30/单查 10 嵌套校验 |
| `engine/router.py` | MODELS = ("image",)；路由退化为**熔断门 + 直通**：显式 image → probe 标记直达（E2 绕过熔断）；auto → 查熔断，OPEN → 快失败 rate_limit（retry=yes，见 E24），CLOSED/HALF_OPEN → 直达 image。reason 枚举缩减为 explicit / bypass_circuit 语义改写 / auto |
| `engine/normalizer.py` | **删** steps/guidance 表、两套吸附档位表、CJK 检测；**留/改**：`parse_size`(WxH 格式校验，直传不吸附 E12)、seconds int→str（E21）、image 无 steps/guidance/seed → 不注入（"未知参数不臆测注入"纪律，见 Q1） |
| `engine/errors.py` | **删** 402 行；**改** 429 行文案（上游服务提供方 每分钟请求数限流，硬熔断 10min 沿用）；**加** `task_not_found`（retry=fixthenretry）、视频 failed 按原因映射（审核→no；5xx/超时→yes；其余按对应类别）、上游 401/403 注明 provider=上游服务提供方 |
| `engine/fallback.py` | **删除文件**。唯一模型无切换；`finalize_error` 的"余额双失败快失败"逻辑随 402 删除而消失，普通失败直接返回原错误 |
| `infra/circuit.py` | 代码零改动（本就按模型名记表）；使用侧模型集 = {image, video} |
| `infra/budget.py` | 零改动；视频侧复用同一 SlotPool（3 槽图/视频共享） |
| `infra/persist.py` | **加** `persist_video(url, deadline, req_id, headers)`：下载 read = min(`PERSIST_TIMEOUT_VIDEO_SEC`=120, 剩余)，落 `VIDEO_DIR`；`/files` 服务覆盖两目录（endpoints 白名单 + 视频扩展名 .mp4/.webm）；磁盘水位检查覆盖 images/+videos/ |
| `app/pipeline.py` | **删** 路由循环/切换/探针归还分支（唯一模型一次调用定生死）；**加** batch 拆分串行（image 不原生支持 batch）：n 次顺序调用，每次前检查 deadline 剩余 ≥ 单次档 30s，不足即止（E9 预算耗尽即止，非错误）；单次失败不中断后续；部分成功 → 成功 URL + meta `requested=N delivered=M`（M<N 时）；不 persist → meta 加 `url_expires=1h`（E13，上游服务提供方 未标注时效按短效对待） |
| `app/tool.py` | 三工具注册：`generate_image`（schema 视 Q1 裁定）、`generate_video`（prompt/seconds/aspect_ratio/seed?/persist）、`query_video`（task_id）；三工具 description **逐字符冻结**（新文本见六节，`test_tool.py` 全等断言） |
| `app/contract.py` | MetaInfo 扩展：`requested`/`delivered`（部分成功）、`url_expires`（短效警示）、`task_id`+`status`+`elapsed`（视频超预算非错误行）、`quality=not_applied`（若 Q1 保留 quality）；标记顺序在既有顺序基础上追加新标记于 persist 之后 |
| `app/endpoints.py` | `_SAFE_NAME` 白名单加 `(\.mp4|\.webm)$`；`make_files_endpoint` 接受双目录（按扩展名或双路由 `/files/` 同根挂载——实现取简单者：images/ 与 videos/ 均软链或子路径，**推荐**：`/files/img/<name>` 与 `/files/video/<name>` 分段，防同名冲突） |
| `ops/logline.py` | 字段集 12 → 15：+`task_id`（非视频请求显式 null）、+`poll_count`（轮询次数，生图 null）、+`split_calls`（拆分调用数，原生 batch=1、视频 null）；规格书八节的 `provider` 字段**建议省略**（单一 Provider 恒 上游服务提供方，零信息量，变更记录注明——如需保留则恒 "上游"，待 Ethan 定，不阻塞） |
| `server.py` | Components：`api_client: UpstreamClient`、+`task_table`、+`video_pipeline`；rebuild task 仅一个；lifespan 启动 `tasks.cleanup_loop()`；停机顺序：STOPPING=1 → 排空（含轮询中视频）→ aclose（上游、persist） |
| `deploy/` | systemd 单元参数不变（Type=notify/WatchdogSec=30/TimeoutStopSec=220/MemoryMax=512M）；`config.example.env` 按新变量集重写；磁盘看护 timer 覆盖 videos/；安装说明 + Agent 配置片段更新（三工具、超时建议 ≥210s 不变） |

---

## 四、37 项验收清单 ↔ 开发任务映射表

处置四类：**回归**（V1 已覆盖，跑既有测试）、**新开发**、**改写**（场景保留但语义换 上游服务提供方）、**删除**（另一家上游专属，N/A）。

| # | 验收项 | 处置 | 对应任务 | 验证方式 |
|---|---|---|---|---|
| ① | Inspector 连接/三工具 | 改写（三工具） | T8 | e2e：tools/list 三工具 + description 全等 |
| ② | 中文→kolors | **删除**（唯一模型直通） | T5 | 单测：任何语言 prompt 均落 image |
| ③ | 英文→schnell | **删除** | — | — |
| ④ | 显式 model 覆盖 | 视 Q1（保留 model 参数则改写为显式 image） | T8 | e2e |
| ⑤ | quality=high+英文→kolors | **删除**（→与㉘合并为 not_applied 验证，视 Q1） | T6 | 单测 |
| ⑥ | batch=4 返 4 URL | 改写：拆分串行返 4 URL | T6 | e2e（mock 4 次 200） |
| ⑦ | 真实 Agent 端到端 | 回归+扩展（三工具） | T10 | 真机 |
| ⑧ | 硅额度耗尽→跨厂商切 | **删除**（无跨厂商） | — | — |
| ⑨ | 429 后 10min 熔断 | 改写：image 429 → OPEN 期 auto 快失败（E24）→ 半开探针恢复 | T5+T6 | e2e + 日志 |
| ⑩ | 审核 400 仅 1 次调用 | 回归 | T6 | e2e |
| ⑪ | 探针失败熔断续期 | 回归（image 侧） | T5 | e2e |
| ⑫ | 错 MCP Token 401 | 回归 | — | e2e（V1 已绿） |
| ⑬ | 错上游 Token 401 | 改写：仅错 UPSTREAM_TOKEN，错误注明 provider | T2 | e2e |
| ⑭ | STOP 僵死→watchdog 重启 | 回归+扩展 | T9 | 真机 |
| ⑮ | 连崩 5 次风暴保护 | 回归 | — | 真机 |
| ⑯ | 更新期在途请求完成 | 回归+**新增子场景：轮询中视频** | T7 | 真机（journalctl 对账） |
| ⑰ | 24h CLOSE-WAIT≈0 | 改写：单客户端 | T2 | 真机 |
| ⑱ | py-spy 可用 | 回归 | — | 真机 |
| ⑲ | /healthz | 回归 | — | e2e |
| ⑳ | 慢死路径 ≤200s 截断 | 回归（生图）+**新增视频侧**（轮询挂起 mock） | T6/T7 | e2e |
| ㉑ | read 动态不误杀 | 改写：拆分串行第 N 次调用 read 仍 30s、总时长受 deadline | T6 | e2e |
| ㉒ | 连续 10 次异常后第 11 次得槽 | 回归+**新增视频路径**（轮询中异常/超预算后槽归还） | T7 | e2e |
| ㉓ | persist 下载失败降级 | 回归（图）+新增（视频） | T7 | e2e |
| ㉔ | /files URL 24h 有效 | 回归+扩展 videos/ | T9 | 真机 |
| ㉕ | 不 persist URL 短效 + meta 警示 | 改写：url_expires=1h 字段存在性 | T6 | e2e |
| ㉖ | 显式 image 生成成功 | 新开发（V1 无） | T6 | e2e |
| ㉗ | size 直传：默认 1024x1024 + 非法 400 附 message | 新开发 | T6 | e2e |
| ㉘ | quality 返 not_applied | 视 Q1（保留参数则新开发） | T6 | e2e |
| ㉙ | batch=2 拆分串行、部分成功 | 新开发 | T6 | e2e（1 成 1 败） |
| ㉚ | 双熔断→兜底落 上游 | **删除**（唯一模型） | — | — |
| ㉛ | 视频同步成功返 URL | 新开发 | T7 | e2e（mock 状态机） |
| ㊜ | 超预算返 task_id+running→query 拿 URL | 新开发 | T7 | e2e（调小 TOTALBUDGETSEC） |
| ㊝ | query 不存在 task_id→task_not_found | 新开发 | T7 | e2e |
| ㊞ | 视频失败分类、不切换、retry 语义 | 新开发 | T7 | e2e（审核/5xx 两态） |
| ㊟ | 视频 persist 大文件下载 | 新开发 | T7 | e2e（mock 慢流 120s 档） |
| ㊱ | TTL 过期清理生效 | 新开发 | T4 | 单测+e2e（调小 TASK_TTL_SEC） |
| ㊲ | GET /v1/models 核对 Kolors ID | **删除**（另一家上游专属；上游服务提供方 模型名按官方文档常量，附录 A-2 实测兜底） | — | — |

**净结果**：删除 6 项（②③⑤⑧㉚㊲），保留/改写 31 项；其中 V1 可回归 12 项、需新开发或改写 19 项、待真机 7 项（⑦⑭⑮⑯⑰⑱㉔）。另增 2 个规格书外新场景（非新增验收项，并入 ⑨/㉒）：E24 熔断期 auto 快失败、视频路径槽归还。

---

## 五、新增高风险点（R14 起）与防错手段

> 管理员原列"双客户端重建互不干扰"在纯 上游服务提供方 下 **N/A**（单客户端）。其余按新需求语境逐项落地：

| # | 风险 | 防错手段 |
|---|---|---|
| R14 | **视频轮询与信号量/deadline 交互**：轮询期持续占槽，3 个长视频可占满 3 槽 → 生图请求排队 30s 超限返 busy | 设计本意（一请求一槽含轮询期，3.4）；单测锁定"轮询期槽不提前释放"；e2e 验证并发混合负载下生图得 busy 契约行而非挂死；文档向 Agent 说明高峰预期 |
| R15 | **超预算/异常路径 finally 释放**：视频返 running、CancelledError、persist 异常等路径漏还槽 → 渐进泄漏伪装繁忙（V1 最高危根因在视频侧复发） | 槽获取唯一入口 `async with pool.slot(...)`，语法上无绕过路径；单测注入轮询中 CancelledError/超预算/persist 抛错三态，验证第 N+1 请求仍得槽（扩展㉒） |
| R16 | **任务表生命周期**：TTL 误清在途任务、容量逐出误逐新任务、清理协程用了线程 | 单测三分支：TTL 边界（2h±ε）、容量边界（第 101 条逐出 submitted_at 最旧而非最新）、cleanup 为事件循环协程（test_no_threads 静态门禁覆盖新文件）；逐出/过期统一走 task_not_found 契约 |
| R17 | **部分成功语义**：拆分串行中途失败/预算耗尽，返回体被误判为全成功或全失败 | 契约锁定：M<N 时 meta 必含 requested/delivered；M=0 时不产生"部分成功"（返最后一个错误的 [error] 行——**裁决**：全失败按错误路径返回，部分成功才走成功路径+计数）；e2e 三场景（1 成 1 败/全败/预算中途耗尽） |
| R18 | **唯一生图模型 + 熔断的可用性**：image OPEN（429 触发 10min）期间 auto 请求全部快失败 | E24 裁决锁定：auto 请求 OPEN 期快失败返 rate_limit（retry=yes，注明"熔断中，约 X 秒后重试"）；显式 image 仍绕过直达（E2 不变，用户自救通道）；7 天校准闭环观察 429 触发率（上游服务提供方 限每分钟请求数，10min 熔断是否过保守由数据裁决，本期不改规格书定案数值） |
| R19 | **watchdog 与视频轮询并存**：轮询协程异常方式阻塞（如 httpx 无超时挂起）→ 心跳假死误判 / 或真死 | 单查 read 显式 10s 动态构造（挂起有界）；轮询 sleep 用 asyncio.sleep（test_no_threads 门禁）；e2e 长视频场景断言心跳日志持续出现 |
| R20 | **拆分串行预算耗尽即止**：第 k 次调用前未查剩余 → 超预算后仍发起调用、或剩余充足却误停 | 每次调用前 `deadline.remaining() ≥ TIMEOUT_UPSTREAM_IMAGE_SEC` 才发起；单测边界（剩余 29s 停/31s 发）；停 = 部分成功（R17 契约），非错误 |
| R21 | **删码回归风险**：大面积删除另一家上游路径时误删共享逻辑（budget/circuit/persist/logline 与 Provider 无关） | 删码前跑全量 V1 基线（225 全绿）→ 按一节删除清单逐文件对账 → 删后全量回归 + `test_no_threads` 等公共门禁必须原样存活；e2e 快速档（--fast）作为冒烟 |
| R22 | **每日重建与视频轮询并存**：04:00 rotate 指针切换时轮询中请求持旧 client 引用 | 复用 V1 rotate 语义（旧 client 排空后 aclose，300s > 200s deadline 上限）；单测 mock 轮询进行中触发 rotate，断言在途查询正常完成 |

---

## 六、红线更新（对齐规格书"红线"精神，V2 增补）

1. **A1′ 三工具 description 逐字符冻结**。`generate_video` / `query_video` 沿用规格书原文（不含另一家上游措辞，零改动）；`generate_image` **必须改写**（原文提及 kolors/flux-schnell 已失效），新文本（Q1 推荐方案的措辞，实现与 `test_tool.py` 全等断言，任何优化冲动一律拒绝）：

   > Generate images from a text prompt using example-image-1. Supports batch generation (split into sequential calls server-side). NOTE: upstream image URLs are short-lived; set persist=true to get a durable self-hosted URL.

2. **E 裁决锁定**：E9–E23 沿用，其中 E1/E3/E15/E16 的双模型语境部分随 Provider 删除自然失效；**新增两条**：
   - **E24**：唯一生图模型熔断期，auto 请求快失败返 rate_limit（retry=yes），显式请求绕过直达（E2 延续）；
   - **E25**：image 请求体仅注入官方字段（model/prompt/size），quality/steps/guidance/seed 一律不注入（"未知参数不臆测注入"纪律的纯 上游服务提供方 表述）；seed 是否保留为 Agent 可见参数由 Q1 裁定。
3. **超时数值全配置化**（含规格书补漏项）：新增/更名变量全集见三节 config 行——`HTTPX_CONNECT_TIMEOUT_SEC`、`TIMEOUT_UPSTREAM_IMAGE_SEC`、`VIDEO_SUBMIT_TIMEOUT_SEC`、`VIDEO_POLL_INTERVAL_SEC`、`VIDEO_QUERY_TIMEOUT_SEC`、`PERSIST_TIMEOUT_IMAGE_SEC`、`PERSIST_TIMEOUT_VIDEO_SEC`、`TASK_TTL_SEC`、`TASK_MAX`，全部进 `config.py` 常量表 + `config.example.env` + 启动自检嵌套不等式；代码中**零裸数字超时**。
4. **既有红线延续**：A2（范围校验在 pipeline 不在 schema）、A4（retry 三值语义）、C4（重试纪律）、C6（persist 永不报错）、C7（/healthz 不参与僵死判据）、C8（熔断不落盘）、R1（finally 归还）、R2（禁线程）、R13（一行一请求一日志）——V1 已落地，回归守护。

---

## 七、开发任务拆解（T1–T10，单轮工作量可控）

> 拆解原则：每任务 = 一个内聚改动面 + 对应单测，单轮会话可完成；上轮"单任务过大超时"教训落地。依赖：T1 → T2 → T3/T4 → T5/T6 → T7(a→b→c) → T8 → T9 → T10；T5 与 T6 可并行。

| 任务 | 内容 | 涉及文件 | 单测 | 规模 |
|---|---|---|---|---|
| T1 | 配置层重构：删另一家上游变量、加 上游服务提供方/视频/任务表变量、自检不等式更新 | config.py, config.example.env | test_config 改造 | 小 |
| T2 | 上游服务提供方 客户端·基础与生图：httpx 池、四段超时、rotate、生图端点、双兼容解析 | infra/upstream.py | test_upstream.py | 中 |
| T3 | 上游服务提供方 客户端·视频两端点：提交（seconds str/mode/size/aspect_ratio）、查询 | infra/upstream.py | test_upstream.py 扩展 | 小 |
| T4 | 任务表：register/update/get/TTL 清理协程/容量逐出 | infra/tasks.py | test_tasks.py | 小 |
| T5 | 引擎瘦身：router 退化、normalizer 瘦身、fallback 删除、errors 改造（402 删/task_not_found 加/视频映射/E24） | engine/ 四文件 | 对应测试改造 | 中 |
| T6 | 生图流水线：单模型直调、batch 拆分串行、部分成功契约、url_expires、E24 快失败路径 | app/pipeline.py, contract.py | test_pipeline 扩展 | 中 |
| T7a | 视频流水线·主体：提交→轮询→完成/失败分类 | app/video_pipeline.py | test_video_pipeline.py | 中 |
| T7b | 视频流水线·边界：超预算返 running、异常路径槽归还、query_video 入口 | app/video_pipeline.py | test_video_pipeline.py 扩展 | 中 |
| T7c | 视频流水线·收尾：persist 120s、logline task_id/poll_count | persist.py, logline.py | 扩展 | 小 |
| T8 | 工具层：三工具注册、schema、description 冻结断言、endpoints 视频扩展名 | app/tool.py, endpoints.py | test_tool.py | 中 |
| T9 | 装配与部署：server.py Components、cleanup_loop 挂载、deploy 物更新 | server.py, deploy/ | 冒烟 | 小 |
| T10 | e2e 与验收：e2e 场景改造（删另一家上游/加视频 mock 状态机）、全量回归、验收报告 | acceptance/, tests/ | e2e 全量 | 中 |

**交付门槛**（对齐"好好测测再交付、零 bug"）：每任务合并前单测全绿；T10 出口 = 全量单测 + e2e 全量 PASS + 37→31 项映射表逐项结论 + 真机项操作清单（⑦⑭⑮⑯⑰⑱㉔）。

---

## 八、待确认（3 项：Q1/Q2 有推荐默认一句话否决即可，Q3 为阻塞项；均不阻塞 T1/T3/T4/T5 开工）

**Q1 · `generate_image` 参数面**（影响 schema/description/多处契约，需在 T6 前定）
image 官方请求体仅 model/prompt/size——model 只剩单值、quality 恒不生效、seed 不在上游字段中（规格书 2.1"另一家上游侧 ≤9999999999"的限定词印证 上游服务提供方 侧语义未定）。
- **推荐 A（彻底瘦身）**：仅保留 `prompt / size / batch_size / persist` 四参数，删除 model/quality/seed——接口零死参数、Agent 零误导（防伪需求：留着只会诱导 Agent 传无效参数）；
- 备选 B：保留五参数，quality 恒报 not_applied、model 仅 image/auto、seed 不注入（meta 注明 seed_not_supported）——兼容 V1 调用习惯，但接口臃肿。
推荐 A。连带：㉣⑤㉘ 等验收项按 A 语义改写。

**Q2 · 删除彻底性**：另一家上游代码/配置/单测**彻底删除**（推荐，零死代码、历史在 git），还是保留配置开关可回切（不推荐——回切概率极低，开关是永久维护税）。

**Q3 · 🔴 阻塞项：有效测试 Key**（阻塞 T2 真机联调与附录 A 全部实测项，不阻塞 T1/T3/T4/T5）
用户 10:23 提供的测试 Key 经芳首轮实测，在 .cn/.com 双节点均返回 401 Invalid token（证据与已确认事实见九节）。请用户检查该 Key 是否过期/属于其他环境/复制不完整，并提供有效 Key；拿到后 Ethan 即执行九节"Key 到位后 30 分钟首轮联调清单"。

---

## 九、首轮真机实测记录（芳执行，2026-10-10 10:30，5 次探测）

### 9.1 已确认事实（可直接入代码，不再臆测）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 双端点均可达且响应快：`api.example.com` 401 耗时 0.31s；`apihub.上游-ai.com` 401 耗时 0.87s | curl 实测 |
| F2 | **鉴权方案确认为 `Authorization: Bearer <key>`**：用 `x-api-key` / `api-key` 头 → 401 "Token not provided"（网关不读这两种头）；用 Bearer → 401 "Invalid token"（网关读取了但校验不过） | 三组对照请求 |
| F3 | **上游错误体格式确认**：`{"error":{"code":"","message":"…(含 request id)…","type":"上游服务提供方AI_error"}}`——errors.py 解析上游错误就按这个 schema 取 message，不必双兼容 | 401 响应体 |
| F4 | 请求 id 随错误体返回（`request id: 2026…`），可透传进日志 detail 便于对账上游工单 | 同上 |

### 9.2 阻塞项：测试 Key 无效

- 现象：同一 Key 在 .cn 与 .com 均 `HTTP 401 {"message":"Invalid token"}`（request id：…3998 82GU0TMyHZ / …65712543xyjYcI3Y，完整值见群内实测过程）。
- 结论：鉴权方式正确（F2 排除头格式问题），**Key 本身无效**——过期/撤销/非本环境 Key/复制截断四者之一。
- 影响面：A-2（响应顶层字段）、A-3（2K/4K 耗时）、A-4（size 值域）、A-5（视频完成耗时与状态枚举）四项实测回填，以及 T10 真机集成测试，全部挂起。

### 9.3 Key 到位后 30 分钟首轮联调清单（Ethan 执行，结果回填本节）

1. 生图默认档：`1024x1024` → 记录响应顶层字段（images or data）、耗时（校 30s 档）；
2. 生图 2K 档（如 `2048x2048`）→ 记录耗时（校 A-3，不够则调 `TIMEOUT_UPSTREAM_IMAGE_SEC`）；
3. 生图非法 size（如 `abc`）→ 记录 400 错误体（校㉗ message 透传）；
4. 视频提交 → 记录响应体字段名（video_id?）→ 间隔 5s 轮询至完成，记录状态枚举值与总耗时（校 A-5）；
5. 连发 3 次生图观察 429 行为与 Retry-After 头（校 E17 文案与熔断映射）。

### 9.4 测试凭据纪律（新增红线 B7）

- Key 仅存部署侧 `EnvironmentFile`（权限 600，systemd 加载），**不入代码仓库、不入本文档、不入任何日志**；`config.example.env` 只留占位符 `UPSTREAM_TOKEN=`；
- 日志/错误透传一律脱敏（errors.py 上游 message 原样透传，但 上游服务提供方 错误体不含 Key，安全；请求 id 可透传）；
- 群内分享过的 Key 视为已暴露的测试凭据，**上线前必须换正式 Key**。

---

下一步建议：用户优先处理 Q3（提供有效测试 Key，一句话即可），顺带确认 Q1（推荐 A）/Q2（推荐彻底删除）；Ethan 即按 T1→T10 开工——T1/T3/T4/T5 不依赖 Key 与 Q1 结论，可立即先行；Key 到位后按九节清单 30 分钟首轮联调，再进 T2/T6/T7 真机校准。
