# MCP 图片生成 Server · 验收报告

- **验收对象**：mcp-imagegen（FastMCP + uvicorn，规格书十一节实施顺序 S5 验收阶段）
- **验收日期**：2026-10-10
- **执行环境**：本机沙箱（真实 uvicorn 临时端口 + 真实 MCP streamable-http 协议：initialize → tools/list → tools/call，唯一替换项为"硅基流动上游"与"persist 图片下载"两处注入式 mock transport）
- **总体结论**：
  - **单测：225/225 全绿**（`python -m pytest -q`，4.73s）
  - **e2e 可模拟项：20/20 全 PASS**（`python acceptance/e2e_acceptance.py`，约 5 分钟；含 ⑲ 181.6s 慢死路径）
  - **真机项：6 项待执行**（⑦⑬⑭⑮⑯⑰，本环境无法模拟 systemd/Caddy/真实 Agent，见 §3 操作清单）
  - 验收过程中累计发现并修复 **4 项缺陷**（1 项产品缺陷 + 1 项测试编排缺陷 + 1 项测试基建缺口 + 1 项跨场景状态污染崩溃，详见 §4）

**复跑命令**：

```bash
cd mcp-imagegen
python -m pytest -q                      # 单测：225 passed
python acceptance/e2e_acceptance.py       # e2e 全量：20/20（约 5 分钟，⑲⑳ 为长耗时项）
python acceptance/e2e_acceptance.py --fast  # 快速档：跳过 ⑲⑳，约 40s
```

---

## 一、结论总表（22 项）

| # | 验收项 | 结论 | 证据来源 |
|---|---|---|---|
| ① | initialize 握手 + 单工具 | ✅ 通过 | e2e（本轮实测） |
| ② | 中文→kolors | ✅ 通过 | e2e |
| ③ | 英文→flux-schnell | ✅ 通过 | e2e |
| ④ | 显式 model 覆盖路由 | ✅ 通过 | e2e |
| ⑤ | quality=high 强制 Kolors | ✅ 通过 | e2e |
| ⑥ | batch=4 返 4 URL | ✅ 通过 | e2e |
| ⑦ | 真实 Agent 端到端 | ⏳ 待真机 | §3.1 命令 |
| ⑧ | 429 额度耗尽→切换+meta | ✅ 通过 | e2e |
| ⑨ | 429 后硬熔断窗口内绕开 + 到期半开探针恢复 | ✅ 通过（两条子场景） | e2e |
| ⑩ | 审核 400 仅 1 次 API 调用 | ✅ 通过 | e2e |
| ⑪ | 半开探针失败→OPEN 续期 | ✅ 通过 | e2e（含独立复现验证，见 §4.2） |
| ⑫ | 错 Token 401 契约行 | ✅ 通过 | e2e |
| ⑬ | STOP 模拟僵死→watchdog 重启 | ⏳ 待真机 | §3.2 命令 |
| ⑭ | 连崩 5 次风暴保护 | ⏳ 待真机 | §3.3 命令 |
| ⑮ | 更新期在途请求完成 | ⏳ 待真机 | §3.4 命令 |
| ⑯ | 24h CLOSE-WAIT≈0 | ⏳ 待真机 | §3.5 命令 |
| ⑰ | py-spy 可用 | ⏳ 待真机 | §3.6 命令 |
| ⑱ | /healthz 探活 | ✅ 通过 | e2e |
| ⑲ | 慢死路径 ≤200s 截断 | ✅ 通过 | e2e |
| ⑳ | read 动态分档（不误杀 + 30s 边界对照） | ✅ 通过（两条子场景） | e2e |
| ㉑ | 连续 10 次异常后第 11 次仍得槽 | ✅ 通过 | e2e |
| ㉒ | persist 成功/失败两态 | ✅ 通过（两条子场景） | e2e |

> 16 个可模拟编号项 → e2e 产出 20 条记录（⑨⑳㉒ 各含两条子场景，另含 ⑪ 的 A2 附验）；+6 真机项 = 22 项。

---

## 二、可模拟项逐项证据（e2e 实测，20/20）

### ⑱ /healthz 探活 200 — PASS
```
GET /healthz → 200 {"status":"ok"}
```

