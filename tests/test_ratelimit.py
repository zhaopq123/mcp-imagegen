"""V3：上游速率限制器（压测驱动的稳定性改造）。

实证背景（2026-10-13 压测）：并发放开到 8 张图同时打上游免费额度 →
立刻 429 → 硬熔断 600s，整段窗口内请求全废。故宁可自己排队。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra import ratelimit  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class Sleeper:
    """推进假时钟的 sleep（不真实等待）。"""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)
        self.clock.advance(sec)


def make(rps=1.0, burst=2, cooldown=15.0):
    clock = FakeClock()
    sleeper = Sleeper(clock)
    return ratelimit.RateLimiter(
        rps=rps, burst=burst, cooldown_sec=cooldown,
        clock=clock, sleep=sleeper), clock, sleeper


def test_burst_consumed_without_waiting():
    rl, clock, _ = make(rps=1.0, burst=2)

    async def _run():
        assert await rl.acquire(wait_cap_sec=30) == 0.0
        assert await rl.acquire(wait_cap_sec=30) == 0.0
        assert rl.available() == pytest.approx(0.0)

    asyncio.run(_run())


def test_beyond_burst_waits_for_refill():
    rl, clock, sleeper = make(rps=2.0, burst=1)

    async def _run():
        await rl.acquire(wait_cap_sec=30)      # 消耗唯一令牌
        waited = await rl.acquire(wait_cap_sec=30)  # 需等 0.5s（1/2 rps）
        assert waited > 0
        assert sleeper.calls, "应当等待令牌补充"

    asyncio.run(_run())


def test_penalty_window_blocks_requests():
    """429/503 → 冻结窗口，后续请求排队（不再硬闯）。"""
    rl, clock, sleeper = make(rps=10.0, burst=5, cooldown=15.0)

    async def _run():
        rl.penalize()
        assert rl.blocked() == pytest.approx(15.0)
        waited = await rl.acquire(wait_cap_sec=30)
        assert waited >= 15.0
        assert rl.blocked() == 0.0
        assert rl.stats["penalties"] == 1

    asyncio.run(_run())


def test_penalize_uses_retry_after_when_provided():
    rl, clock, _ = make(cooldown=15.0)
    rl.penalize(3.0)
    assert rl.blocked() == pytest.approx(3.0)


def test_wait_cap_exceeded_raises_timeout():
    """等不起 → TimeoutError（不占着槽空转；调用方转超时/繁忙语义）。"""
    rl, clock, _ = make(rps=0.01, burst=0 if False else 1)

    async def _run():
        await rl.acquire(wait_cap_sec=1.0)      # 用掉令牌
        with pytest.raises(asyncio.TimeoutError):
            await rl.acquire(wait_cap_sec=1.0)  # 需要 100s，cap 只有 1s

    asyncio.run(_run())


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        ratelimit.RateLimiter(rps=0)
    with pytest.raises(ValueError):
        ratelimit.RateLimiter(rps=1.0, burst=0)


def test_no_thread_constructs():
    """R2：限速器不得引入线程（文档里也不许出现可执行线程构造）。"""
    src = (Path(__file__).resolve().parents[1] / "infra" / "ratelimit.py"
           ).read_text(encoding="utf-8")
    assert "import threading" not in src
    assert "asyncio.to_thread" not in src
