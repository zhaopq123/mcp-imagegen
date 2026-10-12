"""视频流水线（V2 增量设计方案 2.2；T7a 提交+轮询 / T7b query 入口）。

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

日志（R13）：run 与 query 每请求恰好一行访问日志；result 枚举本轮
新增 "running"（E17 非错误收尾）；stages = validate/queue/submit/poll。
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

            # ---- 2. 并发槽（E4 排队计入 deadline；R14 轮询期占槽；R15 finally 释放）----
            try:
                async with self._pool.slot(deadline, req_id):
                    timer.mark("queue")
                    return await self._generate(
                        req_id=req_id, rec=rec, timer=timer, deadline=deadline,
                        state=state, prompt=prompt, seconds=seconds,
                        aspect_ratio=aspect_ratio, persist_flag=persist_flag,
                        seed=seed,
                        http_headers=http_headers,
                    )
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底：内部异常也走契约行
            logger.exception("[video] 内部异常 req_id=%s", req_id)
            return self._finish_error(rec, timer, errors_mod.internal_error(exc))
        finally:
            if self._tracker is not None:
                self._tracker.exit()

    # ------------------------------------------------------------------
    # run 内部：提交 + 轮询（槽内）
    # ------------------------------------------------------------------
    async def _generate(
        self, *, req_id: str, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, state: _RunState, prompt: str,
        seconds: int, aspect_ratio: str, persist_flag: bool,
        seed: Optional[int] = None,
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        rec.model = VIDEO_MODEL

        # ---- 3. 提交（read ≤ 提交档 30s；seed 可选直传，D3/官方 F5）----
        try:
            api = await self._api.submit_video(
                model=VIDEO_MODEL, prompt=prompt, seconds=seconds,
                aspect_ratio=aspect_ratio, deadline=deadline, seed=seed,
            )
        except budget_mod.DeadlineExceededError:
            timer.mark("submit")
            rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
            return self._finish_error(
                rec, timer, errors_mod.deadline_exhausted(deadline.elapsed()))
        timer.mark("submit")

        if not api.ok or not api.video_id:
            return self._finish_error(rec, timer, self._classify(api))

        video_id = api.video_id
        state.task_id = video_id  # 护栏兜底路径可转 E17
        rec.task_id = video_id    # logline 15 字段（T7c）
        self._table.register(
            task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash)

        # ---- 4. 轮询循环 ----
        return await self._poll(
            req_id=req_id, rec=rec, timer=timer, deadline=deadline,
            state=state, video_id=video_id, seconds=seconds,
            aspect_ratio=aspect_ratio, persist_flag=persist_flag,
            seed=seed,
            http_headers=http_headers,
        )

    async def _poll(
        self, *, req_id: str, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, state: _RunState, video_id: str,
        seconds: int, aspect_ratio: str, persist_flag: bool,
        seed: Optional[int] = None,
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        interval = self._cfg.video_poll_interval_sec
        while True:
            # 先等间隔再查（设计 2.2；事件循环 sleep 驱动，R2）
            await self._sleep(interval)
            try:
                api = await self._api.query_video(
                    video_id=video_id, model=VIDEO_MODEL, deadline=deadline)
            except budget_mod.DeadlineExceededError:
                # 单查入口检查：预算耗尽 → E17 非错误（零 IO 快出）
                timer.mark("poll")
                return self._finish_deferred(
                    rec, timer, video_id, deadline.elapsed())
            timer.mark("poll")
            rec.poll_count = (rec.poll_count or 0) + 1  # 完成一次上游轮询（T7c）

            if not api.ok:
                # 查询失败 → 错误分类返回（detail 附 task_id 便于对账/续查）
                info = self._classify(api)
                info = replace(info, reason=f"{info.reason} | task_id={video_id}")
                return self._finish_error(rec, timer, info)

            # 命中后顺手 update——表内 status 永远是最近一次上游实况
            self._table.update(
                task_id=video_id, status=api.video_status, url=api.video_url)

            if api.video_status == "failed":
                # E19：唯一视频模型无切换，按原因映射返回
                return self._finish_error(
                    rec, timer, errors_mod.video_failed_error(f"task_id={video_id}"))
            if api.video_status == "completed":
                if api.video_url:
                    return await self._finish_success(
                        req_id=req_id, rec=rec, timer=timer, deadline=deadline,
                        video_id=video_id, seconds=seconds,
                        aspect_ratio=aspect_ratio, persist_flag=persist_flag,
                        seed=seed, url=api.video_url,
                        http_headers=http_headers)
                # completed 无 URL：上游契约破损 → 按未定义状态分类
                return self._finish_error(rec, timer, errors_mod.classify_http_status(
                    200, f"completed 无 url（task_id={video_id}）"))
            # 非终态（running / 未枚举状态）→ 预算检查后续轮
            if deadline.expired():
                # E17：deadline 到仍 running → 非错误返回
                return self._finish_deferred(
                    rec, timer, video_id, deadline.elapsed())

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

            # ---- 3. 实时打上游（read ≤10s；不占并发槽 E20）----
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

    async def _finish_success(
        self, *, req_id: str, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, video_id: str, seconds: int,
        aspect_ratio: str, persist_flag: bool, url: str,
        seed: Optional[int] = None,
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        final_url, persist_status = url, None
        if persist_flag and self._persist is not None:
            # T7c：persist=true → 下载落盘 VIDEO_DIR（read = min(120s, 剩余)，
            # 计入 deadline，仍在并发槽内）；C6/R5——PersistService 永不报错，
            # 失败/预算不足降级返 (原上游 URL, persist_failed)，非错误；
            # headers 透传用于 /files URL 绝对化（Q3：Host/转发头推断）。
            final_url, persist_status = await self._persist.persist_video(
                url, deadline=deadline, req_id=req_id,
                headers=http_headers)
        rec.persist = persist_status or "skipped"
        meta = contract_mod.VideoMeta(
            model=VIDEO_MODEL, seconds=seconds, aspect_ratio=aspect_ratio,
            elapsed_sec=deadline.elapsed(), seed=seed,
            persist_status=persist_status,
        )
        rec.result = "ok"
        rec.total = timer.total_str()
        rec.emit()
        return contract_mod.format_video_success(final_url, meta)

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