### ⑫ 错 Token 401 + mcp_auth 契约行 — PASS
```
HTTP 401  www-authenticate=Bearer
body: [error] category=mcp_auth retry=no reason=MCP 层鉴权失败(401) suggestion=请联系管理员检查 MCPACCESS_TOKEN
expected: [error] category=mcp_auth retry=no reason=MCP 层鉴权失败(401) suggestion=请联系管理员检查 MCPACCESS_TOKEN
```

### ① initialize 握手 + tools/list 单工具 — PASS
```
serverInfo.name = mcp-imagegen
protocolVersion = 2025-06-18
session_id = d1170cb1…（后续调用均携带）
tools = ['generate_image']
description 全等规格书: True
params(7) = prompt, model, quality, size, batch_size, seed, persist; required = ['prompt']
```

### ② 中文→kolors（路由④） — PASS
```
返回体: https://img.example/a.png
[meta] model=kolors quality=standard size=1024x1024
访问日志: route=cjk model=kolors … result=ok total=0.000s
上游 payload: model=Kwai-Kolors/Kolors steps=20 guidance=7.5（模型专属参数 Agent 不可见）
访问日志恰好 1 行: True（R13）
```

### ③ 英文→flux-schnell（路由⑤默认） — PASS
```
返回体: https://img.example/a.png
[meta] model=flux-schnell quality=standard size=1024x1024
访问日志: route=default model=flux-schnell … result=ok
上游 payload: model=black-forest-labs/FLUX.1-schnell steps=4 guidance=（不发送）
```

### ④ 显式 model=kolors 覆盖路由 — PASS
```
返回体: https://img.example/a.png
[meta] model=kolors quality=standard size=1024x1024
访问日志: route=explicit model=kolors … result=ok
```

### ⑤ quality=high 强制 Kolors — PASS
```
返回体: https://img.example/a.png
[meta] model=kolors quality=high size=1024x1024
访问日志: route=quality_high model=kolors … result=ok
上游 steps=25（high 档 Kolors 25 步）
```

### ⑥ batch=4 返回 4 URL + meta — PASS
```
https://img.example/b0.png … b3.png（4 行 URL）
[meta] model=flux-schnell quality=standard size=1024x1024
上游 payload batch_size=4
```

### ⑩ 审核 400 仅 1 次 API 调用 — PASS
```
返回体: [error] category=content_policy retry=no reason=内容审核未通过(400) | content policy violation suggestion=修改 prompt 后重试
访问日志: route=cjk model=kolors … result=error:content_policy
上游调用次数 = 1（应恰好 1 次：不可切换类不重试不切模型）
```

### ⑪附 范围越界 → invalid_params 契约行（A2） — PASS
```
batch_size 越界 → [error] category=invalid_params retry=fixthenretry reason=参数校验失败: batch_size … 上游调用=0
seed 越界 → [error] category=invalid_params … 上游调用=0
size 越界 → [error] category=invalid_params … 上游调用=0
```
（范围类校验在 pipeline 层而非传输层 schema 报错——A2 红线）

### ㉑ 10 次异常后第 11 次仍得槽成功 — PASS
```
前 10 次均结构化报错: 10/10（共 40 次上游调用，每次失败含重试+切换后重试）
第 11 次返回体: https://img.example/a.png
并发槽回收校验: semaphore 值 = 3（满额 3，无泄漏）
```

### ⑧ 429 额度耗尽→切换+meta — PASS
```
返回体: https://img.example/a.png
[meta] model=flux-schnell quality=standard size=1024x1024 | fallback: 上游限流/额度不足(429) | rate limited, 已切换至 flux-schnell
访问日志: fallback=上游限流/额度不足(429) | rate limited→flux-schnell result=ok
熔断状态: {'kolors': 'OPEN_HARD', 'flux-schnell': 'CLOSED'}（kolors=OPEN_HARD）
```

### ⑨ 429 后硬熔断窗口内直路由绕开 — PASS
```
返回体: https://img.example/a.png
[meta] model=flux-schnell … | fallback: kolors 熔断绕开, 已切换至 flux-schnell
访问日志: route=bypass_circuit model=flux-schnell result=ok
窗口内非显式请求上游调用 = 1 次（直发 schnell，未碰 kolors）
```

