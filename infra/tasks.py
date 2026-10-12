"""视频任务表（V2 增量设计方案 2.3；T4）。

query_video 对账状态源：纯内存 dict，不落盘（重启丢失 = 上游任务
不受影响，Agent 端表现为 task_not_found，重学代价 1 次提交，E18 哲学）。

- TTL 过期视为未命中（TASK_TTL_SEC=7200，2h 对齐上游视频生成时长上限）；
- 容量超限（TASK_MAX=100）按 submitted_at 逐出最旧——**含未完成**：
  上游任务照常完成，仅本端对账记录消失（表现为 task_not_found，
  与 TTL 过期同一契约，R16）；
- cleanup_loop 为事件循环协程（asyncio.sleep 驱动，禁线程 R2；
  tests/test_no_threads.py 静态门禁自动覆盖新文件）。

并发模型：单事件循环内串行访问，无锁（R2 禁线程 → 无跨线程竞争面；
协程切换点仅在 cleanup_loop 的 sleep 处，dict 操作原子）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("mcp_imagegen.tasks")

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# 周期清理间隔（方案 2.3：与心跳同为事件循环 sleep 驱动；TTL 7200s
# 量级下 60s 足够稀疏，且 get 命中路径已做单条惰性清除兜底）
CLEANUP_INTERVAL_SEC = 60.0


@dataclass
class TaskEntry:
    """任务表条目（submitted_at 为单调时钟，与 TTL 比较基准一致）。"""

    task_id: str
    model: str
    prompt_hash: str
    status: str = STATUS_RUNNING
    url: Optional[str] = None
    submitted_at: float = 0.0


class TaskTable:
    """内存任务表：register / update / get + TTL + 容量逐出 + 周期清理。"""

    def __init__(
        self,
        *,
        ttl_sec: float = 7200.0,
        max_entries: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError(f"ttl_sec 必须 > 0，收到 {ttl_sec!r}")
        if max_entries < 1:
            raise ValueError(f"max_entries 必须 ≥ 1，收到 {max_entries!r}")
        self._ttl = ttl_sec
        self._max = max_entries
        self._clock = clock
        self._entries: dict[str, TaskEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # 增删改查
    # ------------------------------------------------------------------
    def register(self, task_id: str, *, model: str, prompt_hash: str) -> TaskEntry:
        """登记任务（status=running）；同 task_id 重复登记视为覆盖（重置状态与时刻）。"""
        entry = TaskEntry(
            task_id=task_id,
            model=model,
            prompt_hash=prompt_hash,
            status=STATUS_RUNNING,
            submitted_at=self._clock(),
        )
        self._entries[task_id] = entry
        self._evict_over_capacity()
        return entry

    def update(self, task_id: str, *, status: str, url: Optional[str] = None) -> Optional[TaskEntry]:
        """更新状态；未登记 / TTL 过期 → None（过期条目顺手清除）。"""
        entry = self._entries.get(task_id)
        if entry is None:
            return None
        if self._expired(entry):
            del self._entries[task_id]
            return None
        entry.status = status
        entry.url = url
        return entry

    def get(self, task_id: str) -> Optional[TaskEntry]:
        """查询条目；TTL 过期视为未命中（R16：与逐出同一 task_not_found 契约）。"""
        entry = self._entries.get(task_id)
        if entry is None:
            return None
        if self._expired(entry):
            del self._entries[task_id]  # 惰性单条清除，等价 cleanup 局部执行
            return None
        return entry

    # ------------------------------------------------------------------
    # 清理（事件循环协程，禁线程）
    # ------------------------------------------------------------------
    def cleanup(self) -> int:
        """单次清除全部过期条目，返回清除数（cleanup_loop 与测试共用）。"""
        expired = [k for k, e in self._entries.items() if self._expired(e)]
        for k in expired:
            del self._entries[k]
        if expired:
            logger.info("[tasks] TTL 清理：%d 条过期任务清除（剩余 %d）", len(expired), len(self._entries))
        return len(expired)

    async def cleanup_loop(
        self,
        interval_sec: float = CLEANUP_INTERVAL_SEC,
        *,
        sleep: Optional[Callable[[float], "asyncio.Future[None]"]] = None,
    ) -> None:
        """周期清理协程：每 interval_sec 清一次过期条目。

        由 server 主循环 create_task 挂载，停机 cancel 即退出
        （CancelledError 自 sleep 点传播，无吞没）；sleep 可注入便于测试。
        """
        sleeper = sleep or asyncio.sleep
        while True:
            await sleeper(interval_sec)
            self.cleanup()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _expired(self, entry: TaskEntry) -> bool:
        """TTL 边界：elapsed ≥ ttl 即过期（含恰好等于——宁早逐出，契约一致）。"""
        return (self._clock() - entry.submitted_at) >= self._ttl

    def _evict_over_capacity(self) -> None:
        """容量超限 → 按 submitted_at 逐出最旧（含未完成；同刻并列时按插入序稳定逐出）。"""
        overflow = len(self._entries) - self._max
        if overflow <= 0:
            return
        victims = sorted(self._entries.values(), key=lambda e: e.submitted_at)[:overflow]
        for victim in victims:
            del self._entries[victim.task_id]
        logger.warning(
            "[tasks] 容量 %d 超限：按 submitted_at 逐出最旧 %d 条（含未完成，"
            "上游任务不受影响，本端表现为 task_not_found，R16）",
            self._max, overflow,
        )
