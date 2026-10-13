"""V4 参考图解析契约（infra/refs.py）：形态覆盖 + 安全边界。

为什么专门补这一份单测
------------------------------------------------
refs 是 V4 的入口闸门，也是**唯一的越权读风险点**：一旦它肯读任意本地路径，
调用方就能把 `/etc/mcp-imagegen/env`（内含真实 Agnes 密钥）当"参考图"送去上游。
这类漏洞在集成测试里几乎抓不到——功能照样跑通，只是凭据悄悄外泄。
所以这里把「形态识别」和「只读允许目录」两条线各自钉死。

V4 产品决策第四条：**参考图解析失败一律报错，绝不静默降级**。
对应的验证是：批量解析里只要有一张不可用，整批就必须失败，不许"跳过坏的继续生成"。
"""
from __future__ import annotations

import base64

import pytest

from infra import refs
from infra.refs import (
    IMAGE_EXTS,
    SAFE_NAME_RE,
    SRC_INLINE,
    SRC_LOCAL,
    SRC_URL,
    ReferenceError,
    _is_private_host,
    to_data_uri,
)

# 合法产物名（与 app/endpoints.py 落盘命名同一套正则）
PNG_NAME = "abc123abc123-01-deadbeef.png"
JPG_NAME = "123456abcdef-07-0123abcd.jpg"
MP4_NAME = "fedcba654321-02-89abcdef.mp4"
OUTSIDE_NAME = "aaaabbbbcccc-03-11112222.png"  # 存在但落在允许目录之外


def write_png(directory, name=PNG_NAME, payload=b"\x89PNG\r\n\x1a\n" + b"body"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(payload)
    return directory / name


@pytest.fixture
def roots(tmp_path):
    image_dir = tmp_path / "images"
    video_dir = tmp_path / "videos"
    for d in (image_dir, video_dir):
        d.mkdir()
    write_png(image_dir)
    return image_dir, video_dir


def resolve(value, image_dir, video_dir=None, max_bytes=10 * 1024 * 1024):
    return refs.resolve_reference(
        value, image_dir=str(image_dir),
        video_dir=str(video_dir) if video_dir else None,
        max_bytes=max_bytes,
    )


# ---------------------------------------------------------------- 形态 1：公网 URL

@pytest.mark.parametrize("url", [
    "https://cdn.example.com/a/b.png",
    "http://cdn.example.com/a.png?x=1&y=2",
    "https://cdn.example.com:8443/deep/path/img.jpeg",
])
def test_public_url_passthrough_untouched(url):
    """公网 URL 原样透传给上游（上游自己抓），来源标记 SRC_URL。"""
    value, source = resolve(url, "/nonexistent")
    assert (value, source) == (url, SRC_URL)


@pytest.mark.parametrize("host", [
    "localhost", "printer.local", "127.0.0.1", "::1", "169.254.1.1",
    "10.1.2.3", "192.168.1.100", "172.16.0.1", "172.31.255.255",
])
def test_private_host_is_detected(host):
    assert _is_private_host(host) is True


@pytest.mark.parametrize("host", [
    "cdn.example.com", "8.8.8.8", "172.15.0.1", "172.32.0.1", "1.1.1.1",
])
def test_public_host_is_not_flagged(host):
    """172.15 与 172.32 都在 RFC1918 段外，误拦会伤到真实公网图床。"""
    assert _is_private_host(host) is False


@pytest.mark.parametrize("url", [
    "http://192.168.1.100/files/x.png",
    "https://localhost/files/x.png",
    "http://127.0.0.1:8000/files/x.png",
])
def test_private_url_rejected_with_actionable_hint(url):
    """内网 URL 必须本地拦下，并把「下一步怎么办」写进错误信息。

    交给上游拦也可以，但上游给的是英文原始报错，Agent 无法据此自愈。
    """
    with pytest.raises(ReferenceError) as exc:
        resolve(url, "/nonexistent")
    assert "内网地址" in str(exc.value)
    assert "/upload" in str(exc.value) or "产物名" in str(exc.value)


# ------------------------------------------------------- 形态 2：data URI / base64

def test_data_uri_passthrough():
    uri = "data:image/png;base64," + base64.b64encode(b"payload").decode()
    value, source = resolve(uri, "/nonexistent")
    assert (value, source) == (uri, SRC_INLINE)


def test_data_uri_without_comma_rejected():
    with pytest.raises(ReferenceError) as exc:
        resolve("data:image/png;base64", "/nonexistent")
    assert "逗号" in str(exc.value)


def test_bare_base64_gets_data_uri_header():
    """裸 base64 必须补 data URI 头——实测上游两种都收，统一形态更稳。"""
    raw = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 200).decode()
    value, source = resolve(raw, "/nonexistent")
    assert source == SRC_INLINE
    assert value == "data:image/png;base64," + raw


