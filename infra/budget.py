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


class LaneFullError(BudgetError):
    """泳道排队深度超限 → busy（retry=yes，与 SlotBusyError 同一契约）。

    V3：多队列改造后每条泳道独立限深——队列积压超过容量即快失败，
    不让请求无限堆积（堆积只会让所有人都撞 deadline）。
    """

    def __init__(self, lane: str, depth: int, max_queued: int):
        self.lane = lane
        self.depth = depth
        self.max_queued = max_queued
        super().__init__(
            f"queue lane {lane!r} full (queued={depth}, max={max_queued})"
        )


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


# ---------------------------------------------------------------------------
# V3：多队列泳道（生图 / 视频分离，互不饿死）
# ---------------------------------------------------------------------------
class QueueLane:
    """单条队列泳道：并发槽 + 排队深度上限（FIFO，R2 禁线程 → 无锁）。

    与 SlotPool 的差别（V3 增量）：
    - 有名（image / video），可被 MultiQueuePool 分片调度；
    - 排队深度可限（max_queued）：积压超限立即 LaneFullError 快失败，
      避免"排队排到所有人都撞 deadline"的假排队；
    - 暴露 waiting/in_use 统计（/healthz 与巡检用）。

    max_queued=0 表示不限深（等价于老 SlotPool 行为）。
    """

    __slots__ = ("_name", "_sem", "_wait_sec", "_size", "_max_queued", "_waiting")

    def __init__(
        self, name: str, size: int, wait_sec: float, max_queued: int = 0,
    ) -> None:
        if size < 1:
            raise ValueError(f"lane {name!r} size 必须 ≥ 1，收到 {size}")
        if wait_sec <= 0:
            raise ValueError(f"lane {name!r} wait_sec 必须 > 0，收到 {wait_sec}")
        if max_queued < 0:
            raise ValueError(f"lane {name!r} max_queued 必须 ≥ 0，收到 {max_queued}")
        self._name = name
        self._sem = asyncio.Semaphore(size)
        self._wait_sec = wait_sec
        self._size = size
        self._max_queued = max_queued
        self._waiting = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        return self._size

    def in_use(self) -> int:
        """当前占用槽数。"""
        return self._size - self._sem._value

    def waiting(self) -> int:
        """当前排队（已登记、未拿到槽）数。"""
        return self._waiting

    def stats(self) -> dict[str, int]:
        return {
            "size": self._size,
            "in_use": self.in_use(),
            "waiting": self._waiting,
            "max_queued": self._max_queued,
        }

    def slot(self, deadline: Deadline, req_id: str = "-") -> "_LaneLease":
        """返回 async context manager；等待上限与排队深度双重约束。"""
        return _LaneLease(self, deadline, req_id)

    def _enter_queue(self, req_id: str) -> None:
        """登记排队；超限即快失败（不占用等待预算）。"""
        if self._max_queued and self._waiting >= self._max_queued:
            logger.warning(
                "[queue] lane=%s 排队深度 %d 已达上限 %d，快失败 req_id=%s",
                self._name, self._waiting, self._max_queued, req_id,
            )
            raise LaneFullError(self._name, self._waiting, self._max_queued)
        self._waiting += 1

    def _leave_queue(self) -> None:
        if self._waiting > 0:
            self._waiting -= 1


class _LaneLease:
    """泳道槽租约：async with 语法保证 release 必达（R1）。"""

    __slots__ = ("_lane", "_deadline", "_req_id", "_acquired", "_queued")

    def __init__(self, lane: QueueLane, deadline: Deadline, req_id: str) -> None:
        self._lane = lane
        self._deadline = deadline
        self._req_id = req_id
        self._acquired = False
        self._queued = False

    async def __aenter__(self) -> "_LaneLease":
        self._lane._enter_queue(self._req_id)
        self._queued = True
        wait_cap = min(
            self._lane._wait_sec, max(self._deadline.remaining(), 0.0))
        try:
            await asyncio.wait_for(self._lane._sem.acquire(), timeout=wait_cap)
        except asyncio.TimeoutError:
            if self._deadline.expired():
                raise DeadlineExceededError(
                    "deadline exhausted while waiting for a concurrency slot"
                ) from None
            logger.debug(
                "lane wait timeout lane=%s req_id=%s waited=%.3fs",
                self._lane._name, self._req_id, wait_cap,
            )
            raise SlotBusyError(wait_cap) from None
        except BaseException:
            raise
        finally:
            # 无论拿到槽 / 超时 / 取消：退出排队登记（R1 同类纪律）
            if self._queued:
                self._queued = False
                self._lane._leave_queue()
        self._acquired = True
        logger.debug(
            "lane slot acquired lane=%s req_id=%s remaining=%.3fs",
            self._lane._name, self._req_id, self._deadline.remaining(),
        )
        try:
            self._deadline.check()
        except DeadlineExceededError:
            self._release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self._acquired:
            self._acquired = False
            self._lane._sem.release()
            logger.debug(
                "lane slot released lane=%s req_id=%s",
                self._lane._name, self._req_id,
            )


class MultiQueuePool:
    """多队列池：按泳道名分流（V3 核心）。

    设计要点：
    - 泳道独立额度 → 视频长任务不会吃光生图额度（V2 单一池的真实痛点：
      3 个视频请求占满 3 个槽轮询 200s，生图全 busy）；
    - 未知泳道名一律回落 default（防新增类型时 KeyError 打死请求）；
    - 统计快照供 /healthz 与 logline 用。
    """

    def __init__(self, lanes: "dict[str, QueueLane]", default: str = "image") -> None:
        if not lanes:
            raise ValueError("MultiQueuePool 至少需要一条泳道")
        if default not in lanes:
            raise ValueError(f"default 泳道 {default!r} 不在 lanes 中")
        self._lanes = dict(lanes)
        self._default = default

    @property
    def lanes(self) -> "dict[str, QueueLane]":
        return self._lanes

    def lane(self, name: str) -> QueueLane:
        return self._lanes.get(name) or self._lanes[self._default]

    def stats(self) -> "dict[str, dict[str, int]]":
        return {name: lane.stats() for name, lane in self._lanes.items()}
