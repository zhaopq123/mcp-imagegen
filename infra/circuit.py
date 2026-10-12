"""熔断器三态状态机（规格书 4.2；红线 C8、R4）。

- 三态：CLOSED → OPEN（硬 600s / 软 300s，来自 config）→ 到期 HALF_OPEN
  → 仅第一个请求获探针资格 → 成功→CLOSED，失败→续期一轮 OPEN；
- 硬熔断：429/402（额度/限流）；软熔断：连续 2 次超时/5xx，成功即清零；
- 半开期间的后续请求仍按 OPEN 处理（防半开泄洪，E8/R4）；
- 纯内存态不落盘（重启即清零，C8）；
- 单线程 asyncio 事件循环内调用，状态变更无 await 点 → 天然原子，无需锁。

E2：显式指定 model 的请求绕过熔断表（router 层不查询），但其成功/失败
结果照样写入熔断状态（record_success 解除熔断 / record_failure 续期）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional


class FailureKind(Enum):
    """熔断触发源：HARD=429/402；SOFT=5xx/超时/网络。"""

    HARD = "hard"
    SOFT = "soft"


# 状态字符串（进日志 / meta）
STATE_CLOSED = "CLOSED"
STATE_OPEN_HARD = "OPEN_HARD"   # 硬熔断（429/402）→ CIRCUITHARDSEC=600s
STATE_OPEN_SOFT = "OPEN_SOFT"   # 软熔断（连败 2 次）→ CIRCUITSOFTSEC=300s
STATE_HALF_OPEN = "HALF_OPEN"   # 到期放行探针

SOFT_FAILURE_THRESHOLD = 2  # 连续 2 次软失败触发熔断


@dataclass(frozen=True)
class CircuitStatus:
    """check() 结果。

    - open=True → 路由②应绕开该模型；
    - probe=True → 本次调用获得探针资格（半开后的第一个请求）；
      注意：一旦授予，下一次 check 将按 OPEN 处理，直到状态变迁（R4）。
    """

    model: str
    state: str
    open: bool
    probe: bool = False
    kind: Optional[FailureKind] = None
    remaining_sec: float = 0.0  # 距离到期（OPEN 态）


@dataclass
class _ModelEntry:
    state: str = STATE_CLOSED
    kind: Optional[FailureKind] = None  # 当前 OPEN 的触发源（续期时沿用）
    opened_at: float = 0.0
    soft_fail_count: int = 0
    probe_taken: bool = False  # 半开探针资格已授予且未归还


class CircuitBreaker:
    """每模型一个状态条目；check 只读+探针资格授予，record_* 写入。"""

    def __init__(
        self,
        hard_sec: float,
        soft_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if hard_sec <= 0 or soft_sec <= 0:
            raise ValueError("熔断时长必须 > 0")
        self._hard_sec = hard_sec
        self._soft_sec = soft_sec
        self._clock = clock
        self._entries: Dict[str, _ModelEntry] = {}

    # ------------------------------------------------------------------
    # 查询（路由②调用；E2：显式路由不经过这里）
    # ------------------------------------------------------------------
    def check(self, model: str) -> CircuitStatus:
        entry = self._entries.get(model)
        if entry is None:
            # 纯查询不落表（V2：审核类等不写熔断的失败不得因查询留下
            # 'CLOSED' 痕迹——snapshot 反映"真实发生过写"的模型集）
            return CircuitStatus(model, STATE_CLOSED, open=False)

        if entry.state in (STATE_OPEN_HARD, STATE_OPEN_SOFT):
            duration = self._hard_sec if entry.state == STATE_OPEN_HARD else self._soft_sec
            elapsed = self._clock() - entry.opened_at
            if elapsed >= duration:
                # 到期 → HALF_OPEN（放行第一个请求探针验证）
                entry.state = STATE_HALF_OPEN
                entry.probe_taken = False
            else:
                return CircuitStatus(
                    model, entry.state, open=True,
                    kind=entry.kind, remaining_sec=duration - elapsed,
                )

        # HALF_OPEN：仅第一个请求获探针资格；后续按 OPEN 处理
        if entry.state == STATE_HALF_OPEN:
            if not entry.probe_taken:
                entry.probe_taken = True
                return CircuitStatus(model, STATE_HALF_OPEN, open=False, probe=True)
            return CircuitStatus(
                model, STATE_HALF_OPEN, open=True,
                kind=entry.kind, remaining_sec=self._open_duration(entry.kind),
            )

        return CircuitStatus(model, STATE_CLOSED, open=False)

    # ------------------------------------------------------------------
    # 写入（pipeline 调用；成功/失败/显式请求结果都进这里）
    # ------------------------------------------------------------------
    def record_success(self, model: str) -> None:
        """任何成功 → 软失败计数清零；非 CLOSED 态（含显式请求）→ 解除熔断。"""
        entry = self._entries.setdefault(model, _ModelEntry())
        entry.soft_fail_count = 0
        if entry.state != STATE_CLOSED:
            entry.state = STATE_CLOSED
            entry.probe_taken = False

    def record_failure(self, model: str, kind: FailureKind) -> None:
        """失败写入。

        - HARD：直接 OPEN_HARD（刷新计时）；
        - SOFT：CLOSED 态连败 2 次触发 OPEN_SOFT；OPEN 态（显式请求失败/
          探针失败）续期一轮，硬熔断不因软失败降级。
        """
        entry = self._entries.setdefault(model, _ModelEntry())
        now = self._clock()
        if kind is FailureKind.HARD:
            entry.state = STATE_OPEN_HARD
            entry.kind = FailureKind.HARD
            entry.opened_at = now
            entry.soft_fail_count = 0
            entry.probe_taken = False
            return

        # SOFT 失败
        entry.soft_fail_count += 1
        if entry.state in (STATE_OPEN_HARD, STATE_OPEN_SOFT):
            # 显式请求失败 / 探针失败 → 续期一轮（沿用原 kind；硬不降级）
            if entry.state == STATE_OPEN_SOFT:
                entry.kind = FailureKind.SOFT
            entry.opened_at = now
            entry.probe_taken = False
        elif entry.state == STATE_HALF_OPEN:
            # 探针失败 → 按失败种类续期一轮
            entry.state = (
                STATE_OPEN_HARD if kind is FailureKind.HARD else STATE_OPEN_SOFT
            )
            entry.kind = kind
            entry.opened_at = now
            entry.probe_taken = False
        else:  # CLOSED
            if entry.soft_fail_count >= SOFT_FAILURE_THRESHOLD:
                entry.state = STATE_OPEN_SOFT
                entry.kind = FailureKind.SOFT
                entry.opened_at = now
                entry.probe_taken = False

    def abandon_probe(self, model: str) -> None:
        """回收探针资格（pipeline 在探针请求未能发出时调用，防半开卡死）。

        不推进 opened_at：下一 check 重新授予资格，状态机不悬死。
        """
        entry = self._entries.get(model)
        if entry is not None and entry.state == STATE_HALF_OPEN:
            entry.probe_taken = False

    # ------------------------------------------------------------------
    def _open_duration(self, kind: Optional[FailureKind]) -> float:
        return self._hard_sec if kind is FailureKind.HARD else self._soft_sec

    def snapshot(self) -> Dict[str, str]:
        """当前各模型状态（进日志/巡检用）。"""
        return {m: e.state for m, e in self._entries.items()}
