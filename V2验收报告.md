# MCP 图片/视频生成 Server · V2 验收报告（纯 Agnes 双工具版）

| 项 | 内容 |
|---|---|
| 验收对象 | mcp-imagegen V2（单 Provider Agnes · 三工具：generate_image / generate_video / query_video） |
| 验收轮次 | V2 第八轮（最终验收） |
| 验收日期 | 2026-10-10 |
| 验收人 | 芳（产品验收人，管理员 15:39 派发） |
| 上位依据 | 《技术规格书》最终交付版 → 《增量设计方案 V2》（需求变更：删硅基）→ 《官方文档对齐 Delta 定案》（D1–D8） |
| 执行环境 | 本机沙箱：真实 uvicorn + 真实 MCP streamable-http 协议（initialize → tools/list → tools/call）；e2e 上游为注入式 mock；真机阶段打真实 api.agnes-ai.cn |
| 总体结论 | **可交付**。31 项处置后：23 项 PASS（单测/e2e/真机证据齐，含 A-8 视频全流程真机闭环）＋1 项待用户侧（⑦ 真实 Agent 接入）＋7 项待部署真机（需 systemd/Caddy 环境，与 V1 同口径遗留）＋3 项 N/A（Q1=A 删参）。发现 1 项非阻塞缺陷（e2e 断言过时），验收轮当场修复并复跑 PASS。 |

**复跑命令**：

```bash
cd mcp-imagegen
python -m pytest -q                        # 单测：424 passed
python server.py --check                   # 配置自检：exit=0
python acceptance/e2e_acceptance.py        # e2e 全量：27/27（含 ⑳ 慢死路径 181s；㉑ 已按 D5 口径修复）
python acceptance/e2e_acceptance.py --fast # 快速档：25 场景约 90s
python scripts/live_check.py --only svc_video   # 真机视频（上游队列满时观察 503/429 契约）
```

---

## 一、验证基线（全部现场复跑，非转抄）

| 验证项 | 结果 | 备注 |
|---|---|---|
| 全量单测 | **424 passed, 0 failed**（7.1s） | 分文件：agnes 60 / normalizer 58 / config 54 / tool 41 / video_pipeline 38 / files_video 29 / tasks 27 / pipeline 27 / errors 25 / circuit 22 / router 15 / budget 14 / video_persist 12 / no_threads 2 |
| `server.py --check` | **exit=0** | 嵌套不等式全链 ✓（read ≤ 60s 档 ≤ 200s deadline < 210 < 220）；轮询 2s；任务表 TTL 7200s/容量 100 |
| e2e 快速档 | **25/25 PASS** | 第七轮记录 + 本轮现场复核 |
| e2e 全量档 | **26/27 → 修复后 27/27** | ㉑ 场景断言未随 D5（30s→60s）同步，判定为测试缺陷（见四节 D-01），验收轮修复后单独复跑 PASS（20.8s / calls=2 / read=20s 先响→重试 1 次成功） |
| 真机生图 | **2 次成功** | 第六轮 svc_image（8s）+ 本轮 ratio 抽测：size="2K"+ratio="16:9" 11.7s，meta 完整 `model=agnes-image size=2K ratio=16:9 url_expires=1h` |
| 真机视频 | **全流程闭环（A-8）** | seed=42 + aspect_ratio=21:9 提交 200（6.1s）→ queued → in_progress(10) → completed(100) → URL HEAD 200/video/mp4/1.87MB（见三节） |
| 真机错误路径 | **503/429 契约已录** | 6 次提交尝试：503 video_queue_full → `upstream_error retry=yes`；429 → `rate_limit retry=yes`；message 与 request id 完整透传（run 940f34fed195） |

---

## 二、31 项验收清单逐项结论

处置口径：37 项 → 删 6 项（②③⑤⑧㉚㊲ 硅基专属）→ 31 项；其中 ④⑤㉘ 因 Q1=A（参数瘦身）再归 N/A，实际判定 28 项。

**图例**：🟢=PASS（证据齐）｜🟡=部分闭环/待条件｜⚪=待部署真机｜⚫=N/A

