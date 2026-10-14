# 真机联调记录（V2 第六轮：端点修正 + 真机联调）

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 日期 | 2026-10-10 14:20–15:10（Asia/Shanghai） |
| 上游端点 | `https://api.example.com`（本轮已修正，原 `apihub` 子域废弃） |
| 执行脚本 | `scripts/live_check.py`（可重复执行；结果逐 run 追加 `scripts/live_check_results.json`） |
| 密钥纪律 | UPSTREAM_TOKEN 仅经 `/home/z/envs/mcp-imagegen.env` 注入进程环境；**零落盘**（不入仓库/git/日志/结果 JSON） |
| 基线 | 修正后全量单测 366 passed（见 §8） |

> 说明：所有「[meta] / [error]」标记前缀在本文引用时可能被渲染层吞掉首字符（`[m`→``，已知现象，V2 第二轮交付说明已实证）；字符串本身经 ord 码逐位验证完好（`[91,109,101,116,97,93,32]` = `[meta] `）。

---

## 1. 端点修正说明

官方文档（wiki.example.com）检查清单明确：`使用 https://api.example.com/v1/images/generations 作为 API 端点`。原默认值 `api.example.com` 为旧子域，本轮全量修正：

| 文件 | 位置 | 修正 |
|---|---|---|
| `config.py` | L21–22 变量表注释 + L82 `UPSTREAM_BASE_URL` 默认值 | `https://api.example.com` → `https://api.example.com`（附注：官方文档端点，原 apihub 子域已废弃） |
| `config.example.env` | L41 | 同上 |
| `tests/test_upstream.py` | L24 `BASE` | 同上（全部 mock 基址跟随） |
| `tests/test_config.py` | L194 `V2_DEFAULTS` | 同上（默认值断言跟随） |
| `deploy/install.md` | §0 前提条件 / §5.1 变量表 / §13 检查清单 ② | 4 处 `api.example.com` → `api.example.com`（root 只读文件，按临时文件+rename 铁律落盘） |
| `deploy/README.md` | 项目简介 | 1 处 |

历史文档（`MCP图片视频生成Server_增量设计方案V2.md`、`docs/V2_T1-T4_交付说明.md`）中的旧端点为当时事实记录，按历史文档纪律不改。修正后 `rg apihub` 在代码/配置/测试层零残留（仅历史文档与修正注释）。

## 2. 生图三档实测（派单 ①）

直连层 = 裸 httpx 打真实上游（read 上限 360s，计真实耗时）；项目链路 = 完整 MCP 协议（initialize → tools/call `generate_image`，真实 uvicorn + 真实上游）。

### 2.1 默认档 1024x1024

| 层 | 耗时 | 结果 |
|---|---|---|
| 直连 | **7.4s** | 200，顶层键 `["created","data","task_id"]`，1 item（`b64_json`/`revised_prompt`/`url`），URL 域名 `cos-platform-outputs.example.com` |
| 项目链路 | **10.1s** | 契约行：URL 首行 + `[meta] model=image size=1024x1024 url_expires=1h`（ord 级验证前缀完好），read 档 `TIMEOUT_UPSTREAM_IMAGE_SEC=30` 内完成 |

### 2.2 2K 档（官方推荐 `size:"2K"` + `ratio:"16:9"`）

| 层 | 耗时 | 结果 |
|---|---|---|
| 直连（ratio 直注入体） | **13.4s** | 200，结构同上；官方推荐组合有效 |

> 项目链路本轮不注入 `ratio`（派单纪律：参数表面待芳定案后下一轮统一实施），故 2K+ratio 仅直连层验证。

### 2.3 非法 size（400 message 透传）

| 输入 | 层 | 结果 |
|---|---|---|
| `size="banana"` | 直连 | **400**，6.2s，错误体 `{"error":{"code":"invalid_request","message":"size 的值 \"banana\" 无效，必须是 1K、2K、3K、4K 或 WIDTHxHEIGHT (request id: …)","type":"invalid_request"}}` |
| `size="99999x99999"`（本地格式合法，直传上游） | 项目链路 | **400**，0.26s，契约行 `[error] category=content_policy retry=no reason=内容审核未通过(400) | size "99999x99999" 超过最大 4096x4096 (request id: …) suggestion=修改 prompt 后重试` |

**值域实测结论（附录 A-4 回填素材）**：
- 合法 size = 档位 `1K / 2K / 3K / 4K`（可配 `ratio`）或 `WIDTHxHEIGHT`（直传，无需 ratio）；
- WxH 上限 **4096x4096**（超限即 400，message 透传含 request id）；
- 项目 size 直传不吸附策略（normalizer 仅 WxH 格式校验）与真实值域**兼容**；
- 偶发传输异常一次：首轮项目链路非法档出现 `RemoteProtocolError: peer closed connection (incomplete chunked read)`，与上游 429 限流窗口时间重叠；单独复跑 2 次均正常（0.19s/0.26s 返回），判定为上游瞬时断流，非项目缺陷（e2e mock 层无法复现，真机层已 2/3 稳定）。

