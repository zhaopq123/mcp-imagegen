"""R1 / R3 / E4 / E5 门禁：预算体系（Deadline + 并发槽）。

R1（最高危）：release 必须封装在 finally 内部——异常/取消/BaseException
路径槽必须归还；验收㉑：连续 10 次中途异常，第 11 次仍能拿到槽。
R3：deadline 单调递减、贯穿传递、耗尽后不再发起 IO。
E4：排队等待耗时计入 deadline。
E5：一个请求一个槽（batch 是 API 参数不是新槽）。
"""
from __future__ import annotations

import asyncio

import pytest

from infra import budget


def make_deadline(total=200.0):
    return budget.Deadline(total)


# ---------------------------------------------------------------------------
# Deadline：单调递减 + 耗尽检查（R3）
# ---------------------------------------------------------------------------
def test_deadline_remaining_decreases(clock):
    d = budget.Deadline(200.0, clock=clock)
    assert d.remaining() == pytest.approx(200.0)
    clock.advance(10.0)
    assert d.remaining() == pytest.approx(190.0)
    clock.advance(190.0)
    assert d.remaining() == pytest.approx(0.0)
    assert d.expired()


def test_deadline_check_raises_when_exhausted(clock):
    d = budget.Deadline(100.0, clock=clock)
    clock.advance(100.1)
    with pytest.raises(budget.DeadlineExceededError):
        d.check()


def test_deadline_check_returns_remaining_when_alive(clock):
    d = budget.Deadline(100.0, clock=clock)
    clock.advance(30.0)
    assert d.check() == pytest.approx(70.0)


def test_deadline_rejects_nonpositive_total():
    with pytest.raises(ValueError):
        budget.Deadline(0.0)


# ---------------------------------------------------------------------------
# 并发槽：正常获取/释放（E5：一请求一槽）
# ---------------------------------------------------------------------------
async def test_slot_acquire_release_cycle(cfg):
    pool = budget.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
    d = make_deadline()
    async with pool.slot(d, "req-1"):
        assert pool.in_use() == 1
    assert pool.in_use() == 0


async def test_slot_exhaustion_then_timeout_busy():
    """占满 3 槽后第 4 个等待超限 → busy（等待上限，测试用 50ms）。"""
    pool = budget.SlotPool(3, 0.05)
    d1, d2, d3 = make_deadline(), make_deadline(), make_deadline()
    async with pool.slot(d1, "r0"), pool.slot(d2, "r1"), pool.slot(d3, "r2"):
        assert pool.in_use() == 3
        with pytest.raises(budget.SlotBusyError):
            async with pool.slot(make_deadline(), "r3"):
                pass
    assert pool.in_use() == 0


# ---------------------------------------------------------------------------
# R1：异常路径槽必须归还（本项目的最高危根因）
# ---------------------------------------------------------------------------
async def test_slot_released_on_exception():
    pool = budget.SlotPool(3, 0.05)
    with pytest.raises(RuntimeError):
        async with pool.slot(make_deadline(), "req-x"):
            raise RuntimeError("boom")
    assert pool.in_use() == 0


async def test_slot_released_on_cancellation():
    """CancelledError（BaseException 子类）路径同样归还。"""
    pool = budget.SlotPool(3, 0.05)

    async def hold_then_cancel():
        async with pool.slot(make_deadline(), "req-cancel"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hold_then_cancel()
    await asyncio.sleep(0)
    assert pool.in_use() == 0


async def test_slot_released_on_base_exception():
    class WeirdBase(BaseException):
        pass

    pool = budget.SlotPool(3, 0.05)
    with pytest.raises(WeirdBase):
        async with pool.slot(make_deadline(), "req-base"):
            raise WeirdBase()
    assert pool.in_use() == 0


async def test_ten_failures_then_eleventh_gets_slot():
    """验收㉑：连续 10 次中途异常，第 11 次立即拿槽（无泄漏）。"""
    pool = budget.SlotPool(3, 0.05)
    for i in range(10):
        with pytest.raises(RuntimeError):
            async with pool.slot(make_deadline(), f"req-{i}"):
                raise RuntimeError(f"fail-{i}")
    # 第 11 次：无需等待（若泄漏则要等满 3+ 槽，0.5s 内拿不到会超时）
    async with asyncio.timeout(0.5):
        async with pool.slot(make_deadline(), "req-11"):
            assert pool.in_use() == 1


# ---------------------------------------------------------------------------
# E4：排队等待耗时计入 deadline
# ---------------------------------------------------------------------------
async def test_queue_wait_counts_into_deadline():
    """等待 ~60ms 释放后，排队者的 deadline 剩余必须相应减少（E4）。"""
    pool = budget.SlotPool(1, 30.0)

    async def waiter():
        d = budget.Deadline(200.0)
        async with pool.slot(d, "waiter"):
            return d.remaining()

    async with pool.slot(make_deadline(), "holder"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.06)  # waiter 正在排队
    # 持槽者已释放 → waiter 获槽
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result <= 200.0 - 0.06  # 排队 ≥60ms 已从预算中扣除


async def test_deadline_expires_during_wait_raises_timeout_not_busy():
    """排队期间 deadline 耗尽 → 超时错误（而非 busy），且槽不泄漏。"""
    pool = budget.SlotPool(1, 30.0)  # 等待上限 30s
    async with pool.slot(make_deadline(), "holder"):
        # 排队者预算仅 40ms；持槽 80ms：等待中预算耗尽
        task = asyncio.create_task(_acquire_with_tiny_deadline(pool, 0.04))
        result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, budget.DeadlineExceededError)
    assert pool.in_use() == 0  # 未成功入槽的等待不泄漏


async def _acquire_with_tiny_deadline(pool, tiny):
    d = budget.Deadline(tiny)
    try:
        async with pool.slot(d, "tiny"):
            return None
    except budget.BudgetError as exc:
        return exc


# ---------------------------------------------------------------------------
# R3：deadline 耗尽 → 不占用槽、不发起 IO
# ---------------------------------------------------------------------------
async def test_acquire_with_expired_deadline_fails_fast():
    """预算已耗尽的请求：拿到槽也立即归还并转超时（不进入业务）。"""
    pool = budget.SlotPool(3, 30.0)
    with pytest.raises(budget.DeadlineExceededError):
        async with pool.slot(_ExpiredDeadline(), "expired"):
            pass
    assert pool.in_use() == 0


class _ExpiredDeadline(budget.Deadline):
    def __init__(self):
        super().__init__(1.0)

    def remaining(self) -> float:
        return -1.0

    def check(self) -> float:
        raise budget.DeadlineExceededError("expired")


# ---------------------------------------------------------------------------
# 日志对账（R1.3：acquire/release 打含 req_id 的 debug 日志）
# ---------------------------------------------------------------------------
async def test_slot_logs_req_id(caplog):
    import logging

    pool = budget.SlotPool(3, 30.0)
    with caplog.at_level(logging.DEBUG, logger="mcp_imagegen.budget"):
        async with pool.slot(make_deadline(), "req-log-42"):
            pass
    combined = caplog.text
    assert "req-log-42" in combined
    assert "acquired" in combined and "released" in combined
