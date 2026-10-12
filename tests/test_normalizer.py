"""V2 门禁：参数归一层（D2 版：size 双格式直传 + ratio 八档）。

D2（官方文档对齐，2026-10-10 定案）：
- 档位式 size：1K/2K/3K/4K（宽容大小写/首尾空白，规范形式大写）；
- WxH 式：\\d{3,4}x\\d{3,4}（历史写法直传；精确尺寸可能被上游
  "标准化"，不影响请求成功）；
- ratio：官方 8 档枚举，仅与档位式 size 配合（冲突拦截在 pipeline 层）。

已删（对应实现删除，此处反向锁定防回归）：
- quality/steps/guidance 归一表（E25：agnes-image 无模型专属参数）；
- 旧上游两套吸附档位表（size 直传）；
- cjk_ratio / is_cjk（消费者路由④已删）。
"""
from __future__ import annotations

import pytest

from engine import normalizer


# ---------------------------------------------------------------------------
# D2：size 双格式（档位式 + WxH，均原样直传）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size, expect", [
    ("1K", "1K"), ("2K", "2K"), ("3K", "3K"), ("4K", "4K"),
    ("2k", "2K"),                        # 大小写宽容，规范大写（F2/F8）
    (" 2k ", "2K"),                      # 档位式也宽容首尾空白
    ("1024x1024", "1024x1024"),
    ("1920x1080", "1920x1080"),          # WxH 历史写法（F9 实测 200）
    ("768x1024", "768x1024"),
    ("2048X2048", "2048x2048"),          # 大写 X 宽容
    (" 1024x1024 ", "1024x1024"),        # 首尾空白宽容
    ("768x1344", "768x1344"),
])
def test_normalize_size_passthrough(size, expect):
    """D2：合法 size 原样直传（档位规范化大写；WxH 不吸附不改写）。"""
    assert normalizer.normalize_size(size) == expect


@pytest.mark.parametrize("size", [
    "abc", "1024", "0x0", "1024x0", "0x1024", "-5x5", "x", "1024.5x1024",
    "1024xx1024", "", "1x1",        # 1x1：WxH 需 3–4 位数字（D2 正则收紧）
    "12x1024", "1024x12",           # 两位数同拒
    "5K", "0.5K", "1K2",            # 非官方档位
])
def test_parse_size_rejects_malformed(size):
    with pytest.raises(normalizer.SizeError):
        normalizer.normalize_size(size)


def test_parse_size_returns_int_pair():
    assert normalizer.parse_size("768x1344") == (768, 1344)


def test_size_error_is_value_error():
    """SizeError 是 ValueError 子类（pipeline 转 invalid_params('size')）。"""
    assert issubclass(normalizer.SizeError, ValueError)


# ---------------------------------------------------------------------------
# D2：档位判定 + ratio 八档校验（F2/F3）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size, expect", [
    ("1K", True), ("2K", True), ("3K", True), ("4K", True),
    ("2k", True), (" 4K ", True),
    ("1024x768", False), ("", False), ("5K", False), (None, False),
])
def test_is_tier_size(size, expect):
    assert normalizer.is_tier_size(size) is expect


@pytest.mark.parametrize("ratio, expect", [
    ("1:1", True), ("3:4", True), ("4:3", True), ("16:9", True),
    ("9:16", True), ("2:3", True), ("3:2", True), ("21:9", True),
    (" 16:9 ", True),                # 首尾空白宽容
    ("5:4", False), ("16:09", False), ("1:1:1", False),
    ("", False), (None, False), ("169", False),
])
def test_validate_ratio(ratio, expect):
    assert normalizer.validate_ratio(ratio) is expect


def test_ratio_eight_tiers_locked():
    """F3 官方 8 档枚举锁定（防悄悄增删）。"""
    assert normalizer.IMAGE_RATIOS == (
        "1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9",
    )
    assert normalizer.SIZE_TIERS == ("1K", "2K", "3K", "4K")


# ---------------------------------------------------------------------------
# 瘦身反向锁定：旧 API 已删，防回归引用
# ---------------------------------------------------------------------------
def test_deleted_apis_are_gone():
    for name in (
        "normalize_params", "GenerationParams", "snap_size",
        "cjk_ratio", "is_cjk", "QUALITY_LEVELS",
        "_STEPS", "_GUIDANCE", "MODELS",
    ):
        assert not hasattr(normalizer, name), f"已删 API 残留: {name}"


def test_module_public_api_is_minimal():
    """公开 API 恰为 D2 六名（瘦身锁定）。

    annotations 是 from __future__ 注入；Tuple 是 typing 导入名；
    re 是 stdlib 导入名（D2 正则引入）——均非模块 API。
    """
    public = {
        n for n in dir(normalizer)
        if not n.startswith("__") and not n.startswith("_")
        and n not in ("annotations", "Tuple", "re")
    }
    assert public == {
        "SizeError", "parse_size", "normalize_size",
        "SIZE_TIERS", "IMAGE_RATIOS", "is_tier_size", "validate_ratio",
    }
