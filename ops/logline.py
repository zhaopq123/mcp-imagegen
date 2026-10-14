"""一行结构化日志（规格书八节；红线 R13；V2 三节 T7c 字段扩展）。

15 字段（缺一不可，缺失显式写 null 而非省略）：
    ts / req_id / prompt_hash / route(路由决策来源) / model(最终模型) /
    params(参数摘要) / stages(各阶段耗时) / result(ok 或 error:<category>
    或 running——E17 视频超预算非错误收尾) / fallback(兜底标记+原因) /
    timeout_hit(超时触发标记) / persist(状态) / task_id(视频任务号，非视频
    请求显式 null) / poll_count(视频轮询次数，生图 null) / split_calls(
    生图拆分调用数，原生 batch=1 即 1；视频 null) / total(总耗时)

- 一行一条（journald 单行可 grep）；值内含空格/分号属预期，消费方按
  key= 前缀解析；
- prompt 只记 sha256 前 16 位，不记原文（审计够用 + 不泄内容）；
- 本模块纯格式化，无副作用；由 pipeline 在请求收尾时调用一次 emit；
- 规格书八节的 provider 字段按方案 V2 三节建议省略（早期单一提供方恒
  单一厂商，零信息量；改动记录已注明）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from dataclasses import dataclass
from typing import ClassVar, Optional

logger = logging.getLogger("mcp_imagegen.access")

# 字段全集（有限集，供单测断言；顺序即输出顺序）
# V2（T7c）12 → 15：+task_id / +poll_count / +split_calls，置于 persist
# 之后、total 之前（total 保持末位收尾；消费方按 key= 前缀解析，顺序
# 变更无解析影响）。
FIELDS: tuple[str, ...] = (
    "ts", "req_id", "prompt_hash", "route", "model", "params",
    "stages", "result", "fallback", "timeout_hit", "persist",
    "task_id", "poll_count", "split_calls", "total",
)

# timeout_hit 有限枚举（哪个环节的钟先响；null = 无超时）
TIMEOUT_NONE = "null"
TIMEOUT_QUEUE_WAIT = "queue_wait"  # 排队等待超限 → busy
TIMEOUT_DEADLINE = "deadline"      # 200s 预算耗尽
TIMEOUT_GUARD = "guard"           # 整体 asyncio.timeout 护栏（兜底，理论不可达）


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class LogRecord:
    """一次工具请求的完整审计记录（pipeline 逐阶段填充；生图/视频共用）。"""

    req_id: str
    prompt_hash: str = "null"
    route: str = "null"            # router reason 枚举（explicit/cjk/…/bypass_circuit）
    model: str = "null"           # 实际执行的（最后一个）模型
    params: str = "null"          # 请求参数摘要（不含 prompt 原文）
    stages: str = "null"          # 阶段=耗时 串
    result: str = "null"          # ok | error:<category> | running(E17)
    fallback: str = "null"         # <原因>→<目标>（无兜底为 null）
    timeout_hit: str = TIMEOUT_NONE
    persist: str = "null"          # persisted | persist_failed | skipped
    task_id: Optional[str] = None   # 视频任务号（非视频请求显式 null）
    poll_count: Optional[int] = None   # 视频轮询次数（生图 null）
    split_calls: Optional[int] = None  # 生图拆分调用数（原生 batch=1 即 1；视频 null）
    total: str = "null"
    # V5：不进 15 字段，仅给指标 hook 用（image / video），保持行格式不变
    kind: str = "image"
    provider: str = ""

    def render(self) -> str:
        """一行 15 字段（顺序与 FIELDS 严格一致；缺失显式 null）。"""
        parts = [
            f"ts={self._ts()}",
            f"req_id={self.req_id}",
            f"prompt_hash={self.prompt_hash}",
            f"route={self.route}",
            f"model={self.model}",
            f"params={self.params}",
            f"stages={self.stages}",
            f"result={self.result}",
            f"fallback={self.fallback}",
            f"timeout_hit={self.timeout_hit}",
            f"persist={self.persist}",
            f"task_id={self.task_id if self.task_id is not None else 'null'}",
            f"poll_count={self.poll_count if self.poll_count is not None else 'null'}",
            f"split_calls={self.split_calls if self.split_calls is not None else 'null'}",
            f"total={self.total}",
        ]
        return " ".join(parts)

    # ts 单独成方法：单测可注入固定时钟（无需 monkeypatch datetime）
    _clock = staticmethod(_dt.datetime.now)

    @classmethod
    def _ts(cls) -> str:
        return cls._clock().astimezone().isoformat(timespec="milliseconds")

    # 行内 ejected 之外的旁路：指标收集（V5）
    #
    # 为什么走 hook 而不是让 pipeline 直接 import metrics：logline 是
    # 「每请求恰好一次」的唯一出口，指标挂在这里就天然保证不重不漏，
    # 也不会把埋点漏在某条分支上（历史上这种漏统计很难查）。
    _hooks: ClassVar[list] = []

    @classmethod
    def add_hook(cls, fn) -> None:
        cls._hooks.append(fn)

    @classmethod
    def clear_hooks(cls) -> None:
        cls._hooks.clear()

    def emit(self) -> None:
        """输出一行（唯一出口；pipeline 每请求恰好调用一次）。"""
        logger.info("%s", self.render())
        for hook in self._hooks:
            try:
                hook(self)
            except Exception:  # noqa: BLE001 —— 埋点绝不能反过来影响业务路径
                pass
