"""心跳与每日重建调度（规格书 6.1/6.2；红线 C7/R2、B4、E6）。

铁律（R2）：watchdog 心跳**绝不放独立线程**——用事件循环内 asyncio task
借位构造代码路径；禁 threading / asyncio.to_thread / run_in_executor
（tests/test_no_threads.py 静态门禁持续守护）。心跳协程独立于请求路径：
长请求进行中事件循环照常转动，心跳照常发——一旦循环僵死，心跳停止，
systemd Watchdog（30s）判死并重启。

sd_notify：vendored 极简实现（Unix 数据报 → NOTIFY_SOCKET，抽象命名空间
"@" 前缀兼容），不依赖 sdnotify 包；Type=notify 需启动发 READY=1。

每日重建（E6）：每天 DAILY_REBUILD_HOUR(04:00) 本地时间调
AgnesClient.rotate()（指针热切换，旧 client 等在途请求完成后
aclose）。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import socket
from typing import Awaitable, Callable, Optional

import config

logger = logging.getLogger("mcp_imagegen.liveness")


# ---------------------------------------------------------------------------
# vendored sd_notify（等价实现；替代 sdnotify 包）
# ---------------------------------------------------------------------------
def _sd_socket_path(path: Optional[str]) -> Optional[str]:
    """归一化 NOTIFY_SOCKET：'@' 前缀 → 抽象命名空间（NUL 开头）。"""
    if not path:
        return None
    if path.startswith("@"):
        return "\0" + path[1:]
    return path


class SdNotifier:
    """向 NOTIFY_SOCKET 发送 sd_notify 消息（不可达时静默 False，永不抛错）。

    systemd 语义即不可靠数据报：发送失败不影响主流程。
    """

    def __init__(self, socket_path: Optional[str] = None) -> None:
        self._path = _sd_socket_path(
            socket_path if socket_path is not None else os.environ.get("NOTIFY_SOCKET")
        )

    @property
    def available(self) -> bool:
        return self._path is not None

    def notify(self, message: str) -> bool:
        """发送单条消息（如 'READY=1' / 'WATCHDOG=1' / 'STOPPING=1'）。"""
        if self._path is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(self._path)
                sock.send(message.encode("utf-8"))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# watchdog 心跳（B4：间隔 HEARTBEAT_INTERVAL_SEC < WatchdogSec/2）
# ---------------------------------------------------------------------------
async def heartbeat_loop(
    notifier: SdNotifier,
    interval_sec: float = config.HEARTBEAT_INTERVAL_SEC,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """每醒一次即循环转动的证明：发 WATCHDOG=1 → 睡 interval → 循环。"""
    while True:
        notifier.notify("WATCHDOG=1")
        await sleep(interval_sec)


# ---------------------------------------------------------------------------
# 每日 04:00 客户端重建调度（E6：rotate 指针热切换 + 排空关闭）
# ---------------------------------------------------------------------------
def seconds_until_next(
    hour: int, now: Optional[datetime.datetime] = None
) -> float:
    """距下一个 `hour` 点整的秒数（本地时间；注入 now 供单测）。"""
    now = now if now is not None else datetime.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


async def daily_rebuild_loop(
    client,
    hour: int = config.DAILY_REBUILD_HOUR,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime.datetime] = datetime.datetime.now,
) -> None:
    """每天 hour 点触发一次 client.rotate()；rotate 异常不致死循环。"""
    while True:
        await sleep(seconds_until_next(hour, now()))
        logger.info("[rebuild] 每日客户端重建触发（rotate）")
        try:
            await client.rotate()
            logger.info("[rebuild] 每日客户端重建完成")
        except asyncio.CancelledError:
            raise
        except Exception:
            # 调度循环绝不因单次失败退出（明日再试）；异常细节已由 rotate 记录
            logger.exception("[rebuild] 每日客户端重建失败（不影响服务）")
