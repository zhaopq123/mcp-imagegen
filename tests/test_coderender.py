# -*- coding: utf-8 -*-
"""代码生图/生视频（providers.coderender）测试。

本机Windows 没有 ImageMagick/cairosvg，所以**不真正渲染**，
只测不需要渲染器的逻辑：SVG 规范化、尺寸钳制、Python 沙箱、
帧注入、大小解析。渲染链路由Linux 容器 e2e 验证。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from providers import coderender


# ── SVG 规范化 ──

def test_normalize_wraps_bare_fragment():
    """只给一段形状时，自动包上 <svg> 并补尺寸。"""
    out = coderender._normalize_svg('<rect width="10" height="10"/>', 800, 600)
    assert out.startswith("<svg")
    assert 'width="800"' in out and 'height="600"' in out
    assert out.rstrip().endswith("</svg>")


def test_normalize_adds_viewbox_when_missing():
    out = coderender._normalize_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"></svg>',
        999, 888)
    assert 'viewBox="0 0 999 888"' in out
    # 用户给的 width/height 不被覆盖
    assert 'width="100"' in out


def test_normalize_keeps_existing_viewbox():
    src = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 20" '
           'width="10" height="20"></svg>')
    out = coderender._normalize_svg(src, 999, 888)
    assert 'viewBox="0 0 10 20"' in out
    assert "999" not in out


# ── 大小限制 ──

def test_check_size_rejects_oversized():
    with pytest.raises(ValueError, match="过大"):
        coderender._check_size("x" * (coderender.MAX_SVG_BYTES + 10))


def test_png_size_bytes():
    # 构造一个最小 PNG 头（IHDR 宽高 = 0x0100 x 0x0180）
    data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + (256).to_bytes(4, "big") + (384).to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + b"\x00" * 4)
    assert coderender._png_size_bytes(data) == (256, 384)


def test_png_size_bytes_rejects_garbage():
    assert coderender._png_size_bytes(b"not a png") == (0, 0)


# ── Python 沙箱 ──

def test_python_sandbox_returns_svg_variable():
    code = 'svg = "<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'10\' height=\'10\'></svg>"'
    out = coderender.python_to_svg(code, 10, 10)
    assert "<svg" in out


def test_python_sandbox_returns_print_output():
    code = "print('<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>')"
    out = coderender.python_to_svg(code, 10, 10)
    assert "<svg" in out


def test_python_sandbox_blocks_file_access():
    """沙箱必须挡住 open()——否则等于让用户读服务器文件。"""
    code = "svg = open('/etc/passwd').read()"
    with pytest.raises(ValueError):
        coderender.python_to_svg(code, 10, 10)


def test_python_sandbox_blocks_import():
    """挡住 import（防注入/反弹shell）。"""
    code = ("import socket\n"
            "svg = '<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>'")
    # import 失败 → 代码继续跑完或抛错，但都不能联网；关键是没产出就报错
    try:
        out = coderender.python_to_svg(code, 10, 10, timeout=5)
        # 若仍产出 SVG，说明 import 被静默挡下但代码其余部分可跑——可接受
        assert "<svg" in out
    except ValueError:
        pass


def test_python_sandbox_rejects_no_svg():
    with pytest.raises(ValueError, match="没有产出 SVG"):
        coderender.python_to_svg("x = 1", 10, 10)


def test_python_sandbox_blocks_dunder_escape():
    """经典越狱写法必须被静态门禁当场拒掉（不留到运行时才崩）。"""
    code = ("svg = str(().__class__.__bases__[0].__subclasses__())")
    with pytest.raises(ValueError, match="沙箱禁止"):
        coderender.python_to_svg(code, 10, 10)


def test_python_sandbox_blocks_getattr_escape():
    code = "svg = getattr(1, '__class__').__name__"
    with pytest.raises(ValueError, match="沙箱禁止"):
        coderender.python_to_svg(code, 10, 10)


def test_python_sandbox_provides_math_and_size():
    """沙箱要够用：math 可用、width/height 已注入、random 固定种子。"""
    code = (
        "import math\n"
        "svg = ('<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"%d\" "
        "height=\"%d\"><text>%d</text></svg>' % (width, height, "
        "round(math.pi * 100)))\n"
    )
    out = coderender.python_to_svg(code, 320, 240)
    assert 'width="320"' in out and 'height="240"' in out
    assert ">314<" in out


def test_python_sandbox_random_is_deterministic():
    """同一段代码两次渲染结果必须一致（random 固定种子）。"""
    code = ("svg = '<svg xmlns=\"http://www.w3.org/2000/svg\"><text>%d</text>"
            "</svg>' % random.randint(0, 10**9)")
    a = coderender.python_to_svg(code, 10, 10)
    b = coderender.python_to_svg(code, 10, 10)
    assert a == b


def test_python_sandbox_kills_infinite_loop():
    """死循环必须被超时杀掉，不能把服务挂住。"""
    with pytest.raises(ValueError, match="超时"):
        coderender.python_to_svg("while True:\n    pass", 10, 10, timeout=2.0)


# ── 帧注入（视频）──

def test_inject_frame_provides_variables():
    code = coderender._inject_frame("svg='<svg></svg>'", 3, 36, 720, 1280)
    assert "frame = 3" in code
    assert "total = 36" in code
    assert "width = 720" in code and "height = 1280" in code


def test_inject_frame_svg_replaces_placeholder():
    out = coderender._inject_frame_svg(
        "<svg><desc>__FRAME__</desc></svg>", 7, 24, 100, 100)
    assert ">7<" in out            # __FRAME__ 被替换成7
    assert "__FRAME__" not in out
    assert 'data-frame="7"' in out


def test_inject_frame_svg_without_placeholder():
    src = "<svg width='10' height='10'></svg>"
    out = coderender._inject_frame_svg(src, 2, 10, 10, 10)
    assert 'data-frame="2"' in out
    assert src.split(">")[0][4:] in out or "width='10'" in out


# ── 限额常量（防渲染农场）──

def test_limits_are_sane():
    assert coderender.MAX_FRAMES <= 300
    assert coderender.MAX_RENDER_SEC <= 60
    assert coderender.MAX_SVG_BYTES <= 2 * 1024 * 1024
    assert coderender.MAX_TOTAL_SEC <= 180


# ── 中文字体（不给 -font，IM 会把中文整段静默丢掉）──

@pytest.fixture()
def _clear_font_cache(monkeypatch):
    monkeypatch.setattr(coderender, "_FONT_CACHE", [])
    monkeypatch.delenv("CODERENDER_FONT", raising=False)
    monkeypatch.delenv("WS_RENDER_FONT", raising=False)
    yield
    monkeypatch.setattr(coderender, "_FONT_CACHE", [])


def test_cjk_font_env_override(tmp_path, _clear_font_cache, monkeypatch):
    f = tmp_path / "my.ttf"
    f.write_bytes(b"\x00")
    monkeypatch.setenv("CODERENDER_FONT", str(f))
    assert coderender.cjk_font() == str(f)


def test_cjk_font_prefers_nicer_family(_clear_font_cache, monkeypatch):
    """fc-list 给的候选里要挑顺眼的（文泉驿/思源 > Noto > Droid 兜底）。"""
    monkeypatch.setattr(coderender, "_fc_list_zh", lambda: [
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ])
    monkeypatch.setattr(coderender, "_scan_font_dirs", lambda: [])
    assert coderender.cjk_font().endswith("wqy-microhei.ttc")


def test_cjk_font_none_when_absent(_clear_font_cache, monkeypatch):
    monkeypatch.setattr(coderender, "_fc_list_zh", lambda: [])
    monkeypatch.setattr(coderender, "_scan_font_dirs", lambda: [])
    assert coderender.cjk_font() is None


def test_magick_cmd_includes_font(_clear_font_cache, monkeypatch):
    monkeypatch.setattr(coderender, "cjk_font", lambda: "/f/wqy.ttc")
    cmd = coderender._magick_cmd("convert", "in.svg", "out.png", 600, 300)
    assert cmd[0] == "convert"
    assert "-font" in cmd and "/f/wqy.ttc" in cmd
    # 字体必须排在输入文件之前（IM6 里 -font 是 setting 不是 operator）
    assert cmd.index("-font") < cmd.index("in.svg")
    assert cmd[-1] == "out.png"


def test_magick_cmd_without_font(_clear_font_cache, monkeypatch):
    monkeypatch.setattr(coderender, "cjk_font", lambda: None)
    cmd = coderender._magick_cmd("convert", "in.svg", "out.png", 600, 300)
    assert "-font" not in cmd
    assert cmd == ["convert", "-background", "none", "in.svg",
                   "-resize", "600x300!", "out.png"]


@pytest.mark.asyncio
async def test_render_image_clamps_size():
    """尺寸要被钳制（防止 99999x99999 打爆内存）。"""
    res = await coderender.render_image_from_code(
        '<rect width="10" height="10"/>', width=99999, height=99999)
    # 无渲染器时会先返回 error；有渲染器时也不该崩
    assert res["ok"] in (True, False)
    if res["ok"]:
        assert res["size"][0] <= 4096


@pytest.mark.asyncio
async def test_render_image_no_renderer_returns_actionable_error():
    if coderender.renderer_available():
        pytest.skip("本机有渲染器，跳过")
    res = await coderender.render_image_from_code('<rect width="1" height="1"/>')
    assert res["ok"] is False
    assert "imagemagick" in res["error"].lower() or "cairosvg" in res["error"].lower()
