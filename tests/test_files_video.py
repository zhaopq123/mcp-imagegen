"""T8 补测：/files 视频分流 + 单段文件名纪律（T7c/T8；全部 mock，无网络）。

覆盖（第四轮派发单第 2 项）：
- 分流：.mp4/.webm 命中 VIDEO_DIR 正常返回（content-type 按扩展名）；
  图片扩展名（.png/.jpg/.jpeg/.webp/.gif）仍走 IMAGE_DIR；
- 反向对账：.mp4 只放 IMAGE_DIR → 404；.png 只放 VIDEO_DIR → 404
  （证明查找目录确由扩展名决定，而非两目录都找）；
- 未配置 VIDEO_DIR（None）：.mp4 → 404，图片不受影响；
- 单段文件名纪律对视频同样生效（白名单正则 → resolve 归一 →
  parent==root → is_file 四重门）：
  - 完整 HTTP 栈（Starlette 路由 + httpx ASGITransport）：双段名（%2F
    编码斜杠）/ 缺失文件 → 404；
  - 端点直调（路由层无法产生的名字形态）：路径穿越前缀/后缀、双段、
    大小写混写、序号一位、hex 七位、非白名单扩展、无扩展、空名 → 404；
  - symlink 出逃：VIDEO_DIR 内符号链接指向目录外同名合法文件 → 404
    （resolve 归一后 parent != root）。

文件名格式（persist 生成）：req_id(12 hex)-序号(2 位)-随机 hex(8)+白名单扩展。
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from app import endpoints

# persist 命名纪律：12 hex - 2 位序号 - 8 hex + 扩展
VALID_MP4 = "abcdef123456-01-00000001.mp4"
VALID_WEBM = "abcdef123456-01-00000002.webm"

# 图片扩展（白名单全集；分流断言用）
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif")
VIDEO_EXTS = ("mp4", "webm")

MP4_PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"files-mp4" * 8
WEBM_PAYLOAD = b"\x1a\x45\xdf\xa3files-webm" * 8


def valid_name(ext: str, index: int) -> str:
    return f"abcdef123456-{index:02d}-{index:0{8}x}.{ext}"


def make_app(image_dir: Path, video_dir: "Path | None") -> Starlette:
    routes = [endpoints.files_route(str(image_dir),
                                   str(video_dir) if video_dir else None)]
    return Starlette(routes=routes)


def make_client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


async def get(app: Starlette, path: str) -> httpx.Response:
    """单次 GET（完整 HTTP 栈：Starlette 路由 → 端点 → FileResponse）。"""
    async with make_client(app) as client:
        return await client.get(path)


@pytest.fixture
def dirs(tmp_path):
    """IMAGE_DIR / VIDEO_DIR 隔离目录对。"""
    image_dir = tmp_path / "images"
    video_dir = tmp_path / "videos"
    image_dir.mkdir()
    video_dir.mkdir()
    return image_dir, video_dir


# ---------------------------------------------------------------------------
# 分流正向：视频扩展 → VIDEO_DIR；图片扩展 → IMAGE_DIR
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ext", VIDEO_EXTS)
async def test_video_ext_served_from_video_dir(dirs, ext):
    image_dir, video_dir = dirs
    name = VALID_MP4 if ext == "mp4" else VALID_WEBM
    payload = MP4_PAYLOAD if ext == "mp4" else WEBM_PAYLOAD
    (video_dir / name).write_bytes(payload)
    # 干扰项：同名若按图片目录找必 404（IMAGE_DIR 为空）
    resp = await get(make_app(image_dir, video_dir), f"/files/{name}")
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"].startswith(f"video/{ext}")


@pytest.mark.parametrize("ext", IMAGE_EXTS)
async def test_image_ext_still_served_from_image_dir(dirs, ext):
    image_dir, video_dir = dirs
    name = valid_name(ext, 3)
    payload = f"image-bytes-{ext}".encode()
    (image_dir / name).write_bytes(payload)
    resp = await get(make_app(image_dir, video_dir), f"/files/{name}")
    assert resp.status_code == 200
    assert resp.content == payload


# ---------------------------------------------------------------------------
# 分流反向：查找目录确由扩展名决定（不是两目录都找）
# ---------------------------------------------------------------------------
async def test_mp4_placed_only_in_image_dir_404(dirs):
    """".mp4 只查 VIDEO_DIR：放错到 IMAGE_DIR → 404。"""
    image_dir, video_dir = dirs
    (image_dir / VALID_MP4).write_bytes(MP4_PAYLOAD)
    resp = await get(make_app(image_dir, video_dir), f"/files/{VALID_MP4}")
    assert resp.status_code == 404


async def test_png_placed_only_in_video_dir_404(dirs):
    """".png 只查 IMAGE_DIR：放错到 VIDEO_DIR → 404。"""
    image_dir, video_dir = dirs
    (video_dir / valid_name("png", 4)).write_bytes(b"misplaced")
    resp = await get(make_app(image_dir, video_dir),
                     f"/files/{valid_name('png', 4)}")
    assert resp.status_code == 404