### A. 协议与工具面

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ① | Inspector 连接/三工具 + description 全等 | 🟢 | e2e ①（tools/list 三工具；A1″ 冻结断言 test_tool.py 41 用例） |
| ④ | 显式 model 覆盖路由 | ⚫ | Q1=A：model 参数已删，无路由语义（唯一模型直通，单测：任何 prompt 落 agnes-image） |
| ⑤ | quality=high 强制 | ⚫ | Q1=A：quality 参数已删 |
| ⑥ | batch=4 拆分串行返 4 URL | 🟢 | e2e ⑥（4 次调用、requested=4 delivered=4） |
| ⑦ | 真实 Agent 端到端 | 🟡 | 真实 MCP 协议全链已验（live_check 三轮）；真实 Agent 客户端接入待用户侧（agent-config.md 已交付） |
| ⑫ | 错 MCP Token 401 | 🟢 | e2e ⑫（HTTP 401 + mcp_auth 契约行） |
| ⑬ | 错上游 Token 401 | 🟢 | e2e ⑬（provider=Agnes 注明）+ 真机 401 错误体格式实锤（Delta 定案 F3） |
| ⑲ | /healthz 200 | 🟢 | e2e ⑲ |

### B. 错误处理与自愈

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ⑨ | 429 → 硬熔断 → OPEN 快失败 → 半开恢复 | 🟢 | e2e ⑨ 三场景（零重试/OPEN 窗口 E24 快失败/半开探针恢复）+ 真机 429 行为（第六轮 raw_429 并发 12 发触发、Retry-After 无头确认） |
| ⑩ | 审核 400 仅 1 次 API 调用 | 🟢 | e2e ⑩（calls==1 断言） |
| ⑪ | 探针失败熔断续期 | 🟢 | e2e ⑪（半开失败→OPEN 续一轮） |
| ⑳ | 慢死路径 ≤200s 结构化截断 | 🟢 | e2e 全量 ⑳ 两条（生图侧 181.6s 结构化超时；视频侧 read 档先响） |
| ㉑ | read 动态不误杀 + C4 重试 | 🟢 | 修复后复跑 PASS：20s 档挂 21s → read 先响 → 重试 1 次成功（calls==2，20.8s）；**行为本身无 bug**（首轮"失败"系断言未随 D5 同步，见 D-01） |
| ㉒ | 10 次中途异常后第 11 次仍得槽 | 🟢 | e2e ㉒（槽泄漏防护 finally 语义） |

### C. 持久化

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ㉓ | persist 成功/降级 | 🟢 | e2e ㉓ 两场景（/files URL + 降级原 URL/persist_failed）+ 真机 svc_persist（第六轮） |
| ㉔ | /files URL 24h 有效 | ⚪ | 文件落盘长期保留设计（规格书：不自动删除）；24h 时间维度验证待部署后（V1 同口径遗留） |
| ㉕ | 不 persist → url_expires=1h 警示 | 🟢 | e2e ㉕ + 真机 meta 实证（本轮 ratio 抽测返回行） |

### D. Agnes 生图（V2 新增）

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ㉖ | agnes-image 生成成功 | 🟢 | e2e ㉖（E25 三键请求体断言）+ 真机 2 次（8s / 11.7s） |
| ㉗ | size 直传 + 非法 400 透传 | 🟢 | e2e ㉗ + 真机 svc_image_bad（"99999x99999" → 400 message 透传） |
| ㉘ | quality → not_applied | ⚫ | Q1=A：quality 参数已删（meta 不再出现该字段） |
| ㉙ | batch 部分成功（requested/delivered） | 🟢 | e2e ㉙（1 成 1 败 → 1 URL + delivered=1/4 拆分调用数） |

