"""本地 mock 上游（压测/闭环验证用，不消耗真实额度）。

端点形状刻意做成 OpenAI 兼容风格，但本服务不绑定任何厂商——
换任何一家 API，只要把面板里的请求模板改成它的形状即可。

行为：
  POST /v1/images/generations → 延迟 IMAGE_DELAY_SEC 后返回 1 张图 URL
  POST /v1/videos             → 返回 video_id
                                （开启 MOCK_VIDEO_REJECT_N 时前 N 次返回
                                  503 video_queue_full，用于验证服务端代排队）
  GET  /v1/videos/query              → 前 POLLS 次 running，之后 completed
  GET  /file.png              → 小 PNG（供 persist 下载）

启动：python mock_upstream.py [port]
"""
from __future__ import annotations

import asyncio
import os
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

IMAGE_DELAY = float(os.environ.get("MOCK_IMAGE_DELAY", "0.6"))
VIDEO_DELAY = float(os.environ.get("MOCK_VIDEO_DELAY", "0.2"))
POLLS = int(os.environ.get("MOCK_VIDEO_POLLS", "6"))
# V4：前 N 次提交返回队列满（503），验证服务端代排队 + 出片闭环
REJECT = int(os.environ.get("MOCK_VIDEO_REJECT_N", "0"))
_counter = {"n": 0}
_polls: dict[str, int] = {}
_rejects = {"n": 0}
_submit_log: list[str] = []

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
)


async def images(request: Request) -> Response:
    await asyncio.sleep(IMAGE_DELAY)
    _counter["n"] += 1
    return JSONResponse({"images": [
        {"url": f"http://{request.headers.get('host', '127.0.0.1')}/img{_counter['n']}.png"}]})


async def videos(request: Request) -> Response:
    await asyncio.sleep(VIDEO_DELAY)
    if _rejects["n"] < REJECT:
        # V4：模拟上游队列满（服务端应当代排队，而不是甩锅给调用方）
        _rejects["n"] += 1
        _submit_log.append("503-queue-full")
        return JSONResponse(
            {"error": {"message": "video_queue_full: 队列已满"}}, status_code=503)
    vid = f"mock-vid-{_counter['n']}"
    _counter["n"] += 1
    _polls[vid] = 0
    _submit_log.append(f"200-{vid}")
    return JSONResponse({"video_id": vid})


async def query(request: Request) -> Response:
    vid = request.query_params.get("video_id", "")
    _polls[vid] = _polls.get(vid, 0) + 1
    if _polls[vid] <= POLLS:
        return JSONResponse({"status": "running"})
    return JSONResponse({
        "status": "completed",
        "url": f"http://{request.headers.get('host', '127.0.0.1')}/v.mp4"})


async def anyfile(request: Request) -> Response:
    return Response(PNG, media_type="image/png")


async def stats(request: Request) -> Response:
    """提交历史（验证代排队重试次数与最终出片）。"""
    return JSONResponse({"submits": _submit_log, "polls": _polls})


app = Starlette(routes=[
    Route("/v1/images/generations", images, methods=["POST"]),
    Route("/v1/videos", videos, methods=["POST"]),
    Route("/v1/videos/query", query, methods=["GET"]),
    Route("/mock-stats", stats, methods=["GET"]),
    Route("/{name:path}", anyfile, methods=["GET"]),
])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8001,
                log_level="warning")