### 2.4 耗时 vs 30s 档评估

实测单发生图 7.4–15.2s（多轮观察区间，含并发拥塞时的慢响应）；503 快速拒绝（0.3–0.5s）不占用 read 档。**当前免费队列下 30s 档够用**（最大观察值 15.2s ≈ 档位 51%）。官方建议 60–360s 为保守上限；若正式 Key/付费计划下出现慢响应，`TIMEOUT_UPSTREAM_IMAGE_SEC` 上调建议（→60s）留待参数面定案统一实施。

## 3. persist 真机（派单 ②）

`generate_image persist=true`（1024x1024）：

| 检查项 | 结果 |
|---|---|
| 总耗时 | 8.4s（生成+下载落盘） |
| 契约行 | URL 首行 `http://127.0.0.1:{port}/files/a816da9f1ab0-01-89ff700c.png` + `[meta] model=image size=1024x1024 persisted` |
| GET /files | **200**，`image/png`，**1,290,765 字节** |
| 字节一致性 | `bytes_match_disk=true`（HTTP GET 响应体 == 磁盘文件逐字节相等） |
| 磁盘 | `IMAGE_DIR`（临时目录）恰 1 文件，文件名含 req_id+序号+hash 前缀 |

## 4. 视频全流程（派单 ③）

### 4.1 队列满观察（503 video_queue_full）

上游 `POST /v1/videos` 连续返回 **503** `{"code":"video_queue_full","message":"视频队列已满，请稍后重试 (request id: …)"}`：

- 服务内 **C4 纪律真机走通**：`[上游] POST /v1/videos 第 1 次上游 503 → 退避重试`（日志可见，退避后仍满则放弃，单次调用总耗时 1.24s 含退避）；
- 项目契约行：`[error] category=upstream_error retry=yes reason=上游服务错误(503) | {"code":"video_queue_full",…} suggestion=稍后重试`——错误体 message 透传 + retry=yes 引导稍后重试；
- 外层观察：脚本按 20s 间隔重提 6 轮（102.9s 观察窗），队列持续满未恢复（14:30–14:43 三个观察窗）；**不阻塞其余项**（生图/persist/429 各阶段在该期间全部独立完成）。

### 4.2 提交成功 → 轮询 → completed → query 链路

（待本轮末次运行结果回填：队列恢复后提交成功，记录提交耗时/轮询次数/总耗时/顶层 url 验证/query 续查链路。）

## 5. 429 行为（派单 ④）

**触发方式**：并发 12 连发 `generate_image`（gather 同时打满）。

| 观察 | 结果 |
|---|---|
| 触发阈值 | 两轮各 12 并发，第 11、12 发（/ 第 11 发）返回 429——**免费限速 ≈ 10 个并发/短窗**；串行 10 连发（每发 8–15s 节奏）不触发 |
| 响应速度 | 0.19–0.38s 快速拒绝 |
| **Retry-After 头** | **无**（两轮 429 响应均未携带；`ratelimit_headers` 亦空——无任何 rate/limit 类响应头） |
| 错误体 | `{"error":{"message":"您已达到免费用户的 API 速率限制。升级 Token Plan 即可解锁更高限额，继续不间断使用 API。 (request id: …)","type":"上游服务提供方AI_error","param":"","code":"rate_limit_exceeded"}}`（F3 结构，message 含 request id） |
| 项目链路真机行为 | 契约行 `[error] category=rate_limit retry=yes reason=上游限流(429) | 您已达到免费用户的 API 速率限制… suggestion=稍后重试`（真机自然触发 2 次：svc_image_bad 重跑、svc_video 第 6 轮提交） |
| 限流窗口 | 短：同一 Key 约 60–100s 后恢复（429 后 ~90s 的 persist 阶段已恢复正常） |

**纪律校验**：项目 C4「429 零重试」与熔断 HARD（600s）在真机 429 下正确执行（429 即时返回契约行，无重试）；上游不提供 Retry-After，退避节奏只能由客户端自定（当前实现符合设计）。

## 6. 响应解析验证（派单 ⑤）

真实生图响应顶层结构（多轮一致）：

```json
{
  "created": …,
  "task_id": "…",
  "data": [
    {"b64_json": "…", "revised_prompt": "…", "url": "https://cos-platform-outputs.example.com/images/t2i/task_…/output_….png"}
  ]
}
```

- 顶层为 **`data` 数组**（非 `images`）——`infra/upstream.py _extract_urls` 的 E14 双兼容 `images or data` **命中 data 分支**，真机全链走通（§2 各阶段成功返回 URL 即证）；
- 视频查询响应结构：`GET /v1/videos/query?video_id=…&model_name=example-video-1`（顶层 `status`/`url` 字段验证见 §4.2）。

## 7. 结论与下一轮输入

