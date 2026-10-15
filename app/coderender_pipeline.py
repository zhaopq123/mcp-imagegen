# -*- coding: utf-8 -*-
"""代码生图 / 代码生视频的工具层（rendercode 两条工具的实现）。

与生图/生视频管道的根本区别
--------------------------
generate_image / generate_video 是「把活交给上游」，要走网关、令牌路由、
预算闸门；本模块是**本地渲染**：代码 → SVG → PNG/GIF/MP4，不联网、
不耗额度、不占泳道。所以它不走 budget/circuit/gateway，只做三件事：

    1. 解析参数（size / lang / frames / fps / format）——越界一律
       `[error] category=invalid_params retry=fixthenretry`（A2 纪律：
       校验在管道层，不在 schema 层，越界也要拿到契约行）；
    2. 调 providers.coderender 渲染；
    3. 落盘到 IMAGE_DIR/VIDEO_DIR，返回与生图同形的产出行 + meta 行
       （URL 行 + [meta] model=… size=…），这样 Agent 侧解析规则完全一致。

落盘纪律与 /upload 一致：O_EXCL 防撞名、0o600、文件名
`<hex12>-<序号>-<hex4><ext>`（生图/上传都是这个形状，便于人工辨认来源）。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Mapping, Optional

from app import contract, endpoints
from engine import errors
from infra.persist import build_base_url
from providers import coderender

log = logging.getLogger("mcp_imagegen.coderender")

MAX_SIDE = 4096
_SIZE_RE = re.compile(r"^\s*(\d{1,4})\s*[x×*]\s*(\d{1,4})\s*$", re.IGNORECASE)
_VIDEO_FORMATS = ("auto", "mp4", "gif")


def parse_size(raw: object, default: tuple[int, int]) -> tuple[int, int]:
    """`"1024x768"` → (1024, 768)；非法就抛 ValueError（由调用方转契约行）。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            w, h = int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            raise ValueError("size 需要 WxH 形式，如 1024x768")
    else:
        hit = _SIZE_RE.match(str(raw))
        if not hit:
            raise ValueError(
                f"size={raw!r} 无法解析；请用 WxH 形式，如 1024x768")
        w, h = int(hit.group(1)), int(hit.group(2))
    if not (16 <= w <= MAX_SIDE and 16 <= h <= MAX_SIDE):
        raise ValueError(f"size 需在 16–{MAX_SIDE} 之间，收到 {w}x{h}")
    return w, h


# Python 代码信号（行首关键字 / print( / 对 svg/out/result 赋值）。
# 判 lang 时先看这个再看 `含 <svg`：Python 代码里几乎必然出现 '<svg' 字面量
# （就是要拼出 SVG 字符串），拿"含 <svg"当 SVG 判据会把最常见的写法判错
# ——线上实测：`svg = '<svg ...>' % ...` 被当成 SVG 送去渲染 → 空文档报错。
_PY_SIGNALS = re.compile(
    r"(?m)^[ \t]*(?:import|from|def|class|for|while|if|elif|else|try|except|"
    r"with|return|lambda|assert|del)\b"
    r"|^[ \t]*(?:svg|out|result|image|output)\s*="
    r"|\bprint\s*\("
)


def detect_lang(code: str, lang: Optional[str]) -> str:
    """lang=auto/None 时按内容判断。

    判据（顺序不能换）：
    1. 以 `<` 开头 → SVG（`<svg>`、`<?xml`、`<rect/>` 形状片段全在这）
    2. 有 Python 信号（行首关键字 / print( / 对 svg 赋值）→ Python
    3. 其余含 `<svg` → SVG
    4. 兜底 Python
    """
    if lang in ("svg", "python"):
        return lang
    text = (code or "").strip()
    if not text:
        return "svg"
    if text.startswith("<"):
        return "svg"
    if _PY_SIGNALS.search(text):
        return "python"
    return "svg" if "<svg" in text.lower() else "python"


