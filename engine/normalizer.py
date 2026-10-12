"""参数归一层（V2 第七轮 D2 版；红线 C2；E12/E25）。

D2（官方文档对齐，2026-10-10 定案）：size 双格式直传——
- 档位式：官方枚举 1K/2K/3K/4K（F2；F8 实测 200）；
- WxH 式：历史写法正则 \\d{3,4}x\\d{3,4}（F9 实测 1920x1080 → 200，
  精确尺寸可能被上游"标准化"——不影响请求成功）。
两种格式均原样直传上游，本地仅做格式校验（非法值交由上游 400
附 message 透传，㉗）。

ratio（D2 新增）：官方 8 档枚举（F3），仅与档位式 size 配合——
与 WxH 式 size 同传属参数冲突（pipeline 层拦截 invalid_params）。
默认不传（上游默认 1:1）。

已删（反向锁定防回归）：quality/steps/guidance/seed 归一表（E25：
生图请求体不含模型专属参数，seed 为视频侧公共参数，见 agnes.py）、
旧上游两套吸附档位表、cjk_ratio / is_cjk（消费者路由④已删）。
"""
from __future__ import annotations

import re
from typing import Tuple

# 官方档位式 size（F2：推荐 1K/2K/3K/4K；宽容大小写/首尾空白，规范形式大写）
SIZE_TIERS: Tuple[str, ...] = ("1K", "2K", "3K", "4K")

# 官方 ratio 8 档（F3；默认 1:1 = 不传）
IMAGE_RATIOS: Tuple[str, ...] = (
    "1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9",
)

# WxH 历史写法（D2 定案正则：3–4 位数字 × 3–4 位数字）
_WXH_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")


class SizeError(ValueError):
    """size 参数非法（pipeline 转为 invalid_params('size')）。"""


def parse_size(size: str) -> Tuple[int, int]:
    """解析 WxH 式 size；非法即抛 SizeError（含具体原因）。

    仅受理历史写法（3–4 位宽 × 3–4 位高，如 1024x768 / 1920x1080）；
    档位式（1K/2K/3K/4K）走 is_tier_size，不进本函数。大小写/首尾
    空白宽容（'1024X1024'/' 1024x1024 ' 均可）。
    """
    text = (size or "").strip().lower()
    m = _WXH_RE.match(text)
    if m is None:
        raise SizeError(
            "size 需为官方档位（1K/2K/3K/4K）或 '宽x高' 格式"
            f"（如 1024x1024），收到 {size!r}"
        )
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise SizeError(f"size 宽高需为正整数，收到 {size!r}")
    return w, h


def is_tier_size(size: str) -> bool:
    """size 是否为官方档位式（1K/2K/3K/4K；宽容大小写/首尾空白）。"""
    return (size or "").strip().upper() in SIZE_TIERS


def normalize_size(size: str) -> str:
    """size 归一（双格式直传语义，D2）。

    档位式 → 规范大写原样返回（'2k' → '2K'）；WxH 式 → '<宽>x<高>'。
    格式本身非法在本地拦截（[error] invalid_params）；值域不校验
    （交由上游 400 message 透传，㉗）。
    """
    text = (size or "").strip()
    upper = text.upper()
    if upper in SIZE_TIERS:
        return upper
    w, h = parse_size(text)
    return f"{w}x{h}"


def validate_ratio(ratio: str) -> bool:
    """ratio 是否在官方 8 档枚举内（F3；宽容首尾空白）。"""
    return (ratio or "").strip() in IMAGE_RATIOS
