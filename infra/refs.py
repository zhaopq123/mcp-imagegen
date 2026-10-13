"""参考图解析（V4 新增）：把用户给的「参考图」统一成上游可接收的形态。

为什么需要这一层（V4 产品分析 §3.1，实测依据）
------------------------------------------------
上游 Agnes 在公网，本服务在内网（如 `192.168.1.100`）。实测上游**拒绝内网地址**：

    image must be a public http(s) URL or valid image base64:
    private URL host is not allowed

用户的参考图又常常是「本服务自己的产物」或「本机文件」——
上游两者都抓不到。因此本模块负责把三类输入统一成**上游能收的两种形态**：

    ① 公网 URL          → 原样透传（上游自取）
    ② data URI / base64 → 原样透传
    ③ 本服务自己的产物   → 读本地文件 → data URI（关键闭环）
       （/files/<name> 形式，或裸文件名；也接受本服务自己的内网 URL）

③ 是**产品关键设计**：它让「生成 → 挑一张满意的 → 微调 / 让它动起来」
形成零摩擦闭环，用户不必把图下载到本机再想办法传回上游。

安全边界（硬约束，不可放宽）
------------------------------------------------
- **只允许读 IMAGE_DIR / VIDEO_DIR 内的文件**，绝不接受任意本地路径。
  否则调用方可用 `/etc/mcp-imagegen/env` 当"参考图"，把服务器凭据
  送去第三方——这是必须堵死的越权读。
- 文件名必须匹配落盘命名白名单（`SAFE_NAME_RE`），杜绝路径穿越。
- 用户本机的图（如 Y 盘）走 `POST /upload` 上传后再引用，不经本模块的路径通道。

失败语义（V4 决策：不静默降级）
------------------------------------------------
参考图解析失败一律抛 `ReferenceError` → 上层转 `[error] invalid_params`。
**绝不"丢了参考图还硬生成"**：那会让用户拿到一张与预期无关的图，
比明确报错糟糕得多（产品原则：宁可说不行，不能骗）。
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple
from urllib.parse import urlparse

# 落盘命名白名单（与 app/endpoints.py 同一出处——本模块为单一来源，
# endpoints 反向引用本常量，杜绝两份正则漂移）
SAFE_NAME_RE = re.compile(
    r"^[0-9a-f]{12}-\d{2}-[0-9a-f]{8}(\.png|\.jpg|\.jpeg|\.webp|\.gif|\.mp4|\.webm)$"
)

# 允许的图片扩展名（参考图只能是图片；视频不作参考图）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# 解析结果的来源标记（进 meta 便于审计与排障）
SRC_URL = "url"        # 公网 URL 原样透传
SRC_INLINE = "inline"  # data URI / 裸 base64 原样透传
SRC_LOCAL = "local"    # 本服务产物 → 读盘转 data URI


class ReferenceError(Exception):
    """参考图不可用（参数错，上层转 invalid_params；不静默降级）。"""


def _is_private_host(host: str) -> bool:
    """内网 / 环回 / 链路本地地址判定（上游抓不到，必须本地拦）。

    覆盖：RFC1918 三段、环回、链路本地、以及 `.local` 内网域名后缀。
    宁可误拦（用户改用上传通道），不可放行让上游报错——后者错误信息
    对 Agent 不可自愈。
    """
    if not host:
        return True
    h = host.strip().lower().strip("[]")
    if h in ("localhost",) or h.endswith(".local"):
        return True
    if h.startswith("127.") or h == "::1":
        return True
    if h.startswith("169.254."):          # 链路本地
        return True
    if h.startswith("10.") or h.startswith("192.168."):
        return True
    # 172.16.0.0 – 172.31.255.255
    if h.startswith("172."):
        parts = h.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


def _mime_for(name: str) -> str:
    lower = name.lower()
    for ext, mime in _EXT_MIME.items():
        if lower.endswith(ext):
            return mime
    return "image/png"


def _extract_files_name(value: str) -> Optional[str]:
    """从 URL / 路径中提取 `/files/<name>` 里的 name；非该形态返回 None。"""
    path = urlparse(value).path if "://" in value else value
    marker = "/files/"
    idx = path.rfind(marker)
    if idx >= 0:
        candidate = path[idx + len(marker):]
        if candidate and SAFE_NAME_RE.match(candidate):
            return candidate
    # 裸文件名形态（用户/Agent 直接拿 task 产物名当输入）
    bare = path.rsplit("/", 1)[-1]
    if bare and SAFE_NAME_RE.match(bare):
        return bare
    return None


def _search_roots(
    image_dir: str, video_dir: Optional[str], extra_dirs: Sequence[str] = (),
) -> Iterable[Path]:
    seen: list[Path] = []
    for raw in (image_dir, video_dir, *extra_dirs):
        if not raw:
            continue
        p = Path(raw)
        if p not in seen:
            seen.append(p)
    return seen


def _read_local(name: str, roots: Iterable[Path], max_bytes: int) -> bytes:
    """在允许的目录内查找并读取；越界/超限/不存在 → ReferenceError。"""
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        candidate = (resolved_root / name).resolve()
        # 路径穿越双保险：解析后父目录必须仍是白名单根
        if candidate.parent != resolved_root:
            continue
        if not candidate.is_file():
            continue
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise ReferenceError(f"参考图读取失败: {name} ({exc})") from exc
        if size <= 0:
            raise ReferenceError(f"参考图内容为空: {name}")
        if size > max_bytes:
            raise ReferenceError(
                f"参考图超过大小上限: {name} 为 {size / 1024 / 1024:.1f}MB，"
                f"上限 {max_bytes / 1024 / 1024:.0f}MB")
        try:
            return candidate.read_bytes()
        except OSError as exc:
            raise ReferenceError(f"参考图读取失败: {name} ({exc})") from exc
    raise ReferenceError(
        f"参考图不存在或不在允许目录内: {name}"
        "（只接受本服务产物名，或 /files/<name>；本机图片请先上传）")


def to_data_uri(raw: bytes, name: str = "ref.png") -> str:
    """字节 → data URI（上游实测可用的内联形态）。"""
    return f"data:{_mime_for(name)};base64,{base64.b64encode(raw).decode('ascii')}"


def _estimate_b64_bytes(value: str) -> int:
    """按 base64 文本长度估算原始字节数（校验大小用，无需真解码）。"""
    body = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    body = body.strip()
    return len(body) * 3 // 4


def resolve_reference(
    value: str,
    *,
    image_dir: str,
    video_dir: Optional[str] = None,
    max_bytes: int,
) -> Tuple[str, str]:
    """单个参考图 → (上游可用值, 来源标记)。

    输入形态与处置：
      - 公网 http(s) URL        → 原样（SRC_URL）
      - 本服务 /files 或产物名   → 读盘转 data URI（SRC_LOCAL）
      - 本服务内网 URL          → 同上（按 /files/<name> 提取，抓不到就报错）
      - data URI / 裸 base64    → 原样（SRC_INLINE）
      - 其他内网 URL / 非法输入  → ReferenceError
    """
    if not isinstance(value, str) or not value.strip():
        raise ReferenceError("参考图不能为空")
    text = value.strip()

    # ---- 形态 3：本服务产物（优先于公网判定——内网 URL 也要能闭环）----
    name = _extract_files_name(text)
    if name is not None:
        if not name.lower().endswith(IMAGE_EXTS):
            raise ReferenceError(
                f"参考图必须是图片（.png/.jpg/.jpeg/.webp/.gif），收到 {name}")
        raw = _read_local(
            name, _search_roots(image_dir, video_dir), max_bytes)
        return to_data_uri(raw, name), SRC_LOCAL

    # ---- 形态 1：公网 URL ----
    if text.startswith("http://") or text.startswith("https://"):
        host = urlparse(text).hostname or ""
        if _is_private_host(host):
            raise ReferenceError(
                f"参考图地址是内网地址（{host}），上游无法访问；"
                "请改用本服务产物名，或先用 /upload 上传本机图片")
        return text, SRC_URL

    # ---- 形态 2：data URI / 裸 base64 ----
    if text.startswith("data:"):
        if "," not in text:
            raise ReferenceError("data URI 格式不合法（缺少逗号分隔的 base64 段）")
        if _estimate_b64_bytes(text) > max_bytes:
            raise ReferenceError(
                f"参考图超过大小上限（约 {_estimate_b64_bytes(text) / 1024 / 1024:.1f}MB）")
        return text, SRC_INLINE

    if _looks_like_base64(text):
        if _estimate_b64_bytes(text) > max_bytes:
            raise ReferenceError(
                f"参考图超过大小上限（约 {_estimate_b64_bytes(text) / 1024 / 1024:.1f}MB）")
        # 裸 base64 补上 data URI 头（上游更稳；实测两种都收，统一形态）
        return "data:image/png;base64," + text, SRC_INLINE

    raise ReferenceError(
        "参考图形态无法识别：需为公网 URL、data URI/base64，"
        "或本服务产物名（/files/<name>）")


def _looks_like_base64(text: str) -> bool:
    """粗判裸 base64（长度足够 + 字符集合法 + 含图片魔数特征）。

    只做"像不像"判断，不做严格解码——真解码校验交给上游（它会给出
    明确的 illegal base64 错误）。这里的目标是给出可自愈的本地错误，
    而不是重复实现上游校验。
    """
    if len(text) < 64:
        return False
    body = text.replace("\n", "").replace("\r", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
        return False
    # PNG/JPEG/GIF/WEBP 的 base64 前缀特征（提高判定精度，避免误吞普通长文本）
    return body.startswith(("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR"))


def resolve_references(
    values: Optional[Sequence[str]],
    *,
    image_dir: str,
    video_dir: Optional[str] = None,
    max_bytes: int,
    max_count: int,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """批量解析 → (上游可用值序列, 来源标记序列)；任一失败即抛（不静默降级）。"""
    if not values:
        return (), ()
    items = list(values)
    if len(items) > max_count:
        raise ReferenceError(
            f"参考图数量超限：收到 {len(items)} 张，上限 {max_count} 张")
    resolved: list[str] = []
    sources: list[str] = []
    for item in items:
        value, source = resolve_reference(
            item, image_dir=image_dir, video_dir=video_dir, max_bytes=max_bytes)
        resolved.append(value)
        sources.append(source)
    return tuple(resolved), tuple(sources)