class CodeRenderPipeline:
    """代码渲染管道（本地，无上游依赖）。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self.reconfigure(cfg)

    def reconfigure(self, cfg) -> None:
        self._cfg = cfg
        self._image_dir = Path(cfg.image_dir)
        self._video_dir = Path(cfg.video_dir)

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _dir_for(self, ext: str) -> Path:
        """**按扩展名决定目录**，与 /files 的分流规则保持一致。

        踩过的坑：render_video 默认出 GIF，而 /files 只把 .mp4/.webm 当视频
        （VIDEO_EXTS）——把 GIF 存进 VIDEO_DIR 就等于返回一个 404 的 URL。
        /files 的"扩展名决定目录"是有测试钉住的纪律（放错目录就是 404），
        所以这里跟着它走，而不是反过来放宽 /files。
        """
        return self._video_dir if ext in endpoints.VIDEO_EXTS else self._image_dir

    def _save(self, data: bytes, ext: str) -> str:
        """落盘并返回 `/files/<name>`（相对；绝对化由 _url 负责）。"""
        root = self._dir_for(ext)
        root.mkdir(parents=True, exist_ok=True)
        name = ""
        for _ in range(3):
            name = f"{uuid.uuid4().hex[:12]}-00-{secrets.token_hex(4)}{ext}"
            candidate = root / name
            try:
                fd = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            except OSError:
                candidate.unlink(missing_ok=True)
                raise
            try:
                os.chmod(candidate, 0o600)  # Windows 无 POSIX 语义，忽略
            except OSError:
                pass
            log.info("[coderender] 落盘 %s (%d bytes)", name, len(data))
            return f"/files/{name}"
        raise OSError("落盘文件名连续冲突（磁盘异常？）")

    def _url(self, path: str, headers: Optional[Mapping[str, str]]) -> str:
        return build_base_url(headers, self._cfg.port) + path

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------
    async def render_image(
        self,
        code: str,
        *,
        size: object = None,
        lang: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """代码 → PNG。返回契约文本（URL 行 + [meta] / [error] 单行）。"""
        try:
            width, height = parse_size(size, coderender.DEFAULT_SIZE)
        except ValueError as exc:
            return contract.format_error(errors.invalid_params("size", str(exc)))
        if not (code or "").strip():
            return contract.format_error(
                errors.invalid_params("code", "代码为空"))
        kind = detect_lang(code, lang)

        result = await coderender.render_image_from_code(
            code, width=width, height=height, lang=kind)
        if not result.get("ok"):
            return self._render_error(result.get("error") or "渲染失败")

        try:
            path = self._save(result["png"], ".png")
        except OSError as exc:
            log.exception("[coderender] 落盘失败")
            return contract.format_error(errors.internal_error(exc))

        w, h = result.get("size") or (width, height)
        meta = contract.META_PREFIX + " ".join([
            "model=code-render", f"size={w}x{h}", f"lang={kind}",
            f"bytes={len(result['png'])}", "renderer=local",
        ])
        return self._url(path, headers) + "\n" + meta

    async def render_video(
        self,
        code: str,
        *,
        size: object = None,
        fps: int = 12,
        frames: int = 36,
        lang: Optional[str] = None,
        video_format: str = "auto",
        headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        """代码 → 动画（GIF / MP4）。返回契约文本。"""
        try:
            width, height = parse_size(size, coderender.DEFAULT_VIDEO_SIZE)
        except ValueError as exc:
            return contract.format_error(errors.invalid_params("size", str(exc)))
        if not (code or "").strip():
            return contract.format_error(
                errors.invalid_params("code", "代码为空"))
        fmt = (video_format or "auto").strip().lower()
        if fmt not in _VIDEO_FORMATS:
            return contract.format_error(errors.invalid_params(
                "format", "可选 " + "/".join(_VIDEO_FORMATS)))
        kind = detect_lang(code, lang)
        if kind == "svg" and "__FRAME__" not in code:
            # 逐帧渲染同一段静态代码 = 一堆一样的帧，白烧 CPU。
            # 必须提醒（拿 frame 变量做动画是 Python 路径的常见写法）。
            return contract.format_error(errors.invalid_params(
                "code",
                "svg 动画需要在代码里用 __FRAME__ 占位（会替换成帧号）；"
                "python 路径可用 frame/total/width/height 变量"))

        result = await coderender.render_video_from_code(
            code, width=width, height=height, fps=fps, frames=frames,
            lang=kind, video_format=fmt)
        workdir = result.get("workdir") or ""
        if not result.get("ok"):
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            return self._render_error(result.get("error") or "视频合成失败")

        try:
            with open(result["path"], "rb") as fh:
                data = fh.read()
            path = self._save(
                data, "." + str(result.get("format") or "gif"))
        except OSError as exc:
            log.exception("[coderender] 视频落盘失败")
            return contract.format_error(errors.internal_error(exc))
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)  # 帧序列不留在磁盘上

        meta = contract.META_PREFIX + " ".join([
            "model=code-render", f"size={result.get('size', [width, height])[0]}"
            f"x{result.get('size', [width, height])[1]}",
            f"frames={result.get('frames', frames)}", f"fps={result.get('fps', fps)}",
            f"format={result.get('format')}", f"lang={kind}",
            f"bytes={len(data)}", "renderer=local",
        ])
        return self._url(path, headers) + "\n" + meta

    # ------------------------------------------------------------------
    def _render_error(self, detail: str) -> str:
        """渲染失败 → 契约错误行（缺依赖属配置问题：retry=no，给安装指引）。"""
        text = str(detail)
        low = text.lower()
        if ("渲染器" in text) or ("imagemagick" in low) or ("cairosvg" in low):
            return contract.format_error(errors.passthrough_error(
                errors.NOT_CONFIGURED,
                "服务端缺少代码渲染依赖",
                "在容器内安装 imagemagick（apt-get install -y imagemagick）"
                "或 cairosvg（pip install cairosvg）",
            ))
        if "ffmpeg" in low:
            return contract.format_error(errors.passthrough_error(
                errors.NOT_CONFIGURED,
                "服务端没有 ffmpeg，无法输出 mp4",
                "改用 format=gif（无需额外依赖），"
                "或让管理员在容器内安装 ffmpeg",
            ))
        if "超时" in text:
            return contract.format_error(errors.passthrough_error(
                errors.UPSTREAM_ERROR, "渲染超时", "简化代码或减少 frames 后重试"))
        return contract.format_error(errors.passthrough_error(
            errors.INVALID_PARAMS, f"渲染失败: {text}", "检查代码后重试"))
