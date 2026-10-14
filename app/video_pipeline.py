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
    服务提供方提交（read ≤ 提交档 30s）得上游任务号 → 任务表 register
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
from typing import Awaitable, Callable, Mapping, Optional, Sequence

from app import contract as contract_mod
from app import context as context_mod
from config import REQUEST_GUARD_SLACK_SEC, Config
from engine import errors as errors_mod
from infra import budget as budget_mod
from infra import persist as persist_mod
from infra import refs as refs_mod
from infra import tasks as tasks_mod
from ops import logline as logline_mod
from ops import shutdown as shutdown_mod
from providers import gateway as gateway_mod

logger = logging.getLogger("mcp_imagegen.video_pipeline")

VIDEO_MODEL = "video"       # 能力别名（对齐生图侧 image 用法）

# 生成模式（由是否传入的媒体字段自动判定；见 decide_mode）
VIDEO_MODE_TEXT = "text"
VIDEO_MODE_KEYFRAME = "keyframe"
VIDEO_MODE_REFERENCE = "reference"
SECONDS_MIN = 4              # seconds 值域 [4, 12]（闭区间）
SECONDS_MAX = 12
# D4（官方视频画幅表，2026-10-10 定案）：三档扩六档——
# 16:9(1280x704) / 9:16(720x1280) / 1:1(720x720) / 4:3(960x720) /
# 3:4(720x960) / 21:9(1680x720)
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

# V4：视频参考图数量上限（多数上游实现硬限 5 张）
VIDEO_REFERENCE_MAX = 5

# V3：视频泳道名（多队列调度；与生图 image 泳道隔离，互不饿死）
LANE_VIDEO = "video"

SleepFn = Callable[[float], Awaitable[None]]

def provider_hint_of(job: tasks_mod.JobEntry) -> Optional[str]:
    """后台 worker 用的路由偏好读取（作业可能不带该字段）。"""
    return getattr(job, "provider_hint", None) or None


def decide_mode(
    first_frame: Optional[str],
    last_frame: Optional[str],
    reference_images: Optional[Sequence[str]],
) -> str:
    """V4：按传入的媒体字段自动判定上游 mode（对用户透明）。

    上游「生成模式规则」表（官方文档）：
        text      无媒体
        keyframe  first_frame / last_frame 至少一个
        reference images / audios / videos 至少一类非空

    自动判定而非让调用方手填 mode——手填极易与媒体字段不匹配，
    而上游对不匹配直接 400。把判定收进服务端，调用方只表达意图
    （"我要这张图当首帧"），不承担上游的字段配对规则。
    """
    if first_frame or last_frame:
        return VIDEO_MODE_KEYFRAME
    if reference_images:
        return VIDEO_MODE_REFERENCE
    return VIDEO_MODE_TEXT


