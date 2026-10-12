"""端点层（规格书一、二；S3.3）。

- /mcp：FastMCP streamable-http 工具端点（由 fastmcp http_app 提供）；
  Bearer=MCPACCESS_TOKEN 鉴权 → 错误 Token 返回 **HTTP 401 + [error]
  mcp_auth 契约行**（验收⑫：返回体即 [error] category=mcp_auth
  retry=no，Agent 可直接读懂）；
- /healthz：人工巡检（无鉴权探活；**不参与僵死判据**——watchdog 只认
  心跳，C7）；
- /files/<name>：persist 图片/视频静态服务（无鉴权；文件名仅单段、白名单
  字符校验 + 路径归一双重防穿越；T7c/T8 起按扩展名分流——.mp4/.webm 查
  VIDEO_DIR，其余查 IMAGE_DIR，两目录同一校验纪律）。
"""
from __future__ import annotations

import hmac
import logging
import re
from pathlib import Path
from typing import Iterable

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

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


def healthz_route() -> Route:
    return Route("/healthz", healthz_endpoint, methods=["GET"])


def files_route(image_dir: str, video_dir: "str | None" = None) -> Route:
    return Route("/files/{name}", make_files_endpoint(image_dir, video_dir), methods=["GET"])


# ---------------------------------------------------------------------------
# /mcp Bearer 鉴权门（纯 ASGI：包住 fastmcp http_app，只拦 /mcp*）
# ---------------------------------------------------------------------------
class BearerGate:
    """401 + [error] mcp_auth 行（验收⑫）；常量时间比较防时序侧信道。"""

    def __init__(self, inner, token: str) -> None:
        self._inner = inner
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            if not self._authorized(scope.get("headers") or []):
                await self._deny(send)
                return
        await self._inner(scope, receive, send)

    def _authorized(self, headers: Iterable) -> bool:
        for name, value in headers:
            if name == b"authorization":
                return hmac.compare_digest(bytes(value), self._expected)
        return False

    @staticmethod
    async def _deny(send) -> None:
        from engine import errors

        body = errors.mcp_auth_error()
        text = (
            f"[error] category={body.category} retry={body.retry} "
            f"reason={body.reason} suggestion={body.suggestion}"
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(text)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": text})
