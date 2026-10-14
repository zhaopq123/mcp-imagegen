# V2 第四轮收尾 · 补测交付说明（T7c/T8/T9 收口 + 覆盖补全）

> **历史存档**：本文是 V2 阶段的设计/验收记录。V5 起上游已改为「服务提供方」可由用户自行配置，代码与文档不再绑定任何厂商；以 [README](README.md) 为准。
| 项 | 内容 |
|---|---|
| 轮次 | V2 第四轮收尾（上轮主体续作 + 本轮补测） |
| 依据 | 《MCP图片视频生成Server_增量设计方案V2.md》T7c / T8 / T9 行 |
| 基线对账 | 本轮开工前全量 **325 passed** ✓（复核于 13:2x） |
| 收口对账 | 全量 **366 passed**（= 325 存量全绿 + **41 新增**；两轮复跑一致） |
| 纪律 | 全部 mock 禁真实 API；FakeClock 控时零真实等待；标记常量 chr(91) 拼接 + ord 验证；本轮零改动实现代码（纯增量测试）+ 零触碰 root 属主文件 |

> 背景说明：第四轮主体（视频 persist 集成、三工具注册、server 装配、--check）已在上轮落地，但交付说明因会话中断未发。本说明按派发单补齐，覆盖"主体成果对账 + 本轮补测"两部分。

---

## 一、第四轮主体成果对账（上轮落地，本轮核实）

### 1. 三工具注册（T8；tests/test_tool.py 共 27 项全绿）

| 工具 | 参数 | 注册与冻结证据 |
|---|---|---|
| generate_image | prompt / size / batch_size / persist（恰四参数，Q1=A） | `test_description_frozen_verbatim`（A1′ 与方案 V2 六节文本逐字符全等）+ `test_tool_names_unchanged` + `test_schema_params_exact` |
| generate_video | prompt / seconds / aspect_ratio / persist（恰四参数，seed 已随 Q1=A 删除） | `test_video_description_uses_real_tool_and_param_names` + `test_video_description_dash_is_unicode_en_dash`（4–12 为 Unicode en-dash）+ 两个显式/缺省参数透传测试（`_RecordingVideoPipeline` 录制，值原样到达流水线） |
| query_video | task_id（单参数） | `test_query_video_params_passthrough`（透传至 query 入口） |

schema 层不做硬范围约束（A2：范围校验在 pipeline 层，`test_schema_has_no_hard_range_constraints`）；R14/E11 描述中已声明轮询阻塞特性。

### 2. 装配（T9；server.py，--check 冒烟通过）

- `Components` 持有 `video_pipeline`（与生图**共用**槽池 / PersistService / 停机协调器，不新建第二套并发面）；
- `tool.register_tool(mcp, pipeline, video_pipeline)` 一次注册三工具；
- `/files` 端点 `make_files_endpoint(cfg.image_dir, cfg.video_dir)` 按扩展名分流（.mp4/.webm → VIDEO_DIR）；
- 任务表 `cleanup_loop` 挂载 lifespan；
- 就绪日志含 `image_dir` 与 `video_dir` 双目录。

### 3. --check（带环境变量冒烟，本轮复跑）

```
MCPACCESS_TOKEN=… UPSTREAM_TOKEN=… python server.py --check   → exit=0
[config] 嵌套不等式自检: read ≤ 分档(max=30s) ≤ deadline(200s) < Agent建议(210s) < TimeoutStop(220s) ✓
[config] 队列等待 30s ≤ 总预算 ✓ | persist 下载 图30s/视频120s ≤ 总预算 ✓ | 并发槽 3 | 熔断 硬600s/软300s
[config] V2 上游服务提供方 read 档自检: 生图 30s / 提交 30s / 单查 10s ≤ 单查 ≤ 提交 ≤ deadline (200s) ✓ | 轮询 5s | 任务表 TTL 7200s / 容量 100 ✓
```

---

## 二、本轮补测（派发单两块缺口，41 项新增，全部一次通过）

### 1. 视频 persist 三分支（tests/test_video_persist.py，12 项）

装配方式：**真实 PersistService**（下载层注入 ScriptedTransport）+ VideoPipeline（FakeVideoApi 上游），IMAGE_DIR/VIDEO_DIR 落 tmp_path 隔离，FakeClock 控时。

| 分支 | 测试 | 断言要点 |
|---|---|---|
| ① 成功 | `test_success_mp4_content_type` | 输出行 = `http://<host>/files/<12hex-01-8hex>.mp4`；meta 行含 persisted 且无 url_expires；文件落 VIDEO_DIR（IMAGE_DIR 零落盘）且字节一致；下载请求确发上游 URL；Content-Type 优先定扩展名 |
| ① 成功（变体） | `test_success_webm_ext_from_url_suffix` | 无 Content-Type → URL 后缀回退 .webm |
| ① 成功（Q3） | `test_success_url_uses_forwarded_headers` | X-Forwarded-Proto/Host 透传 → `https://cdn.example/files/…` |
| ② 下载失败 | `test_download_failure_degrades_to_original_url` ×4（http-500 / connect-error / read-timeout / empty-body） | 降级返**原上游 URL** + meta persist_failed；输出整体非 error 行（R5）；VIDEO_DIR 零落盘；下载确有尝试；四形态共同验证 persist_video 永不报错（C6） |
| ③ 预算不足 | `test_budget_exhausted_skips_download_io` | BudgetBurnSleeper 每跳烧 205s > 200s 总预算 → **零下载 IO**（R3，transport.requests==[]）；同降级（原 URL + persist_failed） |
| 直接级 C6 | `test_persist_video_never_raises_on_exhausted_deadline` / `…_on_network_error` | 绕开 pipeline 直调 persist_video：耗尽 deadline → 返二元组且零 IO；断网异常 → 就地吞掉返二元组 |

