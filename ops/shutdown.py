"""优雅停机（规格书 6.2；R6/⑮）。

职责：
- 停收新请求：uvicorn 收 SIGTERM/SIGINT（capture_signals→should_exit）
  即停止 accept，监听关闭后新连接被拒——不与 uvicorn 抢信号安装；
- 在途排空：uvicorn 等待既有连接完成（timeout_graceful_shutdown=
  TimeoutStopSec=220 强制上限）；本协调器维护在途计数，停机开始时发
  sd_notify STOPPING=1 并打点日志，供排空对账；
- 资源收尾（lifespan shutdown 阶段执行）：AgnesClient.aclose()
  （排空每日重建遗留）+ PersistService.aclose()。

不做：强杀（systemd TimeoutStopSec 之后 SIGKILL 的事）。

信号路径：SIGTERM → uvicorn.handle_exit → should_exit=True → 停 accept
→ 连接排空 → Starlette lifespan shutdown → 本模块 begin_shutdown()
（STOPPING=1 + 日志）→ wait_drained()（对账）→ aclose 收尾 → 进程退出。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import config

logger = logging.getLogger("mcp_imagegen.shutdown")


class ShutdownCoordinator:
    """在途请求计数 + 停机通知 + 排空对账（单事件循环内调用，无锁）。"""

    def __init__(self, notifier=None, drain_poll_sec: float = 0.5) -> None:
        self._notifier = notifier
        self._drain_poll_sec = drain_poll_sec
        self._in_flight = 0
        self._stopping = False

    # ------------------------------------------------------------------
    # 在途计数（pipeline 进/出各调一次；异常路径 finally 保证 exit）
    # ------------------------------------------------------------------
    def enter(self) -> None:
        self._in_flight += 1

    def exit(self) -> None:  # noqa: A001  (与内建 exit 撞名；此处语义即"离开")
        if self._in_flight > 0:
            self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def stopping(self) -> bool:
        return self._stopping

    # ------------------------------------------------------------------
    # 停机序列（lifespan shutdown 阶段调用；此时 uvicorn 已停收新请求）
    # ------------------------------------------------------------------
    def begin_shutdown(self) -> None:
        """标记停机 + sd_notify STOPPING=1（幂等）。"""
        if self._stopping:
            return
        self._stopping = True
        if self._notifier is not None:
            self._notifier.notify("STOPPING=1")
        logger.info("[shutdown] 停机开始：停收新请求，在途 %d 个排空中", self._in_flight)

    async def wait_drained(
        self,
        timeout_sec: float = config.SYSTEMD_TIMEOUT_STOP_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> bool:
        """等待在途清零；超时返回 False（不抛错——systemd 兜底强杀）。"""
        elapsed = 0.0
        while self._in_flight > 0 and elapsed < timeout_sec:
            await sleep(self._drain_poll_sec)
            elapsed += self._drain_poll_sec
        if self._in_flight > 0:
            logger.warning(
                "[shutdown] 排空 %.0fs 后仍有 %d 在途（systemd 将 SIGKILL 兜底）",
                elapsed, self._in_flight,
            )
            return False
        logger.info("[shutdown] 在途清零（耗时 %.1fs）", elapsed)
        return True
