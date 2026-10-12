"""视频流水线（V3 异步化版：设计方案 2.2 T7a 提交 / T7b query 入口）。

V3 核心改造（多队列 + 并发 + 异步，本轮主题）——
    V2 的真实痛点：视频 run 在**并发槽内**轮询到 deadline（R14），
    槽池只有 3 个且生图共用——3 个视频请求就能把服务堵死 200s，期间
    所有生图请求全部 busy（这是本轮要根治的头号性能问题）。
    V3 把"等待"从"占槽"里摘出来：

        提交（占槽，IO ~秒级）→ 注册后台作业 → **立即释放槽**
        → 请求侧在自身剩余预算内 await 作业 future（不占槽）
        → deadline 到仍未完成 → E17 契约返 task_id（客户端续查）

    R14（轮询期持续占槽）由此作废：等待不是 IO，不该占额度。
    对外契约零变化（A2′ 冻结描述逐字成立）：请求仍在预算内同步等待。


串联（run = generate_video 工具后端）：
    参数校验（prompt 必填、seconds 整数 4–12、aspect_ratio 官方六档
    16:9/9:16/1:1/4:3/3:4/21:9（D4）、seed 整数可选直传上游（D3/
    官方 F5）、persist 布尔——范围校验在 pipeline 层而非 schema 层，
    A2）→
    并发槽（与生图共用 MAXCONCURRENCY 池：一请求一槽 E5；排队等待
    计入 deadline E4；轮询等待期持续占槽 R14；async with 语义保证
    finally 释放，超预算返 running 路径同样释放 R15）→
    Agnes 提交（read ≤ 提交档 30s）得 video_id → 任务表 register
    （task_id 即 video_id）→ 轮询循环（每轮先 sleep(
    VIDEO_POLL_INTERVAL_SEC) 再单查上游，单查 read ≤10s，嵌套
    10 ≤ 30 ≤ deadline，B6）：
        failed    → 错误分类返回（E19：唯一视频模型无切换）；
        completed → 成功收尾（meta 含 model/seconds/aspect_ratio/
                    elapsed；不 persist → url_expires 短效警示 E13）；
        deadline 到仍 running → meta 行 task_id+status=running 非错误
                    （E17：文案含已等待秒数与 query_video 提示；槽随
                    with 块退出自然释放）。

query 入口（T7b；工具注册留 T8）：
    task_id 非空校验 → 查任务表：未命中/TTL 过期/容量逐出 →
    task_not_found（fixthenretry，E18/R16）；命中 → 实时打上游（单查
    read ≤10s，不占并发槽，E20）→ completed 返 URL / 非终态返 meta
    状态行 / failed 返分类 error 行。命中后顺手 update——表内 status
    永远是最近一次上游实况（含 run 轮询路径的每次 update）。

persist（T7c 已落地）：persist=true → 调用 persist_video 下载落盘
VIDEO_DIR（read = min(PERSIST_TIMEOUT_VIDEO_SEC=120, deadline 剩余)，
计入 deadline，仍在并发槽内）→ 成功返 /files 完整 URL + meta persisted；
失败/预算不足 → C6/R5 降级返原上游 URL + meta persist_failed（persist
层永不报错）；http_headers 透传至 persist_video（/files URL 绝对化，Q3）。
logline 15 字段：视频请求 task_id=<video_id>（提交后回填）+ poll_count=
<轮询次数>；split_calls 恒 null（生图专属）。

熔断：设计 2.2 视频流水线无熔断门、无失败写入（唯一模型一次定生死，
E19 语义）——本轮与 CircuitBreaker 零交互。

护栏（6.1）：asyncio.timeout(TOTALBUDGETSEC + SLACK) 整体兜底；护栏
先响（理论不可达）时若已提交转 E17 语义、未提交转超时错误。

后台 worker（V3 新增）：
    提交后由 asyncio.create_task 挂到事件循环（R2：禁线程，仍是协程），
    受 VIDEO_JOB_CONCURRENCY 闸门限并发轮询，总等待上限
    VIDEO_JOB_MAX_WAIT_SEC（默认 1800s，远超单请求 deadline —— 请求走了
    作业还在跑，客户端凭 task_id 续查）；completed 且 persist=true →
    **后台自动下载落盘**（VIDEO_DIR）→ 写终态 + future 通知。
    停机时 cancel_background() 只停本端跟踪（上游任务照常跑，E17 哲学）。

query 入口（T7b）：
    task_id 非空 → 查作业表（未命中/TTL 过期/容量逐出 → task_not_found，
    E18/R16）→ **有活跃后台 worker 时返回本端实况（零上游 IO，E20 不占
    槽；V3 快路径）**，否则退回 V2 行为实时打一次上游（兼容路径）。

日志（R13）：run 与 query 每请求恰好一行访问日志；result 枚举含
"running"（E17 非错误收尾）；stages = validate/queue/submit/wait。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Mapping, Optional

from app import contract as contract_mod
from config import REQUEST_GUARD_SLACK_SEC, Config
from engine import errors as errors_mod
from infra import agnes as agnes_mod
from infra import budget as budget_mod
from infra import persist as persist_mod
from infra import tasks as tasks_mod
from ops import logline as logline_mod
from ops import shutdown as shutdown_mod

logger = logging.getLogger("mcp_imagegen.video_pipeline")

VIDEO_MODEL = "agnes-video"  # 别名（对齐生图侧 agnes-image 用法，E15）
SECONDS_MIN = 4              # seconds 值域 [4, 12]（闭区间）
SECONDS_MAX = 12
# D4（官方视频画幅表，2026-10-10 定案）：三档扩六档——
# 16:9(1280x704) / 9:16(720x1280) / 1:1(720x720) / 4:3(960x720) /
# 3:4(720x960) / 21:9(1680x720)
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

# V3：视频泳道名（多队列调度；与生图 image 泳道隔离，互不饿死）
LANE_VIDEO = "video"

SleepFn = Callable[[float], Awaitable[None]]


def validate_video_params(
    prompt, seconds, aspect_ratio, persist, seed,
) -> "tuple[()] | tuple[str, str]":
    """语义校验（返回 () 或 (param_name, 原因)）。

    schema（T8）已保证类型；此处校验 schema 之外的语义范围，保证越界也
    走 error 行 invalid_params 契约（对齐生图 validate_params / A2）。
    D3（官方 F5）：seed 恢复——integer 可选直传，官方未标注值域，
    不臆测上限（本地仅类型校验，超界由上游 400 兜底透传）；
    D4（官方画幅表）：aspect_ratio 扩六档 16:9/9:16/1:1/4:3/3:4/21:9。
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return ("prompt", "不能为空")
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        return ("seconds", f"需为整数（{SECONDS_MIN}–{SECONDS_MAX}）")
    if not SECONDS_MIN <= seconds <= SECONDS_MAX:
        return ("seconds", f"超出 {SECONDS_MIN}–{SECONDS_MAX} 区间，收到 {seconds!r}")
    if aspect_ratio not in ASPECT_RATIOS:
        return ("aspect_ratio", f"限 {'/'.join(ASPECT_RATIOS)} 六档，收到 {aspect_ratio!r}")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        return ("seed", "需为整数（可选；直传上游）")
    if not isinstance(persist, bool):
        return ("persist", "需为布尔值")
    return ()


