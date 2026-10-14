"""端点层。

- /mcp：MCP streamable-http 工具端点（由 fastmcp http_app 提供）；
  Bearer 访问令牌鉴权 → 错误令牌返回 **HTTP 401 + [error] mcp_auth
  契约行**（Agent 可直接读懂）；
- /healthz：人工巡检 / 探活（无鉴权；**不参与僵死判据**——watchdog 只认
  心跳）；
- /files/<name>：产物静态服务（图片/视频；单段文件名白名单 + 路径归一
  双重防穿越）；
- /upload：参考图上传（受访问令牌保护）；
- /admin：管理面板（自带管理员口令会话鉴权，不走 MCP 令牌）。

V5：访问令牌从「单个环境变量」变成「面板里可增删改查的多个令牌」，
并在网关层做两件事——
  1. 把命中的令牌挂到 ContextVar 上，供管道深处做路由与隔离；
  2. 按令牌的并发上限做准入，让某个会话再忙也挤不干别人的额度。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Callable, Iterable

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from app import context as context_mod

logger = logging.getLogger("mcp_imagegen.endpoints")

# 落盘文件名格式（persist 生成）：req_id-序号-随机hex + 扩展名
# （T8：白名单扩展 .mp4/.webm——视频落 VIDEO_DIR，按扩展名分流）
_SAFE_NAME = re.compile(
    r"^[0-9a-f]{12}-\d{2}-[0-9a-f]{8}(\.png|\.jpg|\.jpeg|\.webp|\.gif|\.mp4|\.webm)$"
)

# 视频扩展名（分流依据；与 persist._CONTENT_TYPE_EXT 视频段一致）
VIDEO_EXTS = (".mp4", ".webm")

_NOT_FOUND = PlainTextResponse("not found", status_code=404)


def healthz_endpoint(request: Request) -> Response:
    """GET /healthz → 200（循环活着即可达；不参与僵死判据）。"""
    return JSONResponse({"status": "ok"})


def make_healthz_endpoint(components=None) -> Callable[[Request], Response]:
    """带运行实况的 /healthz（巡检/压测看积压）。

    components 为 None 时退化为纯探活（兼容既有调用与单测）。
    """

    def _endpoint(request: Request) -> Response:
        payload: dict = {"status": "ok"}
        if components is not None:
            pool = getattr(components, "pool", None)
            if pool is not None and hasattr(pool, "stats"):
                payload["queues"] = pool.stats()
            table = getattr(getattr(components, "video_pipeline", None),
                            "_table", None)
            if table is not None:
                payload["video_jobs"] = len(table)
            gateway = getattr(components, "gateway", None)
            if gateway is not None:
                payload["capabilities"] = {
                    kind: gateway.has_capability(kind)
                    for kind in ("image", "video")
                }
                payload["providers"] = len(gateway.stats())
        return JSONResponse(payload)

    return _endpoint


def make_files_endpoint(image_dir: str, video_dir: "str | None" = None):
    """GET /files/<name> → 文件内容（单段文件名 + 白名单 + 归一双重校验）。

    video_dir 缺省 None：仅服务图片（兼容既有调用/测试）；提供后按扩展名
    分流（.mp4/.webm → VIDEO_DIR，其余 → IMAGE_DIR）。两目录同纪律：
    白名单正则 → resolve 归一 → parent 必须等于各自根 → is_file。
    """
    image_root = Path(image_dir).resolve()
    video_root = Path(video_dir).resolve() if video_dir else None

    def _root_for(name: str) -> "Path | None":
        if name.endswith(VIDEO_EXTS):
            return video_root
        return image_root

    async def files_endpoint(request: Request) -> Response:
        name = request.path_params.get("name", "")
        if not _SAFE_NAME.match(name):
            return _NOT_FOUND
        root = _root_for(name)
        if root is None:  # 视频扩展名但未配置 VIDEO_DIR → 不存在
            return _NOT_FOUND
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return _NOT_FOUND
        return FileResponse(path)

    return files_endpoint


def _image_extension(data: bytes) -> str | None:
    """按文件签名识别受支持的图片类型；不信任请求头或文件名。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def make_upload_endpoint(image_dir: str, *, max_bytes: int):
    """POST /upload：限大小、按 magic bytes 识别并保存图片。

    客户端发送原始文件字节（例如 curl --data-binary @image.png）。文件名由
    服务端生成；响应给出的 ``url`` 可直接作为参考图参数。认证由最外层
    BearerGate 执行，避免端点漏鉴权。
    """
    root = Path(image_dir).resolve()

    async def upload_endpoint(request: Request) -> Response:
        content_length = request.headers.get("content-length", "")
        try:
            declared = int(content_length) if content_length else 0
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "invalid_content_length"}, status_code=400
            )
        # 恶意保护上限：声明超此值不读 body 直接断（读不动也读不起）
        drain_cap = max_bytes * 8 + 1024 * 1024
        if declared > drain_cap:
            return JSONResponse(
                {"ok": False, "error": "upload_too_large", "max_bytes": max_bytes},
                status_code=413,
                headers={"connection": "close"},
            )
        # 声明超限但在排空上限内：不在这里 return——客户端（urllib 等不发
        # Expect: 100-continue 的）body 还在半路，连接提前关闭会变成 RST，
        # Agent 拿到的是不可自愈的 ConnectionReset 而非 413（生产实测
        # 16MB 上传即触发）。交给下面的流式循环排空后干净回 413。

        chunks: list[bytes] = []
        size = 0
        overflow = False
        async for chunk in request.stream():
            size += len(chunk)
            if overflow:
                if size > drain_cap:
                    # 超过排空上限（明显恶意）：只能断开，放弃送达 413
                    return JSONResponse(
                        {"ok": False, "error": "upload_too_large",
                         "max_bytes": max_bytes},
                        status_code=413,
                        headers={"connection": "close"},
                    )
                continue
            if size > max_bytes:
                overflow = True
                continue
            chunks.append(chunk)
        if overflow:
            return JSONResponse(
                {"ok": False, "error": "upload_too_large", "max_bytes": max_bytes},
                status_code=413,
            )
        data = b"".join(chunks)
        if not data:
            return JSONResponse({"ok": False, "error": "empty_upload"}, status_code=400)

        extension = _image_extension(data)
        if extension is None:
            return JSONResponse(
                {"ok": False, "error": "unsupported_image_format"}, status_code=415
            )

        try:
            root.mkdir(parents=True, exist_ok=True)
            path = None
            for _ in range(3):
                name = f"{uuid.uuid4().hex[:12]}-00-{secrets.token_hex(4)}{extension}"
                candidate = root / name
                try:
                    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                except OSError:
                    candidate.unlink(missing_ok=True)
                    raise
                path = candidate
                break
            if path is None:
                return JSONResponse(
                    {"ok": False, "error": "upload_name_collision"}, status_code=503
                )
        except OSError:
            logger.exception("参考图上传落盘失败")
            return JSONResponse({"ok": False, "error": "upload_storage_failed"}, status_code=500)

        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows 没有 POSIX 权限语义；创建标志已尽量收紧
            pass
        name = path.name
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "url": f"/files/{name}",
                "bytes": len(data),
                "content_type": {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp",
                }[extension],
            },
            status_code=201,
        )

    return upload_endpoint


