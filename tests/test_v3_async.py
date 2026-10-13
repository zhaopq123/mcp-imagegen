"""V3 专项：多队列泳道 / 并发 / 视频后台异步（本轮性能改造的验收面）。

覆盖点（对应真实痛点，非为覆盖率而写）：
1. 泳道隔离：视频占满 video 槽时，生图仍能拿到 image 槽（V2 单一池
   3 个视频就能堵死全服务 200s —— 这是本轮要根治的头号问题）；
2. 排队深度：泳道积压超 IMAGE_QUEUE_MAX / VIDEO_QUEUE_MAX 立即 busy
   快失败，不让请求排到撞 deadline；
3. batch 并发：batch_size=N 拆成 N 个并发调用（并发峰值 > 1，且总耗时
   显著低于串行），保序回收、部分成功契约不变（R17）；
4. 视频后台异步：请求 deadline 到返 E17 task_id 后，后台作业继续跑到
   完成 —— query_video 能取到最终 URL（异步化的核心价值）；
5. 作业总览：list_jobs 反映在跟踪的作业状态。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as pipeline_mod  # noqa: E402
from app import video_pipeline as video_mod  # noqa: E402
from config import load_config  # noqa: E402
from engine import errors as errors_mod  # noqa: E402
from infra import agnes as agnes_mod  # noqa: E402
from infra import budget as budget_mod  # noqa: E402
from infra import circuit as circuit_mod  # noqa: E402
from infra import tasks as tasks_mod  # noqa: E402

BASE_ENV = {"MCPACCESS_TOKEN": "t", "AGNES_TOKEN": "a"}


def cfg_of(**over):
    env = dict(BASE_ENV)
    env.update({k.upper(): str(v) for k, v in over.items()})
    return load_config(env)


# ---------------------------------------------------------------------------
# 1. 泳道隔离
# ---------------------------------------------------------------------------
def test_lanes_are_independent():
    """video 泳道占满不影响 image 泳道（V2 痛点根治）。"""
    pool = budget_mod.MultiQueuePool({
        "image": budget_mod.QueueLane("image", 2, 30.0, 0),
        "video": budget_mod.QueueLane("video", 1, 30.0, 0),
    }, default="image")

    async def _run():
        # 占满唯一的 video 槽
        async with pool.lane("video").slot(budget_mod.Deadline(100.0), "v1"):
            # image 槽此时必须立刻可得（V2 会在这里卡到 deadline/busy）
            async with pool.lane("image").slot(budget_mod.Deadline(100.0), "i1"):
                assert pool.lane("image").in_use() == 1
            assert pool.lane("image").in_use() == 0

    asyncio.run(_run())


def test_unknown_lane_falls_back_to_default():
    pool = budget_mod.MultiQueuePool({
        "image": budget_mod.QueueLane("image", 1, 5.0, 0),
    }, default="image")
    assert pool.lane("nope").name == "image"


def test_lane_queue_full_fails_fast():
    """排队深度超限 → LaneFullError（不消耗等待预算）。"""
    lane = budget_mod.QueueLane("image", 1, 30.0, max_queued=1)

    async def _run():
        holder = budget_mod.Deadline(100.0)
        async with lane.slot(holder, "hold"):
            assert lane.waiting() == 0
            waiter = asyncio.ensure_future(
                _acquire(lane))          # 第 1 个排队者：允许
            await asyncio.sleep(0)
            assert lane.waiting() == 1
            with pytest.raises(budget_mod.LaneFullError):
                await _acquire_now(lane)  # 第 2 个：超限快失败
            waiter.cancel()

    async def _acquire(l):
        async with l.slot(budget_mod.Deadline(100.0), "w1"):
            await asyncio.sleep(0.05)

    async def _acquire_now(l):
        async with l.slot(budget_mod.Deadline(100.0), "w2"):
            pass

    asyncio.run(_run())


def test_stats_exposes_queue_pressure():
    pool = budget_mod.MultiQueuePool({
        "image": budget_mod.QueueLane("image", 3, 30.0, 8),
        "video": budget_mod.QueueLane("video", 2, 30.0, 4),
    })
    stats = pool.stats()
    assert stats["image"]["size"] == 3 and stats["image"]["in_use"] == 0
    assert stats["video"]["max_queued"] == 4


# ---------------------------------------------------------------------------
# 2. batch 并发（生图）
# ---------------------------------------------------------------------------
class ConcurrencyRecordingApi:
    """记录并发峰值的假生图客户端。"""

    def __init__(self, delay: float = 0.02, fail_index: "set[int] | None" = None):
        self.delay = delay
        self.fail_index = fail_index or set()
        self.now = 0
        self.peak = 0
        self.calls: list[int] = []
        self._seq = 0

    async def generate_image(self, *, model, prompt, size, deadline, ratio=None,
                             images=None):
        idx = self._seq
        self._seq += 1
        self.now += 1
        self.peak = max(self.peak, self.now)
        self.calls.append(idx)
        await asyncio.sleep(self.delay)
        self.now -= 1
        if idx in self.fail_index:
            return agnes_mod.ApiCallResult(
                ok=False, status_code=500, model_id="m",
                error_detail="boom")
        return agnes_mod.ApiCallResult(
            ok=True, status_code=200, model_id="m",
            urls=(f"https://img/{idx}.png",))


def make_pipeline(cfg, api, pool=None):
    pool = pool or budget_mod.MultiQueuePool(
        {"image": budget_mod.QueueLane(
            "image", cfg.image_max_concurrency, cfg.image_queue_wait_sec,
            cfg.image_queue_max)}, default="image")
    return pipeline_mod.GeneratePipeline(
        cfg, pool, circuit_mod.CircuitBreaker(600.0, 300.0), api, persist=None)


def test_batch_runs_concurrently():
    """batch=4 且并发度 4 → 并发峰值 > 1（V2 是严格串行）。"""
    cfg = cfg_of(IMAGE_BATCH_CONCURRENCY=4)
    api = ConcurrencyRecordingApi(delay=0.05)
    p = make_pipeline(cfg, api)
    out = asyncio.run(p.run(
        prompt="a cat", size="1024x1024", batch_size=4, persist_flag=False))
    lines = out.split("\n")
    assert len(lines) == 5                       # 4 URL + meta
    assert api.peak > 1, "batch 必须并发（V2 串行时 peak 恒为 1）"
    assert api.peak == 4
    # 保序：URL 顺序 = 发起序（并发不改变交付顺序）
    assert [l.rsplit("/", 1)[-1] for l in lines[:4]] == [
        "0.png", "1.png", "2.png", "3.png"]


def test_batch_concurrency_is_capped_by_config():
    """并发度受 IMAGE_BATCH_CONCURRENCY 约束（防把上游打爆）。"""
    cfg = cfg_of(IMAGE_BATCH_CONCURRENCY=2)
    api = ConcurrencyRecordingApi(delay=0.05)
    p = make_pipeline(cfg, api)
    asyncio.run(p.run(
        prompt="a cat", size="1024x1024", batch_size=4, persist_flag=False))
    assert api.peak == 2


def test_batch_partial_success_contract_kept():
    """R17 不变：M<N → meta 带 requested/delivered，且不是 error 行。"""
    cfg = cfg_of(IMAGE_BATCH_CONCURRENCY=4)
    api = ConcurrencyRecordingApi(delay=0.01, fail_index={2})
    p = make_pipeline(cfg, api)
    out = asyncio.run(p.run(
        prompt="a cat", size="1024x1024", batch_size=4, persist_flag=False))
    meta = out.split("\n")[-1]
    assert "requested=4" in meta and "delivered=3" in meta
    assert not out.startswith("[error]")


def test_batch_all_failed_is_error_not_partial():
    """M=0 → 错误路径（R17：不产生 0 交付的部分成功）。"""
    cfg = cfg_of(IMAGE_BATCH_CONCURRENCY=4)
    api = ConcurrencyRecordingApi(delay=0.01, fail_index={0, 1, 2, 3})
    p = make_pipeline(cfg, api)
    out = asyncio.run(p.run(
        prompt="a cat", size="1024x1024", batch_size=4, persist_flag=False))
    assert out.startswith("[error]")
    assert "category=upstream_error" in out


# ---------------------------------------------------------------------------
# 3. 视频后台异步
# ---------------------------------------------------------------------------
class ScriptedVideoApi:
    """脚本式视频客户端：提交即返回 video_id，查询按脚本推进。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.query_calls: list[str] = []
        self.submit_calls: list[dict] = []

    async def submit_video(self, *, model, prompt, seconds, aspect_ratio,
                           deadline, seed=None, mode="text", first_frame=None,
                           last_frame=None, reference_images=None):
        self.submit_calls.append({"prompt": prompt, "seconds": seconds})
        return agnes_mod.ApiCallResult(
            ok=True, status_code=200, model_id="m", video_id="vid-async")

    async def query_video(self, *, video_id, model, deadline):
        self.query_calls.append(video_id)
        item = self.script.pop(0) if self.script else {
            "status": "running"}
        if item["status"] == "completed":
            return agnes_mod.ApiCallResult(
                ok=True, status_code=200, model_id="m",
                video_status="completed", video_url=item.get("url"))
        return agnes_mod.ApiCallResult(
            ok=True, status_code=200, model_id="m",
            video_status=item["status"])