def validate_video_params(
    prompt, seconds, aspect_ratio, persist, seed,
    first_frame=None, last_frame=None, reference_images=None,
) -> "tuple[()] | tuple[str, str]":
    """语义校验（返回 () 或 (param_name, 原因)）。

    schema（T8）已保证类型；此处校验 schema 之外的语义范围，保证越界也
    走 error 行 invalid_params 契约（对齐生图 validate_params / A2）。
    D3（官方 F5）：seed 恢复——integer 可选直传，官方未标注值域，
    不臆测上限（本地仅类型校验，超界由上游 400 兜底透传）；
    D4（官方画幅表）：aspect_ratio 扩六档 16:9/9:16/1:1/4:3/3:4/21:9。

    V4：首尾帧 / 参考图的模式互斥与数量校验。上游对「不允许的媒体字段」
    直接 400（且错误信息不指向调用方该改什么），故本地先拦——
    给出可自愈的 invalid_params 契约行。
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

    # ---- V4：媒体字段（首尾帧 / 参考图）----
    for name, value in (("first_frame", first_frame), ("last_frame", last_frame)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return (name, "需为非空字符串（图片 URL 或本服务产物名）")

    if reference_images is not None:
        if isinstance(reference_images, (str, bytes)) or not isinstance(
            reference_images, (list, tuple)
        ):
            return ("reference_images", "需为字符串数组（每项一个参考图）")
        if len(reference_images) == 0:
            return ("reference_images", "传了空数组；不需要参考图请不要传该参数")
        if len(reference_images) > VIDEO_REFERENCE_MAX:
            return (
                "reference_images",
                f"数量超限：收到 {len(reference_images)} 张，"
                f"上限 {VIDEO_REFERENCE_MAX} 张（上游 Flash 硬限）",
            )
        for item in reference_images:
            if not isinstance(item, str) or not item.strip():
                return ("reference_images", "每项必须是非空字符串")

    # 模式互斥：上游 keyframe 与 reference 的媒体字段不可混用
    has_keyframe = bool(first_frame) or bool(last_frame)
    has_reference = bool(reference_images)
    if has_keyframe and has_reference:
        return (
            "mode",
            "first_frame/last_frame（首尾帧）与 reference_images（参考图）"
            "不可同时使用——上游 keyframe 与 reference 是两种互斥模式，"
            "请择一：要「这张图作为成片首帧」用 first_frame；"
            "要「这张图仅作风格参考」用 reference_images",
        )
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
        api_client: gateway_mod.ProviderGateway,
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
        # ---- V4：提交代排队（上游队列满=常态，免费额度视频 RPM=1）----
        # 撞 503 不再立刻失败，改为本端排队重试：把「用户/Agent 反复手动
        # 重试」变成「提交一次，好了来拿」（10-12 实录：20 次重试全失败、
        # 白等 1 小时 43 分）。
        self._queue_wait_sec = float(
            getattr(cfg, "video_submit_queue_wait_sec", 600.0))
        self._retry_min_sec = float(
            getattr(cfg, "video_submit_retry_min_sec", 20.0))
        self._retry_max_sec = float(
            getattr(cfg, "video_submit_retry_max_sec", 90.0))
        # 参考图上限（生图侧同源配置；缺省保守值）
        self._ref_max_bytes = int(
            getattr(cfg, "reference_max_bytes", 10 * 1024 * 1024))
        self._video_ref_max = int(
            getattr(cfg, "video_reference_max", VIDEO_REFERENCE_MAX))

    # ------------------------------------------------------------------
    # 热更新（面板改运行参数后立即生效）
    # ------------------------------------------------------------------
    def reconfigure(self, cfg: Config) -> None:
        self._cfg = cfg
        self._job_gate = asyncio.Semaphore(
            max(1, int(getattr(cfg, "video_job_concurrency", 8))))
        self._job_max_wait = float(
            getattr(cfg, "video_job_max_wait_sec", 1800.0))
        self._job_max = max(1, int(getattr(cfg, "video_job_max", 24)))
        self._queue_wait_sec = float(
            getattr(cfg, "video_submit_queue_wait_sec", 600.0))
        self._retry_min_sec = float(
            getattr(cfg, "video_submit_retry_min_sec", 20.0))
        self._retry_max_sec = float(
            getattr(cfg, "video_submit_retry_max_sec", 90.0))
        self._ref_max_bytes = int(
            getattr(cfg, "reference_max_bytes", 10 * 1024 * 1024))
        self._video_ref_max = int(
            getattr(cfg, "video_reference_max", VIDEO_REFERENCE_MAX))

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
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Optional[Sequence[str]] = None,
        http_headers: Optional[Mapping[str, str]] = None,
        provider: Optional[str] = None,
    ) -> str:
        """单请求入口：返回契约文本（成功 URL+meta / E17 running / error 行）。

        V5：provider 可选——显式指定服务提供方（优先于令牌的默认绑定）。

        D3：seed 可选（None 不注入请求体；官方未标注值域，直传上游）。
        V4：first_frame/last_frame（首帧/尾帧，→ keyframe 模式）与
        reference_images（参考图，→ reference 模式）可选；都不传 = text
        模式（V3 行为逐字不变）。
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
                    first_frame=first_frame, last_frame=last_frame,
                    reference_images=reference_images,
                    http_headers=http_headers,
                    provider=provider,
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
            params=f"model=video;task_id={task_id}",
            kind="video",
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
        first_frame: Optional[str] = None, last_frame: Optional[str] = None,
        reference_images: Optional[Sequence[str]] = None,
        http_headers: Optional[Mapping[str, str]] = None,
        provider: Optional[str] = None,
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
                prompt, seconds, aspect_ratio, persist_flag, seed,
                first_frame=first_frame, last_frame=last_frame,
                reference_images=reference_images)
            timer.mark("validate")
            if bad:
                param, detail = bad
                return self._finish_error(rec, timer, errors_mod.invalid_params(param, detail))

            # ---- 1b. V4：媒体解析（本服务产物/内网 URL → data URI）----
            # 不静默降级：解析失败即 invalid_params（丢了首帧还硬生成
            # 等于给用户一个完全不同的视频，比明确报错糟糕得多）
            try:
                resolved_first, _s1 = self._resolve_media(first_frame)
                resolved_last, _s2 = self._resolve_media(last_frame)
                resolved_refs, ref_sources = refs_mod.resolve_references(
                    reference_images,
                    image_dir=self._cfg.image_dir,
                    video_dir=getattr(self._cfg, "video_dir", None),
                    max_bytes=self._ref_max_bytes,
                    max_count=self._video_ref_max,
                ) if reference_images else ((), ())
            except refs_mod.ReferenceError as exc:
                return self._finish_error(
                    rec, timer, errors_mod.invalid_params("reference_image", str(exc)))
            mode = decide_mode(resolved_first, resolved_last, resolved_refs)
            ref_count = sum(
                1 for v in (resolved_first, resolved_last) if v) + len(resolved_refs)
            timer.mark("refs")

            # ---- 2. video 泳道槽：只覆盖"提交"这段 IO（V3 关键改动）----
            submitted: "tuple[str, str | None]" = ("exhausted", None)
            try:
                async with self._acquire_slot(deadline, req_id):
                    timer.mark("queue")
                    submitted = await self._submit(
                        rec=rec, timer=timer, deadline=deadline,
                        prompt=prompt, seconds=seconds,
                        aspect_ratio=aspect_ratio, seed=seed,
                        mode=mode, first_frame=resolved_first,
                        last_frame=resolved_last,
                        reference_images=resolved_refs,
                        provider_hint=provider or self._provider_hint(),
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
                if kind == "exhausted":
                    timer.mark("submit")
                    rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
                    return self._finish_error(
                        rec, timer, errors_mod.deadline_exhausted(deadline.elapsed()))
                # V4：上游队列满 → 本端排队，给排队号 + 后台代排队 worker
                return await self._enqueue(
                    rec=rec, timer=timer, deadline=deadline, prompt=prompt,
                    seconds=seconds, aspect_ratio=aspect_ratio, seed=seed,
                    persist_flag=persist_flag, mode=mode,
                    first_frame=resolved_first, last_frame=resolved_last,
                    reference_images=resolved_refs,
                    http_headers=http_headers, ref_count=ref_count,
                    provider_hint=provider or self._provider_hint())

            video_id = payload
            state.task_id = video_id  # 护栏兜底路径可转 E17
            rec.task_id = video_id    # logline 15 字段（T7c）
            timer.mark("submit")

            # ---- 3. 注册后台作业（future）+ 启动 worker（槽内已完成提交）----
            job = self._register_job(
                video_id=video_id, prompt=prompt, rec=rec, seconds=seconds,
                aspect_ratio=aspect_ratio, seed=seed, mode=mode,
                ref_count=ref_count, upstream_id=video_id,
                first_frame=resolved_first, last_frame=resolved_last,
                reference_images=resolved_refs,
                provider_hint=provider or self._provider_hint())
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
        mode: str = VIDEO_MODE_TEXT,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Tuple[str, ...] = (),
        provider_hint: Optional[str] = None,
    ) -> "tuple[str, str | None]":
        """提交到上游（槽内执行）。

        V5：provider_hint 由调用方（请求上下文 / 作业记录）显式带入，
        不在深层再读 ContextVar——脱离请求的协程里读不到发起者。

        返回二元组（kind, payload）——不用裸 str 做出口判定：video_id 与
        错误契约行同为 str，混在一个返回值里无法区分（V3 首轮真实 bug）：
            ("ok",        video_id)
            ("queued",    None)      # V4：上游队列满，转服务端代排队
            ("error",     契约错误行)
            ("exhausted", None)      # 预算耗尽，未发起 IO
        """
        rec.model = VIDEO_MODEL
        try:
            api = await self._api.submit_video(
                prompt=prompt, seconds=seconds,
                aspect_ratio=aspect_ratio, deadline=deadline, seed=seed,
                mode=mode, first_frame=first_frame, last_frame=last_frame,
                reference_images=reference_images or None,
                provider=provider_hint,
            )
        except budget_mod.DeadlineExceededError:
            return ("exhausted", None)
        if not api.ok or not api.video_id:
            # V4：上游队列满（503）或限流（429）→ 不立刻失败，转代排队。
            # 上游队列满在免费额度下是**常态**（视频 RPM=1），立刻失败会把
            # 排队成本转嫁成用户的反复重试（10-12 实录：20 次全失败）。
            # 429 纳入依据见 _is_retryable_submit（生产实测补）。
            if self._is_retryable_submit(api):
                return ("queued", None)
            return ("error", self._finish_error(rec, timer, self._classify(api)))
        return ("ok", api.video_id)

    @staticmethod
    def _is_queue_full(api: gateway_mod.ApiCallResult) -> bool:
        """上游「队列满」判定（503 + video_queue_full / 队列已满文案）。

        只认队列满这一类——其余 5xx 仍按原错误路径返回（不能把真正的
        上游故障也拖进排队，那会让用户等一个永远不会好的结果）。
        """
        if api.status_code != 503:
            return False
        detail = (api.error_detail or "").lower()
        return ("video_queue_full" in detail) or ("队列已满" in detail) \
            or ("queue" in detail and "full" in detail)

    @staticmethod
    def _is_retryable_submit(api: gateway_mod.ApiCallResult) -> bool:
        """代排队可重试的提交失败：队列满（503）或上游限流（429）。

        V4 修订（生产实测补）：429 必须纳入。免费额度下视频提交撞
        429（RPM 限制）与撞队列满一样是常态，10-12 实录两者混合出现；
        一次 429 就终态失败，等于把等待成本又甩回给 Agent 盲试——
        与「提交一次，好了来拿」的产品目标相悖。429 不消耗配额，
        尊重 Retry-After 退避重试是安全的；持续 429（如日额度耗尽）
        由排队窗口（VIDEO_SUBMIT_QUEUE_WAIT_SEC）兜底转 abort。
        401/403/审核/参数类错误不在范围——排队永远等不来好结果。
        """
        return api.status_code == 429 or VideoPipeline._is_queue_full(api)

    def _register_job(
        self, *, video_id: str, prompt: str, rec: logline_mod.LogRecord,
        seconds: Optional[int] = None, aspect_ratio: Optional[str] = None,
        seed: Optional[int] = None,
        status: str = tasks_mod.STATUS_RUNNING,
        upstream_id: Optional[str] = None,
        mode: Optional[str] = None,
        ref_count: int = 0,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Optional[tuple] = None,
        provider_hint: Optional[str] = None,
    ) -> tasks_mod.JobEntry:
        """登记作业（含 future 通知）。

        JobTable → register_job；老 TaskTable 退化包装（保持既有单测可
        注入旧表，V3 不强依赖新表类型）。

        V4：status/upstream_id/mode/ref_count 与提交参数随作业携带，
        供「排队中」的作业在后台完成提交与轮询。

        V5：provider_hint 记录发起方令牌绑定的服务提供方——后台 worker
        是脱离请求上下文的协程，ContextVar 在那里读不到发起者的值，
        所以必须显式随作业带上（否则异步续查会打到全局路由的另一个提供方）。
        """
        register = getattr(self._table, "register_job", None)
        if callable(register):
            return register(
                task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash,
                seconds=seconds, aspect_ratio=aspect_ratio, seed=seed,
                status=status, upstream_id=upstream_id, mode=mode,
                ref_count=ref_count, prompt=prompt,
                first_frame=first_frame, last_frame=last_frame,
                reference_images=reference_images,
                provider_hint=provider_hint)
        self._table.register(
            task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash)
        # 退化路径同样必须建 future——否则等待方会"等了个空气"直接转
        # E17（V3 首轮真实 bug：老表注入场景视频必然返 running）
        loop = asyncio.get_event_loop()
        return tasks_mod.JobEntry(
            task_id=video_id, model=VIDEO_MODEL, prompt_hash=rec.prompt_hash,
            seconds=seconds, aspect_ratio=aspect_ratio, seed=seed,
            status=status, upstream_id=upstream_id, mode=mode,
            ref_count=ref_count, prompt=prompt,
            first_frame=first_frame, last_frame=last_frame,
            reference_images=reference_images,
            future=loop.create_future(), provider_hint=provider_hint)

    async def _enqueue(
        self, *, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, prompt: str, seconds: int,
        aspect_ratio: str, persist_flag: bool, seed: Optional[int],
        mode: str, first_frame: Optional[str], last_frame: Optional[str],
        reference_images: Tuple[str, ...],
        http_headers: Optional[Mapping[str, str]], ref_count: int,
        provider_hint: Optional[str] = None,
    ) -> str:
        """V4：上游队列满 → 本端排队（非错误），后台 worker 代为重试提交。

        产品语义：用户提交一次，拿到 task_id（status=queued）；
        后台按退避持续尝试提交，成功后自动转入正常轮询直到出片。
        ——把「反复手动重试」变成「提交一次，好了来拿」。

        若代排队被配置关闭（VIDEO_SUBMIT_QUEUE_WAIT_SEC=0）→ 退回 V3
        的立即失败行为（保持可配置回退，不硬性绑定新行为）。
        """
        if self._queue_wait_sec <= 0:
            return self._finish_error(rec, timer, self._classify_queue_full())

        task_id = "q-" + uuid.uuid4().hex[:12]   # 本端排队号（可读前缀）
        job = self._register_job(
            video_id=task_id, prompt=prompt, rec=rec, seconds=seconds,
            aspect_ratio=aspect_ratio, seed=seed,
            status=tasks_mod.STATUS_QUEUED, mode=mode, ref_count=ref_count,
            first_frame=first_frame, last_frame=last_frame,
            reference_images=reference_images,
            provider_hint=provider_hint)
        self._spawn_worker(
            job, seconds=seconds, aspect_ratio=aspect_ratio,
            persist_flag=persist_flag, seed=seed, http_headers=http_headers)
        timer.mark("submit")
        rec.task_id = task_id
        rec.result = "queued"
        rec.total = timer.total_str()
        rec.emit()
        return contract_mod.format_video_queued(task_id, 0.0, "上游忙（队列满或限流）")

    @staticmethod
    def _classify_queue_full() -> errors_mod.ErrorInfo:
        """代排队关闭时的兜底分类（退回 V3 的 503 → upstream_error/retry=yes）。"""
        return errors_mod.classify_http_status(
            503, "video_queue_full 视频队列已满（本端代排队已关闭）")

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

    async def _drain_submit_queue(
        self, job: tasks_mod.JobEntry, *, seconds: int, aspect_ratio: str,
        seed: Optional[int],
    ) -> bool:
        """V4：为排队中的作业代为提交，直到成功或排队窗口耗尽。

        返回 True = 已拿到上游 video_id（job.upstream_id 已回填，status
        转 running），调用方可继续轮询；False = 排队失败/超时（已
        _mark_fail/_mark_abort 通知等待方，调用方直接返回）。

        退避策略：指数退避（min → max 封顶），并尊重上游 Retry-After。
        排队窗口由 VIDEO_SUBMIT_QUEUE_WAIT_SEC 控制——把「用户/Agent 反复
        手动重试」变成「提交一次，好了来拿」（10-12 实录：盲目重试 20 次
        全失败、白等 1h43m）。
        """
        submit_deadline = budget_mod.Deadline(
            self._queue_wait_sec, clock=self._clock)
        delay = self._retry_min_sec
        while True:
            job.submit_attempts += 1
            try:
                api = await self._api.submit_video(
                    prompt=job.prompt or "",
                    seconds=job.seconds if job.seconds is not None else seconds,
                    aspect_ratio=job.aspect_ratio or aspect_ratio,
                    deadline=submit_deadline,
                    seed=job.seed if job.seed is not None else seed,
                    mode=job.mode or VIDEO_MODE_TEXT,
                    first_frame=job.first_frame,
                    last_frame=job.last_frame,
                    reference_images=job.reference_images or None,
                    provider=provider_hint_of(job),
                )
            except budget_mod.DeadlineExceededError:
                self._mark_abort(
                    job, reason="代排队窗口耗尽（上游队列持续满载）",
                    elapsed=submit_deadline.elapsed())
                return False
            except asyncio.CancelledError:
                raise

            if api.ok and api.video_id:
                # 提交成功：回填上游 id，转 running，进入正常轮询
                job.upstream_id = api.video_id
                job.status = tasks_mod.STATUS_RUNNING
                self._table.update(
                    task_id=job.task_id, status=tasks_mod.STATUS_RUNNING)
                logger.info(
                    "[video] 代排队成功 task_id=%s upstream_id=%s 尝试 %d 次",
                    job.task_id, api.video_id, job.submit_attempts)
                return True

            if not self._is_retryable_submit(api):
                # 非可重试错误（真故障/审核/鉴权）→ 不再排队，明确失败
                info = self._classify(api)
                self._mark_fail(
                    job,
                    reason=f"{info.reason} | task_id={job.task_id}",
                    elapsed=submit_deadline.elapsed(),
                    category=info.category)
                return False

            if submit_deadline.expired():
                # 记下终止类别（429→rate_limit / 队列满→upstream），
                # 供 query 快路径把真实原因回给调用方
                job.error_category = (errors_mod.RATE_LIMIT
                                      if api.status_code == 429
                                      else errors_mod.UPSTREAM_ERROR)
                self._mark_abort(
                    job,
                    reason=(f"代排队超时（{self._queue_wait_sec:.0f}s，尝试 "
                            f"{job.submit_attempts} 次）：上游视频队列持续满载或限流"),
                    elapsed=submit_deadline.elapsed())
                return False

            # 队列满/限流 → 退避后再试；尊重 Retry-After（429 时上游会给）
            wait = api.retry_after if api.retry_after is not None else delay
            wait = max(0.0, min(float(wait), max(submit_deadline.remaining(), 0.0)))
            logger.info(
                "[video] 上游队列满，%.0fs 后重试 task_id=%s（第 %d 次，已排队 %.0fs）",
                wait, job.task_id, job.submit_attempts, submit_deadline.elapsed())
            await self._sleep(wait)
            delay = min(delay * 2, self._retry_max_sec)

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

        - V4：作业若处于 queued（上游队列满），先**代为排队重试提交**，
          提交成功才转入轮询；
        - 轮询闸门 VIDEO_JOB_CONCURRENCY 限并发；总上限 VIDEO_JOB_MAX_WAIT_SEC；
        - completed 且 persist=true → 后台自动下载落盘（persist 层永不报错）；
        - 任何退出路径都保证 future 被 set（防等待方永久挂起）。
        """
        deadline = budget_mod.Deadline(self._job_max_wait, clock=self._clock)
        interval = self._cfg.video_poll_interval_sec
        try:
            async with self._job_gate:
                # ---- V4：排队中 → 先代排队提交（成功才继续轮询）----
                if job.status == tasks_mod.STATUS_QUEUED:
                    if not await self._drain_submit_queue(
                            job, seconds=seconds, aspect_ratio=aspect_ratio,
                            seed=seed):
                        return  # 排队失败/超时：_mark_* 已通知等待方
                while True:
                    await self._sleep(interval)
                    job.poll_count += 1
                    # V4：轮询必须打**上游 video_id**（upstream_id），
                    # 不能用本端 task_id——代排队的作业二者不同。
                    upstream_id = job.upstream_id or job.task_id
                    try:
                        api = await self._api.query_video(
                            video_id=upstream_id, deadline=deadline,
                            provider=provider_hint_of(job))
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
                # V4：模式与上游任务号（排队中的作业 upstream_id 为 null）
                "mode": e.mode,
                "upstream_id": e.upstream_id,
                "submit_attempts": e.submit_attempts or None,
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
                # V4 修订：排队中的作业（尚未拿到上游 id）绝不能掉进
                # 兼容路径拿本端 q- 排队号打上游——上游必回 404「任务
                # 不存在」，把可自愈的排队状态变成假故障（生产实测）。
                or (entry.status == tasks_mod.STATUS_QUEUED
                    and not job.upstream_id)
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
                if (entry.status == tasks_mod.STATUS_QUEUED
                        and job is not None and job.error_reason):
                    # 代排队已终止（窗口耗尽/停机）：把终止原因如实回给
                    # 调用方，而不是永远"仍在排队"让人干等
                    category = (job.error_category
                                or errors_mod.UPSTREAM_ERROR)
                    return self._finish_error(
                        rec, timer, errors_mod.passthrough_error(
                            category, job.error_reason))
                rec.result = "running"
                rec.total = timer.total_str()
                rec.emit()
                return contract_mod.format_video_status(task_id, entry.status)

            # ---- 4. 兼容路径（无后台 worker：老 TaskTable / 独立组件场景）：
            #      实时打上游（read ≤10s；不占并发槽 E20）----
            try:
                api = await self._api.query_video(
                    video_id=task_id, deadline=deadline,
                    provider=self._provider_hint())
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
    def _classify(self, api: gateway_mod.ApiCallResult) -> errors_mod.ErrorInfo:
        """ApiCallResult → ErrorInfo（与生图 pipeline 同一映射）。"""
        detail = api.error_detail or ""  # 防御：ok=True 的异常形态无 detail
        # reason 非空 = 本端配置类问题（同上，与生图 pipeline 同一映射）
        if api.reason:
            return errors_mod.not_configured_error(
                "轮询" if api.reason == "poll_undefined" else "视频", detail)
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
                f"model=video;seconds={seconds};"
                f"aspect_ratio={aspect_ratio};"
                + (f"seed={seed};" if seed is not None else "")
                + f"persist={'true' if persist_flag else 'false'}"
            ),
            kind="video",
        )

    def _provider_hint(self) -> Optional[str]:
        """当前请求的服务提供方偏好（来自访问令牌绑定；无则为全局路由）。"""
        scope = context_mod.token_scope()
        return (scope or {}).get("provider") or None

    def _resolve_media(
        self, value: Optional[str],
    ) -> "tuple[str | None, str | None]":
        """V4：单个媒体引用（首帧/尾帧）→ (上游可用值, 来源标记)。

        None / 空串 → (None, None)。本服务产物与内网 URL 就地转 data URI；
        失败抛 ReferenceError（上层转 invalid_params，不静默降级）。
        """
        if not value or not str(value).strip():
            return None, None
        return refs_mod.resolve_reference(
            str(value).strip(),
            image_dir=self._cfg.image_dir,
            video_dir=getattr(self._cfg, "video_dir", None),
            max_bytes=self._ref_max_bytes,
        )