@pytest.mark.parametrize("bad", [
    "",                      # 空串
    "   ",                   # 纯空白
    "just some chinese text",  # 普通文本（长得不够像 base64）
    "A" * 200,               # 长度够但无图片魔数特征 → 不可识别
])
def test_unrecognized_input_rejected_with_shape_hint(bad):
    with pytest.raises(ReferenceError) as exc:
        resolve(bad, "/nonexistent")
    message = str(exc.value)
    assert "公网 URL" in message or "不能为空" in message


def test_non_string_input_rejected():
    with pytest.raises(ReferenceError):
        refs.resolve_reference(
            12345, image_dir="/nonexistent", max_bytes=1024)


# --------------------------------------------------------- 形态 3：本服务自身产物

@pytest.mark.parametrize("value", [
    PNG_NAME,                                  # 裸产物名
    f"/files/{PNG_NAME}",                      # files 路径
    f"http://192.168.1.100:8000/files/{PNG_NAME}",  # 本服务内网 URL（关键闭环）
])
def test_own_artifact_is_read_and_inlined(tmp_path, roots, value):
    """「挑一张自己的产物当参考图」必须三种写法都能闭环。

    第三条是本轮产品设计的核心：Agent 手里通常只有完整 URL，
    若只认裸名，调用方还得自己截文件名，摩擦就又回来了。
    """
    image_dir, _ = roots
    value_out, source = resolve(value, image_dir, tmp_path / "videos")
    assert source == SRC_LOCAL
    assert value_out.startswith("data:image/png;base64,")
    expected = base64.b64encode((image_dir / PNG_NAME).read_bytes()).decode()
    assert value_out.endswith(expected)


def test_video_frame_name_rejected_as_reference(roots):
    """视频产物不能当参考图：上游不接受 mp4 当 image，本地先拦。"""
    image_dir, _ = roots
    write_png(image_dir, MP4_NAME, b"fake-mp4")
    with pytest.raises(ReferenceError) as exc:
        resolve(MP4_NAME, image_dir)
    assert "必须是图片" in str(exc.value)


def test_jpeg_extension_gets_jpeg_mime(roots):
    image_dir, _ = roots
    write_png(image_dir, JPG_NAME, b"\xff\xd8\xff" + b"jpeg-body")
    value, _source = resolve(JPG_NAME, image_dir)
    assert value.startswith("data:image/jpeg;base64,")


def test_artifact_read_falls_back_to_video_dir(tmp_path, roots):
    """位数存的实现：产物可能落在 image 或 video 两个目录，都要找得到。"""
    image_dir, video_dir = roots
    write_png(video_dir, JPG_NAME, b"\xff\xd8\xff" + b"in-video-dir")
    value, source = resolve(JPG_NAME, image_dir, video_dir)
    assert source == SRC_LOCAL
    assert "jpeg" in value[:40]


# ------------------------------------------------------------------ 安全边界

def test_absolute_path_outside_allowed_dirs_rejected(tmp_path, roots):
    """硬约束：绝不能用任意本地路径（否则服务器凭据会随参考图送上游）。"""
    image_dir, _ = roots
    secret = tmp_path / "env"
    secret.write_text("AGNES_TOKEN=real-secret-value\n")
    with pytest.raises(ReferenceError) as exc:
        resolve(str(secret), image_dir)
    assert "real-secret-value" not in str(exc.value)
    assert "不允许" in str(exc.value) or "无法识别" in str(exc.value)


