"""T7a/T7b 门禁：视频流水线（提交+轮询 / query 入口；V2 方案 2.2）。

覆盖（任务派发单四件事 + 边界 E17/E18/E19/E20/E21 + 风险 R14/R15/R16）：
- 参数校验：prompt 必填、seconds 整数 4–12、aspect_ratio 三档、persist 布尔；
- R15 三分支：正常完成（finally 释放）/ 超预算返 running（E17 非错误 +
  finally 释放）/ failed 分类（E19）——取消与护栏路径同样释放；
- 轮询纪律：间隔精确恒等于 VIDEO_POLL_INTERVAL_SEC；单查 read ≤10s
  且嵌套 10 ≤ 提交 30 ≤ deadline 200（纯函数 + 真客户端集成双重锁定）；
- 任务表联动：register / 每次轮询 update / TTL 过期 task_not_found；
- 信号量：排队等待计入 deadline（E4）；查询路径不占并发槽（E20）；
- E17 文案：task_id + status=running + 已等待秒数 + query_video 提示；
- 铁律：标记前缀一律 chr(91) 拼接构造 + ord 逐码验证；源码零字面量前缀。

全部 mock 上游（FakeVideoApi / ScriptedTransport），禁真实 API；
轮询等待用 FakeClock 免真实等待（唯一真实等待：busy 上限 0.05s 与
护栏兜底 0.5s 两处，均为毫秒级）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import FakeClock, FakeSleeper, make_config, scripted

import app.video_pipeline as vp_mod
from app import contract as contract_mod
from app.video_pipeline import VideoPipeline, validate_video_params
from infra import agnes as agnes_mod
from infra import budget as budget_mod
from infra import tasks as tasks_mod
from ops import logline as logline_mod

# 铁律：标记前缀一律 chr(91)（左方括号）拼接构造，禁止手打字面量
META_PREFIX = chr(91) + "meta] "
ERROR_PREFIX = chr(91) + "error] "
ORD_META = [91, 109, 101, 116, 97, 93, 32]


# ---------------------------------------------------------------------------
# 工具：结果工厂 / 假客户端 / 假 sleep / 装配
# ---------------------------------------------------------------------------
def submit_ok(video_id: str = "vid-1") -> agnes_mod.ApiCallResult:
    return agnes_mod.ApiCallResult(
        ok=True, status_code=200, model_id="agnes-video-2.5-flash",
        video_id=video_id)


def query_result(
    status: str = "running", url: str | None = None,
) -> agnes_mod.ApiCallResult:
    return agnes_mod.ApiCallResult(
        ok=True, status_code=200, model_id="agnes-video-2.5-flash",
        video_status=status, video_url=url)


def api_error(
    status_code: int, detail: str = "boom",
) -> agnes_mod.ApiCallResult:
    return agnes_mod.ApiCallResult(
        ok=False, status_code=status_code, model_id="agnes-video-2.5-flash",
        error_detail=detail)


class FakeVideoApi:
    """AgnesClient 假替身：脚本化 submit/query；记录调用与占槽实况。"""

    def __init__(self, submit_result=None, query_script=None, pool=None):
        self.submit_result = submit_result
        self.query_script = list(query_script or [])
        self.default_query = query_result()  # 脚本耗尽后默认 running
        self.pool = pool
        self.submit_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.in_use_at_query: list[int] = []
        self.submit_arrived = asyncio.Event()

    async def submit_video(self, *, model, prompt, seconds, aspect_ratio, deadline,
                           seed=None, mode="text", first_frame=None,
                           last_frame=None, reference_images=None):
        self.submit_calls.append({
            "model": model, "prompt": prompt, "seconds": seconds,
            "aspect_ratio": aspect_ratio, "deadline": deadline, "seed": seed,
            "mode": mode, "first_frame": first_frame,
            "last_frame": last_frame, "reference_images": reference_images,
        })
        self.submit_arrived.set()
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    async def query_video(self, *, video_id, model, deadline):
        if self.pool is not None:
            self.in_use_at_query.append(self.pool.in_use())
        self.query_calls.append({
            "video_id": video_id, "model": model, "deadline": deadline})
        item = self.query_script.pop(0) if self.query_script else self.default_query
        if isinstance(item, Exception):
            raise item
        return item


class AdvancingSleeper:
    """轮询 sleep 假身：推进 FakeClock 并记录间隔（免真实等待）。"""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)
        self.clock.advance(sec)


class HangingSleeper:
    """永不返回的 sleep 假身（取消 / 护栏测试用）。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def __call__(self, sec: float) -> None:
        self.entered.set()
        await asyncio.Event().wait()


