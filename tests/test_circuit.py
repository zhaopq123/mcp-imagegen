"""C8 / R4 / E2 / E8 门禁：熔断器三态状态机（假时钟，无真实等待）。

- CLOSED → OPEN（硬 600s / 软 300s）→ 到期 HALF_OPEN → 探针 → CLOSED/续期；
- 硬熔断 = 429 一次触发；软熔断 = 连续 2 次，成功清零；
- 半开防泄洪：探针资格仅授予一次，后续按 OPEN；
- 探针失败续期一轮；abandon_probe 回收资格防半开卡死；
- E2：record_* 不区分显式/非显式（写入一视同仁）。
"""
from __future__ import annotations

import pytest

from infra.circuit import (
    CircuitBreaker,
    FailureKind,
    SOFT_FAILURE_THRESHOLD,
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN_HARD,
    STATE_OPEN_SOFT,
)


def make(clock=None, hard=600.0, soft=300.0):
    return CircuitBreaker(hard_sec=hard, soft_sec=soft, clock=clock)


# ---------------------------------------------------------------------------
# CLOSED 基线
# ---------------------------------------------------------------------------
def test_initial_state_closed(clock):
    cb = make(clock)
    s = cb.check("agnes-image")
    assert not s.open and not s.probe
    assert s.state == STATE_CLOSED
    # V2：check 为纯查询，不落表（snapshot 只反映真实发生过写入的模型）
    assert cb.snapshot() == {}


def test_two_models_independent(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    assert cb.check("agnes-image").open
    assert not cb.check("agnes-video").open  # 另一模型不受影响


def test_rejects_nonpositive_durations():
    with pytest.raises(ValueError):
        make(hard=0.0)
    with pytest.raises(ValueError):
        make(soft=-1.0)


# ---------------------------------------------------------------------------
# 硬熔断：429/402 一次触发，600s
# ---------------------------------------------------------------------------
def test_hard_failure_opens_immediately(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    s = cb.check("agnes-image")
    assert s.open
    assert s.state == STATE_OPEN_HARD
    assert s.kind is FailureKind.HARD
    assert s.remaining_sec == pytest.approx(600.0)


def test_hard_open_before_expiry(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(599.9)
    assert cb.check("agnes-image").open  # 未到期仍 OPEN
    assert cb.check("agnes-image").remaining_sec == pytest.approx(0.1, abs=0.05)


def test_hard_open_at_expiry_half_open_probe(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    s = cb.check("agnes-image")  # 到期 → 半开 + 探针资格
    assert not s.open
    assert s.probe
    assert s.state == STATE_HALF_OPEN


def test_half_open_only_one_probe(clock):
    """半开防泄洪（R4）：探针资格只授予一次，后续按 OPEN。"""
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    first = cb.check("agnes-image")
    assert first.probe
    second = cb.check("agnes-image")
    assert second.open  # 第二个请求按 OPEN 处理
    assert not second.probe


def test_probe_success_closes(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    assert cb.check("agnes-image").probe
    cb.record_success("agnes-image")
    s = cb.check("agnes-image")
    assert not s.open and not s.probe
    assert s.state == STATE_CLOSED


def test_probe_hard_failure_reopens_hard(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    assert cb.check("agnes-image").probe
    cb.record_failure("agnes-image", FailureKind.HARD)  # 探针又撞 429
    s = cb.check("agnes-image")
    assert s.open and s.state == STATE_OPEN_HARD
    assert s.remaining_sec == pytest.approx(600.0)  # 续期一整轮


# ---------------------------------------------------------------------------
# 软熔断：连续 2 次，成功清零
# ---------------------------------------------------------------------------
def test_single_soft_failure_does_not_open(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    assert not cb.check("agnes-image").open


def test_two_consecutive_soft_failures_open_soft(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    s = cb.check("agnes-image")
    assert s.open and s.state == STATE_OPEN_SOFT
    assert s.remaining_sec == pytest.approx(300.0)


def test_soft_counter_reset_by_success(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_success("agnes-image")  # 清零
    cb.record_failure("agnes-image", FailureKind.SOFT)
    assert not cb.check("agnes-image").open  # 1 次不计
    cb.record_failure("agnes-image", FailureKind.SOFT)  # 连续第 2 次 → 熔断
    assert cb.check("agnes-image").open


def test_probe_soft_failure_reopens_soft(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    clock.advance(300.0)
    assert cb.check("agnes-image").probe
    cb.record_failure("agnes-image", FailureKind.SOFT)  # 探针失败
    s = cb.check("agnes-image")
    assert s.open and s.state == STATE_OPEN_SOFT
    assert s.remaining_sec == pytest.approx(300.0)


def test_soft_failure_in_hard_open_extends_without_downgrade(clock):
    """硬熔断不因软失败降级（E2：显式请求失败写入同一路径）。"""
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(300.0)
    cb.record_failure("agnes-image", FailureKind.SOFT)  # 续期且不降级
    s = cb.check("agnes-image")
    assert s.open and s.state == STATE_OPEN_HARD
    assert s.remaining_sec == pytest.approx(600.0)  # 从续期点重计


def test_hard_failure_in_soft_open_upgrades(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.HARD)  # 直接升级硬熔断
    s = cb.check("agnes-image")
    assert s.open and s.state == STATE_OPEN_HARD


def test_hard_failure_resets_soft_counter(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.HARD)
    cb.record_success("agnes-image")
    cb.record_failure("agnes-image", FailureKind.SOFT)  # 计数已清零，此为第 1 次
    assert not cb.check("agnes-image").open


def test_success_from_open_state_closes(clock):
    """E2：OPEN 态下显式请求成功 → 解除熔断。"""
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    assert cb.check("agnes-image").open
    cb.record_success("agnes-image")
    assert not cb.check("agnes-image").open


# ---------------------------------------------------------------------------
# 探针资格回收（防半开卡死）
# ---------------------------------------------------------------------------
def test_abandon_probe_regrants_eligibility(clock):
    """探针请求未能发出时回收资格，半开状态机不悬死。"""
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    assert cb.check("agnes-image").probe
    cb.abandon_probe("agnes-image")  # 归还资格（不推进 opened_at）
    assert cb.check("agnes-image").probe  # 再次授予


def test_abandon_probe_ignored_for_non_half_open(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    cb.abandon_probe("agnes-image")  # OPEN 态调用：无副作用
    assert cb.check("agnes-image").open
    cb.abandon_probe("no-such-model")  # 未知模型：不炸


# ---------------------------------------------------------------------------
# 边界：到期判定边界值 + 多轮周期
# ---------------------------------------------------------------------------
def test_full_cycle_hard_close_reopen(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.HARD)
    clock.advance(600.0)
    assert cb.check("agnes-image").probe
    cb.record_success("agnes-image")  # CLOSED
    assert not cb.check("agnes-image").open
    cb.record_failure("agnes-image", FailureKind.HARD)  # 再次硬熔断
    clock.advance(599.99)
    assert cb.check("agnes-image").open
    clock.advance(0.01)
    assert cb.check("agnes-image").probe


def test_snapshot_multi_model(clock):
    cb = make(clock)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    cb.record_failure("agnes-image", FailureKind.SOFT)
    snap = cb.snapshot()
    assert snap["agnes-image"] == STATE_OPEN_SOFT
    assert "agnes-video" not in snap or snap.get("agnes-video") != STATE_OPEN_SOFT


def test_soft_threshold_is_two():
    assert SOFT_FAILURE_THRESHOLD == 2
