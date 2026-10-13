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
# V4：本端排队中（上游队列满，服务端代为排队重试提交）——尚未提交成功，
# 与 running（上游已受理、生成中）语义严格区分，Agent 不会误判进度。
STATUS_QUEUED = "queued"

# V3：后台作业终态（视频异步化后，作业生命周期可跨请求——
# 请求 deadline 到了作业仍在跑，由后台 worker 继续轮询到终态）
JOB_RESULT_OK = "ok"
JOB_RESULT_FAILED = "failed"
JOB_RESULT_TIMEOUT = "timeout"

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


@dataclass
class JobEntry(TaskEntry):
    """视频后台作业条目（V3）：TaskEntry + 完成通知 + 结果载荷。

    future 语义：由后台 worker 在到达终态（completed/failed/超时）时
    set_result；请求侧在自身 deadline 内 await 它（不占并发槽）。
    future 只 set 一次（worker 内做 done() 判断，防重复 set 抛错）。
    """

    future: Optional[asyncio.Future] = None
    final_url: Optional[str] = None     # persist 后的自托管 URL（未 persist 即上游 URL）
    persist_status: Optional[str] = None
    error_reason: Optional[str] = None  # failed 的原因（契约行 detail）
    error_category: Optional[str] = None  # V3：后台已归类的类别（防请求侧二次分类）
    elapsed_sec: float = 0.0
    poll_count: int = 0
    seconds: Optional[int] = None
    aspect_ratio: Optional[str] = None
    seed: Optional[int] = None
    # ---- V4：代排队与生成模式 ----
    # task_id 始终是**本端标识**（排队号）；upstream_id 是提交成功后拿到的
    # 上游 video_id。二者解耦后，「排队中」也能给用户一个可续查的 task_id。
    upstream_id: Optional[str] = None
    mode: Optional[str] = None           # text / keyframe / reference
    ref_count: int = 0                   # 本次使用的参考图/首尾帧张数
    submit_attempts: int = 0             # 代排队提交尝试次数（排障用）
    # 提交所需参数（后台 worker 排队重试时要复用，故随作业携带）
    prompt: Optional[str] = None
    first_frame: Optional[str] = None
    last_frame: Optional[str] = None
    reference_images: Optional[tuple] = None


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


# ---------------------------------------------------------------------------
# V3：视频后台作业表（JobTable）
# ---------------------------------------------------------------------------
class JobTable(TaskTable):
    """视频异步作业表：TaskTable + 完成通知（future）+ 结果载荷。

    与 TaskTable 的关系（避免死代码与双头实现）：
    - 继承复用 TTL / 容量逐出 / 周期清理（同一契约 R16：过期即
      task_not_found）；
    - 仅新增"作业"语义：future 通知 + persist 结果 + 后台轮询计数；
    - register_job 兼容父类 register 的签名（旧的 query 路径仍可 update/get）。

    并发模型同父类：单事件循环内串行访问，无锁（R2）。
    """

    def register_job(
        self, task_id: str, *, model: str, prompt_hash: str,
        seconds: Optional[int] = None, aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
        status: str = STATUS_RUNNING,
        upstream_id: Optional[str] = None,
        mode: Optional[str] = None,
        ref_count: int = 0,
        prompt: Optional[str] = None,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Optional[tuple] = None,
    ) -> JobEntry:
        """登记作业并建完成通知 future；超限逐出同父类契约。

        V4：status 可为 STATUS_QUEUED（本端排队中，尚未提交成功）；
        提交所需参数随作业携带，供后台 worker 排队重试复用。
        """
        entry = JobEntry(
            task_id=task_id,
            model=model,
            prompt_hash=prompt_hash,
            status=status,
            submitted_at=self._clock(),
            seconds=seconds,
            aspect_ratio=aspect_ratio,
            seed=seed,
            upstream_id=upstream_id,
            mode=mode,
            ref_count=ref_count,
            prompt=prompt,
            first_frame=first_frame,
            last_frame=last_frame,
            reference_images=reference_images,
        )
        loop = asyncio.get_event_loop()
        entry.future = loop.create_future()
        self._entries[task_id] = entry
        self._evict_over_capacity()
        return entry

    def get_job(self, task_id: str) -> Optional[JobEntry]:
        """取作业（TTL 过期 → None，同 R16 契约）。"""
        entry = self.get(task_id)
        if isinstance(entry, JobEntry):
            return entry
        return None

    def complete(
        self, task_id: str, *, url: Optional[str], elapsed_sec: float,
        persist_status: Optional[str] = None, final_url: Optional[str] = None,
    ) -> Optional[JobEntry]:
        """作业成功终态：写表 + 通知等待方（future 只 set 一次）。"""
        entry = self.update(
            task_id, status=STATUS_COMPLETED, url=url)
        if not isinstance(entry, JobEntry):
            return None
        entry.final_url = final_url or url
        entry.persist_status = persist_status
        entry.elapsed_sec = elapsed_sec
        self._notify(entry, JOB_RESULT_OK)
        return entry

    def fail(
        self, task_id: str, *, reason: str, elapsed_sec: float,
    ) -> Optional[JobEntry]:
        """作业失败终态：写表 + 通知等待方。"""
        entry = self.update(task_id, status=STATUS_FAILED)
        if not isinstance(entry, JobEntry):
            return None
        entry.error_reason = reason
        entry.elapsed_sec = elapsed_sec
        self._notify(entry, JOB_RESULT_FAILED)
        return entry

    def abort(self, task_id: str, *, reason: str, elapsed_sec: float = 0.0) -> None:
        """作业未达终态即终止（后台轮询超时 / 停机取消）→ 通知等待方。

        注意：不写 STATUS_FAILED——上游任务可能仍在跑，本端只是停止跟踪
        （E17 哲学：上游不受本端影响）。等待方收到 JOB_RESULT_TIMEOUT，
        请求侧据此返回 deferred（task_id + status=running）。
        """
        entry = self._entries.get(task_id)
        if not isinstance(entry, JobEntry):
            return
        entry.elapsed_sec = elapsed_sec
        entry.error_reason = reason
        self._notify(entry, JOB_RESULT_TIMEOUT)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _notify(entry: JobEntry, result: str) -> None:
        if entry.future is not None and not entry.future.done():
            entry.future.set_result(result)