def make_pipeline(*, submit_result=None, query_script=None, cfg=None, sleep=None):
    """装配 VideoPipeline + 同源 FakeClock 的任务表 / 槽池 / 假 sleep。"""
    cfg = cfg or make_config()
    clock = FakeClock()
    table = tasks_mod.TaskTable(
        ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max, clock=clock)
    pool = budget_mod.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
    api = FakeVideoApi(
        submit_result=submit_result, query_script=query_script, pool=pool)
    if sleep is None:
        sleep = AdvancingSleeper(clock)
    vp = VideoPipeline(cfg, pool, api, table, clock=clock, sleep=sleep)
    return SimpleNamespace(
        vp=vp, pool=pool, table=table, clock=clock, sleep=sleep,
        api=api, cfg=cfg)


def access_records(caplog):
    return [r for r in caplog.records if r.name == "mcp_imagegen.access"]


async def run_ok(ns, **kw):
    """便捷调用：默认合法参数。"""
    params = dict(prompt="a cat", seconds=5, aspect_ratio="16:9",
                  persist_flag=False)
    params.update(kw)
    return await ns.vp.run(**params)


# ---------------------------------------------------------------------------
# 铁律专项：标记前缀 ord 逐码验证 + 源码零字面量
# ---------------------------------------------------------------------------
def test_meta_prefix_ord_verified():
    """前 7 字符 ord 序列 91,109,101,116,97,93,32（防渲染层吞字）。"""
    assert [ord(c) for c in META_PREFIX] == ORD_META
    assert [ord(c) for c in contract_mod.META_PREFIX[:7]] == ORD_META
    assert [ord(c) for c in ERROR_PREFIX[:2]] == [91, 101]