### E. Agnes 视频（V2 新增）

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ㉛ | 视频同步成功返 URL | 🟢 | e2e ㉛（mock 状态机 completed → 可播放 URL + meta seconds/aspect_ratio/elapsed） |
| ㊜ | 超预算返 task_id+running → query 续接 | 🟢 | e2e ㊜（调小 TOTALBUDGETSEC → running 行 → query_video 拿到 URL） |
| ㊝ | query 不存在 task_id → task_not_found | 🟢 | e2e ㊝ + ㊱（TTL 过期同契约） |
| ㊞ | 视频失败分类、不切换、retry 语义 | 🟢 | e2e ㊞（failed/400/500 三态：审核类 retry=no 不切换；上游类 retry=yes） |
| ㊟ | 视频 persist（120s 档） | 🟢 | e2e ㊟（/files 可播放 + 降级路径） |
| ㊱ | TTL 过期清理 | 🟢 | e2e ㊱（TASK_TTL_SEC=1 → task_not_found） |

### F. 系统协作（V1 口径遗留，需部署环境）

| # | 验收项 | 结论 | 证据指针 |
|---|---|---|---|
| ⑭ | STOP 僵死 → watchdog 重启 | ⚪ | 单测级已验（liveness/心跳/shutdown 逻辑）；systemd 真机待部署 |
| ⑮ | 连崩 5 次风暴保护 | ⚪ | 熔断状态机 22 用例全绿；systemd 真机待部署 |
| ⑯ | 更新期在途完成（含视频轮询子场景） | ⚪ | 优雅停机单测已验；journalctl 对账待部署 |
| ⑰ | 24h CLOSE-WAIT≈0 | ⚪ | rotate 排空设计已验（300s>200s deadline）；24h 观察待部署 |
| ⑱ | py-spy 可用 | ⚪ | 环境工具项，待部署 |

**净结论**：🟢 23 项（含 A-8 真机闭环）｜ 🟡 1 项（⑦ 真实 Agent 接入待用户）｜ ⚪ 7 项待部署真机 ｜ ⚫ 3 项 N/A。**无阻塞级缺陷。**

---

## 三、真机补测记录（本轮验收新增，已同步真机联调记录）

### 3.1 D2–D7 新参数抽测（MCP 全链路，scripts/live_check_v2_acceptance.py）

| 案例 | 结果 | 证据 |
|---|---|---|
| A · D2 ratio 生图：generate_image(size="2K", ratio="16:9") | ✅ 11.7s | URL 真实生成；meta：`model=agnes-image size=2K ratio=16:9 url_expires=1h`（A_ratio_image, run 15:43） |
| B · D3+D4 seed+21:9 视频：generate_video(seed=42, aspect_ratio="21:9", seconds=4) | ✅ 上游接受 | 直连提交 200（6.1s）：请求体 seed=42 + aspect_ratio=21:9 + seconds="4" 无 400；task_id 返回。**上游成功出片闭环见 3.2** |
| C · D4 边界：aspect_ratio="5:4"（非六档） | ✅ | `[error] invalid_params retry=fixthenretry` + 六档提示文案（零配额消耗） |

### 3.2 A-8 视频全流程（本轮验收唯一新闭环项）

| 环节 | 结果 | 证据 |
|---|---|---|
| 提交 POST /v1/videos | ✅ | 200（6.1s），响应含 id/video_id/task_id **三字段同值**（字段名疑云定案：任取其一皆可，实现取 task_id） |
| 状态机 | ✅ | queued → **in_progress**（progress=10）→ …；官方枚举外加中间态 in_progress 实证，服务端轮询终止条件不变（completed/failed） |
| 查询 GET /agnesapi?video_id&model_name | ✅ | 200，顶层任务对象（无 data 容器），与实现"顶层优先"解析匹配 |
| completed + url 下载 | ✅ **完整闭环** | 15:56 completed（progress=100）→ URL `…/videos/agnes-video-2.5/task_….mp4` HEAD 实测 **HTTP 200 · video/mp4 · 1,871,079 B**；全程：提交 6.1s → queued → in_progress(10) → completed，总耗时约 8 分钟（免费队列渲染慢，非服务端问题） |

### 3.3 503/429 真机契约（run 940f34fed195，6 次提交尝试）

- 503 video_queue_full → `[error] category=upstream_error retry=yes` + 官方 message + request id 完整透传；
- 429 免费速率限制 → `[error] category=rate_limit retry=yes`（两分类与 Delta 定案 D6 一致）；
- 服务端单次退避重试（C4）后仍 503 → 契约行返回，不无限阻塞——**与"单机自用、retry=yes 稍后再来"设计意图一致**。

