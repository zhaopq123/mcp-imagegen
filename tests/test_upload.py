"""V4 参考图上传契约：大小/格式校验、私有文件落盘与 Bearer 鉴权。"""
from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from app import endpoints
from conftest import make_config, make_store
from server import Components, build_app

PNG = b"\x89PNG\r\n\x1a\n" + b"test-png-payload"
JPEG = b"\xff\xd8\xff" + b"test-jpeg-payload"


def make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def upload_app(image_dir, max_bytes=1024):
    return Starlette(routes=[
        Route(
            "/upload",
            endpoints.make_upload_endpoint(str(image_dir), max_bytes=max_bytes),
            methods=["POST"],
        ),
    ])


@pytest.mark.asyncio
async def test_upload_persists_detected_image_and_returns_files_url(tmp_path):
    app = upload_app(tmp_path)
    async with make_client(app) as client:
        response = await client.post("/upload", content=PNG)

    assert response.status_code == 201
    data = response.json()
    assert data["ok"] is True
    assert data["url"] == f"/files/{data['name']}"
    assert re.fullmatch(r"[0-9a-f]{12}-\d{2}-[0-9a-f]{8}\.png", data["name"])
    assert data["bytes"] == len(PNG)
    assert (tmp_path / data["name"]).read_bytes() == PNG


@pytest.mark.asyncio
async def test_upload_detects_jpeg_without_trusting_content_type(tmp_path):
    app = upload_app(tmp_path)
    async with make_client(app) as client:
        response = await client.post(
            "/upload", content=JPEG, headers={"content-type": "image/png"}
        )
    assert response.status_code == 201
    assert response.json()["name"].endswith(".jpg")
    assert (tmp_path / response.json()["name"]).read_bytes() == JPEG


@pytest.mark.asyncio
async def test_upload_rejects_empty_oversized_and_non_image_bodies(tmp_path):
    app = upload_app(tmp_path, max_bytes=16)
    async with make_client(app) as client:
        empty = await client.post("/upload", content=b"")
        oversized = await client.post("/upload", content=PNG)
        unsupported = await client.post("/upload", content=b"not an image")

    assert empty.status_code == 400
    assert oversized.status_code == 413
    assert unsupported.status_code == 415
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_bearer_gate_protects_upload_but_not_file_reads(tmp_path):
    upload = endpoints.make_upload_endpoint(str(tmp_path), max_bytes=1024)

    async def files(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("file route")

    app = Starlette(routes=[
        Route("/upload", upload, methods=["POST"]),
        Route("/files/{name}", files, methods=["GET"]),
    ])
    # V5：门禁认的是 store 里的访问令牌（面板维护），不是环境变量常量
    store = make_store(tmp_path / "gate-store.json", tokens=("test-upload-token",))
    gated = endpoints.AccessGate(app, store)
    async with make_client(gated) as client:
        denied = await client.post("/upload", content=PNG)
        accepted = await client.post(
            "/upload", content=PNG,
            headers={"authorization": "Bearer test-upload-token"},
        )
        public_file = await client.get("/files/a.png")

    assert denied.status_code == 401
    assert "category=mcp_auth" in denied.text
    assert accepted.status_code == 201
    assert public_file.status_code == 200


@pytest.mark.asyncio
async def test_streamed_oversize_upload_returns_clean_413(tmp_path):
    """流式超限必须干净地回 413，服务端状态不受污染。

    生产实测（2026-10-14）：16MB 上传直接 RST 连接（超限即 return，
    客户端 body 还在半路）——Agent 拿到的是 ConnectionReset 而非 413，
    不可自愈。修复后：声明超限（在排空上限内）与流式超限都先把
    body 排空再回 413；只有超过恶意保护上限才立即断开。
    """
    big = b"x" * (1024 * 1024)  # 1MB/块
    app = upload_app(tmp_path, max_bytes=1024)

    async def chunks():
        for _ in range(3):
            yield big

    async with make_client(app) as client:
        declared = await client.post("/upload", content=big * 3)  # 声明式超限
        streamed = await client.post("/upload", content=chunks())  # 流式超限
        # 连接未坏：紧接着的正常上传必须成功
        ok = await client.post("/upload", content=PNG)

    assert declared.status_code == 413
    assert streamed.status_code == 413
    assert declared.json()["error"] == "upload_too_large"
    assert ok.status_code == 201
    assert list(tmp_path.iterdir()) == [tmp_path / ok.json()["name"]]


@pytest.mark.asyncio
async def test_absurd_content_length_is_rejected_without_reading(tmp_path):
    """声明超排空上限（恶意灌库）→ 立即 413+close，不浪费资源读 body。"""
    app = upload_app(tmp_path, max_bytes=1024)
    async with make_client(app) as client:
        absurd = await client.post(
            "/upload", content=b"partial",
            headers={"content-length": str(10 ** 12)})
    assert absurd.status_code == 413
    assert absurd.headers.get("connection") == "close"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_build_app_exposes_upload_and_enforces_auth(tmp_path):
    image_dir = tmp_path / "images"
    cfg = make_config(image_dir=image_dir, video_dir=tmp_path / "videos",
                      store_path=tmp_path / "store.json")
    store = make_store(cfg.store_path, tokens=("test-mcp-token",))
    components = Components.build(cfg, store=store)
    app = build_app(cfg, components=components)

    try:
        async with make_client(app) as client:
            denied = await client.post("/upload", content=PNG)
            accepted = await client.post(
                "/upload", content=PNG,
                headers={"authorization": "Bearer test-mcp-token"},
            )
    finally:
        # ASGITransport 不走 lifespan，这里自己收尾，别留着 httpx 连接池
        await components.gateway.aclose()
        await components.persist.aclose()

    assert denied.status_code == 401
    assert accepted.status_code == 201
    assert (image_dir / accepted.json()["name"]).read_bytes() == PNG
