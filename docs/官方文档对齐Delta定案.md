# MCP 图片/视频生成 Server · 官方文档对齐 Delta 定案

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](../README.md) 为准。
| 项 | 内容 |
|---|---|
| 文档版本 | V2.1（Delta，快速收敛文档） |
| 日期 | 2026-10-10 |
| 作者 | 芳（产品负责人） |
| 性质 | 对已定案规格的官方文档纠偏：**只列变更不重写**；与既有文档冲突但实测已验证的，以实测为准 |
| 依据 | 两份官方文档（生图/视频，用户 10-10 提供）+ 芳真机实测 4 项（本轮）+ 管理员转述真机结果（生图 8s 成功、视频 503） |

---

## 〇、事实底座（定案依据，编号引用）

**官方文档事实**：
- F1 生图端点 `https://api.example.com/v1/images/generations`（检查清单 + 全部示例双确认）；**两份文档通篇无任何 .com / 国际站表述**。
- F2 生图 `size`：必填；推荐档位 `1K/2K/3K/4K`；兼容 `1024x768` 历史写法，但不受支持的精确尺寸"可能会被标准化"（自动映射最近档位+比例）。
- F3 生图 `ratio`：可选，仅与档位式 size 配合；8 档 `1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9`，默认 `1:1`。
- F4 生图参数表**无 `n`/batch 参数**；`response_format` 不得放顶层（需走 `extra_body`），我方用 url 输出无需传。
- F5 视频提交 `POST /v1/videos`：`mode` 必填（text/keyframe/reference）；`seconds` 字符串 `"4"`–`"12"` 默认 `"5"`；`size` Flash 固定 `"720P"`（其他值 400）；`seed` integer 可选；`n` 仅支持 1。
- F6 视频查询 `GET /v1/videos/query?video_id=<id>&model_name=example-video-1`（不带 model_name 仅适用于 text 模式）；**官方建议 1–2s 轮询**；completed 时从顶层 `url` 取地址，`status/progress` 为准、`internal_*` 忽略。
- F7 视频仅 720P；当前限时免费（图/视频均 ¥0）。

**芳本轮真机实测（2026-10-10 14:31–14:38，Key 为用户 14:46 所供）**：
- F8 `size:"2K"+ratio:"16:9"` → HTTP 200，**9.2s**，`data[0].url`（档位式请求格式验证通过）。
- F9 `size:"1920x1080"`（WxH 历史写法）→ HTTP 200，**8.2s**（双格式均可直传，"标准化"不影响请求成功）。
- F10 视频提交 → HTTP 503，0.24s，错误体 `{"code":"video_queue_full","message":"视频队列已满，请稍后重试 (request id: …)","data":null}`（**顶层三段格式**，与 401 的 `{"error":{…}}` 嵌套格式并存）。
- F11 生图响应顶层含 `task_id`（`task_…` 格式），URL 路径亦内嵌——生图也有上游任务号（logline 新字段的意外来源）。

**当前实现状态**（核对于 366 单测全绿基线）：BASE_URL 已是 api.example.com ✓；视频提交已传 mode/size ✓；seconds int→str ✓；查询路径 /v1/videos/query + model_name ✓。

---

## 一、八项定案（每项：结论 → 依据 → 落点）

### D1 · 端点与国际站
**结论**：`UPSTREAM_BASE_URL` 默认值定案 `https://api.example.com`（已落地 ✓）；规格书中"国际站 apihub.上游-ai.com / Key 不互通 / 可切换"（E22 相关表述）**整体删除**；`UPSTREAM_BASE_URL` 保留为可配置项（官方若换域名只改环境变量），不做双端点自动容灾（规格书十二节本就明确不做）。
**依据**：F1（文档通篇仅 .cn）；规格书"明确不做"清单。
**落点**：config.py 已改；文档侧删 E22 表述；config.example.env 注释同步。

### D2 · generate_image 参数面（Q1 后续，定案）
**结论**：**四参数 → 五参数**：`prompt / size / ratio / batch_size / persist`。
- `size` **双格式直传**：档位枚举 `1K/2K/3K/4K` 或 WxH 正则（`\d{3,4}x\d{3,4}`），两种都原样发上游（F2/F8/F9 实锤），不再本地吸附；
- **新增 `ratio`**：可选，8 档枚举（F3），默认不传（上游默认 1:1）；
- **冲突裁决**：`ratio` 与 WxH 式 `size` 同时出现 → `invalid_params`（官方语义"ratio 仅与档位式 size 配合"，显式冲突必须让 Agent 知道，静默忽略违反可预期原则）；
- `batch_size` 保留：官方生图**无 n 参数**（F4），服务端拆分串行的设计正当性成立。
**依据**：F2/F3/F4/F8/F9。
**落点**：app/tool.py schema（description 随之更新并重新冻结——A1″）；pipeline 校验（size 格式 ∪ 档位 ∪ ratio 冲突）；upstream.py 请求体按"档位+ratio / 纯 WxH"两形态构造；normalizer/contract 的 size 语义同步。

### D3 · 视频 seed：恢复
**结论**：`generate_video` 恢复 `seed` 可选参数（integer，默认 None=随机，透传上游）。
**依据**：官方公共参数表明列 `seed integer 否`（F5）——此前删除依据"上游请求体无 seed 字段"**被官方文档证伪**，按"与文档冲突以实测/文档为准"原则回滚。范围沿用规格书 0–2³¹-1（上游未标注范围，超界由上游 400 兜底并透传）。
**落点**：tool.py schema、pipeline 校验、upstream.py payload（None 不注入）。