### ⑨ 熔断到期半开探针→成功→CLOSED — PASS
```
返回体: https://img.example/a.png
[meta] model=kolors quality=standard size=1024x1024
访问日志: route=cjk model=kolors result=ok（首个请求获探针资格）
探针实发模型: Kwai-Kolors/Kolors（恢复 kolors）
探针后熔断状态: {'kolors': 'CLOSED', 'flux-schnell': 'CLOSED'}
```

### ⑪ 半开探针失败→OPEN 续期一轮 — PASS
```
连败 2 次后: {'kolors': 'OPEN_SOFT', 'flux-schnell': 'CLOSED'}
半开探针失败后: {'kolors': 'OPEN_SOFT', 'flux-schnell': 'CLOSED'}（opened_at 12012.3 → 12015.7，续期）
探针失败时请求仍降级出图: True
再到期探针成功后: {'kolors': 'CLOSED', 'flux-schnell': 'CLOSED'}
```
（曾判 FAIL，根因为测试编排缺陷，见 §4.2；产品状态机经独立复现验证无 bug）

### ㉒ persist 成功→/files URL + 持久可访问 — PASS
```
返回体: http://127.0.0.1:39833/files/6284d624e7db-01-d3cdccb4.png
[meta] model=flux-schnell quality=standard size=1024x1024 | persisted
GET /files/6284d624e7db-01-d3cdccb4.png → 200，字节与源一致: True
5 秒后再次 GET → 200（文件长期保留，无过期机制）
落盘: ['6284d624e7db-01-d3cdccb4.png']
```
（曾判 FAIL，根因为产品侧 Host 头剥离缺陷，见 §4.3）

### ㉒ persist 失败→原URL+persist_failed 降级 — PASS
```
返回体: https://img.example/orig2.png
[meta] model=flux-schnell quality=standard size=1024x1024 | persist_failed
[error] 行不存在: True（C6：额度已花，降级出图优于报错）
服务端 WARN 日志: ['[persist] req_id=… 转存失败（降级原 URL）: RuntimeError: 下载返回 502']
```

### ⑲ 慢死路径 ≤200s 截断（结构化超时） — PASS
```
返回体: [error] category=upstream_error retry=yes reason=网络/超时异常(重试1次后仍失败) | ReadTimeout: Read timed out after 60.000s（e2e mock 模拟 httpcore） suggestion=稍后重试
耗时: 181.6s（预算 200s；收到完整 JSON-RPC 结果=非断连）
上游挂起请求数: 4（全部无响应，靠 read 超时+deadline 截断）
```
（mock 补齐 httpcore 超时执行后，本项呈现生产真实形态：read 超时按分档 30/30/60/60 逐段烧完，两模型各试一遍后 181.6s 收口，见 §4.4）

### ⑳ kolors batch=4 慢出图 31s 不被 30s 误杀 — PASS
```
返回体: https://img.example/a.png
[meta] model=kolors quality=standard size=1024x1024
耗时 30.9s，上游调用 1 次（一次成功，read=120s 档未被 30s 误杀）
```

### ⑳ 对照：schnell 档 30s 边界生效（重试 1 次） — PASS
```
返回体: https://img.example/a.png
[meta] model=flux-schnell quality=standard size=1024x1024
耗时 30.9s，上游调用 2 次（第 1 次 read=30s 超时，第 2 次重试拿到挂起释放的响应）
→ 两模型分档独立生效：kolors×batch4=120s / schnell=30s（动态 read）
```
（曾判 FAIL，根因为测试基建缺口（mock 未模拟 read 超时执行），见 §4.4；产品侧动态 read 计算由单测证明正确）

---

## 三、真机项操作清单（6 项，待用户在部署机执行）

> 前置：按 `deploy/install.md` §1–§8 完成安装并 `systemctl is-active mcp-imagegen` → active。

### 3.1 ⑦ 真实 Agent 端到端
按 `deploy/agent-config.md` 给 Agent 挂载工具（URL `http://<服务器IP>:8000/mcp` + Bearer MCPACCESS_TOKEN + 客户端超时建议 210s），对话发起出图：
```
用户："帮我画一只戴帽子的橘猫"
预期：Agent 调 generate_image → 1 行 URL + 1 行 [meta]（model/quality/size）
再试 persist=true → 预期返回 http://<host>/files/… 自托管持久 URL
证据留存：Agent 对话记录截图
```