def test_path_traversal_blocked(tmp_path, roots):
    """即使是合法产物名，越出允许根目录也一律读不到。"""
    image_dir, _ = roots
    # 1) 名字合法、文件真实存在，但落在允许根目录之外 —— 必须读不到
    outside_png = tmp_path / "elsewhere"
    write_png(outside_png, OUTSIDE_NAME, b"\x89PNG\r\n\x1a\n" + b"outside-payload")
    with pytest.raises(ReferenceError):
        resolve(OUTSIDE_NAME, image_dir)

    # 2) 显式穿越形态（即便通过了 name 白名单也要被 parent 校验挡下）
    with pytest.raises(ReferenceError):
        resolve(f"/files/../../{OUTSIDE_NAME}", image_dir)


def test_missing_artifact_reports_actionable_message(roots):
    """不存在必须报错并告知两条出路：产物名，或先上传。"""
    image_dir, _ = roots
    missing = "000000000000-99-00000000.png"
    assert SAFE_NAME_RE.match(missing)
    with pytest.raises(ReferenceError) as exc:
        resolve(missing, image_dir)
    assert "上传" in str(exc.value)


def test_empty_file_rejected(roots):
    image_dir, _ = roots
    write_png(image_dir, JPG_NAME, b"")
    with pytest.raises(ReferenceError) as exc:
        resolve(JPG_NAME, image_dir)
    assert "为空" in str(exc.value)


def test_oversized_local_file_rejected(roots):
    image_dir, _ = roots
    write_png(image_dir, JPG_NAME, b"x" * 5000)
    with pytest.raises(ReferenceError) as exc:
        resolve(JPG_NAME, image_dir, max_bytes=1024)
    assert "上限" in str(exc.value)


def test_oversized_base64_rejected():
    body = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 5000).decode()
    uri = "data:image/png;base64," + body
    with pytest.raises(ReferenceError) as exc:
        resolve(uri, "/nonexistent", max_bytes=1024)
    assert "上限" in str(exc.value)

    with pytest.raises(ReferenceError):
        resolve(body, "/nonexistent", max_bytes=1024)


# ---------------------------------------------------------------- 批量语义

def test_resolve_references_returns_aligned_sources(tmp_path, roots):
    image_dir, _ = roots
    write_png(image_dir, JPG_NAME, b"\xff\xd8\xff" + b"b")
    values, sources = refs.resolve_references(
        ["https://cdn.example.com/a.png", JPG_NAME, to_data_uri(b"z")],
        image_dir=str(image_dir), max_bytes=10 * 1024 * 1024, max_count=5,
    )
    assert sources == (SRC_URL, SRC_LOCAL, SRC_INLINE)
    assert len(values) == 3
    assert values[0] == "https://cdn.example.com/a.png"


def test_resolve_references_empty_input_is_noop():
    assert refs.resolve_references(
        None, image_dir="/x", max_bytes=1, max_count=1) == ((), ())
    assert refs.resolve_references(
        [], image_dir="/x", max_bytes=1, max_count=1) == ((), ())


def test_resolve_references_count_limit(roots):
    image_dir, _ = roots
    with pytest.raises(ReferenceError) as exc:
        refs.resolve_references(
            ["https://cdn.example.com/a.png"] * 3,
            image_dir=str(image_dir), max_bytes=1024, max_count=2)
    assert "数量超限" in str(exc.value)


def test_no_silent_downgrade_when_one_of_batch_is_bad(roots):
    """V4 决策：批量里一张坏 = 整批失败，不许跳过坏的继续生成。

    参考图丢了还硬生成，用户拿到的是一张与预期无关的图，比直接报错糟得多。
    """
    image_dir, _ = roots
    with pytest.raises(ReferenceError):
        refs.resolve_references(
            [PNG_NAME, "totally-not-an-image"],
            image_dir=str(image_dir), max_bytes=1024, max_count=5)


def test_image_ext_whitelist_is_stable():
    """白名单变动会影响错误分支，钉住防止无意扩大到视频。"""
    assert set(IMAGE_EXTS) == {".png", ".jpg", ".jpeg", ".webp", ".gif"}
