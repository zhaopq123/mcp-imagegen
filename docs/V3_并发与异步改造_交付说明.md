# V3 交付说明：多队列并发 + 视频后台异步 + 上游限速

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](../README.md) 为准。
日期：2026-10-13　范围：`mcp-imagegen`（vivo 生产 192.168.3.35:8000，已上线）
提交：`4504e48`（vivo git bare `~/git/mcp-imagegen.git`，分支 main）

---

## 一、为什么改：V2 的两个真实痛点

**痛点 1：视频把服务堵死。** V2 的视频请求在**并发槽内**轮询到 deadline
（原 R14：轮询期持续占槽），而槽池只有 3 个且生图共用。结果：3 个视频请求
就能占满全部槽位并各堵 200 秒，期间所有生图请求一律 `busy`。
实测（V2 生产）：并发压测中生图请求大量返回"并发繁忙"，视频请求占满整个窗口。

**痛点 2：批量生成是串行的。** `generate_image` 的 `batch_size` 由服务端
拆成 N 次**顺序**调用（V2 注释原文"拆分串行"），N 张图的耗时是 N 倍。

**附带发现：** 客户端（Agent）超时断开或 deadline 耗尽时，视频轮询立即终止，
上游任务还在跑但本端已放弃跟踪——异步化能一并解决。

---

## 二、改了什么

### 1. 多队列泳道（`infra/budget.py`）

新增 `QueueLane` 与 `MultiQueuePool`；原 `SlotPool` 保留（单测与兼容路径继续用）。

```
image 泳道：并发 4 / 排队等待 30s / 队列深度上限 48
video 泳道：并发 3 / 排队等待 25s / 队列深度上限 16
```

- 两条泳道额度独立 → 视频长任务不再饿死生图。
- 排队深度超限立即 `busy` 快失败（`queue_full_error`），不让请求排到撞 deadline。
- 槽仍在 `async with` 语义下归还（R1）；全程无线程（R2 铁律，门禁自动覆盖新文件）。

### 2. batch 并发生成（`app/pipeline.py`）

`batch_size` 拆出的 N 次调用改为 `asyncio.gather` 并发，并发度 = `min(batch_size, IMAGE_BATCH_CONCURRENCY)`。

- 保序回收：URL 顺序仍等于发起顺序。
- 部分成功契约（R17 `requested/delivered`）与熔断记账逻辑不变。
- 并发闸门是必需的：否则 `并发槽 × batch` 会直接超过上游连接池。

### 3. 视频后台异步（`app/video_pipeline.py` + `infra/tasks.py`）

```
提交（占槽，秒级 IO）→ 登记后台作业 → 立即释放槽
   → 请求侧在自身剩余预算内 await 作业 future（不占槽）
   → 命中终态返 URL+meta；预算耗尽按 E17 返 task_id（非错误）
后台 worker：独立预算 1800s，轮询到终态，persist=true 时后台自动落盘
```

- **对外契约零变化**：三个工具的 description/schema 逐字未动（冻结门禁
  `tests/test_tool.py` 全等断言仍然通过）。Agent 侧用法与之前一致。
- `R14`（轮询期占槽）由此作废——等待不是 IO，不该占并发额度。
- `JobTable` 继承 `TaskTable`：复用 TTL / 容量逐出 / 周期清理，只增加
  future 通知与结果载荷，不留双头实现。
- `query_video` 有活跃 worker 或已是终态时走**本地实况**（零上游 IO）；
  只有无跟踪记录时才实时打上游（兼容旧表场景）。
- 停机 `cancel_background()`：只停本端跟踪，上游任务照常跑，等待方转 E17。
- 后台作业数达 `VIDEO_JOB_MAX` 时逐出最早的（避免手机内存被跟踪表吃干）。

### 4. 上游限速（`infra/ratelimit.py`，本轮压测逼出来的）

真实链路压测发现：**并发一放开就撞 上游服务提供方 免费额度 429 → 硬熔断 600s**，
比排队等待糟糕得多（撞一次就废十分钟）。于是加客户端侧令牌桶：

| 参数 | 默认 | 生产值 | 含义 |
|---|---|---|---|
| `UPSTREAM_RPS` | 1.0 | 1.0 | 上游请求匀速上限（令牌/秒） |
| `UPSTREAM_BURST` | 2 | 2 | 允许的短突发（桶容量） |
| `UPSTREAM_COOLDOWN_SEC` | 15 | 15 | 收到 429/503 后的冻结时长 |

等待计入 deadline（R3 同纪律）；冻结窗口内请求排队而不是硬闯。

### 5. 运维面

- `GET /healthz` 增加 `queues`（每条泳道的 size/in_use/waiting/max_queued）与 `video_jobs`。
- 新增第 4 个工具 `list_video_tasks(limit=20)`：一次看全部在跟踪的视频作业
  （task_id/status/耗时/URL/persist），批量异步提交后不必逐个轮询。
  三个冻结工具零改动。

### 6. 新增配置（`/etc/mcp-imagegen/env`，旧 env 已备份为 `env.bak-20261013`）