def test_source_no_literal_marker_prefix():
    """video_pipeline.py 源码零字面量前缀序列（一律 chr(91) 拼接构造）。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "video_pipeline.py"
           ).read_bytes()
    assert bytes([91, 109]) not in src, "源码出现字面量左方括号+m（铁律违例）"


# ---------------------------------------------------------------------------
# 参数校验（A2：pipeline 层而非 schema 层；D3 seed 恢复可选、D4 画幅六档）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seconds", [4, 12])
def test_validate_ok(seconds):
    assert validate_video_params("a cat", seconds, "16:9", False, None) == ()
    assert validate_video_params("a cat", seconds, "16:9", False, 42) == ()


@pytest.mark.parametrize("seconds", [3, 13])
def test_validate_seconds_out_of_range(seconds):
    bad = validate_video_params("a cat", seconds, "16:9", False, None)
    assert bad[0] == "seconds"
    assert "4–12" in bad[1]


def test_validate_prompt_blank_or_non_str():
    assert validate_video_params("   ", 5, "16:9", False, None)[0] == "prompt"
    assert validate_video_params(None, 5, "16:9", False, None)[0] == "prompt"


def test_validate_seconds_non_int():
    assert validate_video_params("a cat", 3.5, "16:9", False, None)[0] == "seconds"
    # bool 是 int 子类：True 应被拒绝
    assert validate_video_params("a cat", True, "16:9", False, None)[0] == "seconds"


def test_validate_aspect_ratio_six_options():
    """D4（官方画幅表）：六档全放行，档外拒绝。"""
    assert validate_video_params("a cat", 5, "5:4", False, None)[0] == "aspect_ratio"
    assert validate_video_params("a cat", 5, 123, False, None)[0] == "aspect_ratio"
    for ratio in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        assert validate_video_params("a cat", 5, ratio, False, None) == ()


def test_validate_seed_int_or_none_only():
    """D3（官方 F5）：seed 可选整数；bool/float 拒绝。"""
    assert validate_video_params("a cat", 5, "16:9", False, 0) == ()
    assert validate_video_params("a cat", 5, "16:9", False, None) == ()
    assert validate_video_params("a cat", 5, "16:9", False, True)[0] == "seed"
    assert validate_video_params("a cat", 5, "16:9", False, 3.5)[0] == "seed"
    assert validate_video_params("a cat", 5, "16:9", False, "7")[0] == "seed"


def test_validate_persist_bool_only():
    assert validate_video_params("a cat", 5, "16:9", "true", None)[0] == "persist"
    assert validate_video_params("a cat", 5, "16:9", 1, None)[0] == "persist"


async def test_run_invalid_params_returns_error_contract():
    ns = make_pipeline(submit_result=submit_ok())
    out = await ns.vp.run(prompt="", seconds=5, aspect_ratio="16:9",
                          persist_flag=False)
    assert out.startswith(ERROR_PREFIX)
    assert "category=invalid_params" in out
    assert "retry=fixthenretry" in out
    assert "prompt" in out
    assert ns.api.submit_calls == []      # 校验失败零上游调用
    assert ns.pool.in_use() == 0


# ---------------------------------------------------------------------------
# R15 分支一：正常完成（register / update / meta / 日志一行 / finally 释放）
# ---------------------------------------------------------------------------
async def test_run_success_contract_table_and_log(caplog):
    ns = make_pipeline(
        submit_result=submit_ok("vid-1"),
        query_script=[query_result("running"),
                      query_result("completed", "https://v.example/1.mp4")])
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        out = await run_ok(ns)

    lines = out.split("\n")
    assert lines[0] == "https://v.example/1.mp4"
    meta_line = lines[1]
    assert [ord(c) for c in meta_line[:7]] == ORD_META   # ord 逐码验证
    assert "model=agnes-video" in meta_line
    assert "seconds=5" in meta_line
    assert "aspect_ratio=16:9" in meta_line
    iv = ns.cfg.video_poll_interval_sec          # D7：默认 2s
    assert f"elapsed={2 * iv:.1f}s" in meta_line  # 两次轮询各推进 iv（FakeClock）
    assert "url_expires=1h" in meta_line     # E13：不 persist 短效警示
    assert "persisted" not in meta_line

    # 任务表：register（task_id=video_id）→ 每次轮询 update 至最近实况
    entry = ns.table.get("vid-1")
    assert entry is not None
    assert entry.model == "agnes-video"
    assert entry.status == "completed"
    assert entry.url == "https://v.example/1.mp4"
    assert entry.prompt_hash == logline_mod.prompt_hash("a cat")

    # 上游调用参数与轮询间隔 / 占槽实况
    assert ns.api.submit_calls[0]["prompt"] == "a cat"
    assert ns.api.submit_calls[0]["seconds"] == 5
    assert ns.api.submit_calls[0]["aspect_ratio"] == "16:9"
    assert ns.sleep.calls == [iv, iv]
    # V3：轮询等待移到槽外（R14 作废）——等待不是 IO，不该占并发额度；
    # 生图泳道因此不会被视频长任务饿死（本轮性能改造的核心）
    assert ns.api.in_use_at_query == [0, 0]

    assert ns.pool.in_use() == 0             # R15：完成即释放

    # R13：恰好一行访问日志
    access = access_records(caplog)
    assert len(access) == 1
    rendered = access[0].getMessage()
    for field in ("req_id=", "model=agnes-video", "seconds=5",
                   "result=ok", "total="):
        assert field in rendered, rendered


async def test_run_persist_true_returns_upstream_url_with_expires(caplog):
    # 无 persist 服务注入（独立组件测试场景）：persist=True 视同 skipped
    # （上游 URL + 短效警示），日志参数面如实记录 persist=true——带
    # persist 服务的三分支见下方 T7c 专项
    ns = make_pipeline(
        submit_result=submit_ok("vid-p"),
        query_script=[query_result("completed", "https://v.example/2.mp4")])
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        out = await run_ok(ns, persist_flag=True, seconds=4, aspect_ratio="9:16")
    meta_line = out.split("\n")[1]
    assert "url_expires=1h" in meta_line
    assert "persisted" not in meta_line
    rendered = access_records(caplog)[0].getMessage()
    assert "persist=true" in rendered
    assert "persist=skipped" in rendered


async def test_poll_interval_constant_across_polls():
    ns = make_pipeline(
        submit_result=submit_ok("vid-iv"),
        query_script=[query_result("running")] * 3
                     + [query_result("completed", "https://v.example/4.mp4")])
    result = await run_ok(ns)
    assert result.startswith("https://")
    assert ns.sleep.calls == [ns.cfg.video_poll_interval_sec] * 4  # 间隔恒定不漂移


# ---------------------------------------------------------------------------
# R15 分支二：超预算返 running（E17 非错误 + finally 释放）
# ---------------------------------------------------------------------------
async def test_run_deadline_expires_returns_running_non_error(caplog):
    # 全程 running；FakeClock 推进至 deadline → E17 行（非错误）
    ns = make_pipeline(submit_result=submit_ok("vid-e17"))
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        out = await run_ok(ns)

    assert [ord(c) for c in out[:7]] == ORD_META
    assert "task_id=vid-e17" in out
    assert "status=running" in out
    assert "已等待 200.0s" in out          # 文案含已等待秒数（总预算 200）
    assert "query_video" in out           # 提示稍后用 query_video 查询
    assert not out.startswith(ERROR_PREFIX)  # 非错误
    assert ns.pool.in_use() == 0           # R15：超预算路径同样释放

    entry = ns.table.get("vid-e17")        # 表内最近实况仍 running
    assert entry is not None and entry.status == "running"

    # V3：轮询交给后台作业后，作业**独立于请求**继续跑到自己的预算上限
    # （VIDEO_JOB_MAX_WAIT_SEC=1800s），所以"请求侧恰好轮询 N 轮"的断言
    # 不再成立——断言退化为"间隔恒定不漂移 + 至少轮询过"，轮数由作业
    # 预算决定（这正是"请求走了作业还在跑"的语义）。
    iv = ns.cfg.video_poll_interval_sec
    assert ns.sleep.calls, "后台作业应至少轮询一次"
    assert set(ns.sleep.calls) == {iv}      # D7：间隔恒定，不随轮次漂移
    assert len(ns.api.query_calls) == len(ns.sleep.calls)

    access = access_records(caplog)
    assert len(access) == 1
    assert "result=running" in access[0].getMessage()


async def test_run_poll_client_deadline_exception_returns_running():
    # 单查入口预算耗尽（客户端抛 DeadlineExceededError）→ 同为 E17 语义
    ns = make_pipeline(
        submit_result=submit_ok("vid-dl"),
        query_script=[budget_mod.DeadlineExceededError()])
    out = await run_ok(ns)
    assert [ord(c) for c in out[:7]] == ORD_META
    assert "task_id=vid-dl" in out
    assert "status=running" in out
    assert ns.pool.in_use() == 0


async def test_run_completed_without_url_treated_malformed():
    # completed 无 URL：上游契约破损 → 按未定义状态分类（upstream_error）
    ns = make_pipeline(
        submit_result=submit_ok("vid-nourl"),
        query_script=[query_result("completed", None)])
    out = await run_ok(ns)
    assert out.startswith(ERROR_PREFIX)
    assert "category=upstream_error" in out
    assert "task_id=vid-nourl" in out
    assert ns.pool.in_use() == 0


# ---------------------------------------------------------------------------
# R15 分支三：failed 分类（E19：唯一视频模型无切换）
# ---------------------------------------------------------------------------
async def test_run_failed_status_classified_error(caplog):
    ns = make_pipeline(
        submit_result=submit_ok("vid-f"),
        query_script=[query_result("running"), query_result("failed")])
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        out = await run_ok(ns)
    assert out.startswith(ERROR_PREFIX)
    assert "category=upstream_error" in out     # video_failed_error 映射
    assert "retry=yes" in out
    assert "视频生成失败" in out
    assert "task_id=vid-f" in out
    assert ns.table.get("vid-f").status == "failed"
    assert ns.pool.in_use() == 0                # R15
    assert "result=error:upstream_error" in access_records(caplog)[0].getMessage()


async def test_run_poll_http_error_classified():
    # 轮询查询 HTTP 429 → rate_limit 分类（E19：无切换，分类即返回）
    ns = make_pipeline(
        submit_result=submit_ok("vid-429"),
        query_script=[query_result("running"), api_error(429, "rate limited")])
    out = await run_ok(ns)
    assert out.startswith(ERROR_PREFIX)
    assert "category=rate_limit" in out
    assert "task_id=vid-429" in out            # detail 附 task_id 便于续查
    assert ns.pool.in_use() == 0


async def test_run_submit_error_no_task_registered():
    # 提交即失败（400 审核）：零轮询、零登记、槽释放
    ns = make_pipeline(submit_result=api_error(400, "content policy"))
    out = await run_ok(ns)
    assert out.startswith(ERROR_PREFIX)
    assert "category=content_policy" in out
    assert ns.sleep.calls == []                 # 未进轮询
    assert ns.table.get("vid-1") is None       # 未登记
    assert ns.pool.in_use() == 0


# ---------------------------------------------------------------------------
# 信号量：排队等待计入 deadline（E4）/ busy / 取消与护栏释放（R15）
# ---------------------------------------------------------------------------
async def test_queue_wait_counts_toward_deadline_e17():
    # MAXCONCURRENCY=1：排队 190s（FakeClock）计入 deadline → 拿槽后仅剩
    # 10s → 轮询 10/2=5 轮耗尽 → E17（200s = 190 排队 + 5×2 轮询，D7 间隔 2s）
    cfg = make_config(MAXCONCURRENCY=1)
    ns = make_pipeline(submit_result=submit_ok("vid-q"), cfg=cfg)
    async with ns.pool.slot(budget_mod.Deadline(50.0, clock=ns.clock), "holder"):
        task = asyncio.create_task(run_ok(ns))
        await asyncio.sleep(0.01)          # 让 run 到达排队点
        assert not task.done()              # 仍阻塞在队列中
        ns.clock.advance(190.0)            # 排队 190s：计入 deadline（E4）
    out = await asyncio.wait_for(task, timeout=2.0)
    assert [ord(c) for c in out[:7]] == ORD_META
    assert "task_id=vid-q" in out
    assert "已等待 200.0s" in out
    # V3：排队 190s 仍计入请求 deadline（E4 不变）；等待期不再占槽
    # （R14 作废），故 in_use_at_query 恒为 0
    iv = ns.cfg.video_poll_interval_sec
    assert set(ns.sleep.calls) == {iv}
    assert all(v == 0 for v in ns.api.in_use_at_query)  # V3：槽外等待
    assert ns.pool.in_use() == 0               # R15


async def test_queue_over_cap_returns_busy():
    # QUEUEWAIT_SEC=0.05：唯一槽被占 → busy（retry=yes），零上游调用
    cfg = make_config(MAXCONCURRENCY=1, QUEUEWAIT_SEC=0.05)
    ns = make_pipeline(submit_result=submit_ok(), cfg=cfg)
    async with ns.pool.slot(budget_mod.Deadline(50.0, clock=ns.clock), "holder"):
        task = asyncio.create_task(run_ok(ns))
        out = await asyncio.wait_for(task, timeout=2.0)
    assert out.startswith(ERROR_PREFIX)
    assert "category=busy" in out
    assert "retry=yes" in out
    assert ns.api.submit_calls == []
    assert ns.pool.in_use() == 0


async def test_cancelled_error_propagates_and_releases_slot():
    # 轮询 sleep 中途取消：CancelledError 传播且槽归还（R15 异常路径）
    hang = HangingSleeper()
    ns = make_pipeline(submit_result=submit_ok("vid-cancel"), sleep=hang)
    task = asyncio.create_task(run_ok(ns))
    await asyncio.wait_for(ns.api.submit_arrived.wait(), timeout=2.0)
    await asyncio.wait_for(hang.entered.wait(), timeout=2.0)   # 已入轮询 sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ns.pool.in_use() == 0


async def test_guard_timeout_defensive_path_returns_deferred(monkeypatch):
    # 整体护栏先响（理论不可达）：已提交 → 转 E17；槽随 with 块释放
    cfg = make_config()
    monkeypatch.setattr(
        vp_mod, "REQUEST_GUARD_SLACK_SEC", -(cfg.total_budget_sec) + 0.5)
    hang = HangingSleeper()
    ns = make_pipeline(submit_result=submit_ok("vid-guard"), cfg=cfg, sleep=hang)
    out = await run_ok(ns)   # guard = 0.5s 真实时间后在轮询 sleep 处截断
    assert [ord(c) for c in out[:7]] == ORD_META
    assert "task_id=vid-guard" in out
    assert "status=running" in out
    assert ns.pool.in_use() == 0


# ---------------------------------------------------------------------------
# 轮询间隔与单查超时嵌套（B6：单查 10 ≤ 提交 30 ≤ deadline 200）
# ---------------------------------------------------------------------------
def test_query_read_tier_nesting_pure():
    # 纯函数锁定：满预算 → 档位生效；剩余不足 → 按剩余钳制
    clock = FakeClock()
    deadline = budget_mod.Deadline(200.0, clock=clock)
    assert agnes_mod.compute_read_timeout(10.0, deadline) == 10.0
    clock.advance(197.0)
    assert agnes_mod.compute_read_timeout(10.0, deadline) == pytest.approx(3.0)


def test_deadline_passed_to_submit_and_query_is_request_scoped():
    # pipeline 传递的 deadline：同一对象、总预算 = TOTALBUDGETSEC
    ns = make_pipeline(
        submit_result=submit_ok("vid-d"),
        query_script=[query_result("completed", "https://v.example/5.mp4")])
    asyncio.run(run_ok(ns))
    submit_call = ns.api.submit_calls[0]
    query_call = ns.api.query_calls[0]
    # V3：提交用请求 deadline（总预算）；轮询交给后台作业，用作业自己的
    # 预算（VIDEO_JOB_MAX_WAIT_SEC）——这正是"请求返回了作业还在跑"的基础
    assert submit_call["deadline"].total == ns.cfg.total_budget_sec
    assert query_call["deadline"].total == ns.cfg.video_job_max_wait_sec


async def test_integration_real_client_submit_and_poll(scripted, cfg):
    # 真客户端 + 脚本 transport（零真实 API）：请求体契约（E21 seconds=str）
    # + read 档嵌套（提交 30 / 单查 10）+ 全链路输出
    def ok(json_body):
        return lambda req: httpx.Response(200, json=json_body, request=req)

    transport = scripted([
        ok({"video_id": "vid-int"}),
        ok({"status": "running"}),
        ok({"status": "completed", "url": "https://v.example/int.mp4"}),
    ])
    client = agnes_mod.AgnesClient(cfg, transport=transport, sleep=FakeSleeper())
    try:
        clock = FakeClock()
        table = tasks_mod.TaskTable(
            ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max, clock=clock)
        pool = budget_mod.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
        vp = VideoPipeline(cfg, pool, client, table,
                           clock=clock, sleep=AdvancingSleeper(clock))
        out = await vp.run(prompt="a cat", seconds=5, aspect_ratio="16:9",
                           persist_flag=False)
    finally:
        await client.aclose()

    lines = out.split("\n")
    assert lines[0] == "https://v.example/int.mp4"
    assert [ord(c) for c in lines[1][:7]] == ORD_META
    assert "seconds=5" in lines[1]

    r0, r1, r2 = transport.requests
    # 提交契约：POST /v1/videos；E21：seconds 以字符串入体
    assert r0.method == "POST"
    assert str(r0.url).endswith("/v1/videos")
    assert json.loads(r0.content) == {
        "model": "agnes-video-2.5-flash", "prompt": "a cat",
        "seconds": "5", "mode": "text", "size": "720P",
        "aspect_ratio": "16:9"}
    assert r0.extensions["timeout"]["read"] == pytest.approx(30.0)
    # 查询契约：GET /agnesapi?video_id=&model_name=；read 单查档 10
    for r in (r1, r2):
        assert r.method == "GET"
        assert r.url.path.endswith("/agnesapi")
        assert r.url.params["video_id"] == "vid-int"
        assert r.url.params["model_name"] == "agnes-video-2.5-flash"
        assert r.extensions["timeout"]["read"] == pytest.approx(10.0)
    entry = table.get("vid-int")
    assert entry.status == "completed"
    assert entry.url == "https://v.example/int.mp4"


# ---------------------------------------------------------------------------
# 任务表联动：TTL 过期 / query 各分支（E18 / E20）
# ---------------------------------------------------------------------------
async def test_task_ttl_expiry_query_returns_not_found():
    ns = make_pipeline(
        submit_result=submit_ok("vid-ttl"),
        query_script=[query_result("completed", "https://v.example/3.mp4")])
    out = await run_ok(ns)
    assert out.startswith("https://")
    assert ns.table.get("vid-ttl").status == "completed"

    ns.clock.advance(ns.cfg.task_ttl_sec + 1.0)   # 越过 TTL（7200s）
    out2 = await ns.vp.query(task_id="vid-ttl")
    assert out2.startswith(ERROR_PREFIX)
    assert "category=task_not_found" in out2
    assert "retry=fixthenretry" in out2


async def test_query_unknown_task_not_found():
    ns = make_pipeline(submit_result=submit_ok())
    out = await ns.vp.query(task_id="nope")
    assert out.startswith(ERROR_PREFIX)
    assert "category=task_not_found" in out
    assert "retry=fixthenretry" in out


@pytest.mark.parametrize("bad_id", ["", "   ", None])
async def test_query_empty_task_id_invalid_params(bad_id):
    ns = make_pipeline(submit_result=submit_ok())
    out = await ns.vp.query(task_id=bad_id)  # type: ignore[arg-type]
    assert out.startswith(ERROR_PREFIX)
    assert "category=invalid_params" in out
    assert "task_id" in out


async def test_query_completed_returns_url_and_updates_table():
    ns = make_pipeline(
        submit_result=submit_ok("vid-qc"),
        query_script=[query_result("completed", "https://v.example/q.mp4")])
    await run_ok(ns)                       # 登记表
    ns.api.query_script = [query_result("completed", "https://v.example/q2.mp4")]
    out = await ns.vp.query(task_id="vid-qc")
    assert out == "https://v.example/q2.mp4"    # completed → 仅 URL 行
    assert ns.table.get("vid-qc").url == "https://v.example/q2.mp4"  # 顺手 update


async def test_query_running_returns_status_line():
    ns = make_pipeline(submit_result=submit_ok())
    ns.table.register(task_id="vid-qr", model="agnes-video", prompt_hash="x")
    out = await ns.vp.query(task_id="vid-qr")    # 默认查询结果 running
    assert [ord(c) for c in out[:7]] == ORD_META
    assert "task_id=vid-qr" in out
    assert "status=running" in out
    assert "上游仍在生成" in out
    assert ns.table.get("vid-qr").status == "running"


async def test_query_failed_returns_classified_error():
    ns = make_pipeline(submit_result=submit_ok())
    ns.table.register(task_id="vid-qf", model="agnes-video", prompt_hash="x")
    ns.api.query_script = [query_result("failed")]
    out = await ns.vp.query(task_id="vid-qf")
    assert out.startswith(ERROR_PREFIX)
    assert "category=upstream_error" in out
    assert "视频生成失败" in out
    assert ns.table.get("vid-qf").status == "failed"


async def test_query_upstream_error_classified_no_table_corruption():
    # 查询请求本身失败（5xx）→ 分类错误返回；表内状态不被污染
    ns = make_pipeline(submit_result=submit_ok())
    ns.table.register(task_id="vid-qe", model="agnes-video", prompt_hash="x")
    ns.api.query_script = [api_error(500, "boom")]
    out = await ns.vp.query(task_id="vid-qe")
    assert out.startswith(ERROR_PREFIX)
    assert "category=upstream_error" in out
    assert ns.table.get("vid-qe").status == "running"


async def test_query_does_not_take_slot():
    # E20：查询路径不占并发槽——唯一槽被占住时 query 照常完成
    cfg = make_config(MAXCONCURRENCY=1)
    ns = make_pipeline(submit_result=submit_ok(), cfg=cfg)
    ns.table.register(task_id="vid-noslot", model="agnes-video", prompt_hash="x")
    ns.api.query_script = [query_result("completed", "https://v.example/ns.mp4")]
    async with ns.pool.slot(budget_mod.Deadline(50.0, clock=ns.clock), "holder"):
        out = await ns.vp.query(task_id="vid-noslot")
    assert out == "https://v.example/ns.mp4"
    assert ns.api.in_use_at_query == [1]    # 仅 holder 占槽，query 未加占