### 3.2 ⑬ STOP 模拟僵死→watchdog 重启
```bash
OLD_PID=$(systemctl show -p MainPID --value mcp-imagegen)
sudo kill -STOP "$OLD_PID"          # 模拟事件循环僵死（进程在，不再心跳）
sleep 35
systemctl show -p MainPID --value mcp-imagegen   # 预期：新 PID ≠ OLD_PID（已自动重启）
systemctl show mcp-imagegen -p NRestarts          # 预期：+1
sudo journalctl -u mcp-imagegen --since "-2 min" --no-pager  # 预期：WatchdogSec=30 超时判死记录
```
预期：30s 内 systemd 判死并自动重启，无需人工干预。

### 3.3 ⑭ 连崩 5 次风暴保护
```bash
for i in 1 2 3 4 5; do sudo systemctl kill -s SIGKILL mcp-imagegen; sleep 4; done
systemctl status mcp-imagegen --no-pager   # 预期：failed（start request repeated too quickly）
sudo journalctl -u mcp-imagegen --since "-3 min" --no-pager | tail -20
# 验证后恢复：
sudo systemctl reset-failed mcp-imagegen && sudo systemctl start mcp-imagegen
```
预期：60s 窗口内第 5 次崩溃后 StartLimit（Burst=5/Interval=60s）触发，systemd 停止重启进入 failed——防崩风暴。

### 3.4 ⑮ 更新期在途请求完成
```bash
TOKEN=$(sudo awk -F= '/MCPACCESS_TOKEN/{print $2}' /etc/mcp-imagegen/env)
SID=$(curl -s -D - -o /dev/null http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  | awk -F': ' '/^mcp-session-id/{print $2}' | tr -d '\r')
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer $TOKEN" -H "mcp-session-id: $SID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null
# 发起 Kolors batch=4 慢请求（真实出图 ~30s），随即执行 restart：
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer $TOKEN" -H "mcp-session-id: $SID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"generate_image","arguments":{"prompt":"a cat","model":"kolors","batch_size":4}}}' > /tmp/inflight.json &
sleep 3 && sudo systemctl restart mcp-imagegen && wait
cat /tmp/inflight.json   # 预期：完整 JSON-RPC 结果（4 行 URL + meta），非断连
sudo journalctl -u mcp-imagegen --since "-3 min" --no-pager | grep -E "shutdown|startup"
# 预期：[shutdown] 停机开始 → 在途清零 → 新一轮 [startup] 就绪
```
预期：SIGTERM 先停收新请求、排空在途（上限 220s > 最坏叠加 180s），在途请求完整返回。

### 3.5 ⑯ 24h CLOSE-WAIT≈0
```bash
# 每小时采样，跑满 24h（也可 crontab 化）：
for i in $(seq 24); do
  echo "$(date +%F_%T) close_wait=$(ss -tan state close-wait | wc -l)" >> /tmp/cw.log
  sleep 3600
done
cat /tmp/cw.log   # 预期：全部 ≈ 0（偶发 1–2 为短时收尾；持续 >0 才异常）
```
健康基线 ≈0；结合每日 04:00 客户端重建与 keep-alive 25s 观察。

### 3.6 ⑰ py-spy 可用
```bash
sudo /opt/mcp-imagegen/venv/bin/py-spy dump --pid $(systemctl show -p MainPID --value mcp-imagegen)
# 预期：输出 Python 调用栈（长请求疑似卡死时先 dump 再决定是否 restart）
```

---

## 四、修复记录（验收阶段累计 4 项）

