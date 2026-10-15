"""V4 代排队生命周期专项：429 纳入重试 + 排队窗口兜底 + 鉴权不排队。

背景（生产实测，2026-10-13 凌晨）：
真实链路首次打通时，提交撞上游 429（免费额度 RPM 限制），作业在 35s 内被
判终态失败——一次限流就把作业判死，等于把等待成本甩回给 Agent 盲试，与
「提交一次，好了来拿」的产品目标相悖。修复：429 与队列满（503）同样进入
代排队退避重试（尊重 Retry-After），排队窗口 VIDEO_SUBMIT_QUEUE_WAIT_SEC
兜底；401/403/审核类错误仍然立即失败——排队永远等不来好结果。

全部 mock 上游；时钟走 FakeClock，窗口耗尽场景零真实等待。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import FakeClock, make_config

import app.video_pipeline as vp_mod
from app.video_pipeline import VideoPipeline
from providers import gateway as gateway_mod
from infra import budget as budget_mod
from infra import tasks as tasks_mod

ERROR_PREFIX = chr(91) + "error] "
META_PREFIX = chr(91) + "meta] "


def submit_ok(video_id: str = "vid-ok") -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=True, status_code=200, provider_id="video", model_id="video",
        video_id=video_id)


def rate_limited(retry_after: float | None = 30.0) -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=False, status_code=429, provider_id="video", model_id="video",
        retry_after=retry_after, error_detail="rate limit exceeded")


def queue_full() -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=False, status_code=503, provider_id="video", model_id="video",
        error_detail="video_queue_full: 队列已满")


def auth_denied() -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=False, status_code=401, provider_id="video", model_id="video",
        error_detail="invalid token")


class ScriptedSubmitApi:
    """按脚本逐次返回提交结果；脚本耗尽后重复最后一项。"""

    def __init__(self, submit_script, query_result=None, query_script=None):
        self.submit_script = list(submit_script)
        self.query_script = list(query_script or [])
        self.submit_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.query_result = query_result

    async def submit_video(self, *, model=None, prompt, seconds, aspect_ratio,
                           deadline, seed=None, mode="text", first_frame=None,
                           last_frame=None, reference_images=None, provider=None):
        self.submit_calls.append({"mode": mode, "first_frame": first_frame})
        if len(self.submit_script) > 1:
            item = self.submit_script.pop(0)
        else:
            item = self.submit_script[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def query_video(self, *, video_id, model=None, deadline, provider=None):
        self.query_calls.append({"video_id": video_id})
        if self.query_script:
            # 脚本耗尽后沿用最后一条（避免 pop 空栈把作业推成异常路径）
            return (self.query_script.pop(0) if len(self.query_script) > 1
                    else self.query_script[0])
        if self.query_result is None:
            return gateway_mod.ApiCallResult(
                ok=True, status_code=200, model_id=model,
                video_status="completed", video_url="http://x/v.mp4")
        return self.query_result


class AdvancingSleeper:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)
        self.clock.advance(sec)


def make_ns(submit_script, query_result=None, cfg=None, query_script=None):
    cfg = cfg or make_config()
    clock = FakeClock()
    # JobTable（生产同款）：V4 作业语义（error_reason / future）在 JobTable
    table = tasks_mod.JobTable(
        ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max, clock=clock)
    pool = budget_mod.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
    api = ScriptedSubmitApi(submit_script, query_result, query_script)
    sleep = AdvancingSleeper(clock)
    vp = VideoPipeline(cfg, pool, api, table, clock=clock, sleep=sleep)
    return SimpleNamespace(vp=vp, pool=pool, table=table, clock=clock,
                           sleep=sleep, api=api, cfg=cfg)


async def run_default(ns):
    return await ns.vp.run(prompt="a cat", seconds=5, aspect_ratio="16:9",
                           persist_flag=False)


async def wait_workers(ns):
    pending = [t for t in list(ns.vp._bg.values()) if not t.done()]
    if pending:
        await asyncio.gather(*pending)


# --------------------------------------------------------------- 正向：429 排队

async def test_submit_429_returns_queued_contract_not_error():
    """首次提交撞 429 → 返回排队契约行（非错误），作业进后台代排队。"""
    ns = make_ns([rate_limited(retry_after=30.0), submit_ok()])
    out = await run_default(ns)
    assert out.startswith(META_PREFIX)
    assert "status=queued" in out
    assert "task_id=q-" in out
    # 撞限流不应该把错误甩给调用方
    assert not out.startswith(ERROR_PREFIX)
    await wait_workers(ns)


async def test_worker_retries_429_then_submit_succeeds():
    """429 → 按 Retry-After 退避 → 重试 → 第三次提交成功 → 转轮询到 completed。"""
    ns = make_ns([rate_limited(retry_after=30.0), rate_limited(retry_after=30.0),
                  submit_ok("vid-after-429")],
                 query_result=gateway_mod.ApiCallResult(
                     ok=True, status_code=200, model_id="m",
                     video_status="completed", video_url="http://x/v.mp4"))
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)

    # 主路径 1 次 + drain 重试 2 次
    assert len(ns.api.submit_calls) == 3
    # drain 里那次 429 按 Retry-After=30 退避（而非立即重试或固定间隔）
    assert ns.sleep.calls.count(30.0) == 1
    entry = ns.table.get(task_id)
    assert entry.status == tasks_mod.STATUS_COMPLETED
    assert entry.upstream_id == "vid-after-429"   # 轮询打上游 id，不是 q- 前缀


async def test_mixed_queue_full_and_429_all_retry():
    """503 队列满与 429 混合出现（10-12 实录形态）→ 全部重试不放弃。"""
    ns = make_ns([queue_full(), rate_limited(retry_after=20.0), submit_ok()])
    out = await run_default(ns)
    assert "status=queued" in out
    await wait_workers(ns)
    assert len(ns.api.submit_calls) == 3


async def test_persistent_429_exhausts_window_and_aborts():
    """持续 429（如日额度耗尽）→ 排队窗口耗尽 → abort（明确原因，非静默）。"""
    ns = make_ns([rate_limited(retry_after=300.0)])
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)

    entry = ns.table.get(task_id)
    # abort 不写 failed（E17 哲学：请求侧已 deferred 成 running，上游可能
    # 仍在跑）；但 error_reason 必须可自愈——说清是排队窗口耗尽
    assert "代排队超时" in (entry.error_reason or "")
    assert "限流" in (entry.error_reason or "")
    assert entry.status != tasks_mod.STATUS_FAILED
    # 多次重试发生过（不是一撞就死）
    assert ns.api.submit_calls, "应至少尝试过一次提交"
    # 排队窗口被封顶：累计等待不超过窗口上限 + 最后一次 sleep
    assert sum(ns.sleep.calls) <= ns.cfg.video_submit_queue_wait_sec + 300.0 + 1.0


# --------------------------------------------------------------- 反向：不排队

async def test_auth_error_at_first_submit_fails_immediately():
    """401 在首次提交就出现 → 直接错误契约，不产生排队号。"""
    ns = make_ns([auth_denied()])
    out = await run_default(ns)
    assert out.startswith(ERROR_PREFIX)
    assert "task_id=q-" not in out
    assert ns.api.submit_calls.__len__() == 1     # 只试一次，不重试
    assert ns.vp._bg == {}                        # 没有后台作业


async def test_auth_error_during_drain_fails_without_more_retries():
    """排队中撞 401 → 立即失败（排队等不来好结果）。"""
    ns = make_ns([queue_full(), auth_denied()])
    out = await run_default(ns)
    assert "status=queued" in out
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)

    entry = ns.table.get(task_id)
    assert entry.status == tasks_mod.STATUS_FAILED
    assert len(ns.api.submit_calls) == 2          # 503 重试一次后 401 判死

# ------------------------------------------------- 排队期/终止后的查询语义

async def test_query_during_queueing_never_hits_upstream():
    """worker 还在代排队时 query → 纯本地状态行，绝不拿排队号打上游。

    生产实录 bug：排队作业被拿去打上游 → 404「任务不存在」，
    可自愈的排队状态变成假故障。
    """
    ns = make_ns([rate_limited(retry_after=300.0)])
    out = await run_default(ns)
    assert "status=queued" in out
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    # 此刻 worker 必然还在 drain 退避 sleep 中（retry_after=300s）
    body = await ns.vp.query(task_id=task_id)
    assert ns.api.query_calls == []               # 零上游 IO
    assert "404" not in body
    assert "status=queued" in body
    # 清理：取消后台 worker 防止悬挂
    ns.vp.cancel_background()


async def test_query_after_drain_timeout_reports_reason_not_upstream_404():
    """代排队窗口耗尽 → worker 退出 → query 必须回终止原因，非 404。"""
    ns = make_ns([rate_limited(retry_after=300.0)])
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)

    body = await ns.vp.query(task_id=task_id)
    assert ns.api.query_calls == []               # 从未打上游（404 不可能）
    assert "代排队超时" in body
    assert "rate_limit" in body                   # 终止类别如实透传（429 所致）


async def test_query_queued_with_error_reason_is_error_not_silent_wait():
    """终止后的作业再查 → error 契约行（retry=yes），Agent 可据此决策。"""
    ns = make_ns([rate_limited(retry_after=300.0)])
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)
    body = await ns.vp.query(task_id=task_id)
    assert body.startswith(ERROR_PREFIX)
    assert "retry=yes" in body


# --------------------------------------------------------------- 判定函数

def test_is_retryable_submit_covers_429_and_queue_full_only():
    """可重试集合 = {429} ∪ {503+队列满文案}；鉴权/审核/其他 5xx 不在内。"""
    f = VideoPipeline._is_retryable_submit
    assert f(rate_limited()) is True
    assert f(rate_limited(retry_after=None)) is True
    assert f(queue_full()) is True
    assert f(auth_denied()) is False
    assert f(gateway_mod.ApiCallResult(
        ok=False, status_code=500, error_detail="internal")) is False
    # 503 但不是队列满（如网关维护）→ 不拖进排队
    assert f(gateway_mod.ApiCallResult(
        ok=False, status_code=503, error_detail="gateway maintenance")) is False


def test_is_queue_full_requires_503_with_queue_marker():
    f = VideoPipeline._is_queue_full
    assert f(queue_full()) is True
    assert f(gateway_mod.ApiCallResult(
        ok=False, status_code=503, error_detail="busy")) is False
    assert f(gateway_mod.ApiCallResult(
        ok=False, status_code=429, error_detail="video_queue_full")) is False


async def test_query_while_queueing_says_queueing_not_generating():
    """仍卡在本端代排队（无上游 id）时，query 不能回「上游仍在生成」。

    生产实测（2026-10-15）：上游免费额度持续限流，作业提交 8 次仍未成功，
    而 query_video 回的是「上游仍在生成，请稍后再次查询」——把「还没提交
    上去」说成「已在生成」，Agent 会误判成"等着就行"，白白干等。
    """
    ns = make_ns([queue_full()])
    out = await run_default(ns)
    assert "status=queued" in out
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")

    q = await ns.vp.query(task_id=task_id)
    assert q.startswith(META_PREFIX)
    assert "status=queued" in q
    assert "代排队" in q
    assert "上游仍在生成" not in q          # 本次修复的核心断言

    # 尝试次数要透出：分辨「刚排上」与「排很久了」
    ns.table.get(task_id).submit_attempts = 8
    q2 = await ns.vp.query(task_id=task_id)
    assert "已尝试提交 8 次" in q2
    await wait_workers(ns)


def test_format_video_queueing_wording_and_attempts():
    from app import contract as contract_mod

    line = contract_mod.format_video_queueing("q-abc", attempts=8)
    assert line.startswith(META_PREFIX)
    assert "task_id=q-abc" in line and "status=queued" in line
    assert "已尝试提交 8 次" in line
    # 没尝试过就不该编造次数
    assert "已尝试提交" not in contract_mod.format_video_queueing("q-abc")


# ---------------------------------------------------------------------------
# 轮询阶段的上游限流（2026-10-15 真机抓到的真 bug）
# ---------------------------------------------------------------------------
def completed(url: str = "http://x/v.mp4") -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=True, status_code=200, provider_id="video", model_id="video",
        video_status="completed", video_url=url)


def running() -> gateway_mod.ApiCallResult:
    return gateway_mod.ApiCallResult(
        ok=True, status_code=200, provider_id="video", model_id="video",
        video_status="running")


async def test_poll_429_does_not_kill_the_job():
    """轮询撞上游 429 → 退避续轮，**绝不能判成终态失败**。

    真机实测（2026-10-15）：上游查询接口有自己的速率限制，2s 间隔连查两次
    就回 429「查询过于频繁」，作业被 `_mark_fail` 判死后再也不查——而上游
    任务 4 分钟后就 completed 了，成片永久漏收（"轮询器静默漏收"又一真因）。
    """
    ns = make_ns([submit_ok("vid-1")],
                 query_script=[rate_limited(30.0), running(), completed()])
    out = await run_default(ns)
    await wait_workers(ns)
    # 修复前：第一次 429 就把作业判死，调用方拿到 [error]。
    # 修复后：退避续轮 → running → completed，调用方直接拿到成片地址。
    assert "http://x/v.mp4" in out and not out.startswith(ERROR_PREFIX)
    task_id = next(iter(ns.table._entries))
    entry = ns.table.get(task_id)
    assert entry.status == tasks_mod.STATUS_COMPLETED
    assert entry.url == "http://x/v.mp4"
    # 三次查询都真的发生了（没在第一次 429 就退出）
    assert len(ns.api.query_calls) == 3


async def test_poll_429_backoff_grows_and_respects_retry_after():
    """退避取 Retry-After（上游给了就照办），无 Retry-After 时指数增长且封顶。"""
    ns = make_ns([submit_ok("vid-2")],
                 query_script=[rate_limited(7.0), rate_limited(None),
                               rate_limited(None), completed()])
    await run_default(ns)
    await wait_workers(ns)
    interval = ns.cfg.video_poll_interval_sec
    # 首片是常规轮询间隔；随后 429 带 Retry-After 就照办；再往后指数退避
    assert ns.sleep.calls[:4] == [pytest.approx(interval), pytest.approx(7.0),
                                  pytest.approx(interval * 2),
                                  pytest.approx(interval * 4)]
    assert max(ns.sleep.calls) <= vp_mod._POLL_BACKOFF_MAX_SEC


async def test_poll_429_until_budget_exhausted_aborts_not_fails():
    """一直 429 到总等待超限 → **abort（保持 running）**，不是 FAILED。

    保持 running 才能让后续 query_video 走实况查询把成片补回来（E17：
    上游任务不受本端影响）；写 FAILED 就等于把可自愈状态钉死成故障。
    """
    cfg = make_config(video_job_max_wait_sec=60.0)
    ns = make_ns([submit_ok("vid-3")], cfg=cfg,
                 query_script=[rate_limited(None)])
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)
    entry = ns.table.get(task_id)
    assert entry.status != tasks_mod.STATUS_FAILED
    assert entry.status == tasks_mod.STATUS_RUNNING
    assert "后台轮询总等待超限" in (entry.error_reason or "")


async def test_poll_non_retryable_error_still_fails_fast():
    """非可重试类（如 403 额度不足）仍应立即判死，不进退避循环。"""
    quota = gateway_mod.ApiCallResult(
        ok=False, status_code=403, provider_id="video", model_id="video",
        error_detail="用户额度不足，剩余额度: ￥0.000000")
    ns = make_ns([submit_ok("vid-4")], query_script=[quota])
    out = await run_default(ns)
    task_id = out.split("task_id=")[1].split()[0].strip("；;, ")
    await wait_workers(ns)
    entry = ns.table.get(task_id)
    assert entry.status == tasks_mod.STATUS_FAILED
    assert entry.error_category == "insufficient_quota"
    assert len(ns.api.query_calls) == 1
