"""服务入口：装配 → 自检 → 前台运行。

装配链::

    load_config → Store（Provider/令牌/管理员凭据）→ ProviderGateway
    → 多队列泳道池 → Persist → 生图管道 / 视频管道 → FastMCP（工具 +
    /healthz + /files + /upload + /admin）→ 访问令牌门 → uvicorn

systemd 协作（Type=notify）：
    - 绑定完成 → sd_notify READY=1；
    - 心跳协程每 15s 发 WATCHDOG=1（R2：绝不开线程）；
    - 连接池定期热重建（CLIENT_ROTATE_SEC）；
    - SIGTERM → uvicorn 停收新请求 → 在途排空（TimeoutStopSec）→
      lifespan shutdown：STOPPING=1 + 组件收尾 → 退出。

运行::

    python server.py            # 前台服务
    python server.py --check    # 仅自检（退出码 0/2）
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import AsyncIterator

import uvicorn
from fastmcp import FastMCP

from admin import AdminAPI, AdminContext, RingLogHandler
from admin.auth import SessionManager
from app import endpoints, tool
from app import coderender_pipeline
from app.pipeline import GeneratePipeline
from app.video_pipeline import VideoPipeline
from config import SYSTEMD_TIMEOUT_STOP_SEC, Config, ConfigError, load_config
from infra import budget, circuit, persist, tasks
from ops import liveness, logline, metrics as metrics_mod, shutdown
from providers import Store, gateway as gateway_mod
from providers import coderender as coderender_mod
from providers.store import new_token

logger = logging.getLogger("mcp_imagegen.server")

VERSION = "5.0.0"


# ---------------------------------------------------------------------------
# 组件集合
# ---------------------------------------------------------------------------
class Components:
    """全部组件（构造顺序 = 依赖顺序；单一事件循环内使用）。"""

    def __init__(
        self, cfg: Config, store: Store, circuit_: circuit.CircuitBreaker,
        pool: budget.SlotPool, gateway: gateway_mod.ProviderGateway,
        persist_: persist.PersistService, pipeline: GeneratePipeline,
        notifier: liveness.SdNotifier, coordinator: shutdown.ShutdownCoordinator,
        task_table: tasks.TaskTable, video_pipeline: VideoPipeline,
        metrics: metrics_mod.Metrics, ring: RingLogHandler,
        sessions: SessionManager,
        code_render: coderender_pipeline.CodeRenderPipeline,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.circuit = circuit_
        self.pool = pool
        self.gateway = gateway
        self.persist = persist_
        self.pipeline = pipeline
        self.notifier = notifier
        self.coordinator = coordinator
        self.task_table = task_table
        self.video_pipeline = video_pipeline
        self.metrics = metrics
        self.ring = ring
        self.sessions = sessions
        self.code_render = code_render

    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        cfg: Config,
        *,
        store: Store | None = None,
        notifier: liveness.SdNotifier | None = None,
        transport=None,
        persist_transport=None,
    ) -> "Components":
        store = store or Store(cfg.store_path)
        if not store.loaded:
            store.load()
        notifier = notifier if notifier is not None else liveness.SdNotifier()
        circuit_ = circuit.CircuitBreaker(
            hard_sec=cfg.circuit_hard_sec, soft_sec=cfg.circuit_soft_sec)
        pool = budget.MultiQueuePool(
            {
                "image": budget.QueueLane(
                    "image", cfg.image_max_concurrency,
                    cfg.image_queue_wait_sec, cfg.image_queue_max),
                "video": budget.QueueLane(
                    "video", cfg.video_max_concurrency,
                    cfg.video_queue_wait_sec, cfg.video_queue_max),
            },
            default="image",
        )
        gateway = gateway_mod.ProviderGateway(
            cfg, store, transport=transport)
        persist_ = persist.PersistService(cfg, transport=persist_transport)
        coordinator = shutdown.ShutdownCoordinator(notifier=notifier)
        pipeline = GeneratePipeline(
            cfg, pool, circuit_, gateway, persist_, tracker=coordinator)
        task_table = tasks.JobTable(
            ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max)
        video_pipeline = VideoPipeline(
            cfg, pool, gateway, task_table, tracker=coordinator,
            persist=persist_)
        code_render = coderender_pipeline.CodeRenderPipeline(cfg)

        metrics = metrics_mod.Metrics()
        ring = RingLogHandler()
        sessions = SessionManager(store)
        components = cls(
            cfg, store, circuit_, pool, gateway, persist_, pipeline,
            notifier, coordinator, task_table, video_pipeline,
            metrics, ring, sessions, code_render)
        components._install_metrics_hook()
        return components

    # ------------------------------------------------------------------
    def _install_metrics_hook(self) -> None:
        """把指标采集挂到「每请求恰好一次」的日志出口上。

        不挂在 pipeline 里是为了：任何一条新增的收尾分支都不会漏统计——
        漏统计的指标比没有指标更危险（会让人误判系统健康）。
        """
        logline.LogRecord.clear_hooks()
        logline.LogRecord.add_hook(self._metrics_hook)

    def _metrics_hook(self, record: logline.LogRecord) -> None:
        total_raw = str(record.total or "").rstrip("s")
        try:
            latency_ms = float(total_raw) * 1000 if total_raw else 0.0
        except ValueError:
            latency_ms = 0.0
        self.metrics.record(
            kind=getattr(record, "kind", "image"),
            result=record.result or "null",
            latency_ms=latency_ms,
            provider=getattr(record, "provider", "") or "",
            detail=record.params or "",
        )

    # ------------------------------------------------------------------
    # 热更新（管理面板 → 运行参数）
    # ------------------------------------------------------------------
    def apply_settings(self, patch: dict) -> "Config":
        """应用可热更新的运行参数，返回新 Config。

        非法值抛 ConfigError（由面板转 400）。返回新 cfg 而不是快照字典：
        调用方（管理面板）要拿它替换自己的引用——否则面板读到的还是旧值，
        用户会看到"改了没生效"，而实际上运行态早就换了。
        """
        new_cfg = self.cfg.with_overrides(patch)
        new_cfg.self_check_report()  # 违例即抛，绝不把坏配置写进运行态
        self.cfg = new_cfg
        lane_image = self.pool.resize(
            "image", new_cfg.image_max_concurrency,
            new_cfg.image_queue_wait_sec, new_cfg.image_queue_max)
        lane_video = self.pool.resize(
            "video", new_cfg.video_max_concurrency,
            new_cfg.video_queue_wait_sec, new_cfg.video_queue_max)
        self.pipeline.reconfigure(new_cfg)
        self.video_pipeline.reconfigure(new_cfg)
        self.persist.reconfigure(new_cfg)
        self.gateway.reconfigure(new_cfg)
        self.code_render.reconfigure(new_cfg)
        logger.info(
            "[settings] 运行参数已热更新 | 泳道 生图%s 视频%s",
            lane_image.stats(), lane_video.stats())
        return new_cfg


# ---------------------------------------------------------------------------
# 引导：令牌与管理员
# ---------------------------------------------------------------------------
def bootstrap_access(cfg: Config, store: Store, log=logger) -> None:
    """确保至少有一个可用访问令牌（否则客户端一个都连不上）。

    优先级：store 里已有 → 不动；
           环境变量 MCPACCESS_TOKEN（兼容旧部署）→ 落成 store 里的 default；
           都没有 → 自动签发一个随机令牌并打印到日志（**唯一一次**明文机会）。
    """
    if store.tokens:
        return
    from providers.schema import TokenRecord

    secret = (cfg.mcp_access_token or "").strip()
    source = "环境变量 MCPACCESS_TOKEN"
    if not secret:
        secret = new_token()
        source = "随机生成"
    store.upsert_token(TokenRecord(
        id="default", name="默认令牌", token=secret,
        enabled=True, created_at=time.time()))
    log.warning(
        "[bootstrap] 已创建默认访问令牌（来源：%s）：%s\n"
        "            请把它配到 MCP 客户端的 Authorization: Bearer <token>；\n"
        "            之后可在管理面板「访问令牌」里随时轮换或新增。",
        source, secret)


# ---------------------------------------------------------------------------
# ASGI 应用
# ---------------------------------------------------------------------------
def build_app(
    cfg: Config,
    components: "Components | None" = None,
    *,
    transport=None,
    persist_transport=None,
    notifier: "liveness.SdNotifier | None" = None,
) -> "AccessApp":
    """组件组装 + ASGI 应用（供 uvicorn 与端到端测试）。"""
    if components is None:
        components = Components.build(
            cfg, notifier=notifier, transport=transport,
            persist_transport=persist_transport)

    @contextlib.asynccontextmanager
    async def lifespan(app) -> AsyncIterator[None]:
        active = components
        active.notifier.notify("READY=1")
        logger.info(
            "[startup] 就绪 port=%d image_dir=%s video_dir=%s 总预算=%.0fs | "
            "泳道 %s | 服务提供方 %d 个（生图 %s / 视频 %s）| 代码渲染=%s "
            "中文字体=%s",
            active.cfg.port, active.cfg.image_dir, active.cfg.video_dir,
            active.cfg.total_budget_sec, active.pool.stats(),
            len(active.gateway.stats()),
            active.gateway.has_capability("image"),
            active.gateway.has_capability("video"),
            # 顺手把渲染器探活结果预热并打进启动日志：render_image /
            # render_video 缺依赖时要能一眼看出来（否则只有用户调用才知道）
            coderender_mod.renderer_available() or "不可用",
            # 中文字体缺失时中文会被静默丢掉，必须在启动日志里可见
            coderender_mod.cjk_font() or "缺失（中文会掉字）",
        )
        heartbeat = asyncio.create_task(
            liveness.heartbeat_loop(active.notifier), name="watchdog-heartbeat")
        rebuild = asyncio.create_task(
            liveness.interval_rebuild_loop(
                active.gateway, active.cfg.client_rotate_sec),
            name="pool-rebuild")
        cleanup = asyncio.create_task(
            active.task_table.cleanup_loop(), name="task-table-cleanup")
        sweeper = asyncio.create_task(
            _session_sweep_loop(active.sessions), name="session-sweep")
        try:
            yield
        finally:
            for task_ in (heartbeat, rebuild, cleanup, sweeper):
                task_.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task_
            active.coordinator.begin_shutdown()
            # 先停后台视频作业跟踪（等待方立即转 deferred；上游任务不受影响）
            active.video_pipeline.cancel_background()
            await active.coordinator.wait_drained(SYSTEMD_TIMEOUT_STOP_SEC)
            await active.gateway.aclose()
            await active.persist.aclose()
            logger.info("[shutdown] 组件收尾完成，进程退出")

    mcp = FastMCP(name="mcp-imagegen", lifespan=lifespan)
    tool.register_tool(
        mcp, components.pipeline, components.video_pipeline,
        components.code_render)
    mcp.custom_route("/healthz", methods=["GET"])(
        endpoints.make_healthz_endpoint(components))
    mcp.custom_route("/files/{name}", methods=["GET"])(
        endpoints.make_files_endpoint(
            components.cfg.image_dir, components.cfg.video_dir))
    mcp.custom_route("/upload", methods=["POST"])(
        endpoints.make_upload_endpoint(
            components.cfg.image_dir,
            max_bytes=components.cfg.upload_max_bytes))

    # ---- 管理面板：整棵子树交给 AdminAPI 自己分发 ----
    prefix = (components.cfg.admin_path_prefix or "/admin").rstrip("/")
    admin_api = AdminAPI(AdminContext(
        cfg=components.cfg, store=components.store, gateway=components.gateway,
        pool=components.pool, video_pipeline=components.video_pipeline,
        persist=components.persist, sessions=components.sessions,
        metrics=components.metrics, ring=components.ring,
        apply_settings=components.apply_settings, version=VERSION,
    ), prefix=prefix)
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    mcp.custom_route(prefix, methods=["GET"])(admin_api)
    mcp.custom_route(prefix + "/{rest:path}", methods=methods)(admin_api)

    inner = mcp.http_app(path="/mcp")
    return AccessApp(inner, components.store)


async def _session_sweep_loop(sessions: SessionManager) -> None:
    """定期清理过期会话（内存表不无限增长）。"""
    while True:
        await asyncio.sleep(300)
        try:
            sessions.sweep()
        except Exception:  # noqa: BLE001
            logger.debug("[admin] 会话清理异常（忽略）")


class AccessApp:
    """ASGI 组合：访问令牌门（/mcp、/upload）+ fastmcp + 管理面板。"""

    def __init__(self, inner, store: Store) -> None:
        self.gate = endpoints.AccessGate(inner, store)
        self._inner = inner

    async def __call__(self, scope, receive, send) -> None:
        await self.gate(scope, receive, send)

    @property
    def routes(self):
        return getattr(self._inner, "routes", [])


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
        timeout_graceful_shutdown=int(SYSTEMD_TIMEOUT_STOP_SEC),
    )
    server = uvicorn.Server(uconfig)
    try:
        await server.serve()
    finally:
        await components.persist.aclose()
        await components.gateway.aclose()
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

    store = Store(cfg.store_path)
    try:
        store.load()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 配置仓库加载失败: {exc}", file=sys.stderr)
        return 2
    bootstrap_access(cfg, store)
    if not store.admin_ready:
        logger.warning(
            "[startup] 管理面板尚未设置管理员口令：首次访问 %s 会进入初始化向导",
            cfg.admin_path_prefix)

    return asyncio.run(serve(cfg))


if __name__ == "__main__":
    raise SystemExit(main(check_only="--check" in sys.argv))