### 4.1 e2e 崩溃：env_hard_circuit 场景 httpx.RemoteProtocolError（incomplete chunked read）
- **现象**：e2e 跑到第 11 个场景（⑧⑨ 所在环境）的 mcp_initialize 时，客户端报 `httpx.RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)`，后续 6 个场景全部无法执行。
- **根因**：**测试脚本问题（非服务端 bug）**。sse_starlette 响应层以**进程级单例** `AppStatus` + 每线程 `_shutdown_watcher` 协调 uvicorn 优雅停机：任一 server 停机置 `should_exit` 后该标志**永不复位** → 同进程内后续所有 SSE 响应的 `_listen_for_exit_signal` 立即返回 → 任务组取消 → 响应体半途截断（uvicorn 报 "ASGI callable returned without completing response" → 客户端 incomplete chunked read）。生产部署为单进程单生命周期（systemd 重启即拉全新进程），语义正确；本脚本在单进程内串行模拟多个"进程生命周期"才触发。
- **修复**：`spawn_env` 边界复位进程级停机标志 + 取消回收残留 watcher 任务（`_reap_sse_starlette_state()`），等价于拉起新进程。修复后 20 场景全部跑通。

### 4.2 ⑪ 半开探针失败→OPEN 续期（曾判 FAIL：探针前后状态均 CLOSED、opened_at 0.0→0.0）
- **判定：测试编排缺陷，状态机无 bug**。独立复现脚本（真实 uvicorn + 埋点熔断器）验证产品行为完全符合规格：连败 2 次（每次最终失败在重试耗尽后计 1）→ `OPEN_SOFT`；到期首个请求获探针资格（`HALF_OPEN`）；探针失败 → 回 `OPEN_SOFT` 且 `opened_at` 前移（续期一轮）；再到期探针成功 → `CLOSED`。
- **旧测试缺陷**：每请求只排 1 个 500 响应——kolors 的 C4 重试吃掉了本应留给切换后 flux-schnell 的 200 响应 → `record_success(kolors)` 把软计数清零 → 熔断永远打不开（状态恒 CLOSED、opened_at 恒 0.0），与"续期"断言矛盾。
- **修复**（acceptance 侧）：每请求排 2×500（首调+重试各消费一个），ok 留给切换后的 schnell。修后证据：`连败 2 次后 OPEN_SOFT → 探针失败 opened_at 12012.3→12015.7 续期 → 探针失败时请求仍降级出图 → 再到期探针成功 CLOSED`。

### 4.3 ㉒ persist 成功（曾判 FAIL：证据全优却 ok=False，URL 为 http://localhost:0/files/…）
- **判定：产品侧真缺陷（工具层取 HTTP 头方式错误）**。Q3 决议要求 persist URL 按请求 Host 头推断；但旧版 `_current_http_headers()` 用 fastmcp `get_http_headers()` 默认模式——该模式剥离 host / x-forwarded-host / x-forwarded-proto 等头 → persist 拿不到 Host → `build_base_url` 回退 `localhost:{cfg.PORT}`；e2e 环境 PORT=0 故生成 `http://localhost:0/files/…`，测试断言 `startswith("http://127.0.0.1:")` 判 FAIL。persist 本体（落盘、/files 服务、字节一致、长期可访问）全部正常。生产直连场景下 Host 是唯一来源，此缺陷会导致真实 persist URL 错误——必须修产品。
- **修复**（产品 `app/tool.py`）：`get_http_headers(include_all=True)` 取全量后**白名单过滤**（仅放行 host / x-forwarded-host / x-forwarded-proto 三个 URL 构造所需头，不向 pipeline 深处扩散）。修后证据：URL 为 `http://127.0.0.1:<真实端口>/files/…`。

### 4.4 ⑳ schnell 30s 边界对照（曾判 FAIL："上游调用 1 次"与"第 1 次超时第 2 次成功"矛盾）
- **判定：测试基建缺口，产品无 bug**。经核实：计数口径无误（按场景环境统计上游请求总数）；矛盾的真因是 **httpx 把 per-request 超时写入 `request.extensions["timeout"]`，超时的执行属于 transport 层**——真实 httpcore 会在 socket 读超时后抛 ReadTimeout，而 e2e 自定义 mock transport（TimedRelease/NeverRelease）忽略 extensions → 挂起请求永不超时 → 首调一直等到 31s 释放点成功返回 → 上游恰 1 次调用，"read=30s 超时→退避重试 1 次"路径从未触发。产品侧正确性已由单测证明：`compute_read_timeout` 对 schnell（batch=1）恰返回 30s，经 per-request timeout 传入 httpx（tests/test_siliconflow.py 覆盖）；真实上游挂起时由 httpcore 执行该超时。
- **修复**（acceptance 侧）：
  1. mock transport 补齐 read 超时执行——`_await_release_or_read_timeout()` 按 extensions 的 read 值对挂起等待加帽，到期抛 `httpx.ReadTimeout`（与 httpx 内置 transport 的映射产物同类，产品侧 `httpx.TimeoutException` 捕获路径一致），使 mock 与生产行为对齐；
  2. 断言口径修正——G1/G2 耗时下界 30.5/31.0 → 30.0（31s 释放计时自 mock 创建起算、早于工具调用起点，按调用起点计可达 30.x；行为主证据是上游调用计数：G1 calls==1 证明未误杀、G2 calls==2 证明恰重试 1 次，符合 C4）。
