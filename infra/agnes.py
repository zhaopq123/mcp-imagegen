"""Agnes AI API 客户端（V2 增量设计方案 2.1；T2 生图 + T3 视频两端点）。

单上游双模型（V2 需求变更①）：
    agnes-image-2.5-flash（生图，POST /v1/images/generations）
    agnes-video-2.5-flash（生视频，POST /v1/videos 提交 +
    GET /agnesapi 查询）

httpx 四段超时（对齐 V1 模式，R7）——四段全显式：
    connect/write/pool = config 派生项（HTTPXCONNECTSEC 等，默认 5s）；
    read 动态 = min(调用档, deadline 剩余)，每次尝试重算：
        生图 = min(TIMEOUT_AGNES_IMAGE_SEC=60, 剩余)   # D5：30→60
        提交 = min(VIDEO_SUBMIT_TIMEOUT_SEC=30, 剩余)
        单查 = min(VIDEO_QUERY_TIMEOUT_SEC=10, 剩余)

重试纪律（C4，三端点一致）：
    仅 5xx / 网络异常 / 超时 → 退避重试 1 次（0.25–1s 全抖动，尊重
    Retry-After；退避同样计入 deadline）；429 / 4xx 零重试。
    503 video_queue_full 走本纪律（重试 1 次大概率仍满 → 落 Agent 侧
    retry=yes 稍后再来，语义正确，无需特判）。

上游错误体（D6 双格式，真机实证两种并存）：
    顶层 {code, message, data}（F10：503 video_queue_full）与嵌套
    {"error": {code, message, type}}（F3：401）——message 提取逻辑见
    engine/errors.upstream_error_message（顶层优先）；解析失败回退
    响应体前 200 字符。

响应解析：
    生图 200：images / data 双兼容（E14）；
    提交 200：video_id（顶层；data 容器兜底——A-5 实测回填前的防御性
    兼容，与生图双兼容同一哲学）；
    查询 200：{status, url?}（顶层优先，data 容器兜底，A-5 同上）。

E25（V2 第七轮 D2 修订）：生图请求体 {model, prompt, size} + 可选
    ratio（官方 F3：仅与档位式 size 配合——冲突拦截在 pipeline 层，
    本层直传）——quality/steps/guidance 等仍一律不注入。

E21：视频提交 seconds 以字符串入体（int → str）。
D3（官方 F5）：视频提交可选 seed（integer，None 不注入——官方未标注
    值域，不臆测上限，直传上游）。

每日重建（R8/E6，对齐 V1 语义）：rotate() 指针热切换，旧 client 等
    在途请求完成后 aclose（300s 排空上限 > 200s deadline 上限）。

不负责：错误分类（errors.py）、参数归一、任务表（tasks.py）、
    轮询循环与视频落盘（video_pipeline，后续轮次）。
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Tuple

import httpx

from . import budget
from engine import errors as errors_mod  # D6：错误体双格式 message 提取（纯函数）

logger = logging.getLogger("mcp_imagegen.agnes")

# 端点路径（F1/F2：生图与视频提交同域不同径；F2 视频查询独立路径）
GENERATIONS_PATH = "/v1/images/generations"
VIDEOS_PATH = "/v1/videos"
QUERY_PATH = "/agnesapi"

# 模型注册表（E15 精神：内部别名 → 真实模型 ID，禁止自由文本 model）
# 默认值可被配置覆盖（AGNES_IMAGE_MODEL / AGNES_VIDEO_MODEL）：
# 上游新增或更名模型时改配置即可，无需改代码。
DEFAULT_MODEL_IDS = {
    "agnes-image": "agnes-image-2.5-flash",
    "agnes-video": "agnes-video-2.5-flash",
}
MODEL_IDS = dict(DEFAULT_MODEL_IDS)


def resolve_model_ids(cfg) -> dict[str, str]:
    """从配置解析模型 ID 映射（空串回落默认）。

    cfg 为 None 时返回内置默认（供未接配置的调用点/测试使用）。
    """
    ids = dict(DEFAULT_MODEL_IDS)
    if cfg is not None:
        image = (getattr(cfg, "agnes_image_model", "") or "").strip()
        video = (getattr(cfg, "agnes_video_model", "") or "").strip()
        if image:
            ids["agnes-image"] = image
        if video:
            ids["agnes-video"] = video
    return ids

# 视频提交固定参数（方案 V2 2.1：mode="text"、size="720P"）
VIDEO_MODE_TEXT = "text"
VIDEO_SIZE_DEFAULT = "720P"

# 重试纪律（C4）：仅一次重试；无 Retry-After 时退避 0.25–1s 全抖动；
# 有 Retry-After 则尊重（封顶 60s——对齐 V1 客户端纪律）
RETRY_MAX_ATTEMPTS = 2
RETRY_BACKOFF_MIN_SEC = 0.25
RETRY_BACKOFF_MAX_SEC = 1.0
RETRY_AFTER_MAX_SEC = 60.0

# rotate 排空（E6）：轮询旧 client 在途计数至归零，300s 上限 > 200s deadline
DRAIN_POLL_SEC = 0.05
DRAIN_MAX_SEC = 300.0

# 客户端默认构造：池上限沿用规格书五节（Limits 10/4）
DEFAULT_MAX_CONNECTIONS = 10
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 4


@dataclass(frozen=True)
class ApiCallResult:
    """三端点统一返回（失败不抛异常，交由上层分类）。

    生图：ok + urls；提交：ok + video_id；查询：ok + video_status /
    video_url（video_status 为上游任务状态 running/completed/failed，
    HTTP 层成功即 ok=True，failed 的错误分类在 pipeline 层做）。
    """

    ok: bool
    status_code: Optional[int] = None
    model_id: str = ""
    retry_after: Optional[float] = None
    error_detail: str = ""
    # 生图
    urls: Tuple[str, ...] = ()
    # 视频提交
    video_id: Optional[str] = None
    # 视频查询
    video_status: str = ""
    video_url: Optional[str] = None


def build_http_limits(cfg) -> httpx.Limits:
    """连接池上限（规格书五节：Limits 10/4；四段超时与池上限全显式）。"""
    return httpx.Limits(
        max_connections=cfg.httpx_max_connections,
        max_keepalive_connections=cfg.httpx_max_keepalive_connections,
    )


def compute_read_timeout(tier_sec: float, deadline: budget.Deadline) -> float:
    """动态 read = min(调用档, deadline 剩余)（R7：每次尝试重算）。

    生图档 TIMEOUT_AGNES_IMAGE_SEC / 提交档 VIDEO_SUBMIT_TIMEOUT_SEC /
    单查档 VIDEO_QUERY_TIMEOUT_SEC 由调用方传入；剩余不足时钳到 0
    （httpx 视 0 为立即超时，与 deadline 检查配合）。
    """
    return max(0.0, min(tier_sec, deadline.remaining()))


def backoff_delay(retry_after: Optional[float]) -> float:
    """退避时长：尊重 Retry-After（>=0 有效，封顶 60s）；否则 [0.25, 1.0] 全抖动。"""
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), RETRY_AFTER_MAX_SEC)
    return random.uniform(RETRY_BACKOFF_MIN_SEC, RETRY_BACKOFF_MAX_SEC)


def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
    """Retry-After 头（秒数形式；缺失/非法 → None）。"""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _error_detail(resp: httpx.Response) -> str:
    """上游错误体 → error_detail（D6 双格式：顶层 {code,message,data} /
    嵌套 {"error":{code,message,type}}，真机 F10/F3 实证并存）。

    message 提取（含请求 id，可透传对账）交 engine/errors 纯函数；
    两种格式都不命中 → 回退响应体前 200 字符（对齐 V1 截断纪律）。
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200].strip()
    msg = errors_mod.upstream_error_message(body)
    if msg:
        return msg[:200].strip()
    return resp.text[:200].strip()