### D4 · aspect_ratio：扩至 6 档
**结论**：`16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9` 全量暴露（当前 3 档 → 6 档）。
**依据**：官方"支持的 aspect_ratio 值见视频尺寸与画幅"6 档全列（F5 交叉管理员转述）；只暴露 3 档属于自造约束，违反归一根柱精神（Agent 表达力被无谓限制）。
**落点**：tool.py 枚举 + description 更新（重新冻结）；pipeline 校验枚举集。

### D5 · 生图超时档：30s → 60s
**结论**：`TIMEOUT_UPSTREAM_IMAGE_SEC` 默认值 **60**。
**依据**：官方建议客户端超时 60–360s；真机 1K≈8s、2K≈9.2s（F8/F9），60s 给 4K 留 ~7x 余量；**不可取 360**——分档 ≤ deadline 200s 是 B6 嵌套不等式红线。
**落点**：config.py 默认值 + 自检不等式仍满足（60 ≤ 200 ✓）；config.example.env。

### D6 · 503 video_queue_full 错误形态
**结论**：归 **UPSTREAM_ERROR**（上游 5xx 家族）：客户端退避重试 1 次（计入 deadline）、`retry=yes`（稍后重调）、软熔断（连败 2 次 5min）；`message`（含"视频队列已满"与 request id）透传给 Agent。
**依据**：F10 实锤错误体为**顶层** `{code, message, data}` 三段式——错误解析器需与 401 的 `{"error":{…}}` 嵌套式**双格式兼容**（两种都已在真机出现，不是臆测）。503 本质是"稍后重试"类，与 V1 5xx 纪律零冲突、零新增语义。
**落点**：engine/errors.py：上游错误体解析双格式（顶层 code/message 优先，嵌套 error.* 兜底）；503 无需新增类别。
**注意**：视频提交 503 重试 1 次大概率仍 503（队列满不会瞬间缓解），最终落到 Agent 侧 retry=yes 稍后再来——语义正确，无需特判。

### D7 · 轮询间隔：5s → 2s
**结论**：`VIDEO_POLL_INTERVAL_SEC` 默认值 **2**。
**依据**：官方明确"建议每隔 1–2 秒查询一次"（F6）；取官方区间上限（最保守侧），兼顾单查请求量；预算内轮询次数 ~16 轮（单查 10s read 档 + 2s 间隔，200s 预算）足够覆盖 4–12s 视频生成。
**落点**：config.py 默认值；附录 A-5 销号。

### D8 · 附录 A 逐项销号/回填
| 项 | 原内容 | 处置 |
|---|---|---|
| A-1 | Kolors 模型 ID 核对 | **作废**（另一家上游已删，随 V2 变更） |
| A-2 | 响应顶层 images vs data | **销号**：实锤 `data[0].url`（F8/F9 + 管理员真机）；双兼容解析保留（防御性，零成本） |
| A-3 | 2K/4K 耗时校 30s 档 | **部分销号**：1K≈8s、2K≈9.2s（F8/F9）；**新增待测**：4K 档计时（D5 改 60s 后预计充裕） |
| A-4 | size 值域 | **销号**：双格式直传均 200（F8/F9）；档位+ratio 定案（D2） |
| A-5 | 轮询间隔 5s 是否下调 | **销号**：定案 2s（D7） |
| A-6 | 上游服务提供方 国际端点 .com | **销号**：官方文档无 .com，E22 表述删除（D1） |
| A-7 | schnell 步数上限 | **作废**（另一家上游已删） |
| **新增 A-8** | 视频全流程真机 | **待测**：503 队列满缓解后跑通 提交→轮询→completed→url 可访问（含 status 枚举实测、url 有效期观察） |

---

## 二、Key 配置处置（用户 14:46 指示，已完成）

**用户要求**：Key 写进项目、后续自行替换、做成环境变量只换一次。

**已落地**（芳执行，跨配置边界操作特此披露）：
1. **唯一替换点 = `mcp-imagegen/.env` 文件**：
   - `UPSTREAM_TOKEN=` ← 换 上游服务提供方 Key 只改这一行；
   - `MCPACCESS_TOKEN=` ← MCP 层访问令牌（芳随机生成，可自行改掉）；
2. `config.py` 新增 `_load_dotenv()`（纯标准库 15 行，零新依赖）：项目根存在 `.env` 则自动加载；已 export 的环境变量优先（不覆盖）；文件不存在零副作用；
3. **本地运行**：无需 export，直接 `python server.py`；**服务器部署**：systemd 单元 `EnvironmentFile=` 直接指向同一 `.env` 文件（install.md 同步说明）——真正只换一处；
4. 回归：**366/366 单测全绿**；真机生图 2 次通过（F8/F9，即用 .env 中的 Key）。

**纪律（红线 B7 落地）**：`.env` 含密钥**严禁入 git/截图/粘贴对话**（项目将来 init git 时 .gitignore 必须含 `.env`）；`config.example.env` 保持占位符；群内出现过的测试 Key 上线前务必换正式 Key。

---

## 三、剩余待真机（不阻塞开发，阻塞最终验收）

1. **A-8 视频全流程**：503 队列满缓解后：提交 → 2s 轮询 → status 枚举实测（completed/failed + 是否有中间态）→ url 下载可访问 → url 有效期观察（规格书 1h 警示实测）；
2. **A-3 补充**：4K 档生图计时（对照 60s 档）。

---

下一步建议：Ethan 按 D2/D3/D4 改 tool/pipeline/上游（schema 变更后 description 重新冻结并更新全等断言），D5/D6/D7 改 config 与 errors（各为小 diff）；芳在视频队列缓解后补 A-8 全流程实测并出验收结论。