- **连带收益**：⑲ 慢死路径由"仅测到外层护栏 201s 强杀"变为生产真实形态——read 超时按分档 30/30/60/60 逐段烧完、两模型各试一遍、181.6s 返回结构化错误（≤200s）。

---

## 五、单测 ↔ 验收项映射（225 项，10 个文件）

| 测试文件 | 用例数 | 覆盖内容 | 支撑的验收项 |
|---|---|---|---|
| tests/test_config.py | 38 | 变量名/默认值原样（B5）、嵌套不等式 fail-fast（read≤分档≤200<210<220）、env 解析 | 全项配置底座；⑲⑳ 的预算/分档合法性 |
| tests/test_normalizer.py | 40 | size 就近吸附、batch 1–4 裁剪、steps/guidance 模型专属参数翻译 | ①②③④⑤⑥ 的参数面（Agent 永不见模型专属参数） |
| tests/test_router.py | 20 | 路由链五优先级（explicit > bypass_circuit > quality_high > cjk > default + draft 放行）、探针资格授予 | ②③④⑤；⑧⑨ 熔断绕开/半开路由 |
| tests/test_errors.py | 22 | 九类错误矩阵逐格（category/retry/switchable/文案） | ⑩ 400、⑫ 401、⑧ 429、⑲ 超时、⑪附 invalid_params |
| tests/test_fallback.py | 18 | 可切换类才切、切换仅 1 次、两模型各试一遍、Q2 快失败 | ⑧ 切换+meta、⑩ 不切换、㉑ 4 次调用/请求 |
| tests/test_circuit.py | 22 | 三态全迁移（OPEN 续期/HALF_OPEN 探针/CLOSED）、硬 600s/软 300s 窗口、软计数清零、显式失败照写回 | ⑧⑨⑪ 熔断全行为 |
| tests/test_budget.py | 14 | 排队计入 deadline、退避计入、剩余预算、外层 guard | ⑲ 预算贯穿（R3） |
| tests/test_siliconflow.py | 30 | read=min(分档, 剩余) 动态计算（30/60/120）、C4 重试纪律（5xx/超时重试 1 次；429/402/4xx 零重试）、rotate 指针热切换+排空 | ⑳ 动态 read 与重试路径；⑮ 客户端重建（E6） |
| tests/test_tool.py | 19 | tools/list 七参数 schema、A1 description 逐字符全等、A2 越界走 pipeline | ①；⑪附 |
| tests/test_no_threads.py | 2 | R2 无线程铁律（全部逻辑在事件循环内） | ⑬⑮ 自愈/优雅停机的正确性底座 |

---

## 六、结论

- 规格书第十节 22 项验收：**16 项可模拟项全部通过（e2e 20/20，单测 225/225 全绿）**，6 项真机项（⑦⑬⑭⑮⑯⑰）已给出可直接执行的命令与预期（§3），待部署机执行。
- 验收阶段累计修复 4 项缺陷（§4）：1 项产品缺陷（㉒ Host 头剥离）、1 项测试编排缺陷（⑪ 脚本消费序列）、1 项测试基建缺口（⑳ mock 未模拟 read 超时执行）、1 项跨场景状态污染崩溃（sse_starlette 进程级单例）。产品代码现无已知未修复缺陷。
- 证据 JSON：`acceptance/e2e_results.json`（本轮 20/20 全 PASS 记录）。

下一步建议：在部署机按 §3.1–§3.6 执行 6 项真机验收并留存证据（journalctl/截图/采样日志），全部通过后即可转入 24h 稳定性观察（⑯）并交付生产。
