# V2 第七轮 · D2–D7 官方对齐收口交付说明

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 轮次 | V2 第七轮（最后收口：修绿遗留 32 失败 + 交付说明补发） |
| 依据 | 《官方文档对齐Delta定案.md》（2026-10-10 14:26 定案，D1–D8） |
| 开工基线 | 全量 **391 passed + 32 failed**（test_video_pipeline 23 / test_video_persist 8 / test_上游 1） |
| 收口对账 | 全量 **424 passed, 0 failed**（391 存量 + 31 修绿 + 1 新增 seed 校验用例） |
| 端到端 | `acceptance/e2e_acceptance.py --fast`：**25/25 通过，exit=0** |
| 自检 | `python server.py --check`：**exit=0**（嵌套不等式/队列/persist/上游服务提供方 分档/轮询/任务表全 ✓） |
| 纪律 | 全部 mock 禁真实 API；FakeClock 控时零真实等待；标记前缀 chr(91) 拼接 + ord 逐码验证；本轮零实现代码改动（纯测试断言适配 + 1 新增用例）；本轮触碰的 root 属主文件已按临时文件+os.replace 铁律归位 z |

> 说明：D2–D7 实施与官方文档对齐发生在第五/六轮（其间会话两度中断，交付说明未发）；第六轮真机联调记录已单独成文（`docs/真机联调记录_V2第六轮.md`）。本文按派发单补齐 **D2–D7 逐项落地清单**，并收口本轮 32 项遗留失败与最终数字。

---

## 一、D2–D7 逐项落地清单（实施轮已完成，本轮逐项核实）

### D2 · generate_image 参数面：四参数 → 五参数 ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| app/tool.py | schema 五参数 `prompt / size / ratio / batch_size / persist`；A1″ description 追加 size/ratio 语义句并重新冻结 | `tests/test_tool.py` 41 项全绿（含 description 逐字符冻结、schema 参数集精确匹配） |
| app/pipeline.py | size 双格式直传（档位枚举 ∪ WxH 正则 `\\d{3,4}x\\d{3,4}`），不再本地吸附；ratio 仅与档位式 size 配合，与 WxH 同传 → `invalid_params` | `test_ratio_passthrough_with_tier_size` / `test_ratio_with_wxh_size_rejected_invalid_params` / `test_ratio_bad_value_rejected_invalid_params` / `test_ratio_none_by_default_not_sent` |
| infra/upstream.py | 请求体两形态构造：`{model, prompt, size}` + 可选 ratio（None 不注入） | `上游.generate_image(..., ratio=None)`，payload L305–315 |
| engine/normalizer.py | 档位枚举 `SIZE_TIERS=("1K","2K","3K","4K")`；官方 ratio 8 档枚举与校验 | `test_normalizer.py` 58 项全绿（含 `test_validate_ratio` 8 档参数化） |

### D3 · 视频 seed 恢复（integer 可选直传） ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| infra/upstream.py | `submit_video(..., seed: Optional[int] = None)`；None 不注入请求体，官方未标注值域不臆测上限 | `test_upstream.py` 视频提交组全绿 |
| app/video_pipeline.py | `validate_video_params` 第 5 参数 seed：仅类型校验（int / None），bool 拒绝；`run(..., seed=None)` 全链路透传 | 本轮新增 `test_validate_seed_int_or_none_only`（0/None 放行；True/3.5/"7" 拒绝） |
| app/contract.py | `VideoMeta.seed`：None 不输出 meta 字段，显式传入才出现 `seed=N` | `test_video_pipeline.py` meta 行断言 |
| app/tool.py | generate_video schema 恢复 seed 可选参数 | `test_tool.py` |

### D4 · aspect_ratio 扩至 6 档 ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| app/video_pipeline.py | `ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")`（D4 官方画幅表定案） | 本轮改名重写 `test_validate_aspect_ratio_six_options`（六档全放行；"5:4"/123 拒绝） |
| app/tool.py | schema 枚举 + description 更新（重新冻结） | `test_tool.py` 冻结测试全绿 |

### D5 · 生图超时档 30s → 60s ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| config.py | `TIMEOUT_UPSTREAM_IMAGE_SEC` 默认 60.0（B6 嵌套不等式 60 ≤ 200 ✓） | `test_config.py` 54 项全绿；`server.py --check` 输出"生图 60s" |
| config.example.env | 注释同步（30→60，4K 档实测 9s+ 留裕量） | 文件核对 |

### D6 · 503 video_queue_full 错误形态（归 UPSTREAM_ERROR） ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| engine/errors.py | 上游错误体解析**双格式兼容**：顶层 `{code, message, data}` 优先（F10 实锤 503 形态），嵌套 `{"error":{code,message,type}}` 兜底（F3 实锤 401 形态） | `test_errors.py` 25 项全绿（双格式参数化用例） |
| infra/upstream.py | 503 走 5xx 家族纪律：退避重试 1 次（计入 deadline）→ 仍 503 → 落 Agent 侧 retry=yes | `test_upstream.py` 5xx 重试组 |
| 分类语义 | 无新增错误类别；message（含"视频队列已满"+request id）透传 | `test_video_pipeline.py` upstream 分类用例 |

### D7 · 轮询间隔 5s → 2s ✅