def _extract_urls(resp: httpx.Response) -> list:
    """生图 URL 解析（E14：images / data 双兼容）。"""
    try:
        data = resp.json()
    except ValueError:
        return []
    items = data.get("images") or data.get("data") or []
    if not isinstance(items, list):
        return []
    return [it.get("url") for it in items if isinstance(it, dict) and it.get("url")]


def _extract_video_id(resp: httpx.Response) -> Optional[str]:
    """提交响应 video_id 解析（顶层优先；data 容器兜底，A-5 实测回填前防御性兼容）。"""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    vid = body.get("video_id")
    if isinstance(vid, str) and vid:
        return vid
    data = body.get("data")
    if isinstance(data, dict):
        vid = data.get("video_id")
        if isinstance(vid, str) and vid:
            return vid
    return None


def _extract_video_fields(resp: httpx.Response) -> Tuple[Optional[str], Optional[str]]:
    """查询响应 {status, url?} 解析（顶层优先；data 容器兜底，A-5 同上）。"""
    try:
        body = resp.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    source = body
    if not source.get("status"):
        data = source.get("data")
        if isinstance(data, dict):
            source = data
    status = source.get("status")
    url = source.get("url")
    return (
        status if isinstance(status, str) else None,
        url if isinstance(url, str) else None,
    )


