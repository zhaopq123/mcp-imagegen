"""预算体系：Deadline 对象 + 并发槽（规格书 3.3、五节；红线 R1/R3、E4/E5）。

职责（设计方案 2.2）：
- Deadline 剩余计算（单调递减）、耗尽检查；
- 并发信号量槽：acquire 等待上限 QUEUEWAIT_SEC（计入 deadline，E4），
  到点返 busy；release 封装在 context manager 的 finally 内部，
  调用方在语法上无法绕过（R1：本项目最高危根因）。

不负责：分档上限数值（来自 config，经 api_tier_timeout 查询）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger("mcp_imagegen.budget")


class BudgetError(Exception):
    """预算体系异常基类（pipeline 层转为对应 [error] 行）。"""


class DeadlineExceededError(BudgetError):
    """deadline 耗尽（200s 总预算）→ 立即转超时错误，不再发起任何 IO。"""


class SlotBusyError(BudgetError):
    """并发槽等待超限 → busy（retry=yes，稍后重调）。"""

    def __init__(self, waited_sec: float):
        self.waited_sec = waited_sec
        super().__init__(f"concurrency slots busy after waiting {waited_sec:.3f}s")


class Deadline:
    """200s 总预算：入口创建，作为参数传入每一个异步阶段（R3：贯穿传递）。"""

    __slots__ = ("_total", "_start", "_clock")

    def __init__(self, total_sec: float, clock: Callable[[], float] = time.monotonic):
        if total_sec <= 0:
            raise ValueError(f"deadline total_sec 必须 > 0，收到 {total_sec}")
        self._total = total_sec
        self._start = clock()
        self._clock = clock

    @property
    def total(self) -> float:
        return self._total

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        """单调递减的剩余预算（受 monotonic clock 保证）。"""
        return self._total - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self) -> float:
        """阶段入口检查：耗尽 → DeadlineExceededError（不再发起 IO）。返回剩余值。"""
        remaining = self.remaining()
        if remaining <= 0.0:
            raise DeadlineExceededError(
                f"deadline exhausted after {self.elapsed():.3f}s (budget={self._total:g}s)"
            )
        return remaining


class SlotPool:
    """并发槽（MAXCONCURRENCY=3；一个请求一槽，E5；batch 是单次 API 调用参数）。

    用法（调用方无法绕过 release，R1）：

        async with pool.slot(deadline, req_id) as slot:
            await call_api(...)
    """

    def __init__(self, size: int, queue_wait_sec: float):
        if size < 1:
            raise ValueError(f"slot pool size 必须 ≥ 1，收到 {size}")
        if queue_wait_sec <= 0:
            raise ValueError(f"queue_wait_sec 必须 > 0，收到 {queue_wait_sec}")
        self._sem = asyncio.Semaphore(size)
        self._wait_sec = queue_wait_sec
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    def in_use(self) -> int:
        """当前占用槽数（巡检/对账日志用；借 asyncio.Semaphore 内部计数）。"""
        return self._size - self._sem._value

    def slot(self, deadline: Deadline, req_id: str = "-") -> "_SlotLease":
        """返回 async context manager；等待上限 30s 且计入 deadline（E4）。"""
        return _SlotLease(self, deadline, req_id)


class _SlotLease:
    """async with 语法保证 release 必达（含 BaseException / CancelledError，R1）。"""

    __slots__ = ("_pool", "_deadline", "_req_id", "_acquired")

    def __init__(self, pool: SlotPool, deadline: Deadline, req_id: str):
        self._pool = pool
        self._deadline = deadline
        self._req_id = req_id
        self._acquired = False

    async def __aenter__(self) -> "_SlotLease":
        # 等待上限 = min(队列等待上限, 剩余预算)——排队耗时计入 deadline（E4）
        wait_cap = min(self._pool._wait_sec, max(self._deadline.remaining(), 0.0))
        try:
            await asyncio.wait_for(self._pool._sem.acquire(), timeout=wait_cap)
        except asyncio.TimeoutError:
            # 区分：deadline 先耗尽 → 超时错误；否则等待上限到点 → busy
            if self._deadline.expired():
                raise DeadlineExceededError(
                    "deadline exhausted while waiting for a concurrency slot"
                ) from None
            logger.debug("slot wait timeout req_id=%s waited=%.3fs", self._req_id, wait_cap)
            raise SlotBusyError(wait_cap) from None
        self._acquired = True
        logger.debug(
            "slot acquired req_id=%s remaining=%.3fs",
            self._req_id,
            self._deadline.remaining(),
        )
        # 排队期间预算可能已耗尽：拿到槽但不再发起 IO，立即归还并转超时错误
        try:
            self._deadline.check()
        except DeadlineExceededError:
            self._release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # finally 语义：任何异常（含 CancelledError / BaseException）都归还
        self._release()
        return False  # 不吞异常

    def _release(self) -> None:
        if self._acquired:
            self._acquired = False
            self._pool._sem.release()
            logger.debug("slot released req_id=%s", self._req_id)