async def test_missing_file_404(dirs):
    image_dir, video_dir = dirs
    resp = await get(make_app(image_dir, video_dir), f"/files/{VALID_MP4}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 未配置 VIDEO_DIR：视频 404，图片不受影响（兼容旧装配）
# ---------------------------------------------------------------------------
async def test_no_video_dir_configured_mp4_404_png_ok(tmp_path):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    png = valid_name("png", 5)
    (image_dir / png).write_bytes(b"png-still-fine")
    app = make_app(image_dir, None)          # video_dir 缺省 None
    assert (await get(app, f"/files/{VALID_MP4}")).status_code == 404
    resp = await get(app, f"/files/{png}")
    assert resp.status_code == 200
    assert resp.content == b"png-still-fine"


# ---------------------------------------------------------------------------
# 单段文件名纪律 · 完整 HTTP 栈（Starlette 路由层 + 端点层双保险）
# ---------------------------------------------------------------------------
async def test_encoded_slash_double_segment_404(dirs):
    """%2F 编码的斜杠（httpx 解码后成双段路径）→ 404，绝不落盘外查找。"""
    image_dir, video_dir = dirs
    (video_dir / VALID_MP4).write_bytes(MP4_PAYLOAD)  # 引诱：前段合法
    app = make_app(image_dir, video_dir)
    resp = await get(app, f"/files/{VALID_MP4}%2F..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code == 404


async def test_empty_name_404(dirs):
    """空名（/files/）→ 路由不匹配 → 404。"""
    image_dir, video_dir = dirs
    resp = await get(make_app(image_dir, video_dir), "/files/")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 单段文件名纪律 · 端点直调（路由层无法产生的名字形态）
# ---------------------------------------------------------------------------
class FakeRequest:
    """最小 Request 假身：只提供 path_params。"""

    def __init__(self, name: str) -> None:
        self.path_params = {"name": name}


BAD_NAMES = [
    "../" + VALID_MP4,                                   # 穿越前缀
    VALID_MP4 + "/../x",                                 # 穿越后缀
    valid_name("png", 6) + "/../../etc/passwd",           # 穿越长链
    "foo/" + VALID_WEBM,                                 # 双段
    "ABCDEF123456-01-00000009.mp4",                      # 大写 hex
    "abcdef123456-1-0000000a.mp4",                       # 序号一位
    "abcdef123456-01-000000b.mp4",                       # 随机段 hex 七位
    "abcdef123456-01-0000000c.avi",                       # 非白名单扩展
    "abcdef123456-01-0000000d.MP4",                      # 扩展名大写
    "abcdef123456-01-0000000e.mp4x",                     # 尾缀粘连
    "abcdef123456-01-0000000f",                          # 无扩展
    "",                                                  # 空名
    "x" * 32,                                            # 乱名
    "..",                                                # 当前目录
]


@pytest.mark.parametrize("name", BAD_NAMES)
async def test_endpoint_rejects_bad_names(dirs, name):
    """端点自有四重门：白名单正则先行拒绝一切非法形态（视频/图片同纪律）。"""
    image_dir, video_dir = dirs
    # 引诱项：若校验失效，这些名字 resolve 后可能命中目录内文件
    (video_dir / VALID_MP4).write_bytes(MP4_PAYLOAD)
    (image_dir / valid_name("png", 7)).write_bytes(b"decoy-png")
    endpoint = endpoints.make_files_endpoint(str(image_dir), str(video_dir))
    resp = await endpoint(FakeRequest(name))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 单段文件名纪律 · symlink 出逃（resolve 归一层）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not hasattr(os, "symlink"),
                    reason="平台无 symlink 支持")
async def test_symlink_escape_rejected(tmp_path):
    """VIDEO_DIR 内符号链接指向目录外 → resolve 后 parent != root → 404。"""
    image_dir = tmp_path / "images"
    video_dir = tmp_path / "videos"
    image_dir.mkdir()
    video_dir.mkdir()
    outside = tmp_path / VALID_MP4            # 目录外目标（名字完全合法）
    outside.write_bytes(b"secret-outside")
    try:
        os.symlink(outside, video_dir / VALID_MP4)
    except OSError as exc:
        pytest.skip(f"当前环境不允许创建 symlink：{exc}")
    resp = await get(make_app(image_dir, video_dir), f"/files/{VALID_MP4}")
    assert resp.status_code == 404


async def test_real_mp4_still_ok_alongside_symlink(tmp_path):
    """对照组：同目录真实 .mp4 正常返回（拒绝确因 symlink，非误伤）。"""
    image_dir = tmp_path / "images"
    video_dir = tmp_path / "videos"
    image_dir.mkdir()
    video_dir.mkdir()
    (video_dir / VALID_WEBM).write_bytes(WEBM_PAYLOAD)
    resp = await get(make_app(image_dir, video_dir), f"/files/{VALID_WEBM}")
    assert resp.status_code == 200
    assert resp.content == WEBM_PAYLOAD