def upload_route(image_dir: str, *, max_bytes: int) -> Route:
    return Route(
        "/upload", make_upload_endpoint(image_dir, max_bytes=max_bytes),
        methods=["POST"],
    )


def healthz_route() -> Route:
    return Route("/healthz", healthz_endpoint, methods=["GET"])


def files_route(image_dir: str, video_dir: "str | None" = None) -> Route:
    return Route("/files/{name}", make_files_endpoint(image_dir, video_dir), methods=["GET"])


# ---------------------------------------------------------------------------
# /mcp 与 /upload 的访问令牌门（纯 ASGI：包住 fastmcp http_app）
#
# V5：令牌可以在面板里增删改查（不再是一个写死的环境变量），且每个令牌
# 可独立限并发——这条是「多会话 / 多工作空间互不干扰」的关键：
#  pipelines 的泳道额度是全局的，令牌额度是租户级的，两者都要过。
# ---------------------------------------------------------------------------
# 单个令牌等待**并发准入**的上限：超过就快失败（宁可让对方稍后重试，
# 也不能让排队排到 deadline 耗尽——那会浪费它已经拿到的上游额度）
TOKEN_QUEUE_WAIT_SEC = 5.0


class AccessGate:
    """多令牌鉴权 + 租户级并发准入；兼容旧名 BearerGate。"""

    def __init__(self, inner, store, *, wait_sec: float = TOKEN_QUEUE_WAIT_SEC,
                 logger_=None) -> None:
        self._inner = inner
        self._store = store
        self._wait_sec = wait_sec
        self._gates: dict = {}
        self._log = logger_ or logger

    # ------------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return
        path = scope.get("path", "")
        protected = path.startswith("/mcp") or path.startswith("/upload")
        if not protected:
            await self._inner(scope, receive, send)
            return

        secret = self._bearer(scope.get("headers") or [])
        record = self._store.find_token_by_secret(secret) if secret else None
        if record is None or not record.enabled:
            await self._deny(send)
            return

        gate = self._gate_for(record)
        if gate is not None:
            try:
                await asyncio.wait_for(gate.acquire(), timeout=self._wait_sec)
            except asyncio.TimeoutError:
                await self._deny_busy(send)
                return
        try:
            token_ctx = context_mod.current_token
            previous = token_ctx.set(record)
            self._store.touch_token(record)
            try:
                await self._inner(scope, receive, send)
            finally:
                token_ctx.reset(previous)
        finally:
            if gate is not None:
                gate.release()

    # ------------------------------------------------------------------
    def _gate_for(self, record):
        """租户级并发闸门（max_concurrency=0 → 不限，返回 None）。"""
        limit = int(getattr(record, "max_concurrency", 0) or 0)
        if limit <= 0:
            return None
        gate = self._gates.get(record.id)
        if gate is None:
            gate = asyncio.Semaphore(limit)
            self._gates[record.id] = gate
        return gate

    @staticmethod
    def _bearer(headers: Iterable) -> str:
        for name, value in headers:
            if bytes(name).lower() == b"authorization":
                text = bytes(value).decode("latin-1").strip()
                if text[:7].lower() == "bearer ":
                    return text[7:].strip()
                return ""
        return ""

    @staticmethod
    async def _deny(send) -> None:
        from engine import errors

        body = errors.mcp_auth_error()
        await AccessGate._send_error(send, 401, body)

    @staticmethod
    async def _deny_busy(send) -> None:
        from engine import errors

        body = errors.busy_error(TOKEN_QUEUE_WAIT_SEC)
        await AccessGate._send_error(send, 429, body)

    @staticmethod
    async def _send_error(send, status: int, body) -> None:
        text = (
            f"[error] category={body.category} retry={body.retry} "
            f"reason={body.reason} suggestion={body.suggestion}"
        ).encode("utf-8")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(text)).encode("ascii")),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": text})


# 兼容旧命名（历史测试与外部引用）
BearerGate = AccessGate