class _ClientHandle:
    """rotate 语义的载体：client + 在途计数（E6 热切换排空）。"""

    __slots__ = ("client", "in_flight")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.in_flight = 0


class AgnesClient:
    """Agnes AI 客户端（生图 + 视频提交/查询，三端点共享重试与重建语义）。"""

    def __init__(
        self,
        cfg,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client_factory: Optional[Callable[..., httpx.AsyncClient]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        drain_poll_sec: float = DRAIN_POLL_SEC,
    ) -> None:
        self._cfg = cfg
        self._transport = transport
        self._client_factory = client_factory or httpx.AsyncClient
        self._sleep = sleep
        self._drain_poll_sec = drain_poll_sec
        # 模型 ID 映射：由配置解析（空串回落内置默认），实例级持有
        self._model_ids = resolve_model_ids(cfg)
        self._image_model_id = self._model_ids["agnes-image"]
        self._video_model_id = self._model_ids["agnes-video"]
        self._current = _ClientHandle(self._new_client())

    # ------------------------------------------------------------------
    # client 生命周期
    # ------------------------------------------------------------------
    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "base_url": self._cfg.agnes_base_url,
            "limits": build_http_limits(self._cfg),
            # 兜底四段（每次请求以动态 read 覆盖；此处保证无请求裸奔）
            "timeout": httpx.Timeout(
                self._cfg.httpx_connect_sec,
                write=self._cfg.httpx_write_sec,
                pool=self._cfg.httpx_pool_sec,
                read=self._cfg.total_budget_sec,
            ),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return self._client_factory(**kwargs)

    @property
    def in_flight(self) -> int:
        return self._current.in_flight

    async def rotate(self) -> None:
        """每日重建（R8/E6）：指针热切换，旧 client 排空后 aclose。"""
        old = self._current
        new = _ClientHandle(self._new_client())
        self._current = new  # 先切换，新请求即刻走新 client
        logger.info("[agnes] rotate: 指针热切换完成，开始排空旧 client（在途 %d）", old.in_flight)
        elapsed = 0.0
        while old.in_flight > 0 and elapsed < DRAIN_MAX_SEC:
            await self._sleep(self._drain_poll_sec)
            elapsed += self._drain_poll_sec
        await old.client.aclose()
        if old.in_flight > 0:
            logger.warning("[agnes] rotate: 排空超时 %.0fs（在途 %d 未归零，强制关闭）", DRAIN_MAX_SEC, old.in_flight)
        else:
            logger.info("[agnes] rotate: 旧 client 已排空并关闭")

    async def aclose(self) -> None:
        await self._current.client.aclose()

    # ------------------------------------------------------------------
    # 公共端点
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        size: str,
        deadline: budget.Deadline,
        ratio: Optional[str] = None,
    ) -> ApiCallResult:
        """生图（T2）：POST /v1/images/generations。

        请求体 {model, prompt, size}（E25）+ 可选 ratio（D2/官方 F3：
        仅与档位式 size 配合——冲突已由 pipeline 拦截，None 不注入）。
        """
        model_id = self._model_id(model, expected=self._image_model_id)
        payload = {"model": model_id, "prompt": prompt, "size": size}
        if ratio is not None:
            payload["ratio"] = ratio
        return await self._call(
            model_id,
            "POST",
            GENERATIONS_PATH,
            payload=payload,
            params=None,
            read_tier=self._cfg.timeout_agnes_image_sec,
            deadline=deadline,
            parse=self._parse_image_response,
        )

    async def submit_video(
        self,
        *,
        model: str,
        prompt: str,
        seconds: int,
        aspect_ratio: str,
        deadline: budget.Deadline,
        seed: Optional[int] = None,
    ) -> ApiCallResult:
        """视频提交（T3）：POST /v1/videos → video_id（seconds int→str，E21）。

        可选 seed（D3/官方 F5：integer 直传，None 不注入——官方未标注
        值域，不臆测上限）。
        """
        model_id = self._model_id(model, expected=self._video_model_id)
        payload = {
            "model": model_id,
            "prompt": prompt,
            "seconds": str(seconds),  # E21：int → str
            "mode": VIDEO_MODE_TEXT,
            "size": VIDEO_SIZE_DEFAULT,
            "aspect_ratio": aspect_ratio,
        }
        if seed is not None:
            payload["seed"] = seed
        return await self._call(
            model_id,
            "POST",
            VIDEOS_PATH,
            payload=payload,
            params=None,
            read_tier=self._cfg.video_submit_timeout_sec,
            deadline=deadline,
            parse=self._parse_submit_response,
        )

    async def query_video(
        self,
        *,
        video_id: str,
        model: str,
        deadline: budget.Deadline,
    ) -> ApiCallResult:
        """视频单查（T3）：GET /agnesapi?video_id=…&model_name=… → {status, url?}。"""
        model_id = self._model_id(model, expected=self._video_model_id)
        params = {"video_id": video_id, "model_name": model_id}
        return await self._call(
            model_id,
            "GET",
            QUERY_PATH,
            payload=None,
            params=params,
            read_tier=self._cfg.video_query_timeout_sec,
            deadline=deadline,
            parse=self._parse_query_response,
        )

    # ------------------------------------------------------------------
    # 内部：模型注册表校验（E15：别名查表，禁止自由文本 model）
    # ------------------------------------------------------------------
    def _model_id(self, model: str, *, expected: str) -> str:
        model_id = self._model_ids.get(model)
        if model_id is None:
            raise ValueError(
                f"未知模型: {model!r}（应为 {sorted(self._model_ids)} 之一）"
            )
        if model_id != expected:
            raise ValueError(
                f"模型 {model!r} 不适用于该端点（期望 {expected}）"
            )
        return model_id

    # ------------------------------------------------------------------
    # 内部：发送 + 重试（C4/R3/R7），三端点共享
    # ------------------------------------------------------------------
    async def _call(
        self,
        model_id: str,
        method: str,
        path: str,
        *,
        payload: Optional[dict],
        params: Optional[dict],
        read_tier: float,
        deadline: budget.Deadline,
        parse: Callable[[httpx.Response, str], ApiCallResult],
    ) -> ApiCallResult:
        handle = self._current
        handle.in_flight += 1
        try:
            return await self._send_with_retry(
                handle,
                model_id,
                method,
                path,
                payload=payload,
                params=params,
                read_tier=read_tier,
                deadline=deadline,
                parse=parse,
            )
        finally:
            handle.in_flight -= 1

    async def _send_with_retry(
        self,
        handle: _ClientHandle,
        model_id: str,
        method: str,
        path: str,
        *,
        payload: Optional[dict],
        params: Optional[dict],
        read_tier: float,
        deadline: budget.Deadline,
        parse: Callable[[httpx.Response, str], ApiCallResult],
    ) -> ApiCallResult:
        attempt = 0
        while True:
            deadline.check()  # R3：预算耗尽 → 不发起任何 IO
            # R7：每次尝试重算动态 read = min(调用档, 剩余)
            timeout = httpx.Timeout(
                self._cfg.httpx_connect_sec,
                write=self._cfg.httpx_write_sec,
                pool=self._cfg.httpx_pool_sec,
                read=compute_read_timeout(read_tier, deadline),
            )
            try:
                resp = await handle.client.request(
                    method,
                    path,
                    json=payload,
                    params=params,
                    headers={"Authorization": f"Bearer {self._cfg.agnes_token}"},
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < RETRY_MAX_ATTEMPTS:
                    attempt += 1
                    logger.warning(
                        "[agnes] %s %s 第 %d 次网络异常 %s: %s → 退避重试",
                        method, path, attempt, type(exc).__name__, exc,
                    )
                    await self._backoff(deadline, retry_after=None)
                    continue
                return ApiCallResult(
                    ok=False,
                    model_id=model_id,
                    error_detail=f"{type(exc).__name__}: {exc}",
                )
            status = resp.status_code
            if 500 <= status <= 599 and attempt + 1 < RETRY_MAX_ATTEMPTS:
                attempt += 1
                logger.warning(
                    "[agnes] %s %s 第 %d 次上游 %d → 退避重试",
                    method, path, attempt, status,
                )
                await self._backoff(deadline, retry_after=_parse_retry_after(resp))
                continue
            if status == 200:
                return parse(resp, model_id)
            # 4xx / 429 / 重试耗尽的 5xx：零重试或不再重试，直接返回
            return ApiCallResult(
                ok=False,
                status_code=status,
                model_id=model_id,
                retry_after=_parse_retry_after(resp),
                error_detail=_error_detail(resp),
            )

    async def _backoff(self, deadline: budget.Deadline, retry_after: Optional[float]) -> None:
        """退避计入预算（R3）：sleep min(delay, 剩余)；退避后预算耗尽 → 转超时。"""
        delay = backoff_delay(retry_after)
        capped = min(delay, max(deadline.remaining(), 0.0))
        logger.debug(
            "retry backoff delay=%.3fs (capped=%.3fs, retry_after=%s)",
            delay, capped, retry_after,
        )
        await self._sleep(capped)
        deadline.check()

    # ------------------------------------------------------------------
    # 内部：200 响应解析（三端点各自）
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_image_response(resp: httpx.Response, model_id: str) -> ApiCallResult:
        urls = _extract_urls(resp)
        if not urls:
            return ApiCallResult(
                ok=False,
                status_code=200,
                model_id=model_id,
                error_detail="malformed 200 response: no image url",
            )
        return ApiCallResult(
            ok=True, status_code=200, model_id=model_id, urls=tuple(urls)
        )

    @staticmethod
    def _parse_submit_response(resp: httpx.Response, model_id: str) -> ApiCallResult:
        video_id = _extract_video_id(resp)
        if not video_id:
            return ApiCallResult(
                ok=False,
                status_code=200,
                model_id=model_id,
                error_detail="malformed 200 response: no video_id",
            )
        return ApiCallResult(
            ok=True, status_code=200, model_id=model_id, video_id=video_id
        )

    @staticmethod
    def _parse_query_response(resp: httpx.Response, model_id: str) -> ApiCallResult:
        status, url = _extract_video_fields(resp)
        if not status:
            return ApiCallResult(
                ok=False,
                status_code=200,
                model_id=model_id,
                error_detail="malformed 200 response: no status",
            )
        return ApiCallResult(
            ok=True, status_code=200, model_id=model_id,
            video_status=status, video_url=url,
        )