logline 联动（T7c 字段）：三分支各断言恰好一行访问日志，含 `result=ok`、`persist=persisted/persist_failed`、`task_id=<video_id>`、`poll_count=1`、`split_calls=null`。

### 2. /files 视频分流（tests/test_files_video.py，29 项）

装配方式：Starlette 路由 + httpx ASGITransport 走**完整 HTTP 栈**（无真实网络）；端点纪律另以直调补测路由层无法产生的名字形态。

| 分组 | 数量 | 断言要点 |
|---|---|---|
| .mp4/.webm → VIDEO_DIR | 2 | 200 + 字节一致 + content-type（video/mp4、video/webm） |
| 图片扩展名 → IMAGE_DIR | 5（png/jpg/jpeg/webp/gif） | 200，图片路径不受视频分流影响 |
| 反向对账 | 2 | .mp4 只放 IMAGE_DIR → 404；.png 只放 VIDEO_DIR → 404（查找目录确由扩展名决定） |
| 未配置 VIDEO_DIR | 1 | video_dir=None：.mp4 → 404，同端点 .png 照常 200 |
| 缺失文件 / 空名 | 2 | 合法形态但不存在 → 404 |
| 完整栈单段纪律 | 2 | `%2F` 编码斜杠双段名 → 404；`/files/`（空段）→ 404 |
| 端点直调纪律 | 12 | 路径穿越前缀/后缀、双段、大小写混写（.MP4/大写 hex）、序号一位、hex 七位、非白名单扩展 .avi、无扩展、空名、纯点号等全部 404 |
| symlink 出逃 | 1 | VIDEO_DIR 内符号链接指向目录外同名合法文件 → resolve 归一后 parent≠root → 404 |
| 对照组 | 1 | 同目录真实 .webm 照常 200（拒绝确因 symlink，非误伤） |

---

## 三、测试数字（R21 基准）

| 时点 | 数字 | 说明 |
|---|---|---|
| 补测前（本轮开工复核） | **325 passed** | 第四轮主体落地后的基线 |
| 补测后（全量，两轮复跑一致） | **366 passed** | = 325 存量全绿 + 41 新增 |
| 新增明细 | test_video_persist.py **12** + test_files_video.py **29** | 首跑即全绿，0 修复迭代 |
| 单文件规模对账 | test_tool.py 27 / test_video_pipeline.py 37 | 三工具注册与流水线主体测试原样全绿 |

耗时：全量 366 项约 6.1–6.7s；新增 41 项约 0.3s。

---

## 四、遗留与风险

1. **e2e 脚本待 T10 改造（非功能回归，已实证定位）**：本轮 `--fast` 冒烟跑 acceptance/e2e_acceptance.py，场景①在 `s_handshake` 断言"恰 1 个工具"（写于单工具时代）——三工具注册后该断言必然失败，属 e2e 场景改造范围（方案 T10："e2e 场景改造（删另一家上游/加视频 mock 状态机）"），非功能回归。既有 e2e_results.json 为生图轮 16/16 通过的历史证据。
2. **app/contract.py docstring 三处 meta 字面量（ord 核实，范围外未动）**：偏移 182 / 1374 / 1973 处存在 ord 序列 91,109,101,116,97,93（docstring 说明文字）；`META_PREFIX` 常量本身为 `chr(91) + "meta] "` 拼接、功能零影响。但渲染层吞 91,109 序列现象本轮再次实证（终端显示 / Read 回显均可见），后续轮次若经渲染层读该文件可能误判"污染"——建议后续轮次顺手将三处 docstring 改写为不含该序列的表述，或按本说明偏移直接 ord 级核对。
3. **A-5 真机联调**：仍待有效 上游服务提供方 Key（上轮已知：当前 Key 上游 401 Invalid token）；三工具 + 325→366 单测不受影响。
4. **deploy 文档层未同步 VIDEO_DIR（已核实，功能不受影响）**：install.md 变量表与 mcp-imagegen.service 注释仍只提 IMAGE_DIR；VIDEO_DIR 有默认值（/var/lib/mcp-imagegen/videos）、`ProtectSystem=full` 不禁止 /var/lib 写入、磁盘水位检查经 `PersistService.disk_usage_ratio()` 代码层已覆盖 images/+videos/ 双目录——故功能无碍，建议 T10 或部署轮在文档/注释补一句视频目录说明。

---

## 五、纪律对账

| 检查项 | 结果 |
|---|---|
| 全部 mock（FakeVideoApi / ScriptedTransport / ASGITransport），禁真实 API | ✓ |
| FakeClock 控时：轮询零真实等待（AdvancingSleeper / BudgetBurnSleeper） | ✓ |
| 标记常量 chr(91) 拼接 + ord 逐码验证（`test_meta_prefix_ord_verified`） | ✓ |
| 新增源文件零字面量 91,109 序列（自扫描 clean；`test_source_no_literal_marker_prefix` 覆盖 persist.py / endpoints.py） | ✓ |
| root 只读文件 | 本轮零触碰（纯新增两个测试文件，均落 z 属主可写目录） |
| R2 禁线程（新增测试零线程） | ✓ |
| 实现代码改动 | **零**（纯增量测试，325 存量原样全绿为证） |

---

下一步建议：T10 e2e 场景改造（三工具断言 + 视频 mock 状态机 + ㉔/files videos 场景回归），与 A-5 真机联调（待有效 Key）同批收口。