def make_video_pipeline(cfg, api, table=None):
    pool = budget_mod.MultiQueuePool({
        "video": budget_mod.QueueLane(
            "video", cfg.video_max_concurrency, cfg.video_queue_wait_sec,
            cfg.video_queue_max)}, default="video")
    table = table or tasks_mod.JobTable(ttl_sec=cfg.task_ttl_sec,
                                        max_entries=cfg.task_max)
    return video_mod.VideoPipeline(cfg, pool, api, table, persist=None), table


def test_video_wait_does_not_hold_lane_slot():
    """等待期不占槽（R14 作废）：等待中 video 泳道 in_use 归零。"""
    cfg = cfg_of(VIDEO_MAX_CONCURRENCY=1)
    api = ScriptedVideoApi([{"status": "running"}])
    vp, table = make_video_pipeline(cfg, api)

    async def _run():
        task = asyncio.ensure_future(vp.run(
            prompt="a cat", seconds=5, aspect_ratio="16:9",
            persist_flag=False))
        await asyncio.sleep(0.05)     # 提交完成，进入等待
        lane = vp._pool.lane("video")
        assert lane.in_use() == 0, "等待不是 IO，不该占并发额度"
        task.cancel()

    asyncio.run(_run())


def test_video_job_finishes_after_request_deferred():
    """E17 返回 task_id 后，后台作业继续跑完 → query_video 取到 URL。

    这是异步化的核心价值：V2 里请求一走轮询即终止，上游白跑。
    """
    # 轮询间隔压到 10ms，让后台作业在测试时间尺度内跑完
    cfg = cfg_of(VIDEO_MAX_CONCURRENCY=2, VIDEO_POLL_INTERVAL_SEC=0.01)
    # 前 3 次 running，第 4 次 completed（间隔 0.02s → 约 0.08s 后完成）
    # 前 20 次 running（约 0.2s）→ 取消必然发生在作业未完成阶段
    api = ScriptedVideoApi([{"status": "running"}] * 20
                           + [{"status": "completed",
                               "url": "https://v.example/async.mp4"}])
    vp, table = make_video_pipeline(cfg, api)

    async def _run():
        # 模拟"请求侧先走了"：客户端断连 / Agent 超时取消（V2 里请求一走
        # 轮询即终止，上游任务白跑；V3 后台作业不受影响）
        task = asyncio.ensure_future(vp.run(
            prompt="a cat", seconds=5, aspect_ratio="16:9",
            persist_flag=False))
        await asyncio.sleep(0.05)          # 提交已完成，进入等待
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # 后台作业继续轮询 → 最终 completed，query 能取到 URL
        for _ in range(100):
            await asyncio.sleep(0.02)
            q = await vp.query(task_id="vid-async")
            if "async.mp4" in q:
                return q
        pytest.fail("后台作业未在请求返回后继续跑完")

    result = asyncio.run(_run())
    assert "https://v.example/async.mp4" in result


