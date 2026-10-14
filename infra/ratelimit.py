"""上游速率限制器（V3 稳定性；R2 禁线程 —— 纯协程令牌桶）。

为什么需要（压测实证，2026-10-13）：
    V3 把并发放开后（image 泳道 4 × batch 并发 4 = 峰值 16），实测
    多张图同时打上游立刻撞上对方的额度限制（429）→ 硬熔断 600s，
    后续所有请求在 10 分钟内一律快失败。**并发能力不是越大越好**——
    超出上游承受力只会换来更长的不可用窗口。

    故加客户端侧限速：与其撞 429 被熔断，不如自己排队慢慢发。

模型：
    - 令牌桶：UPSTREAM_RPS 匀速补充，桶容量 UPSTREAM_BURST（允许短突发）；
    - 惩罚窗口（penalty box）：收到 429 / 503（上游明确过载）→ 冻结
      UPSTREAM_COOLDOWN_SEC，期间所有请求排队等待而非硬闯；
    - 等待计入 deadline（R3 同纪律）——等不起就快失败，不占着槽空转；
    - 单事件循环内串行访问，无锁（R2）。

不负责：重试次数与退避（仍由网关层 C4 纪律负责）、熔断（circuit）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("mcp_imagegen.ratelimit")


class RateLimiter:
    """令牌桶 + 惩罚窗口（协程实现）。"""

    def __init__(
        self,
        rps: float,
        burst: int = 1,
        cooldown_sec: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], "asyncio.Future[None]"] = asyncio.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError(f"rps 必须 > 0，收到 {rps}")
        if burst < 1:
            raise ValueError(f"burst 必须 ≥ 1，收到 {burst}")
        self._rps = rps
        self._burst = burst
        self._cooldown = max(0.0, cooldown_sec)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._blocked_until = 0.0
        self.stats = {"waited_sec": 0.0, "penalties": 0, "acquires": 0}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def blocked(self) -> float:
        """当前仍处于惩罚窗口的剩余秒数（0 = 未冻结）。"""
        return max(0.0, self._blocked_until - self._clock())

    def available(self) -> float:
        self._refill()
        return self._tokens

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------
    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(
                float(self._burst), self._tokens + elapsed * self._rps)
            self._last = now

    async def acquire(
        self, wait_cap_sec: float = 30.0,
    ) -> float:
        """取一个令牌；必要时等待（返回实际等待秒数）。

        wait_cap_sec 由调用方按 deadline 剩余传入（R3：等待计入预算）；
        等待超限 → 抛 asyncio.TimeoutError，由调用方转为超时/繁忙语义。
        """
        self.stats["acquires"] += 1
        waited = 0.0
        deadline_at = self._clock() + max(0.0, wait_cap_sec)
        while True:
            self._refill()
            block_left = self.blocked()
            if block_left <= 0 and self._tokens >= 1.0:
                self._tokens -= 1.0
                self.stats["waited_sec"] += waited
                return waited
            need = block_left if block_left > 0 else (1.0 - self._tokens) / self._rps
            remaining_cap = deadline_at - self._clock()
            if remaining_cap <= 0:
                raise asyncio.TimeoutError("rate limiter wait exceeded cap")
            step = min(need, remaining_cap, 0.25)
            await self._sleep(max(step, 0.0))
            waited += max(step, 0.0)

    def penalize(self, sec: Optional[float] = None) -> None:
        """上游过载（429/503…）→ 进入/延长惩罚窗口。"""
        amount = self._cooldown if sec is None else min(
            float(sec), self._cooldown * 6 if self._cooldown else 60.0)
        until = self._clock() + max(0.0, amount)
        if until > self._blocked_until:
            self._blocked_until = until
            self.stats["penalties"] += 1
            logger.warning(
                "[ratelimit] 上游过载 → 冻结 %.1fs（期间请求排队，不硬闯）",
                amount,
            )
