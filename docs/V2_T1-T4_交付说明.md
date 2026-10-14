# V2 增量开发·第一轮（基座层 T1–T4）交付说明

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 轮次 | V2 第一轮（T1 配置层 / T2 上游服务提供方 客户端 / T3 视频两端点 / T4 任务表） |
| 依据 | 《MCP图片视频生成Server_增量设计方案V2.md》三节 T1–T4 |
| 基线对账 | 开工前 V1 全量 225 passed ✓（R21 基准） |
| 收口对账 | 全量 **337 passed**（= 225 存量原样全绿 + 112 新增） |
| 纪律 | 纯增量：另一家上游全部保留（删除留待引擎瘦身轮）；全部 mock，未调真实 API；凭据零落盘 |

---

## 一、单测统计（R21）

| 文件 | 数量 | 说明 |
|---|---|---|
| tests/test_upstream.py | **60** | 新增。三端点成功/4xx/429/5xx 重试/超时/read 动态/rotate 排空/E25/E21/F3/E14 |
| tests/test_tasks.py | **27** | 新增。R16 三分支：TTL 边界（含恰好等于）、容量逐出最旧（含未完成）、清理协程 |
| tests/test_config.py | **63**（+25） | V2 增量段 25 个：新变量默认值/env 覆盖/UPSTREAM_TOKEN 必填/旧名失效/8 条不等式违例/边界通过 |
| **新增合计** | **112** | 全量 225 → 337，全部通过（5.3s） |

存量测试改动（详见第四节"测试基建改动清单"）：仅必要同步，逻辑零改动。

## 二、变量最终命名表（T1）

### 新增（11 个，命名与方案 V2 三节逐字一致）

| 环境变量 | Config 字段 | 默认值 | 用途 |
|---|---|---|---|
| `UPSTREAM_BASE_URL` | 上游_base_url | https://api.example.com | 上游服务提供方 上游基地址 |
| `UPSTREAM_TOKEN` | 上游_token | **必填**（fail-fast，同 V1 双密钥纪律） | 上游鉴权 |
| `VIDEO_DIR` | video_dir | /var/lib/mcp-imagegen/videos | 视频落盘目录 |
| `TIMEOUT_UPSTREAM_IMAGE_SEC` | timeout_上游_image_sec | 30 | 生图 read 档 |
| `VIDEO_SUBMIT_TIMEOUT_SEC` | video_submit_timeout_sec | 30 | 视频提交 read 档 |
| `VIDEO_POLL_INTERVAL_SEC` | video_poll_interval_sec | 5 | 轮询间隔 |
| `VIDEO_QUERY_TIMEOUT_SEC` | video_query_timeout_sec | 10 | 视频单查 read 档 |
| `PERSIST_TIMEOUT_VIDEO_SEC` | persist_timeout_video_sec | 120 | 视频下载 read 档 |
| `TASK_TTL_SEC` | task_ttl_sec | 7200 | 任务表 TTL |
| `TASK_MAX` | task_max | 100 | 任务表容量 |
| `PERSIST_TIMEOUT_IMAGE_SEC` | persist_timeout_image_sec | 30 | **更名**自 `PERSISTTIMEOUTSEC`（纯改名，persist.py/config.example.env/install.md 引用已同步） |

### 与方案写法的出入（以现状一致性为准，共 1 处）

| 方案写法 | 最终执行 | 理由 |
|---|---|---|
| `HTTPX_CONNECT_TIMEOUT_SEC`(5) | **复用既有 `HTTPXCONNECTSEC`（默认 5）** | V1 已将 connect/write/pool 三段全部配置化（各 5s，可独立调整），再增设别名即同一超时双头控制。UpstreamClient 四段超时全显式使用 `cfg.httpx_connect_sec / httpx_write_sec / httpx_pool_sec`。 |

### 新增自检不等式（fail-fast）

- `TIMEOUT_UPSTREAM_IMAGE_SEC ≤ TOTALBUDGETSEC`（生图档 ≤ deadline）
- `VIDEO_SUBMIT_TIMEOUT_SEC ≤ TOTALBUDGETSEC`（提交档 ≤ deadline）
- `VIDEO_QUERY_TIMEOUT_SEC ≤ VIDEO_SUBMIT_TIMEOUT_SEC`（**单查 10 ≤ 提交 30 嵌套校验**；传递性亦覆盖单查 ≤ deadline，另有显式一条冗余防御）
- `VIDEO_POLL_INTERVAL_SEC > 0`；`PERSIST_TIMEOUT_VIDEO_SEC ≤ TOTALBUDGETSEC`
- `TASK_TTL_SEC > 0`；`TASK_MAX ≥ 1`
- 自检报告新增第三行（三 read 档 + 嵌套链 + 任务表参数），已有测试锁定输出