| 落点 | 内容 | 证据 |
|---|---|---|
| config.py | `VIDEO_POLL_INTERVAL_SEC` 默认 2.0（官方建议 1–2s 取上限侧） | `test_config.py`；`server.py --check` 输出"轮询 2s" |
| 预算内轮数 | 200s 预算 / 2s ≈ 100 轮，远覆盖 4–12s 视频生成 | `test_run_deadline_expires_returns_running_non_error`（本轮适配后断言 100 轮） |

> D1（端点 .cn 定案）与 D8（附录 A 销号）属第六轮真机联调范围，详见《真机联调记录_V2第六轮.md》；D6 的真机 503 缓解后全流程验证仍列"剩余待真机"（A-8），不阻塞本轮收口。

---

## 二、本轮修绿 32 项失败（三根因，全部为测试断言适配，零实现 bug）

### 根因 1 · seed 参数签名变化（D3 实施后测试替身未跟进）——29 项

- **现象**：`FakeVideoApi.submit_video() got an unexpected keyword argument 'seed'`，`validate_video_params() missing 1 required positional argument: 'seed'`；
- **修复**：`tests/test_video_persist.py` 与 `tests/test_video_pipeline.py` 的 FakeVideoApi.submit_video 签名补 `seed=None` 并记入 submit_calls；validate 组 13 处调用补第 5 实参（None / 42 两种形态）；
- **覆盖面**：test_video_persist 8 项 + test_video_pipeline 21 项 = 29 项。

### 根因 2 · 轮询间隔 5→2（D7）后旧断言硬编码 5.0 —— 5 项

- **修复**（全部改为引用 `ns.cfg.video_poll_interval_sec`，防未来再漂移）：
  - `test_run_success_contract_table_and_log`：sleep 断言 `[iv, iv]`，elapsed 随之 10.0s→4.0s（`f"elapsed={2*iv:.1f}s"`）；
  - `test_poll_interval_constant_across_polls`：`[iv]*4`；
  - `test_run_deadline_expires_returns_running_non_error`：轮数改为按预算推导 `round(total_budget/iv)`（=100 轮，实测 poll_count=100 吻合）；
  - `test_queue_wait_counts_toward_deadline_e17`：拿槽后剩余 10s → `round(10/iv)`=5 轮，in_use_at_query 同步 `[1]*5`；
  - test_video_persist `test_budget_exhausted_skips_download_io`：`[iv]` 恰一次轮询间隔。

### 根因 3 · 生图超时档 30→60（D5）后旧断言硬编码 30 —— 1 项

- `test_upstream.py::test_read_timeout_image_tier_capped_by_deadline`：断言改为 `cfg.timeout_上游_image_sec`（min(档,200)=档，现值 60），注释同步标注 D5。

### 新增用例（1 项）

- `test_video_pipeline.py::test_validate_seed_int_or_none_only`：D3 seed 类型校验专项（0/None 放行；bool/float/str 拒绝）——修绿同时补齐 D3 的直测覆盖缺口。

---

## 三、铁律与纪律复核（本轮执行情况）

1. **标记常量 chr(91) 拼接 + ord 验证**：本轮零触碰任何标记常量；三个被改测试文件复核 `bytes([91,109])` 零字面量残留（test_video_pipeline 源码扫描门禁亦绿）；
2. **全部 mock 禁真实 API**：本轮改动均在测试层，Fake 上游 + FakeClock 控时，无任何真实网络调用；
3. **root 只读文件临时文件+rename**：本轮触碰的 3 个 root:root 644 文件（test_上游 / test_video_pipeline / test_video_persist）按同目录临时文件 + `os.replace` 原子替换落盘，属主归位 z（目录 z 属主可 rename）；
4. **无实现代码改动**：本轮 32 项失败全部判定为"旧断言适配"，未发现任何实现 bug，app/ 与 infra/、engine/、config.py 零改动（mtime 核对）。

---

## 四、最终数字汇总

| 验证项 | 结果 |
|---|---|
| 全量单测 | **424 passed, 0 failed**（7.2s；开工前 391 passed + 32 failed） |
| 分文件分布 | 上游 60 / normalizer 58 / config 54 / tool 41 / video_pipeline 38 / files_video 29 / tasks 27 / pipeline 27 / errors 25 / circuit 22 / router 15 / budget 14 / video_persist 12 / no_threads 2 |
| `python server.py --check` | **exit=0**（read ≤ 分档(max=60s) ≤ deadline(200s) < 210s < 220s 嵌套链 ✓；轮询 2s ✓；任务表 TTL 7200s/容量 100 ✓） |
| e2e `--fast` | **25/25 通过，exit=0**（真实 uvicorn + MCP streamable-http，上游全 mock） |
| e2e 慢档（⑳㉑） | 派发单未要求，代码保留可复跑 |

## 五、剩余待真机（不阻塞本轮，源自 Delta 定案第三节）

- A-3 追加：4K 档计时（D5 改 60s 后预计充裕）；
- A-8：视频全流程真机（503 队列满缓解后：提交→轮询→completed→url 可访问，含 status 枚举实测与 url 有效期观察）。

---

*交付：docs/V2_D2-D7官方对齐_第七轮收口_交付说明.md · 2026-10-10*
