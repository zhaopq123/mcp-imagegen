"""Provider 网关：把「选谁发、怎么发、结果怎么读」收敛到一处。

职责边界
--------
- **路由**：同一能力可能配了多个 provider —— 按优先级排序、跳过熔断中的、
  正常态轮转分摊（多 provider 自动容错：某个上游挂了不影响整体可用）。
- **隔离**：每个 provider 独占 httpx 连接池、限速令牌桶、熔断状态。
  慢的上游拖不垮快的，被限流的上游不影响别人。
- **协议适配**：请求模板渲染、鉴权头注入、响应字段抽取。
- **不负责**：重试次数以外的语义、任务跟踪（tasks.py）、落盘（persist）。

性能要点
--------
- 连接池 per-provider：避免共享池时「一个慢上游占满全部连接」
  （这是 httpx 默认用法最容易踩的坑）；
- in_flight 计数不发网络请求、不持锁，rotate 时按旧 client 排空；
- 解析全在内存，无线程（R2）。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Tuple

import httpx

from infra import budget as budget_mod
from infra import circuit as circuit_mod
from infra import ratelimit as ratelimit_mod
from . import schema, template

logger = logging.getLogger("mcp_imagegen.gateway")

# 重试纪律（仅 5xx / 网络异常重试 1 次；429 与 4xx 零重试）
RETRY_MAX_ATTEMPTS = 2
RETRY_BACKOFF_MIN_SEC = 0.25
RETRY_BACKOFF_MAX_SEC = 1.0
RETRY_AFTER_MAX_SEC = 60.0

# rotate 排空：轮询旧 client 在途计数至归零
DRAIN_POLL_SEC = 0.05
DRAIN_MAX_SEC = 300.0

_JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}


@dataclass(frozen=True)
class ApiCallResult:
    """一次上游调用的统一结果（失败不抛异常，交由上层分类）。"""

    ok: bool
    status_code: Optional[int] = None
    provider_id: str = ""
    model_id: str = ""          # 兼容旧字段语义：本次使用的 provider id
    retry_after: Optional[float] = None
    error_detail: str = ""
    reason: str = ""  # no_provider / poll_undefined：本端配置类问题（非网络失败）
    # 生图
    urls: Tuple[str, ...] = ()
    # 视频提交
    video_id: Optional[str] = None
    # 视频查询
    video_status: str = ""
    video_url: Optional[str] = None


class NoProviderError(Exception):
    """没有任何可用 provider（配置缺失/全被禁用/全部熔断）。"""


def _resolve_secret(value: str) -> str:
    """``env:VAR`` 引用 → 环境变量值；其余原样返回。"""
    text = (value or "").strip()
    if text.startswith("env:"):
        var = text[4:].strip()
        return os.environ.get(var, "")
    return value or ""


def compute_read_timeout(tier_sec: float, deadline: budget_mod.Deadline) -> float:
    """动态 read = min(调用档, deadline 剩余)：每次尝试重算。

    剩余不足时钳到 0（httpx 视 0 为立即超时，与 deadline 检查配合）。
    """
    return max(0.0, min(tier_sec, deadline.remaining()))


def backoff_delay(retry_after: Optional[float]) -> float:
    """尊重 Retry-After（封顶 60s），否则 [0.25, 1.0] 全抖动。"""
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), RETRY_AFTER_MAX_SEC)
    return random.uniform(RETRY_BACKOFF_MIN_SEC, RETRY_BACKOFF_MAX_SEC)


def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _body_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _upstream_message(body: Any, path: str = "message") -> str:
    """错误体 → 可读 message（顶层 message 优先，其次 error.message）。

    绝大多数 OpenAI 兼容实现两者必有其一；都没有就返回空串，调用方
    回退到响应体截断文本。
    """
    if not isinstance(body, dict):
        return ""
    if path:
        value = template.extract(body, path)
        if isinstance(value, str) and value.strip():
            return value
    nested = body.get("error")
    if isinstance(nested, dict):
        value = nested.get("message")
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(nested, str) and nested.strip():
        return nested
    return ""


def _error_detail(resp: httpx.Response, path: str = "message") -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        text = ""
    body = _body_json(resp)
    msg = _upstream_message(body, path)
    return (msg or text)[:300].strip()


def _as_image_urls(values: list, *, b64: bool) -> list:
    """把抽取结果归一化成「可直接用的图片地址」。

    b64=True 或值不是 URL/已是 data URI → 按 base64 补 data URI 头。
    """
    out: list = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if text.startswith(("http://", "https://", "data:")):
            out.append(text)
            continue
        if b64:
            out.append(f"data:image/png;base64,{text}")
            continue
        # 裸 base64（OpenAI 风格 b64_json）→ 补 data URI
        try:
            base64.b64decode(text[:16], validate=True)
        except Exception:  # noqa: BLE001 —— 不是 base64 就当作 URL 原样放行
            out.append(text)
        else:
            out.append(f"data:image/png;base64,{text}")
    return out


# ---------------------------------------------------------------------------
# 单个 provider 的运行时
# ---------------------------------------------------------------------------
class ProviderRuntime:
    """provider 的运行时状态（连接池 / 限速 / 熔断 / 统计）。"""

    def __init__(
        self,
        cfg: schema.ProviderConfig,
        base_limits: httpx.Limits,
        base_timeout: httpx.Timeout,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client_factory: Optional[Callable[..., httpx.AsyncClient]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        circuit_hard_sec: float = 600.0,
        circuit_soft_sec: float = 300.0,
    ) -> None:
        self.cfg = cfg
        self._base_limits = base_limits
        self._base_timeout = base_timeout
        self._transport = transport
        self._factory = client_factory or httpx.AsyncClient
        self._clock = clock
        self._sleep = sleep
        self.circuit = circuit_mod.CircuitBreaker(
            hard_sec=circuit_hard_sec, soft_sec=circuit_soft_sec, clock=clock)
        self.limiter = ratelimit_mod.RateLimiter(
            rps=cfg.limits.rps, burst=cfg.limits.burst,
            cooldown_sec=cfg.limits.cooldown_sec, clock=clock, sleep=sleep)
        self.stats: dict = {
            "calls": 0, "ok": 0, "fail": 0, "retries": 0,
            "last_error": "", "last_ok_at": 0.0, "last_latency_ms": 0.0,
        }
        self.client = self._new_client()
        self._old: list = []   # rotate 排空中的旧 client
        self.weight = 0        # 轮转计数（公平分摊）

    # ------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.cfg.id

    def _new_client(self) -> httpx.AsyncClient:
        pool = self.cfg.pool
        kwargs: dict = {
            "base_url": self.cfg.base_url or "http://localhost",
            "limits": httpx.Limits(
                max_connections=pool.max_connections
                or self._base_limits.max_connections,
                max_keepalive_connections=pool.max_keepalive_connections
                or self._base_limits.max_keepalive_connections,
            ),
            # httpx.Timeout 的关键字是 connect/read/write/pool（不是 *_timeout，
            # 那个是 requests 的命名，混用会静默走默认 5s）。
            "timeout": httpx.Timeout(
                connect=pool.connect_sec or self._base_timeout.connect,
                write=pool.write_sec or self._base_timeout.write,
                pool=pool.pool_sec or self._base_timeout.pool,
                read=pool.read_sec or self._base_timeout.read,
            ),
            "follow_redirects": True,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return self._factory(**kwargs)

    def in_flight(self) -> int:
        return getattr(self.client, "_gw_in_flight", 0)

    async def rotate(self) -> None:
        """热切换 client（定期重建，避免长连接僵死）。"""
        old = self.client
        self.client = self._new_client()
        elapsed = 0.0
        await self._sleep(0)  # 让在途请求有机会推进
        logger.info("[gateway] provider=%s rotate：新 client 就位（旧连接排空）",
                    self.cfg.id)
        await old.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------
    def url_for(self, path: str) -> str:
        return path

    def auth_parts(self) -> "tuple[dict, dict]":
        """→ (headers, query)；token 缺失时返回空（由调用方报错更明确）。"""
        auth = self.cfg.auth
        secret = _resolve_secret(auth.token)
        headers: dict = {}
        query: dict = {}
        if auth.kind == schema.AUTH_NONE or not secret:
            return headers, query
        if auth.kind == schema.AUTH_BEARER:
            headers[auth.name or "Authorization"] = f"{auth.prefix}{secret}".strip()
        elif auth.kind == schema.AUTH_HEADER:
            headers[auth.name or "X-API-Key"] = f"{auth.prefix or ''}{secret}".strip()
        elif auth.kind == schema.AUTH_QUERY:
            query[auth.name or "key"] = secret
        return headers, query


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------
class ProviderGateway:
    """多 provider 路由 + 协议适配 + 隔离资源。"""

    def __init__(
        self,
        cfg,
        store,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client_factory: Optional[Callable[..., httpx.AsyncClient]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._transport = transport
        self._factory = client_factory
        self._clock = clock
        # 显式传 None（测试或部分调用方）也要能跑：退回真实 sleep。
        self._sleep = sleep or asyncio.sleep
        self._runtime: dict[str, ProviderRuntime] = {}
        self._rr: dict[str, int] = {}
        self.reload()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def reload(self) -> "tuple[list[str], list[str]]":
        """按 store 重建 runtime 集合；返回 (新增/更新, 移除) 的 id 列表。

        面板改动 provider 后调用即可热生效——**不需要重启服务**。
        """
        added: list[str] = []
        removed: list[str] = []
        wanted = {p.id: p for p in (self._store.providers if self._store else [])}

        for pid, spec in wanted.items():
            exists = self._runtime.get(pid)
            if exists is None:
                self._runtime[pid] = self._build_runtime(spec)
                added.append(pid)
            else:
                exists.cfg = spec
                # 限速参数热更新（重建桶，保留已发生的时间基准）
                exists.limiter = ratelimit_mod.RateLimiter(
                    rps=spec.limits.rps, burst=spec.limits.burst,
                    cooldown_sec=spec.limits.cooldown_sec,
                    clock=self._clock, sleep=self._sleep)
        for pid in list(self._runtime):
            if pid not in wanted:
                removed.append(pid)
                runtime = self._runtime.pop(pid)
                asyncio.create_task(self._safe_close(runtime),
                                    name=f"close-{pid}")
        return added, removed

    async def _safe_close(self, runtime: ProviderRuntime) -> None:
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001 —— 收尾异常不上抛到事件循环
            logger.debug("[gateway] provider=%s 关闭时异常（忽略）", runtime.id)

    def _build_runtime(self, spec: schema.ProviderConfig) -> ProviderRuntime:
        return ProviderRuntime(
            spec,
            httpx.Limits(
                max_connections=self._cfg.httpx_max_connections,
                max_keepalive_connections=self._cfg.httpx_max_keepalive,
            ),
            httpx.Timeout(
                self._cfg.httpx_connect_sec,
                write=self._cfg.httpx_write_sec,
                pool=self._cfg.httpx_pool_sec,
                read=self._cfg.total_budget_sec,
            ),
            transport=self._transport,
            client_factory=self._factory,
            clock=self._clock,
            sleep=self._sleep,
            circuit_hard_sec=self._cfg.circuit_hard_sec,
            circuit_soft_sec=self._cfg.circuit_soft_sec,
        )

    def reconfigure(self, cfg) -> None:
        """热更新全局运行参数（超时 / 池上限 / 熔断时长）后立即生效。"""
        self._cfg = cfg
        for runtime in self._runtime.values():
            runtime.circuit = circuit_mod.CircuitBreaker(
                hard_sec=cfg.circuit_hard_sec,
                soft_sec=cfg.circuit_soft_sec, clock=self._clock)

    async def aclose(self) -> None:
        for runtime in self._runtime.values():
            await runtime.aclose()

    async def rotate(self) -> None:
        """定期热重建全部连接池（长连接僵死预防）；daily_rebuild_loop 调用。"""
        await self.rotate_all()

    async def rotate_all(self) -> None:
        for runtime in self._runtime.values():
            await runtime.rotate()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def candidates(self, kind: str) -> list[ProviderRuntime]:
        """具备该能力且已启用的 runtime（按 priority 升序）。

        未填 base_url / 未启用 / 能力未启用的 provider 直接不出现。
        """
        items = [r for r in self._runtime.values() if r.cfg.has(kind)]
        items.sort(key=lambda r: (r.cfg.priority, r.cfg.id))
        return items

    def has_capability(self, kind: str) -> bool:
        return bool(self.candidates(kind))

    def resolve(
        self, kind: str, *, prefer: Optional[str] = None,
        allowed: Optional[list] = None,
    ) -> "tuple[Optional[ProviderRuntime], Optional[ApiCallResult]]":
        """选 provider，并区分「一个都没有」与「点名的那个没有」。

        返回 (runtime, error)；二者必有其一为 None。

        显式点名却静默改用别家是最坏的结果：用户以为在打 A（模型、计费、
        地域全都不同），实际落到 B 上，账单和出图都对不上，日志里还一点
        痕迹都没有。所以**点名未命中是错误，不是兜底**。

        allowed：令牌白名单限制（多工作空间隔离用）。
        """
        items = self.candidates(kind)
        if allowed:
            allow = set(allowed)
            items = [r for r in items if r.id in allow]
        if prefer:
            for item in items:
                if item.id == prefer:
                    return item, None
            return None, ApiCallResult(
                ok=False, provider_id=prefer,
                error_detail=(
                    f"找不到名为 {prefer!r} 的服务提供方：未配置 / 已禁用 / "
                    f"不具备该能力 / 当前访问令牌无权使用"),
                reason="provider_not_found")
        if not items:
            return None, ApiCallResult(
                ok=False, provider_id="",
                error_detail=f"没有任何可用的 {kind} 服务提供方"
                             "（请在管理面板添加并启用）",
                reason="no_provider")
        # 过滤掉熔断 OPEN 的（探针态视为可用）
        healthy = []
        for item in items:
            status = item.circuit.check(item.id)
            if not status.open or status.probe:
                healthy.append(item)
        pool_items = healthy or items  # 全熔断时也要返回（由调用方给出明确错误）
        if len(pool_items) == 1:
            return pool_items[0], None
        # 只在「同一优先级」内轮转：priority 是第一决策（主 / 备），
        # 同档位才分摊。否则轮转会越过主用打到备用上，配了主备却随机打，
        # 故障切换语义形同虚设（测试实测：a=10 / b=1 会选中 a）。
        best = min(r.cfg.priority for r in pool_items)
        peers = [r for r in pool_items if r.cfg.priority == best]
        if len(peers) == 1:
            return peers[0], None
        cursor = self._rr.get(kind, 0) + 1
        self._rr[kind] = cursor
        return peers[cursor % len(peers)], None

    def get(self, pid: str) -> Optional[ProviderRuntime]:
        return self._runtime.get(pid)

    def stats(self) -> "dict[str, dict]":
        out: dict[str, dict] = {}
        for pid, runtime in self._runtime.items():
            state = runtime.circuit.check(pid)
            out[pid] = {
                "enabled": runtime.cfg.enabled,
                "base_url": runtime.cfg.base_url,
                "priority": runtime.cfg.priority,
                "capabilities": sorted(runtime.cfg.capabilities.keys()),
                "circuit": state.state,
                "circuit_open": state.open,
                "rate_blocked_sec": round(runtime.limiter.blocked(), 2),
                "tokens_available": round(runtime.limiter.available(), 2),
                **runtime.stats,
            }
        return out

    # ------------------------------------------------------------------
    # 调用：生图
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        *,
        prompt: str,
        size: str,
        deadline: budget_mod.Deadline,
        ratio: Optional[str] = None,
        images: Optional[Tuple[str, ...]] = None,
        provider: Optional[str] = None,
        model: str = schema.CAP_IMAGE,
    ) -> ApiCallResult:
        runtime, failure = self.resolve(schema.CAP_IMAGE, prefer=provider)
        if runtime is None:
            return failure
        cap = runtime.cfg.cap(schema.CAP_IMAGE)
        ctx = {
            "prompt": prompt, "size": size, "ratio": ratio,
            "model": cap.model, "images": list(images or []),
            "image": (images[0] if images else None),
            "n": 1,
        }
        headers, query = runtime.auth_parts()
        return await self._call(
            runtime, cap.request,
            deadline=deadline,
            read_tier=runtime.cfg.pool.read_sec or self._cfg.timeout_upstream_image_sec,
            ctx=ctx, extra_headers=headers, extra_query=query,
            parse=self._parse_image, extract_spec=cap.extract,
        )

    # ------------------------------------------------------------------
    # 调用：视频提交 / 查询
    # ------------------------------------------------------------------
    async def submit_video(
        self,
        *,
        prompt: str,
        seconds: int,
        aspect_ratio: str,
        deadline: budget_mod.Deadline,
        seed: Optional[int] = None,
        mode: str = "text",
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Optional[Tuple[str, ...]] = None,
        provider: Optional[str] = None,
        model: str = schema.CAP_VIDEO,
    ) -> ApiCallResult:
        runtime, failure = self.resolve(schema.CAP_VIDEO, prefer=provider)
        if runtime is None:
            return failure
        cap = runtime.cfg.cap(schema.CAP_VIDEO)
        ctx = {
            "prompt": prompt, "seconds": str(seconds), "aspect_ratio": aspect_ratio,
            "model": cap.model, "seed": seed, "mode": mode,
            "images": list(reference_images or []),
            "image": (reference_images[0] if reference_images else None),
            "first_frame": first_frame, "last_frame": last_frame,
            "first_image": first_frame or (
                reference_images[0] if reference_images else None),
        }
        headers, query = runtime.auth_parts()
        return await self._call(
            runtime, cap.request,
            deadline=deadline,
            read_tier=runtime.cfg.pool.read_sec or self._cfg.video_submit_timeout_sec,
            ctx=ctx, extra_headers=headers, extra_query=query,
            parse=self._parse_submit, extract_spec=cap.extract,
        )

    async def query_video(
        self,
        *,
        video_id: str,
        deadline: budget_mod.Deadline,
        provider: Optional[str] = None,
        model: str = schema.CAP_VIDEO,
    ) -> ApiCallResult:
        runtime, failure = self.resolve(schema.CAP_VIDEO, prefer=provider)
        if runtime is None:
            return failure
        cap = runtime.cfg.cap(schema.CAP_VIDEO)
        poll = cap.poll
        if poll is None:
            # 同步型（提交即出结果）：没有查询端点
            return ApiCallResult(
                ok=False, provider_id=runtime.id,
                error_detail="该服务提供方未配置轮询端点（poll）",
                reason="poll_undefined",
            )
        ctx = {
            "task_id": video_id, "video_id": video_id, "id": video_id,
            "model": cap.model,
        }
        headers, query = runtime.auth_parts()
        extract_spec = cap.poll_extract or cap.extract
        return await self._call(
            runtime, poll,
            deadline=deadline,
            read_tier=runtime.cfg.pool.read_sec or self._cfg.video_query_timeout_sec,
            ctx=ctx, extra_headers=headers, extra_query=query,
            parse=self._parse_query, extract_spec=extract_spec,
        )

    # ------------------------------------------------------------------
    # 通用发送（限速 → 重试 → 解析）
    # ------------------------------------------------------------------
    async def _call(
        self,
        runtime: ProviderRuntime,
        request: schema.RequestSpec,
        *,
        deadline: budget_mod.Deadline,
        read_tier: float,
        ctx: dict,
        extra_headers: dict,
        extra_query: dict,
        parse: Callable[..., ApiCallResult],
        extract_spec: Optional[schema.ExtractSpec] = None,
    ) -> ApiCallResult:
        pid = runtime.id
        # 调用方必须显式带 extract_spec（提交用 cap.extract、轮询用
        # cap.poll_extract）；缺省时退化为「全部走启发式猜测」，宁可猜错
        # 也不要静默返回空结果——那会被上层当成"生成成功但没图"。
        spec = extract_spec or schema.ExtractSpec()
        # 1) 令牌桶：等待计入 deadline（撞 429 被冻结的代价远高于自己排队）
        try:
            await runtime.limiter.acquire(
                wait_cap_sec=max(deadline.remaining(), 0.0))
        except asyncio.TimeoutError:
            return ApiCallResult(
                ok=False, provider_id=pid,
                error_detail="本地限速等待超出预算（上游限速排队）",
            )

        method = request.method
        path = str(template.render_value(request.path, ctx) or "")
        headers: dict = dict(_JSON_HEADERS)
        headers.update(template.render_mapping(extra_headers, ctx))
        headers.update(template.render_mapping(request.headers, ctx))
        query = template.render_mapping(request.query, ctx)
        query.update(template.render_mapping(extra_query, ctx))
        body = None
        form = None
        if request.body is not None and method != "GET":
            rendered = template.render_node(request.body, ctx)
            if request.body_encoding == schema.BODY_FORM:
                form = rendered if isinstance(rendered, dict) else {}
            else:
                body = rendered
        if not headers.get("Content-Type") and form is not None:
            headers.pop("Content-Type", None)

        client = runtime.client
        setattr(client, "_gw_in_flight", getattr(client, "_gw_in_flight", 0) + 1)
        started = self._clock()
        try:
            result = await self._send_with_retry(
                runtime, client, method, path,
                body=body, form=form, headers=headers, query=query,
                read_tier=read_tier, deadline=deadline,
                parse=parse, spec=spec, ctx=ctx,
            )
        finally:
            setattr(client, "_gw_in_flight",
                    max(0, getattr(client, "_gw_in_flight", 1) - 1))

        runtime.stats["calls"] += 1
        if result.ok:
            runtime.stats["ok"] += 1
            runtime.stats["last_ok_at"] = self._clock()
        else:
            runtime.stats["fail"] += 1
            runtime.stats["last_error"] = result.error_detail[:200]
        runtime.stats["last_latency_ms"] = round((self._clock() - started) * 1000, 1)
        return result

    async def _send_with_retry(
        self, runtime: ProviderRuntime, client: httpx.AsyncClient,
        method: str, path: str, *, body, form, headers, query,
        read_tier: float, deadline: budget_mod.Deadline,
        parse: Callable[..., ApiCallResult], spec, ctx: dict,
    ) -> ApiCallResult:
        pid = runtime.id
        attempt = 0
        while True:
            deadline.check()  # 预算耗尽 → 不发起任何 IO
            timeout = httpx.Timeout(
                runtime.cfg.pool.connect_sec or self._cfg.httpx_connect_sec,
                write=runtime.cfg.pool.write_sec or self._cfg.httpx_write_sec,
                pool=runtime.cfg.pool.pool_sec or self._cfg.httpx_pool_sec,
                read=max(0.0, min(read_tier, deadline.remaining())),
            )
            try:
                resp = await client.request(
                    method, path or "/", json=body, data=form,
                    params=query or None, headers=headers, timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < RETRY_MAX_ATTEMPTS:
                    attempt += 1
                    runtime.stats["retries"] += 1
                    logger.warning(
                        "[gateway] provider=%s %s %s 第 %d 次网络异常 %s → 退避重试",
                        pid, method, path, attempt, type(exc).__name__)
                    await self._backoff(deadline, retry_after=None)
                    continue
                return ApiCallResult(
                    ok=False, provider_id=pid,
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
            except httpx.HTTPError as exc:  # 其余 httpx 错误不重试
                return ApiCallResult(
                    ok=False, provider_id=pid,
                    error_detail=f"{type(exc).__name__}: {exc}",
                )

            status = resp.status_code
            if 500 <= status <= 599 and attempt + 1 < RETRY_MAX_ATTEMPTS:
                attempt += 1
                runtime.stats["retries"] += 1
                await self._backoff(deadline, retry_after=_parse_retry_after(resp))
                continue
            if 200 <= status < 300:
                return parse(resp, runtime.id, spec)
            # 上游明确过载 → 冻结本地限速窗口（让后续请求排队而不是硬闯）
            if status == 429 or 500 <= status <= 599:
                runtime.limiter.penalize(_parse_retry_after(resp))
            return ApiCallResult(
                ok=False, status_code=status, provider_id=pid, model_id=pid,
                retry_after=_parse_retry_after(resp),
                error_detail=_error_detail(
                    resp, (spec.error_message if spec else "message")),
            )

    async def _backoff(self, deadline: budget_mod.Deadline,
                       retry_after: Optional[float]) -> None:
        """退避等待。走注入的 sleep（测试可观测），不用裸 asyncio.sleep。"""
        delay = backoff_delay(retry_after)
        capped = min(delay, max(deadline.remaining(), 0.0))
        await self._sleep(capped)
        deadline.check()

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_image(
        resp: httpx.Response, pid: str, spec: Optional[schema.ExtractSpec],
    ) -> ApiCallResult:
        body = _body_json(resp)
        spec = spec or schema.ExtractSpec()
        if spec.urls:
            raw = template.extract_list(body, spec.urls)
        else:
            raw = template.guess_urls(body)
        urls = _as_image_urls(raw, b64=spec.b64)
        if not urls:
            return ApiCallResult(
                ok=False, status_code=resp.status_code, provider_id=pid,
                model_id=pid,
                error_detail="响应中抽取不到图片地址（检查 extract.urls 配置）",
            )
        return ApiCallResult(
            ok=True, status_code=resp.status_code, provider_id=pid,
            model_id=pid, urls=tuple(urls))

    @staticmethod
    def _parse_submit(
        resp: httpx.Response, pid: str, spec: Optional[schema.ExtractSpec],
    ) -> ApiCallResult:
        body = _body_json(resp)
        spec = spec or schema.ExtractSpec()
        if spec.task_id:
            value = template.extract(body, spec.task_id)
            video_id = str(value) if value is not None else None
        else:
            video_id = template.guess_task_id(body)
        if not video_id:
            return ApiCallResult(
                ok=False, status_code=resp.status_code, provider_id=pid,
                model_id=pid,
                error_detail="响应中抽取不到任务号（检查 extract.task_id 配置）",
            )
        return ApiCallResult(
            ok=True, status_code=resp.status_code, provider_id=pid,
            model_id=pid, video_id=video_id)

    @staticmethod
    def _parse_query(
        resp: httpx.Response, pid: str, spec: Optional[schema.ExtractSpec],
    ) -> ApiCallResult:
        body = _body_json(resp)
        spec = spec or schema.ExtractSpec()
        if spec.status:
            raw_status = template.extract(body, spec.status)
        else:
            raw_status = template.guess_status(body)
        status = template.normalize_status(
            str(raw_status) if raw_status is not None else "", spec.status_map)
        url = None
        if spec.url:
            value = template.extract(body, spec.url)
            url = str(value) if value is not None else None
        if not url and status == "completed":
            url = template.guess_url(body)
        if not status:
            return ApiCallResult(
                ok=False, status_code=resp.status_code, provider_id=pid,
                model_id=pid,
                error_detail="响应中抽取不到任务状态（检查 extract.status 配置）",
            )
        return ApiCallResult(
            ok=True, status_code=resp.status_code, provider_id=pid,
            model_id=pid, video_status=status, video_url=url)