## 三、新增模块说明

### 1. infra/upstream.py（T2 + T3，约 530 行）

`UpstreamClient(cfg, transport=None, client_factory=None, sleep=asyncio.sleep, drain_poll_sec=…)`——模式与 V1 另一家上游Client 完全对齐（独立实现，不 import 另一家上游，为瘦身轮删文件解耦）。

- **生图** `generate_image(model, prompt, size, deadline)` → POST `/v1/images/generations`
  - 请求体**仅** `{model, prompt, size}`（E25：quality/steps/guidance/seed/batch_size 一律不注入，测试以严格相等断言锁定）
  - 模型别名 `image → example-image-1`（查表，自由文本 model 拒绝）
  - 响应 images/data 双兼容（E14）
- **提交** `submit_video(model, prompt, seconds, aspect_ratio, deadline)` → POST `/v1/videos`
  - `{model: example-video-1, prompt, seconds: str(int)（E21）, mode: "text", size: "720P", aspect_ratio}`
- **单查** `query_video(video_id, model, deadline)` → GET `/v1/videos/query?video_id=…&model_name=example-video-1`
  - 返回 `video_status`（running/completed/failed）与 `video_url`（completed 时）
- **httpx 纪律（R7）**：Limits 10/4；四段超时全显式（connect/write/pool=5s 配置派生；read=min(档, deadline 剩余) 每次尝试重算：生图 30/提交 30/单查 10）
- **重试（C4）**：仅 5xx/网络/超时退避重试 1 次（无 Retry-After 时 0.25–1s 全抖动；Retry-After 尊重封顶 60s——对齐 V1 语义；退避计入 deadline）；429/4xx 零重试
- **rotate/aclose（R8/E6）**：每日重建热切换 + 300s 排空（> 200s deadline）
- **错误体（F3）**：`{"error":{code,message,type}}` → error_detail 取 error.message（含请求 id）；解析失败回退响应体前 200 字符
- **A-5 防御性兼容**（待实测回填）：提交响应 video_id、查询响应 status/url 均为顶层优先 + `data` 容器兜底

### 2. infra/tasks.py（T4，约 130 行）

`TaskTable(ttl_sec=7200.0, max_entries=100, clock=time.monotonic)`

- `register / update / get`：TTL 过期与容量逐出统一表现为未命中（R16：调用方 T7 统一翻译 task_not_found）
- **TTL 语义（锁死）**：`elapsed ≥ TTL` 即过期（含恰好等于，宁早逐出；测试锁定 7200±ε 边界）
- **容量逐出**：超 100 按 submitted_at 逐出最旧（**含未完成**——上游任务不受影响；同刻并列按插入序稳定逐出）
- `cleanup_loop(interval_sec=60)`：事件循环协程（asyncio.sleep 驱动），可 cancel；`cleanup()` 单次清理返回条数
- 无锁：单事件循环串行访问（R2 禁线程 → 无竞争面；静态门禁已自动覆盖本文件）

## 四、测试基建改动清单（除新增文件外的全部测试改动）

| 文件 | 改动 | 性质 |
|---|---|---|
| tests/conftest.py | make_config 默认 env 补 `UPSTREAM_TOKEN="test-上游-token"` | 任务授权的唯一基建改动 |
| tests/test_config.py | ① VALID_ENV 补 UPSTREAM_TOKEN（必填密钥所致，否则全文件 fail-fast）② `PERSISTTIMEOUTSEC→PERSIST_TIMEOUT_IMAGE_SEC` 两处引用同步（改名波及）③ os_environ 回退测试补 UPSTREAM_TOKEN ④ 尾部追加 V2 增量段 25 个测试 | ①②③ 为改名/必填的必要同步（逻辑零改动），④ 为 T1 单测交付物 |

## 五、遗留与风险

1. **A-5 挂起**（方案附录）：视频提交/查询响应字段名未经真机实测——当前按任务指定字段（video_id / status / url）+ data 容器双兼容，字段名出入时仅需调整 `_extract_video_id / _extract_video_fields` 两函数。
2. 429 → 熔断映射（E17/T13）不在本轮范围，UpstreamClient 现仅返回 retry_after 供上层使用。
3. 另一家上游全部保留（含 conftest 另一家上游 token 占位）——删除与 test_另一家上游 同批在引擎瘦身轮执行。
4. server.py 未接 UpstreamClient（T9 装配轮）；启动现已要求 UPSTREAM_TOKEN（缺省 fail-fast，属预期）。

---

下一步建议：进入 T5（query_video 工具语义层）/T7（视频流水线）与 T9（装配）设计确认；A-5 真机联调清单待有效 Key 后按方案 9.3 执行。