---

## 四、缺陷清单（本轮发现）

| # | 级别 | 缺陷 | 处置 |
|---|---|---|---|
| D-01 | **非阻塞**（测试断言过时，行为无 bug） | e2e ㉑ 场景仍按默认 30s 档设计（挂 31s 期望 read 先响）；D5 已将 TIMEOUT_AGNES_IMAGE_SEC 调至 60，31s 挂起在窗口内首次即成功（calls=1），断言 calls==2 失败 | **验收轮当场修复**：场景显式注入 `TIMEOUT_AGNES_IMAGE_SEC=20` + 挂 21s，回归"read 档先响 + 重试 1 次"语义，不耦合全局默认值；单独复跑 PASS（20.8s / calls=2）。修复仅动 e2e 场景参数，零实现代码改动，424 单测复跑全绿 |

**阻塞级缺陷：0。**

---

## 五、交付物完整性核对（八件套）

| # | 交付物 | 存在 | V2 最终口径同步 |
|---|---|---|---|
| 1 | 源码（app/engine/infra/ops 四层） | ✅ | 12 模块 + 424 单测；tool.py 五参数生图（prompt/size/ratio/batch_size/persist）+ 五参数视频（prompt/seconds/aspect_ratio/seed/persist）+ 单参 query_video |
| 2 | .env（Key 唯一替换点） | ✅ | AGNES_TOKEN/MCPACCESS_TOKEN 双令牌 + "唯一替换点"注释（用户 14:46 指示落地）；600 权限待部署侧确认 |
| 3 | config.example.env | ✅ | 占位符无密钥；TIMEOUT_AGNES_IMAGE_SEC=60（D5）、VIDEO_POLL_INTERVAL_SEC=2（D7）已同步 |
| 4 | systemd 单元（mcp-imagegen.service） | ✅ | EnvironmentFile + ExecStartPre --check 门禁；TimeoutStopSec 220 |
| 5 | 安装说明（install.md） | ✅ | D5/D7 调整注明来源；变量表全量同步 |
| 6 | Agent 配置片段（agent-config.md） | ✅ | 三工具参数表（ratio 八档/aspect_ratio 六档/seed）+ meta 契约 + 视频 running 续查语义完整 |
| 7 | 验收报告 | ✅ | 本文档（V1 版归档为验收报告_V1.md） |
| 8 | 真机记录（docs/真机联调记录_V2第六轮.md） | ✅ | 本轮验收补测已追加第八节（3.1–3.3 素材） |

---

## 六、结论与遗留

**结论：可交付。**

- 代码质量门禁全部达标：424 单测全绿、--check 通过、e2e 27/27（修复后）、真机生图/错误路径实证；
- 唯一非阻塞缺陷 D-01 已闭环（修复+复跑 PASS）；
- 七项待部署真机（⑭–⑱㉔⑦后半）为 V1 即有的部署环境依赖项，不阻塞代码交付，随 systemd 上线逐项销号。

**遗留清单（移交用户/部署阶段）**：
1. ~~上游视频队列恢复后补 A-8 最后一环~~（✅ 已于 15:56 闭环：completed + URL HEAD 200/video/mp4/1.87MB）；
2. systemd/Caddy 部署后销号 ⑭⑮⑯⑰⑱㉔ 六项；
3. 真实 Agent 客户端接入验证（agent-config.md 按图施工）；
4. 上线前换正式 Key（.env 唯一替换点一行）；
5. 4K 档计时（A-3 追加，D5 调 60s 后预计充裕，非阻塞）；
6. 视频免费队列高峰期 503 属上游容量常态——契约已正确引导"retry=yes 稍后再试"，建议 Agent 侧遵守，不做更长阻塞重试。

---

*验收人：芳 · 2026-10-10 · 证据文件：acceptance/e2e_results.json / scripts/live_check_results.json / docs/真机联调记录_V2第六轮.md（第八节追加）*
