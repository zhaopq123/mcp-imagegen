"""V2 门禁：路由器退化（别名常量 + 熔断门直通；E2/E8/E24）。

- 显式 agnes-image → 绕过熔断表直达（E2）；
- auto → CLOSED/HALF_OPEN 直达（HALF_OPEN 首个请求 probe=True，E8）；
- auto → OPEN → 快失败决策（E24：pipeline 转 rate_limit，注明约 X 秒）。
"""
from __future__ import annotations

import pytest

from engine import router

from conftest import FakeClock

from infra import circuit


def make_circuit(clock, hard_sec=600.0, soft_sec=300.0):
    return circuit.CircuitBreaker(
        hard_sec=hard_sec, soft_sec=soft_sec, clock=clock,
    )


# ---------------------------------------------------------------------------
# 别名常量（E15 延续）
# ---------------------------------------------------------------------------
def test_models_is_single_image_model():
    assert router.MODELS == ("agnes-image",)


def test_router_does_not_hardcode_upstream_model():
    """路由层不感知上游真实模型名（由配置驱动，见 infra/agnes.resolve_model_ids）。"""
    assert router.MODEL_ALIASES == {}
    # 真实模型 ID 不得出现在本模块（防回退为硬编码）
    src = (router.__file__ or "")
    import pathlib
    text = pathlib.Path(src).read_text(encoding="utf-8")
    assert "agnes-image-2.5-flash" not in text


def test_reason_enum_is_closed_set():
    assert {router.REASON_EXPLICIT, router.REASON_AUTO,
            router.REASON_CIRCUIT_OPEN} == {"explicit", "auto", "circuit_open"}


# ---------------------------------------------------------------------------
# 显式路由（E2：绕过熔断表直达）
# ---------------------------------------------------------------------------
def test_explicit_model_bypasses_circuit():
    """显式 agnes-image：即便熔断 OPEN 也直达（用户自救通道，E2）。"""
    clock = FakeClock()
    cb = make_circuit(clock)
    cb.record_failure("agnes-image", circuit.FailureKind.HARD)  # 触发硬熔断
    d = router.route("agnes-image", cb)
    assert d.model == "agnes-image"
    assert d.reason == router.REASON_EXPLICIT
    assert d.probe is False


def test_explicit_model_without_circuit():
    d = router.route("agnes-image", None)
    assert d.model == "agnes-image"
    assert d.reason == router.REASON_EXPLICIT


def test_unknown_model_rejected():
    with pytest.raises(ValueError, match="未知模型"):
        router.route("unknown-model", None)


def test_unknown_model_rejected_even_with_circuit():
    clock = FakeClock()
    with pytest.raises(ValueError):
        router.route("unknown-model", make_circuit(clock))


# ---------------------------------------------------------------------------
# auto 路由（熔断门）
# ---------------------------------------------------------------------------
def test_auto_closed_direct():
    clock = FakeClock()
    d = router.route(None, make_circuit(clock))
    assert d.model == "agnes-image"
    assert d.reason == router.REASON_AUTO
    assert d.probe is False


def test_auto_without_circuit_direct():
    d = router.route(None, None)
    assert d.model == "agnes-image"
    assert d.reason == router.REASON_AUTO


def test_auto_open_hard_fast_fail_with_retry_after():
    """E24：硬熔断 OPEN 期 auto 快失败，注明约 X 秒后重试。"""
    clock = FakeClock()
    cb = make_circuit(clock, hard_sec=600.0)
    cb.record_failure("agnes-image", circuit.FailureKind.HARD)  # OPEN
    d = router.route(None, cb)
    assert d.model is None
    assert d.reason == router.REASON_CIRCUIT_OPEN
    assert d.retry_after_sec == pytest.approx(600.0)


def test_auto_open_soft_fast_fail():
    """软熔断 OPEN 同样快失败（E24 不区分硬/软——唯一模型无处可切）。"""
    clock = FakeClock()
    cb = make_circuit(clock, soft_sec=300.0)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)  # 连败 2 → 软 OPEN
    d = router.route(None, cb)
    assert d.model is None
    assert d.reason == router.REASON_CIRCUIT_OPEN
    assert d.retry_after_sec == pytest.approx(300.0)


def test_auto_open_retry_after_counts_down():
    clock = FakeClock()
    cb = make_circuit(clock, hard_sec=600.0)
    cb.record_failure("agnes-image", circuit.FailureKind.HARD)
    clock.advance(123.4)
    d = router.route(None, cb)
    assert d.retry_after_sec == pytest.approx(477.0, abs=1.0)  # ceil(600-123.4)


def test_auto_half_open_first_request_is_probe():
    """E8：软熔断到期 HALF_OPEN，首个请求获探针资格。"""
    clock = FakeClock()
    cb = make_circuit(clock, soft_sec=300.0)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    clock.advance(300.0)  # 到期 → HALF_OPEN
    d = router.route(None, cb)
    assert d.model == "agnes-image"
    assert d.reason == router.REASON_AUTO
    assert d.probe is True


def test_auto_half_open_second_request_still_direct():
    """半开期间探针已被取走 → 后续请求按 OPEN 快失败（防半开泄洪，R4）。"""
    clock = FakeClock()
    cb = make_circuit(clock, soft_sec=300.0)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    clock.advance(300.0)
    assert router.route(None, cb).probe is True  # 首个取走探针
    d = router.route(None, cb)  # 第二个
    assert d.model is None
    assert d.reason == router.REASON_CIRCUIT_OPEN


def test_probe_success_reopens_route():
    """探针成功 → CLOSED → auto 恢复直达（E8 闭环）。"""
    clock = FakeClock()
    cb = make_circuit(clock, soft_sec=300.0)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    cb.record_failure("agnes-image", circuit.FailureKind.SOFT)
    clock.advance(300.0)
    assert router.route(None, cb).probe is True
    cb.record_success("agnes-image")  # 探针成功 → CLOSED
    d = router.route(None, cb)
    assert d.model == "agnes-image"
    assert d.probe is False
