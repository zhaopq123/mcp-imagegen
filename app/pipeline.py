"""生图请求管道（V2 重写版；红线 E3/E4/E8/E9/E13/E24、R3/R5/R17/R20）。

串联：参数校验 → 熔断门路由 → 并发槽 → Agnes 生图（拆分串行）→
persist（永不报错）→ contract 格式化 → logline 一行收尾。

V2 变更（增量设计方案三节 pipeline 行）：
- 单模型直调 agnes-image：删路由循环/切换/探针归还分支（唯一模型
  一次调用定生死，E19）；fallback.py 删除，finalize_error 语义并入
  本模块（全失败按错误路径返回最后一个错误）；
- batch 拆分串行（agnes-image 不原生支持 batch）：n 次顺序调用，
  **每次前检查 deadline 剩余 ≥ 单次档（TIMEOUT_AGNES_IMAGE_SEC）才发起**，
  不足即止 = 部分成功，非错误（R20/E9）；单次失败不中断后续
  （预算耗尽除外）；
- R17 部分成功契约：M<N 时 meta 必含 requested/delivered；M=0 时不
  产生"部分成功"（走错误路径返回最后一个错误的 [error] 行）；
- E13：不 persist → meta `url_expires=1h`；
- E24：auto 请求 OPEN 期快失败（rate_limit, retry=yes，注明约 X 秒后重试）。

- Deadline 入口创建一次，贯穿槽等待/API/退避/persist（R3）；
- 整体护栏 asyncio.timeout(TOTALBUDGETSEC + SLACK)（6.1 自救兜底）；
- E8：半开探针=普通请求；探针未能发出时归还资格；
- 任何路径的出口都是一个契约字符串 + 恰好一行访问日志（R13）。

V2 第七轮（官方文档对齐 D2）：generate_image 四参数 → 五参数
（prompt/size/ratio/batch_size/persist）——size 双格式直传（档位
1K/2K/3K/4K 或 WxH 正则），ratio 官方 8 档仅与档位式 size 配合
（WxH 同传即 invalid_params，冲突显式告知 Agent）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable, Mapping, Optional

from app import contract as contract_mod
from config import DISK_WATERMARK_RATIO, REQUEST_GUARD_SLACK_SEC, Config
from engine import errors as errors_mod
from engine import normalizer as normalizer_mod
from engine import router as router_mod
from infra import agnes as agnes_mod
from infra import budget as budget_mod
from infra import circuit as circuit_mod
from infra import persist as persist_mod
from ops import logline as logline_mod
from ops import shutdown as shutdown_mod

logger = logging.getLogger("mcp_imagegen.pipeline")

BATCH_MAX = 4  # batch 合法区间 1–4（规格书二；schema 不硬约束，此处校验）


def validate_params(
    prompt, size, ratio, batch_size, persist,
) -> "tuple[()] | tuple[str, str]":
    """语义校验（返回 () 或 (param_name, 原因)）。

    schema 已保证类型；此处校验 schema 之外的语义范围，保证越界也走
    [error] invalid_params 契约行（验收⑪⑫），而非传输层报错。

    D2（官方文档对齐）：size 双格式——档位枚举（1K/2K/3K/4K）或
    WxH（\\d{3,4}x\\d{3,4}），均直传上游；ratio 官方 8 档枚举，仅与
    档位式 size 配合（F3——WxH 已含比例信息，同传即参数冲突）。
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return ("prompt", "不能为空")
    if not isinstance(prompt, str) or len(prompt) > 65535:
        return ("prompt", "超出长度上限（65535 字符）")
    try:
        normalizer_mod.normalize_size(size)
    except normalizer_mod.SizeError as exc:
        return ("size", str(exc))
    if ratio is not None:
        if not normalizer_mod.validate_ratio(ratio):
            return ("ratio", f"需为官方 8 档之一 {normalizer_mod.IMAGE_RATIOS}")
        if not normalizer_mod.is_tier_size(size):
            return (
                "ratio",
                "仅与档位式 size（1K/2K/3K/4K）配合使用；"
                "WxH 式 size 已含比例信息，不可同传",
            )
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        return ("batch_size", "需为整数")
    if not 1 <= batch_size <= BATCH_MAX:
        return ("batch_size", f"超出 1–{BATCH_MAX} 区间")
    if not isinstance(persist, bool):
        return ("persist", "需为布尔值")
    return ()