1. **端点修正完成且全量绿**：默认值/样例/测试/部署文档六文件修正，366 单测 passed，真机全链连通（生图 200 / 视频端点可达）。
2. **30s 档评估**：免费队列下单发 7–15s，30s 档当前够用；上调 60s 的建议留参数面定案（与芳对齐后统一实施）。
3. **限速事实**：免费 Key ≈10 并发/短窗、429 无 Retry-After、窗口 ~1 分钟——Agent 侧并发建议 ≤8（`MAXCONCURRENCY=3` 天然低于阈值）。
4. **size 值域**（A-4 回填素材）：`1K/2K/3K/4K (+ratio)` 或 `WxH ≤ 4096x4096`；当前直传策略兼容。
5. **视频队列**：免费队列高峰期持续满（503），C4 单次退避重试不足以穿越，属上游容量问题；建议部署侧保留「retry=yes + 稍后再试」契约引导，不做更长阻塞重试。

## 8. 修正后全量单测

```
366 passed in 7.11s（端点修正后；修正前基线同为 366 passed）
```

## 9. run 索引（scripts/live_check_results.json）

| run_id | 阶段 |
|---|---|
| 5252e1071599 | raw_img_default / raw_img_bad |
| 2b7dc9a15e84 | raw_img_2k / svc_image / svc_image_bad（偶发断连记录） |
| d1e46475fc8e | svc_image_bad（重跑：真机 429 契约） |
| 9bbc3bd64102 | svc_persist |
| 2a6fbe2f28f6 | svc_video（首轮：503 队列满） |
| f6a711df6bb6 | svc_video（6 轮重提观察：队列持续满+429 限流） |
| adbbd679ea09 | raw_429 串行 10 发（未触发 429） |
| 08ea902e6735 | raw_429 并发 12 发（触发 429 + 5×503） |
| e64527183509 | raw_429 并发 12 发复测（2×429，Retry-After 无头确认） |
| （末次） | svc_video（队列恢复后完整流程，回填 §4.2） |

---

## 10. 第八节 · V2 最终验收轮补测（芳，2026-10-10 15:40–16:00，追加不改旧数据）

> 管理员 15:39 派发最终验收，真机补测三项：A-8 视频全流程、D2–D7 新参数抽测、结果回填本记录。执行脚本 `scripts/live_check_v2_acceptance.py`（本轮新增，走完整 MCP 链路）+ 直连 curl + 后台轮询。

### 10.1 D2–D7 新参数抽测（MCP 全链路 + 直连）

| 项 | 结果 | 证据 |
|---|---|---|
| D2 ratio 生图 | ✅ | `generate_image(size="2K", ratio="16:9")` 走 MCP 全链路 11.7s 成功；meta `model=image size=2K ratio=16:9 url_expires=1h`；URL 真实落 COS（run v2_accept.case.A） |
| D4 边界拒绝 | ✅ | `aspect_ratio="5:4"` → `[error] category=invalid_params retry=fixthenretry … 限 16:9/9:16/1:1/4:3/3:4/21:9 六档，收到 '5:4'`（零配额消耗） |
| D3 seed + D4 六档 | ✅ 完整闭环 | `seed=42, aspect_ratio=21:9, seconds="4"` 提交 200 → 全程跑至 completed 出片（见 10.2，上游成功路径实证）；服务端 schema→pipeline→上游 请求体链路由单测+e2e mock 覆盖（第七轮新增 seed 校验用例）。注：MCP 链路 seed 成功路径当轮撞 503/429 未单跑，透传链路证据以单测+直连互补 |
| 503/429 契约 | ✅ | 队列高峰期 6 次提交尝试全返回结构化契约行：503→`upstream_error retry=yes`、429→`rate_limit retry=yes`，官方 message + request id 完整透传（run 940f34fed195） |

### 10.2 A-8 视频全流程闭环（15:44–15:56）

| 环节 | 实测 |
|---|---|
| 提交 | POST /v1/videos（seed=42, 21:9, "4"s）→ 200（6.1s）；**响应含 id/video_id/task_id 三字段同值**（字段名疑云定案：任取其一皆可） |
| 状态机 | queued → **in_progress**（progress=10，官方文档未列的中间态实证）→ completed（progress=100）；总耗时约 8 分钟（免费队列渲染慢，非服务端问题） |
| 查询 | GET /v1/videos/query?video_id&model_name=example-video-1 → 200 顶层任务对象（无 data 容器，与实现"顶层优先"解析匹配） |
| 产物 | `https://cos-platform-outputs.example.com/videos/video-2.5/task_qyGR11scbwDsKmqzo3hy1LyPvlH8yPr5.mp4` HEAD 实测 **HTTP 200 · video/mp4 · 1,871,079 B** |

### 10.3 e2e 全量档复核（验收轮）

- 全量 27 场景首跑 26/27：㉑ FAIL 定性为**测试断言未随 D5 同步**（场景假设 30s 档，默认已调 60s；上游挂 31s 在 60s read 窗口内首次即成功 calls=1，行为本身正确），非实现 bug；
- 修复：场景显式注入 `TIMEOUT_UPSTREAM_IMAGE_SEC=20` + 挂 21s，回归"read 档先响 + C4 重试 1 次"语义（不再耦合全局默认）；
- 复跑：**PASS**（20.8s / calls=2 / 重试成功）；实现代码零改动。