```
IMAGE_MAX_CONCURRENCY=4     VIDEO_MAX_CONCURRENCY=3
IMAGE_QUEUE_WAIT_SEC=30     VIDEO_QUEUE_WAIT_SEC=25
IMAGE_QUEUE_MAX=48          VIDEO_QUEUE_MAX=16
IMAGE_BATCH_CONCURRENCY=4   VIDEO_JOB_MAX_WAIT_SEC=1800
VIDEO_JOB_MAX=24            VIDEO_JOB_CONCURRENCY=8
UPSTREAM_RPS=1.0  UPSTREAM_BURST=2  UPSTREAM_COOLDOWN_SEC=15
HTTPXMAXCONNECTIONS=24  HTTPXMAXKEEPALIVECONNECTIONS=8   # 随并发上调
```

全部进入启动自检（嵌套不等式 + 正数校验），配错直接 fail-fast 拒绝启动。

---

## 三、测试

`447 passed`（改造前基线 424，新增 23）

- `tests/test_v3_async.py`（16）：泳道隔离、排队深度快失败、batch 并发峰值/保序/
  部分成功/全失败、等待不占槽、请求取消后后台跑完、作业表与总览、后台取消、
  错误分类透传、配置边界。
- `tests/test_ratelimit.py`（7）：令牌桶突发、补充等待、惩罚窗口、Retry-After、
  等待超时、无线程。
- 更新因行为变更失效的既有用例（占槽断言、轮数断言、落盘位置、日志字段），
  每处都标注了 V3 变更理由。

改造过程中真抓到的自身缺陷（都已修并加测试）：
1. 提交返回值与错误契约行同为 `str`，出口判定混淆 → 改二元组；
2. 兼容路径登记的作业没建 future，等待方"等了个空气"直接转 E17；
3. 兼容路径只更新表不更新手上的条目副本 → 终态永远读成 running；
4. E17 的"已等待"报了后台作业预算（1800s）而非请求预算；
5. `asyncio.wait_for` 走真实时钟、deadline 走注入时钟 → 请求被后台作业拖满；
6. 作业完成后 worker 退出，查询退化成打上游（本地终态未命中）；
7. 异步化把 429/400 等具体类别压成 upstream_error（分类信息丢失）。

---

## 四、压测（本地 mock 上游，不消耗真实额度）

负载：8 个 MCP 会话 × 3 次生图（batch=2） + 3 个视频 = 27 个请求。

| 指标 | V2 串行基线（并发=1） | V3 并发配置 | 变化 |
|---|---|---|---|
| 总耗时 | 39.7s | 19.5s | **−51%** |
| 吞吐 | 0.68 req/s | 1.38 req/s | **2.03×** |
| 延迟 P50 | 11.3s | 1.6s | **7.1×** |
| 延迟 P95 | 19.8s | 18.5s | −7% |
| 成功 / busy / 错误 | 27 / 0 / 0 | 27 / 0 / 0 | 一致 |

视频异步链路实测：3 个作业全部 `completed` + `persist=persisted`，
`list_video_tasks` 正确回读，后台独立跑完（客户端断开不影响）。

压测基础设施（本机 `~/乱七八糟/`）：
- `stress_mcp.py`：多会话并发压测器（`--base/--token/--sessions/--images/--batch/--videos`）。
- `mock_upstream.py`：mock 上游（上传到 vivo `~/mock_upstream.py`）。
- vivo `/opt/mcp-imagegen/start-stress.sh`：压测专用实例（8002 端口，
  `CIRCUIT*` 调低以缩短熔断窗口）。跑法：
  ```bash
  tmux new-session -d -s mcp-mock "proot-distro login ubuntu -- bash -c \
    'cd ~/ && /opt/mcp-imagegen/venv/bin/python -m uvicorn mock_upstream:app --workers 2 --port 8001'"
  tmux new-session -d -s mcp-stress "proot-distro login ubuntu -- /opt/mcp-imagegen/start-stress.sh"
  python stress_mcp.py --base http://192.168.3.35:8002/mcp --token stresstoken \
      --sessions 8 --images 3 --batch 2 --videos 3
  ```
  ⚠ 后台进程必须挂 tmux（ssh 会话结束会被回收，proot 下的 `nohup` 活不下来）。

---

## 五、已知限制（别误判成 bug）

1. **真实上游免费额度有限。** 6 会话 × batch2 的真实压测会撞 429（额度限制，
   不是并发 bug），随后 600s 硬熔断。已用限速器缓解，但根治要升级 Token Plan。
   撞限额后的表现：熔断期内一律 `rate_limit / retry=yes`，`retry_after` 可见。
2. **熔断参数仍是规格书值**（硬 600s / 软 300s）。免费额度型 429 用 10 分钟
   硬熔断偏重，但改它要动规格书红线，本次没动。
3. 任务表在内存，重启丢失（沿用 E17/R16 契约，表现为 `task_not_found`）。
4. 手机资源（内存 3.7G / ARM）决定了默认并发保守值；要更高吞吐先升上游额度。

---

## 六、上线状态

- 生产 `/opt/mcp-imagegen` 已同步 V3 代码并重启，`/healthz` 可见双泳道。
- 真实链路冒烟：2 张图并发生成成功（各 9.1s），视频链路此前已验证。
- 代码已提交并推送：`4504e48`（`~/git/mcp-imagegen.git`，main）。
- WorkBuddy 侧无需改动配置；下次调用会重新拉取工具列表（新增 `list_video_tasks`）。