class _StageTimer:
    """各阶段耗时（stages 字段来源；monotonic）。"""

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


class GeneratePipeline:
    def __init__(
        self,
        cfg: Config,
        pool: budget_mod.SlotPool,
        circuit: circuit_mod.CircuitBreaker,
        api_client: agnes_mod.AgnesClient,
        persist: persist_mod.PersistService,
        tracker: Optional[shutdown_mod.ShutdownCoordinator] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._circuit = circuit
        self._api = api_client
        self._persist = persist
        self._tracker = tracker
        self._clock = clock or time.monotonic  # deadline 时钟（测试注入 FakeClock）

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        prompt: str,
        size: str,
        batch_size: int,
        persist_flag: bool,
        ratio: Optional[str] = None,
        http_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """单请求入口：永不抛错（CancelledError 除外），返回契约文本。

        D2：ratio 可选（None 不注入请求体；仅档位式 size 时合法）。
        """
        req_id = uuid.uuid4().hex[:12]
        guard = self._cfg.total_budget_sec + REQUEST_GUARD_SLACK_SEC
        try:
            async with asyncio.timeout(guard):
                return await self._execute(
                    req_id=req_id, prompt=prompt, size=size,
                    batch_size=batch_size, persist_flag=persist_flag,
                    ratio=ratio,
                    http_headers=http_headers,
                )
        except TimeoutError:
            # 护栏触发（deadline 检查应先到；此为 6.1 防御纵深）
            rec = self._new_record(
                req_id, prompt, size, batch_size, persist_flag, ratio)
            rec.timeout_hit = logline_mod.TIMEOUT_GUARD
            rec.result = "error:" + errors_mod.UPSTREAM_ERROR
            rec.total = f"{guard:.1f}s"
            rec.emit()
            return contract_mod.format_error(errors_mod.deadline_exhausted(guard))
        except asyncio.CancelledError:
            raise  # 客户端断连/停机取消：绝不吞（R1 槽归还由 finally 保证）

    # ------------------------------------------------------------------
    # 校验 + 路由门 + 并发槽
    # ------------------------------------------------------------------
    async def _execute(
        self, *, req_id: str, prompt: str, size: str, batch_size: int,
        persist_flag: bool, ratio: Optional[str],
        http_headers: Optional[Mapping[str, str]],
    ) -> str:
        # D2：ratio 规范化——校验层宽容首尾空白（validate_ratio），
        # 透传/审计统一用清洗后的枚举原值（与 size 规范化同哲学）
        if isinstance(ratio, str):
            ratio = ratio.strip()
        rec = self._new_record(
            req_id, prompt, size, batch_size, persist_flag, ratio)
        timer = _StageTimer()
        deadline = budget_mod.Deadline(self._cfg.total_budget_sec, clock=self._clock)
        if self._tracker is not None:
            self._tracker.enter()
        try:
            # ---- 1. 参数语义校验（验收⑪⑫：越界也走 [error] invalid_params）----
            bad = validate_params(prompt, size, ratio, batch_size, persist_flag)
            timer.mark("validate")
            if bad:
                param, detail = bad
                info = errors_mod.invalid_params(param, detail)
                rec.result = f"error:{info.category}"
                rec.total = timer.total_str()
                rec.emit()
                return contract_mod.format_error(info)

            # ---- 2. 并发槽（E4：排队等待计入 deadline）----
            try:
                async with self._pool.slot(deadline, req_id):
                    timer.mark("queue")
                    return await self._generate(
                        req_id=req_id, rec=rec, timer=timer, deadline=deadline,
                        prompt=prompt, size=size, batch_size=batch_size,
                        persist_flag=persist_flag, ratio=ratio,
                        http_headers=http_headers,
                    )
            except budget_mod.SlotBusyError as exc:
                timer.mark("queue")
                info = errors_mod.busy_error(exc.waited_sec)
                rec.timeout_hit = logline_mod.TIMEOUT_QUEUE_WAIT
                rec.result = f"error:{info.category}"
                rec.total = timer.total_str()
                rec.emit()
                return contract_mod.format_error(info)
            except budget_mod.DeadlineExceededError:
                timer.mark("queue")
                info = errors_mod.deadline_exhausted(deadline.elapsed())
                rec.timeout_hit = logline_mod.TIMEOUT_DEADLINE
                rec.result = f"error:{info.category}"
                rec.total = timer.total_str()
                rec.emit()
                return contract_mod.format_error(info)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 防御纵深（零 bug 目标下理论不可达）
            logger.exception("[pipeline] req_id=%s 内部异常", req_id)
            info = errors_mod.internal_error(exc)
            rec.result = f"error:{info.category}"
            rec.total = timer.total_str()
            rec.emit()
            return contract_mod.format_error(info)
        finally:
            if self._tracker is not None:
                self._tracker.exit()

    # ------------------------------------------------------------------
    # 熔断门 + 拆分串行调用（E24/R20/R17）
    # ------------------------------------------------------------------
    async def _generate(
        self, *, req_id: str, rec: logline_mod.LogRecord, timer: _StageTimer,
        deadline: budget_mod.Deadline, prompt: str, size: str,
        batch_size: int, persist_flag: bool, ratio: Optional[str],
        http_headers: Optional[Mapping[str, str]],
    ) -> str:
        # ---- 3. 熔断门路由（工具层无 model 参数 = 恒 auto；E24）----
        decision = router_mod.route(None, self._circuit)
        timer.mark("route")
        rec.route = decision.reason

        if decision.model is None:
            # E24：OPEN 期快失败（rate_limit, retry=yes，注明约 X 秒后重试）
            info = errors_mod.circuit_open_error(decision.retry_after_sec)
            rec.model = router_mod.MODEL_IMAGE
            rec.result = f"error:{info.category}"
            rec.total = timer.total_str()
            rec.emit()
            return contract_mod.format_error(info)

        model = decision.model
        rec.model = model
        normalized_size = normalizer_mod.normalize_size(size)

        # ---- 4. 拆分串行（agnes-image 无原生 batch；单次 1 张）----
        urls: list[str] = []
        last_error: Optional[errors_mod.ErrorInfo] = None
        tier = self._cfg.timeout_agnes_image_sec
        call_count = 0
        probe_sent = False  # 探针是否已实际发出（发出即消费，E8）

        for _ in range(batch_size):
            # R20：每次调用前 deadline 剩余 ≥ 单次档才发起；不足即止
            if deadline.remaining() < tier:
                logger.info(
                    "[pipeline] req_id=%s 预算剩余 %.1fs < 单次档 %.1fs，"
                    "拆分串行即止（%d/%d 已发）",
                    req_id, deadline.remaining(), tier, call_count, batch_size,
                )
                break
            probe_sent = True  # 即将发出（含调用中预算耗尽的情形）
            try:
                api = await self._api.generate_image(
                    model=model, prompt=prompt, size=normalized_size,
                    deadline=deadline, ratio=ratio,
                )
            except budget_mod.DeadlineExceededError:
                # 调用中预算耗尽（退避后 check 抛出）→ 即止，非错误（E9）
                break
            call_count += 1
            timer.mark("api")

            if api.ok and api.urls:
                self._circuit.record_success(model)
                urls.extend(api.urls)
                last_error = None
                continue

            # ---- 单次失败：分类 → 写熔断 → 不中断后续（预算耗尽除外）----
            info = self._classify(api)
            last_error = info
            self._record_failure(model, info)
            logger.warning(
                "[pipeline] req_id=%s 拆分第 %d 次调用失败: %s（不中断后续）",
                req_id, call_count, info.reason,
            )

        # ---- 5. 收尾分流（R17：M=0 走错误路径，不产生部分成功）----
        if not urls:
            # E8：探针请求未发出（R20 首轮即止）→ 归还半开资格；
            # 已发出（含调用中耗尽）= 探针已消费，不归还
            if decision.probe and not probe_sent:
                self._circuit.abandon_probe(model)
            final = last_error or errors_mod.deadline_exhausted(deadline.elapsed())
            rec.result = f"error:{final.category}"
            rec.split_calls = call_count  # T7c：拆分调用数（含中途失败/耗尽）
            rec.total = timer.total_str()
            rec.emit()
            return contract_mod.format_error(final)

        return await self._finish_success(
            req_id=req_id, rec=rec, timer=timer, urls=urls, model=model,
            size=normalized_size, batch_size=batch_size, call_count=call_count,
            persist_flag=persist_flag, deadline=deadline, ratio=ratio,
            http_headers=http_headers, probe=decision.probe,
        )

    # ------------------------------------------------------------------
    # 成功收尾：persist → meta 组装 → 一行日志
    # ------------------------------------------------------------------
    async def _finish_success(
        self, *, req_id: str, rec: logline_mod.LogRecord, timer: _StageTimer,
        urls: list[str], model: str, size: str, batch_size: int,
        call_count: int, persist_flag: bool, deadline: budget_mod.Deadline,
        ratio: Optional[str], http_headers: Optional[Mapping[str, str]],
        probe: bool,
    ) -> str:
        out_urls: list[str] = list(urls)
        persist_status: Optional[str] = None

        if persist_flag:
            # ---- 6. persist（C6 永不报错；E7 下载超时 min(30s, 剩余)）----
            try:
                results = await self._persist.persist_images(
                    tuple(urls), deadline=deadline, req_id=req_id,
                    headers=http_headers,
                )
                out_urls = [u for u, _ in results]
                statuses = {s for _, s in results}
                persist_status = (
                    persist_mod.PERSIST_FAILED
                    if persist_mod.PERSIST_FAILED in statuses
                    else persist_mod.PERSISTED
                )
            except Exception:
                # 双保险：persist 自身永不抛；此处兜住防御纵深（降级原 URL）
                logger.exception("[persist] req_id=%s persist 层异常（降级原 URL）", req_id)
                persist_status = persist_mod.PERSIST_FAILED
            timer.mark("persist")
            rec.persist = persist_status
        else:
            rec.persist = "skipped"

        # C10：水位超线只 WARN + meta 提示（不删图）
        disk_note: Optional[str] = None
        if persist_flag:
            disk_ratio = self._persist.disk_usage_ratio()
            if disk_ratio is not None and disk_ratio >= DISK_WATERMARK_RATIO:
                disk_note = contract_mod.note_disk_watermark(disk_ratio)
                logger.warning(
                    "[persist] req_id=%s 磁盘水位 %.0f%% ≥ %.0f%%（仅提示，不自动删图）",
                    req_id, disk_ratio * 100, DISK_WATERMARK_RATIO * 100,
                )

        meta = contract_mod.MetaInfo(
            model=model, size=size, ratio=ratio,
            # R17：部分成功标记仅在 M<N 时出现（M==N 全成功不输出）
            requested=batch_size if len(urls) < batch_size else None,
            delivered=len(urls) if len(urls) < batch_size else None,
            persist_status=persist_status, disk_note=disk_note,
        )
        rec.result = "ok"
        rec.split_calls = call_count  # T7c：拆分调用数（原生 batch=1 即 1）
        rec.total = timer.total_str()
        rec.emit()
        return contract_mod.format_success(out_urls, meta)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _classify(self, api: agnes_mod.ApiCallResult) -> errors_mod.ErrorInfo:
        if api.status_code is None:  # 网络/超时异常（重试 1 次后仍失败）
            return errors_mod.network_error(api.error_detail)
        return errors_mod.classify_http_status(api.status_code, api.error_detail)

    def _record_failure(self, model: str, info: errors_mod.ErrorInfo) -> None:
        """熔断写入：仅 429（硬）与 5xx/超时/网络（软）；其余类别不写。"""
        if info.category == errors_mod.RATE_LIMIT:
            self._circuit.record_failure(model, circuit_mod.FailureKind.HARD)
        elif info.category == errors_mod.UPSTREAM_ERROR:
            self._circuit.record_failure(model, circuit_mod.FailureKind.SOFT)

    def _new_record(
        self, req_id: str, prompt: str, size: str, batch_size: int,
        persist_flag: bool, ratio: Optional[str] = None,
    ) -> logline_mod.LogRecord:
        return logline_mod.LogRecord(
            req_id=req_id,
            prompt_hash=logline_mod.prompt_hash(prompt),
            params=(
                f"model=agnes-image;size={size};"
                + (f"ratio={ratio};" if ratio is not None else "")
                + f"batch={batch_size};"
                + f"persist={'true' if persist_flag else 'false'}"
            ),
        )