def test_video_job_status_tracked_in_table():
    cfg = cfg_of()
    api = ScriptedVideoApi([{"status": "running"}])
    vp, table = make_video_pipeline(cfg, api)

    async def _run():
        task = asyncio.ensure_future(vp.run(
            prompt="a cat", seconds=5, aspect_ratio="16:9",
            persist_flag=False))
        await asyncio.sleep(0.05)
        jobs = vp.list_jobs()
        assert jobs and jobs[0]["task_id"] == "vid-async"
        assert jobs[0]["status"] == "running"
        assert isinstance(table.get_job("vid-async"), tasks_mod.JobEntry)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_cancel_background_stops_workers():
    cfg = cfg_of()
    api = ScriptedVideoApi([{"status": "running"}])
    vp, _ = make_video_pipeline(cfg, api)

    async def _run():
        task = asyncio.ensure_future(vp.run(
            prompt="a cat", seconds=5, aspect_ratio="16:9",
            persist_flag=False))
        await asyncio.sleep(0.05)
        assert vp.cancel_background() >= 1
        assert vp._bg == {}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_video_error_category_preserved_from_worker():
    """后台归类的 429 不会被压成 upstream_error（契约不退化）。"""
    cfg = cfg_of()

    class RateLimitedApi(ScriptedVideoApi):
        async def query_video(self, *, video_id, model, deadline):
            await asyncio.sleep(0)
            return agnes_mod.ApiCallResult(
                ok=False, status_code=429, model_id="m",
                error_detail="rate limited")

    api = RateLimitedApi([])
    vp, _ = make_video_pipeline(cfg, api)
    out = asyncio.run(vp.run(
        prompt="a cat", seconds=5, aspect_ratio="16:9", persist_flag=False))
    assert "category=rate_limit" in out, out


# ---------------------------------------------------------------------------
# 4. 配置自检（V3 新增项）
# ---------------------------------------------------------------------------
def test_v3_config_defaults():
    cfg = cfg_of()
    assert cfg.image_max_concurrency >= 1
    assert cfg.video_max_concurrency >= 1
    assert cfg.image_queue_wait_sec <= cfg.total_budget_sec
    assert 1 <= cfg.image_batch_concurrency <= pipeline_mod.BATCH_MAX
    assert cfg.video_job_max_wait_sec > 0
    assert cfg.video_job_max >= 1


def test_v3_config_rejects_batch_concurrency_out_of_range():
    from config import ConfigError

    with pytest.raises(ConfigError):
        cfg_of(IMAGE_BATCH_CONCURRENCY=9)


def test_v3_config_rejects_queue_wait_above_budget():
    from config import ConfigError

    with pytest.raises(ConfigError):
        cfg_of(IMAGE_QUEUE_WAIT_SEC=400)