class _StageTimer:
    """各阶段耗时（stages 字段来源；monotonic；对齐生图 pipeline 同名类）。"""

    __slots__ = ("_marks", "_last")

    def __init__(self) -> None:
        self._marks: list[tuple[str, float]] = []
        self._last = time.monotonic()

    def mark(self, stage: str) -> None:
        now = time.monotonic()
        self._marks.append((stage, now - self._last))
        self._last = now

    def render(self) -> str:
        return ";".join(f"{name}={elapsed:.3f}s" for name, elapsed in self._marks)

    def total_str(self) -> str:
        return f"{sum(elapsed for _, elapsed in self._marks):.3f}s"


@dataclass
class _RunState:
    """单次 run 的跨阶段状态（护栏兜底路径需感知 task_id）。"""

    task_id: Optional[str] = None


class VideoPipeline:
    """视频请求管道：run（生成）+ query（续查）；永不抛错（CancelledError 除外）。"""

    def __init__(
        self,
        cfg: Config,
        pool: budget_mod.SlotPool,
        api_client: agnes_mod.AgnesClient,
        task_table: tasks_mod.TaskTable,
        tracker: Optional[shutdown_mod.ShutdownCoordinator] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[SleepFn] = None,
        persist: Optional[persist_mod.PersistService] = None,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._api = api_client
        self._table = task_table
        self._tracker = tracker
        self._clock = clock or time.monotonic  # deadline 时钟（测试注入 FakeClock）
        self._sleep: SleepFn = sleep or asyncio.sleep  # 轮询间隔（测试注入假身）
        # T7c：persist 服务（与生图共用同一实例/下载池；None = 组件未注入，
        # persist=True 视同 skipped，独立组件测试用）
        self._persist = persist
        # ---- V3：后台作业（视频异步化）----
        self._job_gate = asyncio.Semaphore(
            max(1, int(getattr(cfg, "video_job_concurrency", 8))))
        self._job_max_wait = float(
            getattr(cfg, "video_job_max_wait_sec", 1800.0))
        self._job_max = max(1, int(getattr(cfg, "video_job_max", 24)))
        self._bg: dict[str, asyncio.Task] = {}  # task_id → 后台 worker

    # ------------------------------------------------------------------
    # 入口一：generate_video
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        prompt: str,
        seconds: int,
        aspect_ratio: str,
        persist_flag: bool,
        seed: Optional[int] = None,
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """单请求入口：返回契约文本（成功 URL+meta / E17 running / error 行）。

        D3：seed 可选（None 不注入请求体；官方未标注值域，直传上游）。
        """
        req_id = uuid.uuid4().hex[:12]
        guard = self._cfg.total_budget_sec + REQUEST_GUARD_SLACK_SEC
        state = _RunState()
        try:
            async with asyncio.timeout(guard):
                return await self._execute(
                    req_id=req_id, state=state, prompt=prompt,
                    seconds=seconds, aspect_ratio=aspect_ratio,
                    persist_flag=persist_flag, seed=seed,
                    http_headers=http_headers,
                )
        except TimeoutError:
            # 护栏触发（deadline 检查应先到；此为 6.1 防御纵深）——
            # 已提交 → 转 E17 语义（上游任务照常跑，Agent 凭 task_id 续查）；
            # 未提交 → 超时错误。
            rec = self._new_record(
                req_id, prompt, seconds, aspect_ratio, persist_flag, seed)
            rec.timeout_hit = logline_mod.TIMEOUT_GUARD
            rec.total = f"{guard:.1f}s"
            if state.task_id:
                rec.model = VIDEO_MODEL
                rec.result = "running"
                rec.emit()
                return contract_mod.format_video_deferred(state.task_id, guard)
            rec.result = "error:" + errors_mod.UPSTREAM_ERROR
            rec.emit()
            return contract_mod.format_error(errors_mod.deadline_exhausted(guard))
        except asyncio.CancelledError:
            raise  # 客户端断连/停机取消：绝不吞（R15 槽归还由 finally 保证）

    # ------------------------------------------------------------------
    # 入口二：query_video（工具注册留 T8）
    # ------------------------------------------------------------------
    async def query(self, *, task_id: str) -> str:
        """续查入口：不占并发槽（E20）；completed 返 URL / 非终态返状态行。"""
        req_id = uuid.uuid4().hex[:12]
        guard = self._cfg.total_budget_sec + REQUEST_GUARD_SLACK_SEC
        rec = logline_mod.LogRecord(
            req_id=req_id,
            prompt_hash="null",  # 查询无 prompt
            params=f"model={VIDEO_MODEL};task_id={task_id}",
        )
        try:
            async with asyncio.timeout(guard):
                return await self._query(req_id=req_id, rec=rec, task_id=task_id)
        except TimeoutError:
            rec.timeout_hit = logline_mod.TIMEOUT_GUARD
            rec.model = VIDEO_MODEL
            rec.result = "error:" + errors_mod.UPSTREAM_ERROR
            rec.total = f"{guard:.1f}s"
            rec.emit()
            return contract_mod.format_error(errors_mod.deadline_exhausted(guard))
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # run 内部：校验 + 并发槽
    # ------------------------------------------------------------------
    async def _execute(
        self, *, req_id: str, state: _RunState, prompt: str, seconds: int,
        aspect_ratio: str, persist_flag: bool, seed: Optional[int],
        http_headers: Optional[Mapping[str, str]],
    ) -> str:
        rec = self._new_record(
            req_id, prompt, seconds, aspect_ratio, persist_flag, seed)
        timer = _StageTimer()
        deadline = budget_mod.Deadline(self._cfg.total_budget_sec, clock=self._clock)
        if self._tracker is not None:
            self._tracker.enter()  # 停机排空计数（对齐生图；视频在途 +1）
        try:
            # ---- 1. 参数语义校验（越界也走 error 行 invalid_params）----
            bad = validate_video_params(
                prompt, seconds, aspect_ratio, persist_flag, seed)
            timer.mark("validate")
            if bad:
                param, detail = bad
                return self._finish_error(rec, timer, errors_mod.invalid_params(param, detail))

            # ---- 2. video 泳道槽：只覆盖"提交"这段 IO（V3 关键改动）----
            submitted: "tuple[str, str | None]" = ("exhausted", None)
            try:
                async with self._acquire_slot(deadline, req_id):
                    timer.mark("queue")
                    submitted = await self._submit(
                        rec=rec, timer=timer, deadline=deadline,
                        prompt=prompt, seconds=seconds,
                        aspect_ratio=aspect_ratio, seed=seed,
                    )
            except budget_mod.LaneFullError as exc:
                # V3：泳道排队超限 → busy 快失败（不消耗等待预算）
                timer.mark("queue")
                rec.timeout_hit = logline_mod.TIMEOUT_QUEUE_WAIT
                return self._finish_error(
                    rec, timer,
                    errors_mod.queue_full_error(exc.lane, exc.depth, exc.max_queued))
            except budget_mod.SlotBusyError as exc:
                timer.mark("queue")
                rec.timeout_hit = logline_mod.TIMEOUT_QUEUE_WAIT
                return self._finish_error(rec, timer, errors_mod.busy_error(exc.waited_sec))
            except budget_mod.DeadlineExceededError:
                # 排队耗尽预算（拿槽后检查即抛）→ 快失败，未发起任何 IO
                timer.mark("queue")
                rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
                return self._finish_error(
                    rec, timer, errors_mod.deadline_exhausted(deadline.elapsed()))

            # 提交阶段出口：错误契约行 / 预算耗尽快出（未发 IO）
            kind, payload = submitted
            if kind == "error":
                return payload or contract_mod.format_error(
                    errors_mod.deadline_exhausted(deadline.elapsed()))
            if kind == "exhausted" or not payload:
                timer.mark("submit")
                rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
                return self._finish_error(
                    rec, timer, errors_mod.deadline_exhausted(deadline.elapsed()))

            video_id = payload
            state.task_id = video_id  # 护栏兜底路径可转 E17
            rec.task_id = video_id    # logline 15 字段（T7c）
            timer.mark("submit")

            # ---- 3. 注册后台作业（future）+ 启动 worker（槽内已完成提交）----
            job = self._register_job(
                video_id=video_id, prompt=prompt, rec=rec, seconds=seconds,
                aspect_ratio=aspect_ratio, seed=seed)
            self._spawn_worker(
                job, seconds=seconds, aspect_ratio=aspect_ratio,
                persist_flag=persist_flag, seed=seed, http_headers=http_headers)

            # ---- 4. 槽外等待：剩余预算内 await（不占任何并发额度，V3）----
            return await self._await_job(
                rec=rec, timer=timer, deadline=deadline, job=job,
                seed=seed, seconds=seconds, aspect_ratio=aspect_ratio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底：内部异常也走契约行
            logger.exception("[video] 内部异常 req_id=%s", req_id)
            return self._finish_error(rec, timer, errors_mod.internal_error(exc))
        finally:
            if self._tracker is not None:
                self._tracker.exit()


    # ------------------------------------------------------------------
    # V3：提交（槽内）/ 作业登记 / 槽外等待 / 后台 worker
    # ------------------------------------------------------------------
    async def _submit(
        self, *, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, prompt: str, seconds: int,
        aspect_ratio: str, seed: Optional[int] = None,
    ) -> "tuple[str, str | None]":
        """提交到上游（槽内执行）。

        返回二元组（kind, payload）——不用裸 str 做出口判定：video_id 与
        错误契约行同为 str，混在一个返回值里无法区分（V3 首轮真实 bug）：
            ("ok",        video_id)
            ("error",     契约错误行)
            ("exhausted", None)      # 预算耗尽，未发起 IO
        """
        rec.model = VIDEO_MODEL
        try:
            api = await self._api.submit_video(
                model=VIDEO_MODEL, prompt=prompt, seconds=seconds,
                aspect_ratio=aspect_ratio, deadline=deadline, seed=seed,
            )
        except budget_mod.DeadlineExceededError:
            return ("exhausted", None)
        if not api.ok or not api.video_id:
            return ("error", self._finish_error(rec, timer, self._classify(api)))
        return ("ok", api.video_id)

    def _register_job(
        self, *, video_id: str, prompt: str, rec: logline_mod.LogRecord,
        seconds: Optional[int] = None, aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> tasks_mod.JobEntry:
        """登记作业（含 future 通知）。

        JobTable → register_job；老 TaskTable 退化包装（保持既有单测可
        注入旧表，V3 不强依赖新表类型）。
        """
        register = getattr(self._table, "register_job", None)
        if callable(register):
            return register(
                task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash,
                seconds=seconds, aspect_ratio=aspect_ratio, seed=seed)
        self._table.register(
            task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash)
        # 退化路径同样必须建 future——否则等待方会"等了个空气"直接转
        # E17（V3 首轮真实 bug：老表注入场景视频必然返 running）
        loop = asyncio.get_event_loop()
        return tasks_mod.JobEntry(
            task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash,
            seconds=seconds, aspect_ratio=aspect_ratio, seed=seed,
            future=loop.create_future())

    async def _await_job(
        self, *, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, job: tasks_mod.JobEntry,
        seed: Optional[int], seconds: int, aspect_ratio: str,
    ) -> str:
        """在剩余预算内等待后台作业终态（**不占并发槽**，V3）。"""
        waited = 0.0
        if job.future is not None:
            # 分片等待：asyncio.wait_for 走的是**事件循环真实时钟**，而
            # deadline 走的是可注入时钟（生产=真实单调钟，测试=FakeClock）。
            # 一次性 wait_for(remaining) 在注入时钟场景永远不会超时 →
            # 请求被后台作业拖到作业预算耗尽才返回（V3 首轮真实 bug）。
            # 故切成小片循环：每片结束回到 deadline 检查（两种时钟都认）。
            slice_sec = 0.02
            while not job.future.done():
                remaining = deadline.remaining()
                if remaining <= 0:
                    waited = deadline.elapsed()   # 预算耗尽 → E17
                    break
                try:
                    await asyncio.wait_for(
                        asyncio.shield(job.future),
                        timeout=min(remaining, slice_sec),
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
        timer.mark("wait")
        rec.poll_count = job.poll_count or None

        if job.status == tasks_mod.STATUS_COMPLETED and job.final_url:
            rec.persist = job.persist_status or "skipped"
            meta = contract_mod.VideoMeta(
                model=VIDEO_MODEL, seconds=seconds, aspect_ratio=aspect_ratio,
                elapsed_sec=job.elapsed_sec, seed=seed,
                persist_status=job.persist_status,
            )
            rec.result = "ok"
            rec.total = timer.total_str()
            rec.emit()
            return contract_mod.format_video_success(job.final_url, meta)

        if job.status == tasks_mod.STATUS_FAILED:
            reason = f"task_id={job.task_id}"
            if job.error_reason:
                reason = f"{reason} | {job.error_reason}"
            category = job.error_category or errors_mod.UPSTREAM_ERROR
            if category == errors_mod.UPSTREAM_ERROR:
                return self._finish_error(
                    rec, timer, errors_mod.video_failed_error(reason))
            # 后台已归类（429/400/…）→ 原样转发，不压成 upstream_error
            return self._finish_error(
                rec, timer, errors_mod.passthrough_error(category, reason))

        # 预算耗尽 / 后台中止 → E17 非错误（上游照常跑）
        # 契约上的"已等待"必须是**请求侧**等待秒数：后台作业有独立预算
        # （默认 1800s）且共享同一时钟，直接取 deadline.elapsed() 会报出
        # 后台预算而非请求预算（V3 首轮真实 bug）→ 钳到总预算。
        waited_sec = min(waited or deadline.elapsed(), self._cfg.total_budget_sec)
        return self._finish_deferred(rec, timer, job.task_id, waited_sec)

    def _spawn_worker(
        self, job: tasks_mod.JobEntry, *, seconds: int, aspect_ratio: str,
        persist_flag: bool, seed: Optional[int],
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """启动后台轮询 worker（每 task_id 至多一个；幂等）。

        在跟踪数达 VIDEO_JOB_MAX 时逐出**最早**的作业（abort，等待方转
        E17）：后台作业是内存跟踪，无上限增长会把手机内存吃干。
        """
        existing = self._bg.get(job.task_id)
        if existing is not None and not existing.done():
            return
        if len(self._bg) >= self._job_max:
            oldest_id = next(iter(self._bg))   # dict 保插入序
            oldest = self._bg.pop(oldest_id, None)
            if oldest is not None and not oldest.done():
                oldest.cancel()
            entry = self._table.get(oldest_id)
            if isinstance(entry, tasks_mod.JobEntry):
                self._mark_abort(entry, reason="后台作业数超上限，逐出最早者")
            logger.warning(
                "[video] 后台作业数达上限 %d，逐出最早作业 %s（上游不受影响）",
                self._job_max, oldest_id)
        task = asyncio.create_task(
            self._job_worker(
                job, seconds=seconds, aspect_ratio=aspect_ratio,
                persist_flag=persist_flag, seed=seed, http_headers=http_headers),
            name=f"video-job-{job.task_id[:8]}",
        )
        self._bg[job.task_id] = task
        task.add_done_callback(
            lambda _t, tid=job.task_id: self._bg.pop(tid, None))

    async def _job_worker(
        self, job: tasks_mod.JobEntry, *, seconds: int, aspect_ratio: str,
        persist_flag: bool, seed: Optional[int],
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """后台轮询到终态（脱离请求 deadline），终态通知等待方。

        - 轮询闸门 VIDEO_JOB_CONCURRENCY 限并发；总上限 VIDEO_JOB_MAX_WAIT_SEC；
        - completed 且 persist=true → 后台自动下载落盘（persist 层永不报错）；
        - 任何退出路径都保证 future 被 set（防等待方永久挂起）。
        """
        deadline = budget_mod.Deadline(self._job_max_wait, clock=self._clock)
        interval = self._cfg.video_poll_interval_sec
        try:
            async with self._job_gate:
                while True:
                    await self._sleep(interval)
                    job.poll_count += 1
                    try:
                        api = await self._api.query_video(
                            video_id=job.task_id, model=VIDEO_MODEL,
                            deadline=deadline)
                    except budget_mod.DeadlineExceededError:
                        self._mark_abort(
                            job, reason="后台轮询预算耗尽",
                            elapsed=deadline.elapsed())
                        return
                    if not api.ok:
                        info = self._classify(api)
                        self._mark_fail(
                            job,
                            reason=f"{info.reason} | task_id={job.task_id}",
                            elapsed=deadline.elapsed(), category=info.category)
                        return
                    self._table.update(
                        task_id=job.task_id, status=api.video_status,
                        url=api.video_url)
                    if api.video_status == tasks_mod.STATUS_FAILED:
                        self._mark_fail(
                            job, reason=f"task_id={job.task_id}",
                            elapsed=deadline.elapsed())
                        return
                    if api.video_status == tasks_mod.STATUS_COMPLETED:
                        url = api.video_url
                        if not url:
                            self._mark_fail(
                                job,
                                reason=f"completed 无 url（task_id={job.task_id}）",
                                elapsed=deadline.elapsed(),
                                category=errors_mod.UPSTREAM_ERROR)
                            return
                        final_url, persist_status = url, None
                        if persist_flag and self._persist is not None:
                            # 后台落盘：用作业自己的下载预算（不受请求 deadline 约束）
                            final_url, persist_status = await self._persist.persist_video(
                                url,
                                deadline=budget_mod.Deadline(
                                    self._cfg.persist_timeout_video_sec,
                                    clock=self._clock),
                                req_id=job.task_id[:12],
                                headers=http_headers)
                        self._mark_complete(
                            job, url=url, elapsed=deadline.elapsed(),
                            persist_status=persist_status, final_url=final_url)
                        return
                    if deadline.expired():
                        self._mark_abort(
                            job, reason="后台轮询总等待超限",
                            elapsed=deadline.elapsed())
                        return
        except asyncio.CancelledError:
            self._mark_abort(job, reason="服务停机，后台跟踪已取消")
            raise
        except Exception as exc:  # noqa: BLE001 —— 后台作业绝不外抛
            logger.exception("[video] 后台作业异常 task_id=%s", job.task_id)
            self._mark_fail(
                job, reason=f"后台异常 {type(exc).__name__}: {exc}",
                elapsed=deadline.elapsed())
        finally:
            if job.future is not None and not job.future.done():
                self._mark_abort(
                    job, reason="后台作业提前退出",
                    elapsed=deadline.elapsed())

    def _acquire_slot(self, deadline: budget_mod.Deadline, req_id: str):
        """取并发槽：优先 video 泳道（V3 多队列），老 SlotPool 回退原路径。"""
        lane_of = getattr(self._pool, "lane", None)
        if callable(lane_of):
            return lane_of(LANE_VIDEO).slot(deadline, req_id)
        return self._pool.slot(deadline, req_id)

    def list_jobs(self, limit: int = 20) -> list[dict]:
        """在跟踪的视频作业快照（最近提交在前；供 list_video_tasks 工具）。"""
        entries = [
            e for e in getattr(self._table, "_entries", {}).values()
            if isinstance(e, tasks_mod.JobEntry)
        ]
        entries.sort(key=lambda e: e.submitted_at, reverse=True)
        return [
            {
                "task_id": e.task_id,
                "status": e.status,
                "elapsed": round(e.elapsed_sec, 1),
                "url": e.final_url or e.url,
                "persist": e.persist_status,
            }
            for e in entries[:max(1, limit)]
        ]

    # ------------------------------------------------------------------
    # 终态写入（JobTable 直用；老 TaskTable 退化到 update + future 通知）
    # ------------------------------------------------------------------
    def _notify_done(self, job: tasks_mod.JobEntry, result: str) -> None:
        if job.future is not None and not job.future.done():
            job.future.set_result(result)

    def _mark_complete(
        self, job: tasks_mod.JobEntry, *, url: Optional[str], elapsed: float,
        persist_status: Optional[str], final_url: Optional[str],
    ) -> None:
        table = self._table
        if hasattr(table, "complete"):
            table.complete(
                job.task_id, url=url, elapsed_sec=elapsed,
                persist_status=persist_status, final_url=final_url)
            return
        table.update(job.task_id, status=tasks_mod.STATUS_COMPLETED,
                     url=final_url or url)
        # 退化路径：表里是 TaskEntry、手上持有的是 JobEntry 副本，
        # status 必须同步写到副本——否则等待方读到的永远是 running
        job.status = tasks_mod.STATUS_COMPLETED
        job.url = final_url or url
        job.final_url = final_url or url
        job.persist_status = persist_status
        job.elapsed_sec = elapsed
        self._notify_done(job, tasks_mod.JOB_RESULT_OK)

    def _mark_fail(
        self, job: tasks_mod.JobEntry, *, reason: str, elapsed: float,
        category: Optional[str] = None,
    ) -> None:
        """标记失败（category 为后台已归类的类别，供请求侧原样转发）。"""
        job.error_category = category or errors_mod.UPSTREAM_ERROR
        table = self._table
        if hasattr(table, "fail"):
            table.fail(job.task_id, reason=reason, elapsed_sec=elapsed)
            return
        table.update(job.task_id, status=tasks_mod.STATUS_FAILED)
        job.status = tasks_mod.STATUS_FAILED
        job.error_reason = reason
        job.elapsed_sec = elapsed
        self._notify_done(job, tasks_mod.JOB_RESULT_FAILED)

    def _mark_abort(
        self, job: tasks_mod.JobEntry, *, reason: str, elapsed: float = 0.0,
    ) -> None:
        """未达终态即停止跟踪（上游不受影响，等待方转 E17 deferred）。"""
        table = self._table
        if hasattr(table, "abort"):
            table.abort(job.task_id, reason=reason, elapsed_sec=elapsed)
            return
        job.error_reason = reason
        job.elapsed_sec = elapsed
        self._notify_done(job, tasks_mod.JOB_RESULT_TIMEOUT)

    def cancel_background(self) -> int:
        """停机：取消全部后台 worker（返回取消数；上游任务不受影响）。"""
        count = 0
        for task_id, task in list(self._bg.items()):
            if not task.done():
                task.cancel()
                count += 1
            self._bg.pop(task_id, None)
        if count:
            logger.info("[video] 停机：取消 %d 个后台作业（上游任务照常跑）", count)
        return count

    # ------------------------------------------------------------------
    # query 内部
    # ------------------------------------------------------------------
    async def _query(
        self, *, req_id: str, rec: logline_mod.LogRecord, task_id: str,
    ) -> str:
        timer = _StageTimer()
        deadline = budget_mod.Deadline(self._cfg.total_budget_sec, clock=self._clock)
        rec.model = VIDEO_MODEL
        try:
            # ---- 1. 非空校验（E18 前置：空 id 是参数错而非任务缺失）----
            if not isinstance(task_id, str) or not task_id.strip():
                timer.mark("validate")
                return self._finish_error(
                    rec, timer, errors_mod.invalid_params("task_id", "不能为空"))

            # ---- 2. 任务表（TTL 过期/容量逐出/不存在 → 同一契约 R16）----
            rec.task_id = task_id  # logline 15 字段（T7c；对账任务号）
            entry = self._table.get(task_id)
            timer.mark("table")
            if entry is None:
                return self._finish_error(
                    rec, timer, errors_mod.task_not_found_error(task_id))

            # ---- 3. V3 快路径：终态**或**有活跃后台 worker → 实况已在本地，
            #      直接返回（零上游 IO，E20 不占槽）。
            #      注意：作业完成后 worker 会退出并从 _bg 摘除——此时若只看
            #      worker 活跃度就会退化成打上游（V3 首轮真实缺陷），故终态
            #      必须无条件走本地。----
            worker = self._bg.get(task_id)
            worker_active = worker is not None and not worker.done()
            job = entry if isinstance(entry, tasks_mod.JobEntry) else None
            if job is not None and (
                worker_active
                or entry.status in (
                    tasks_mod.STATUS_COMPLETED, tasks_mod.STATUS_FAILED)
            ):
                rec.poll_count = (job.poll_count or None) if job else None
                if entry.status == tasks_mod.STATUS_COMPLETED:
                    url = (job.final_url if job else None) or entry.url
                    if not url:
                        return self._finish_error(rec, timer, errors_mod.classify_http_status(
                            200, f"completed 无 url（task_id={task_id}）"))
                    rec.result = "ok"
                    rec.persist = (job.persist_status if job else None) or "skipped"
                    rec.total = timer.total_str()
                    rec.emit()
                    if job is not None and job.seconds is not None:
                        meta = contract_mod.VideoMeta(
                            model=VIDEO_MODEL, seconds=job.seconds,
                            aspect_ratio=job.aspect_ratio or "16:9",
                            elapsed_sec=job.elapsed_sec, seed=job.seed,
                            persist_status=job.persist_status)
                        return contract_mod.format_video_success(url, meta)
                    return url
                if entry.status == tasks_mod.STATUS_FAILED:
                    reason = (job.error_reason if job else None) or f"task_id={task_id}"
                    category = (job.error_category if job else None) or errors_mod.UPSTREAM_ERROR
                    if category == errors_mod.UPSTREAM_ERROR:
                        return self._finish_error(
                            rec, timer, errors_mod.video_failed_error(reason))
                    return self._finish_error(
                        rec, timer, errors_mod.passthrough_error(category, reason))
                rec.result = "running"
                rec.total = timer.total_str()
                rec.emit()
                return contract_mod.format_video_status(task_id, entry.status)

            # ---- 4. 兼容路径（无后台 worker：老 TaskTable / 独立组件场景）：
            #      实时打上游（read ≤10s；不占并发槽 E20）----
            try:
                api = await self._api.query_video(
                    video_id=task_id, model=VIDEO_MODEL, deadline=deadline)
            except budget_mod.DeadlineExceededError:
                rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
                return self._finish_error(
                    rec, timer, errors_mod.deadline_exhausted(deadline.elapsed()))
            timer.mark("query")
            rec.poll_count = 1  # 单次上游实况查询，计一次轮询（T7c）

            if not api.ok:
                return self._finish_error(rec, timer, self._classify(api))

            self._table.update(
                task_id=task_id, status=api.video_status, url=api.video_url)

            if api.video_status == "completed":
                if api.video_url:
                    rec.result = "ok"
                    rec.total = timer.total_str()
                    rec.emit()
                    return api.video_url
                # completed 无 URL：上游契约破损 → 按未定义状态分类
                return self._finish_error(rec, timer, errors_mod.classify_http_status(
                    200, f"completed 无 url（task_id={task_id}）"))
            if api.video_status == "failed":
                return self._finish_error(
                    rec, timer, errors_mod.video_failed_error(f"task_id={task_id}"))
            # 非终态 → 状态行
            rec.result = "running"
            rec.total = timer.total_str()
            rec.emit()
            return contract_mod.format_video_status(task_id, api.video_status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[video] query 内部异常 req_id=%s", req_id)
            return self._finish_error(rec, timer, errors_mod.internal_error(exc))

    # ------------------------------------------------------------------
    # 收尾辅助（三出口共用：emit 恰好一次 + 契约行，R13）
    # ------------------------------------------------------------------
    def _finish_error(
        self, rec: logline_mod.LogRecord, timer: _StageTimer,
        info: errors_mod.ErrorInfo,
    ) -> str:
        rec.result = f"error:{info.category}"
        rec.total = timer.total_str()
        rec.emit()
        return contract_mod.format_error(info)

    def _finish_deferred(
        self, rec: logline_mod.LogRecord, timer: _StageTimer,
        task_id: str, waited_sec: float,
    ) -> str:
        rec.result = "running"  # E17 非错误收尾（result 枚举本轮新增值）
        rec.total = timer.total_str()
        rec.emit()
        return contract_mod.format_video_deferred(task_id, waited_sec)

    # ------------------------------------------------------------------
    # 其他辅助
    # ------------------------------------------------------------------
    def _classify(self, api: agnes_mod.ApiCallResult) -> errors_mod.ErrorInfo:
        """ApiCallResult → ErrorInfo（与生图 pipeline 同一映射）。"""
        detail = api.error_detail or ""  # 防御：ok=True 的异常形态无 detail
        if api.status_code is None:  # 网络/超时异常（重试 1 次后仍失败）
            return errors_mod.network_error(detail)
        return errors_mod.classify_http_status(api.status_code, detail)

    def _new_record(
        self, req_id: str, prompt: str, seconds: int, aspect_ratio: str,
        persist_flag: bool, seed: Optional[int] = None,
    ) -> logline_mod.LogRecord:
        return logline_mod.LogRecord(
            req_id=req_id,
            prompt_hash=logline_mod.prompt_hash(prompt if isinstance(prompt, str) else ""),
            params=(
                f"model={VIDEO_MODEL};seconds={seconds};"
                f"aspect_ratio={aspect_ratio};"
                + (f"seed={seed};" if seed is not None else "")
                + f"persist={'true' if persist_flag else 'false'}"
            ),
        )
