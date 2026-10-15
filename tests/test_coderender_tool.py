# -*- coding: utf-8 -*-
"""代码生图/生视频的工具层测试（app.coderender_pipeline + 工具注册）。

本机（Windows）没有 ImageMagick/ffmpeg，所以这里**不真渲染**：
- 落盘/契约路径用 monkeypatch 喂假渲染结果（确定性，不依赖环境）
- 缺依赖的报错路径反过来正是本机能覆盖的（无渲染器）
真实渲染链路的权威门禁在 Linux 容器（容器自带 ImageMagick）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import coderender_pipeline as crp  # noqa: E402
from app import tool as tool_mod  # noqa: E402
from providers import coderender  # noqa: E402

# 最小合法 PNG（8 字节签名 + IHDR；字节数够 _png_size_bytes 解析）
_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
        + (320).to_bytes(4, "big") + (240).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)


class _Cfg:
    def __init__(self, tmp: Path) -> None:
        self.image_dir = str(tmp / "images")
        self.video_dir = str(tmp / "videos")
        self.port = 8000


@pytest.fixture()
def pipe(tmp_path):
    return crp.CodeRenderPipeline(_Cfg(tmp_path))


# ── 参数解析 ──

@pytest.mark.parametrize("raw,expect", [
    ("1024x768", (1024, 768)),
    (" 800 × 600 ", (800, 600)),
    ("512*512", (512, 512)),
    ([640, 480], (640, 480)),
    (None, (1024, 1024)),
    ("", (1024, 1024)),
])
def test_parse_size_ok(raw, expect):
    assert crp.parse_size(raw, coderender.DEFAULT_SIZE) == expect


@pytest.mark.parametrize("raw", ["abc", "0x100", "99999x99999", "1024", "xx768"])
def test_parse_size_rejects(raw):
    with pytest.raises(ValueError):
        crp.parse_size(raw, coderender.DEFAULT_SIZE)


def test_detect_lang():
    assert crp.detect_lang("<svg/>", None) == "svg"
    assert crp.detect_lang("svg = '<svg/>'", "auto") == "python"
    assert crp.detect_lang("x = 1", "auto") == "python"
    assert crp.detect_lang("x = 1", "svg") == "svg"      # 显式优先
    # 形状片段（没有 <svg 外层）也是 SVG，不能误判成 Python
    assert crp.detect_lang('<rect width="10" height="10"/>', "auto") == "svg"
    assert crp.detect_lang("<?xml version='1.0'?><svg/>", "auto") == "svg"
    assert crp.detect_lang("", "auto") == "svg"


def test_detect_lang_python_building_svg_is_python():
    """最常见写法 `svg = '<svg ...>'` 里必然出现 `<svg`，不能被当成 SVG。"""
    code = ("import math\n"
            "svg = '<svg xmlns=\"http://www.w3.org/2000/svg\">%d</svg>' % width\n")
    assert crp.detect_lang(code, "auto") == "python"
    assert crp.detect_lang("print('<svg/>')", "auto") == "python"
    assert crp.detect_lang("for i in range(3):\n    pass", "auto") == "python"


# ── 产物目录必须与 /files 的分流规则一致（否则返回的是 404 URL）──

@pytest.mark.parametrize("ext,is_video", [(".png", False), (".gif", False),
                                          (".mp4", True)])
def test_saved_dir_follows_files_endpoint_rules(pipe, ext, is_video):
    from app import endpoints

    root = pipe._dir_for(ext)
    assert (root == pipe._video_dir) is is_video
    assert (ext in endpoints.VIDEO_EXTS) is is_video


def test_rendered_artifact_is_actually_servable(pipe, monkeypatch, tmp_path):
    """端到端对账：render_* 返回的 URL 必须能被 /files 取到。

    这条是补线上实测出来的坑——GIF 存进了 VIDEO_DIR，而 /files 只把
    .mp4/.webm 当视频扩展名 → 返回的 URL 404。
    """
    import httpx
    from starlette.applications import Starlette

    from app import endpoints

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    gif = frames_dir / "out.gif"
    gif.write_bytes(b"GIF89a" + b"\x00" * 64)

    async def fake_video(code, width=720, height=720, fps=12, frames=36,
                         lang="svg", video_format="auto", timeout=20.0):
        return {"ok": True, "path": str(gif), "format": "gif", "frames": frames,
                "fps": fps, "size": [width, height], "workdir": str(frames_dir)}

    async def fake_image(code, width=1024, height=1024, lang="svg",
                         timeout=20.0):
        return {"ok": True, "png": _PNG, "size": [width, height], "svg": code}

    monkeypatch.setattr(coderender, "render_video_from_code", fake_video)
    monkeypatch.setattr(coderender, "render_image_from_code", fake_image)

    app = Starlette(routes=[endpoints.files_route(
        pipe._cfg.image_dir, pipe._cfg.video_dir)])

    async def _run():
        out_img = await pipe.render_image('<rect width="4" height="4"/>')
        out_vid = await pipe.render_video("<svg>__FRAME__</svg>", frames=2)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://t") as c:
            results = []
            for text in (out_img, out_vid):
                url = text.splitlines()[0]
                path = url[url.index("/files/"):]
                r = await c.get(path)
                results.append((path, r.status_code, r.content))
            return results

    for path, status, content in asyncio.run(_run()):
        assert status == 200, f"{path} 取不到（回给 Agent 的就是死链）"
        assert content[:4] in (b"\x89PNG", b"GIF8")


# ── 成功路径（假渲染器）：落盘 + 契约行 ──

def test_render_image_saves_and_formats(pipe, monkeypatch, tmp_path):
    async def fake(code, width=1024, height=1024, lang="svg", timeout=20.0):
        return {"ok": True, "png": _PNG, "size": [width, height], "svg": code}

    monkeypatch.setattr(coderender, "render_image_from_code", fake)
    out = asyncio.run(pipe.render_image(
        '<rect width="10" height="10"/>', size="320x240",
        headers={"host": "192.168.3.35:8000"}))
    lines = out.splitlines()
    assert lines[0].startswith("http://192.168.3.35:8000/files/")
    assert lines[0].endswith(".png")
    # 文件真的落盘了（能被 /files 服务）
    saved = Path(pipe._image_dir) / lines[0].rsplit("/", 1)[-1]
    assert saved.read_bytes() == _PNG
    assert lines[1].startswith("[meta] ")
    assert "model=code-render" in lines[1]
    assert "size=320x240" in lines[1] and "lang=svg" in lines[1]


def test_render_image_relative_when_no_host(pipe, monkeypatch):
    async def fake(code, width=1024, height=1024, lang="svg", timeout=20.0):
        return {"ok": True, "png": _PNG, "size": [width, height], "svg": code}

    monkeypatch.setattr(coderender, "render_image_from_code", fake)
    out = asyncio.run(pipe.render_image("<svg/>"))
    assert out.splitlines()[0].startswith("http://localhost:8000/files/")


def test_render_video_saves_and_formats(pipe, monkeypatch, tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    src = frames_dir / "out.gif"
    src.write_bytes(b"GIF89a" + b"\x00" * 32)

    async def fake(code, width=720, height=720, fps=12, frames=36,
                   lang="svg", video_format="auto", timeout=20.0):
        return {"ok": True, "path": str(src), "format": "gif", "frames": frames,
                "fps": fps, "size": [width, height], "workdir": str(frames_dir)}

    monkeypatch.setattr(coderender, "render_video_from_code", fake)
    out = asyncio.run(pipe.render_video(
        "<svg><desc>__FRAME__</desc></svg>", size="720x480", frames=10, fps=10,
        headers={"host": "h:8000"}))
    lines = out.splitlines()
    assert lines[0].endswith(".gif")
    # GIF 是图片扩展名 → 落 IMAGE_DIR（/files 只把 .mp4/.webm 当视频）
    saved = Path(pipe._dir_for(".gif")) / lines[0].rsplit("/", 1)[-1]
    assert saved.read_bytes().startswith(b"GIF89a")
    assert "frames=10" in lines[1] and "fps=10" in lines[1]
    assert "format=gif" in lines[1]
    # 帧序列目录必须清掉（否则每请求都在磁盘上留一堆 PNG）
    assert not frames_dir.exists()


# ── 参数错误：必须给契约错误行，不能抛异常 ──

def test_bad_size_gives_contract_error(pipe):
    out = asyncio.run(pipe.render_image("<svg/>", size="huge"))
    assert out.startswith("[error] category=invalid_params retry=fixthenretry")
    assert "size" in out


def test_empty_code_gives_contract_error(pipe):
    out = asyncio.run(pipe.render_image("   "))
    assert "[error] category=invalid_params" in out


def test_video_svg_without_frame_placeholder_is_rejected(pipe):
    """静态 SVG 逐帧渲染 = 一堆一样的帧，白烧 CPU：必须明确拒绝并给出写法。"""
    out = asyncio.run(pipe.render_video("<svg><circle/></svg>"))
    assert "[error] category=invalid_params" in out
    assert "__FRAME__" in out


def test_video_bad_format_rejected(pipe):
    out = asyncio.run(pipe.render_video(
        "<svg>__FRAME__</svg>", video_format="avi"))
    assert "[error] category=invalid_params" in out


def test_video_mp4_without_ffmpeg_is_actionable(pipe, monkeypatch):
    """要 mp4 但服务端没 ffmpeg：报 not_configured + 引导用 gif，不许悄悄降级。"""
    monkeypatch.setattr(coderender, "_ffmpeg", lambda: None)
    if coderender._magick() is None and coderender.renderer_available() is None:
        pytest.skip("本机无渲染器，先撞到缺渲染器分支")
    out = asyncio.run(pipe.render_video(
        "<svg>__FRAME__</svg>", frames=2, video_format="mp4"))
    assert "[error] category=not_configured" in out
    assert "gif" in out


def test_missing_renderer_is_actionable(pipe):
    if coderender.renderer_available():
        pytest.skip("本机有渲染器")
    out = asyncio.run(pipe.render_image("<svg/>"))
    assert "[error] category=not_configured retry=no" in out
    assert "imagemagick" in out.lower() or "cairosvg" in out.lower()


# ── MCP 工具注册 ──

def _registered(mcp, name):
    getter = getattr(mcp, "get_tool", None)
    if callable(getter):
        import inspect

        got = getter(name)
        return asyncio.run(got) if inspect.isawaitable(got) else got
    return asyncio.run(mcp._tool_manager.get_tool(name))


def test_tools_registered():
    from fastmcp import FastMCP

    mcp = FastMCP(name="test-coderender")
    tool_mod.register_tool(mcp, pipeline=None, video_pipeline=None,
                           code_render=object())
    img = _registered(mcp, tool_mod.TOOL_NAME_RENDER_IMAGE)
    vid = _registered(mcp, tool_mod.TOOL_NAME_RENDER_VIDEO)
    assert img is not None and vid is not None
    assert tuple(img.parameters["properties"]) == tool_mod.TOOL_PARAMS_RENDER_IMAGE
    assert tuple(vid.parameters["properties"]) == tool_mod.TOOL_PARAMS_RENDER_VIDEO
    # 严格模型兼容：不给 outputSchema
    assert img.output_schema is None and vid.output_schema is None
    # 描述里必须说清「这是代码渲染，不是 AI 生图」
    assert "generate_image" in img.description
    assert "quota" in img.description and "quota" in vid.description
    assert "__FRAME__" in vid.description


def test_tools_absent_without_pipeline():
    """老调用（不传 code_render）→ 只注册原有工具，不报错。"""
    from fastmcp import FastMCP

    mcp = FastMCP(name="test-coderender-off")
    tool_mod.register_tool(mcp, pipeline=None, video_pipeline=None)
    assert _registered(mcp, tool_mod.TOOL_NAME_RENDER_IMAGE) is None
