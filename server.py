"""MCP 图片生成 Server · 入口（S1 配置自检 + S3 全装配 + S4 systemd 协作）。

装配链：
    config.load_config → 组件（circuit / slot pool / api client / persist /
    pipeline）→ FastMCP（tool + /healthz + /files + lifespan）→ BearerGate
    （/mcp 鉴权）→ uvicorn（0.0.0.0:PORT）。

systemd 协作（Type=notify，6.1/6.2）：
    - startup（绑定完成）→ sd_notify READY=1；
    - 心跳 task（事件循环内，R2 铁律禁线程）每 15s 发 WATCHDOG=1；
    - 每日 04:00 客户端重建（rotate 指针热切换，E6）；
    - SIGTERM → uvicorn 停收新请求 → 在途连接排空
      （timeout_graceful_shutdown=220 = TimeoutStopSec）→ lifespan
      shutdown：STOPPING=1 + 组件 aclose 收尾 → 退出。

运行：
    python server.py            # 前台服务（systemd 下运行）
    python server.py --check    # 仅配置自检（退出码 0/2，部署前检查）
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

import uvicorn
from fastmcp import FastMCP

from app import endpoints, tool
from app.pipeline import GeneratePipeline
from app.video_pipeline import VideoPipeline
from config import SYSTEMD_TIMEOUT_STOP_SEC, Config, ConfigError, load_config
from infra import agnes, budget, circuit, persist, tasks
from ops import liveness, shutdown

logger = logging.getLogger("mcp_imagegen.server")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_app(
    cfg: Config,
    components: "Components | None" = None,
    *,
    transport=None,
    persist_transport=None,
    notifier: "liveness.SdNotifier | None" = None,
) -> "BearerApp":
    """组件组装 + ASGI 应用（供 uvicorn 与端到端测试）。

    transport/persist_transport/notifier 仅供测试注入；生产缺省 None。
    """
    if components is None:
        components = Components.build(
            cfg, notifier=notifier, transport=transport,
            persist_transport=persist_transport,
        )

    @contextlib.asynccontextmanager
    async def lifespan(app) -> AsyncIterator[None]:
        # ---- startup（uvicorn 绑定完成后进入）：READY=1 + 后台任务 ----
        components.notifier.notify("READY=1")
        logger.info(
            "[startup] 就绪 port=%d image_dir=%s video_dir=%s 并发=%d 总预算=%.0fs",
            cfg.port, cfg.image_dir, cfg.video_dir,
            cfg.max_concurrency, cfg.total_budget_sec,
        )
        heartbeat = asyncio.create_task(
            liveness.heartbeat_loop(components.notifier),
            name="watchdog-heartbeat",
        )
        rebuild = asyncio.create_task(
            liveness.daily_rebuild_loop(components.api_client),
            name="daily-rebuild",
        )
        # T9：任务表 TTL 清理（事件循环协程，禁线程 R2；与心跳同纪律）
        cleanup = asyncio.create_task(
            components.task_table.cleanup_loop(),
            name="task-table-cleanup",
        )
        try:
            yield
        finally:
            # ---- shutdown：停机通知 → 在途对账 → 组件收尾 ----
            heartbeat.cancel()
            rebuild.cancel()
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            with contextlib.suppress(asyncio.CancelledError):
                await rebuild
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            components.coordinator.begin_shutdown()  # STOPPING=1 + 日志
            await components.coordinator.wait_drained(SYSTEMD_TIMEOUT_STOP_SEC)
            await components.api_client.aclose()
            await components.persist.aclose()
            logger.info("[shutdown] 组件收尾完成，进程退出")

    mcp = FastMCP(name="mcp-imagegen", lifespan=lifespan)
    tool.register_tool(mcp, components.pipeline, components.video_pipeline)
    mcp.custom_route("/healthz", methods=["GET"])(endpoints.healthz_endpoint)
    mcp.custom_route("/files/{name}", methods=["GET"])(
        endpoints.make_files_endpoint(cfg.image_dir, cfg.video_dir))

    inner = mcp.http_app(path="/mcp")
    return BearerApp(inner, cfg.mcp_access_token, lifespan_holder=lifespan)


class BearerApp:
    """ASGI 组合：BearerGate(/mcp 鉴权) + fastmcp http_app（lifespan 透传）。"""

    def __init__(self, inner, token: str, lifespan_holder=None) -> None:
        self.gate = endpoints.BearerGate(inner, token)
        self._inner = inner
        self._lifespan_holder = lifespan_holder  # 仅供测试引用

    async def __call__(self, scope, receive, send) -> None:
        await self.gate(scope, receive, send)

    @property
    def routes(self):
        return getattr(self._inner, "routes", [])


class Components:
    """全组件集合（构造顺序 = 依赖顺序；单一事件循环内使用）。"""

    def __init__(
        self, cfg: Config, circuit_: circuit.CircuitBreaker,
        pool: budget.SlotPool, api_client: agnes.AgnesClient,
        persist_: persist.PersistService, pipeline: GeneratePipeline,
        notifier: liveness.SdNotifier, coordinator: shutdown.ShutdownCoordinator,
        task_table: tasks.TaskTable, video_pipeline: VideoPipeline,
    ) -> None:
        self.cfg = cfg
        self.circuit = circuit_
        self.pool = pool
        self.api_client = api_client
        self.persist = persist_
        self.pipeline = pipeline
        self.notifier = notifier
        self.coordinator = coordinator
        self.task_table = task_table        # T9：query_video 对账状态源
        self.video_pipeline = video_pipeline  # T9：与生图共用槽池/persist/停机协调

    @classmethod
    def build(
        cls,
        cfg: Config,
        *,
        notifier: liveness.SdNotifier | None = None,
        transport=None,
        persist_transport=None,
    ) -> "Components":
        notifier = notifier if notifier is not None else liveness.SdNotifier()
        circuit_ = circuit.CircuitBreaker(
            hard_sec=cfg.circuit_hard_sec, soft_sec=cfg.circuit_soft_sec
        )
        pool = budget.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
        api_client = agnes.AgnesClient(cfg, transport=transport)
        persist_ = persist.PersistService(cfg, transport=persist_transport)
        coordinator = shutdown.ShutdownCoordinator(notifier=notifier)
        pipeline = GeneratePipeline(
            cfg, pool, circuit_, api_client, persist_, tracker=coordinator,
        )
        # T9：任务表 + 视频流水线（共用 slot pool / persist / 停机协调；
        # query_video 不占并发槽——槽只在 run 路径获取，E20）
        task_table = tasks.TaskTable(
            ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max)
        video_pipeline = VideoPipeline(
            cfg, pool, api_client, task_table, tracker=coordinator,
            persist=persist_,
        )
        return cls(
            cfg, circuit_, pool, api_client, persist_, pipeline,
            notifier, coordinator, task_table, video_pipeline,
        )


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
async def serve(cfg: Config, components: Components | None = None) -> int:
    """前台运行（systemd 守护）。信号由 uvicorn 捕获（不与之竞争安装）。"""
    if components is None:
        components = Components.build(cfg)
    await components.persist.start()
    app = build_app(cfg, components)
    uconfig = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=cfg.port,
        log_level="info",
        access_log=False,  # 每请求一行访问日志由 logline 统一输出（避免双行）
        timeout_graceful_shutdown=int(SYSTEMD_TIMEOUT_STOP_SEC),  # ≤ TimeoutStopSec
    )
    server = uvicorn.Server(uconfig)
    try:
        await server.serve()
    finally:
        await components.persist.aclose()
        await components.api_client.aclose()
    return 0


def main(check_only: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[startup] 配置自检失败: {exc}", file=sys.stderr)
        return 2

    for line in cfg.self_check_report():
        print(line)

    if check_only:
        print("[startup] 配置自检通过（--check）")
        return 0

    return asyncio.run(serve(cfg))


if __name__ == "__main__":
    raise SystemExit(main(check_only="--check" in sys.argv))
