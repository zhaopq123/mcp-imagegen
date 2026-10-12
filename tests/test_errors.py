"""C3 / R10 门禁：错误分类矩阵（V2 改造版）。

V2 变更：
- 402 分支已删（Agnes 无限期间免费，不可能出现）；
- task_not_found 新增（fixthenretry，R16）；
- upstream_auth 注明 provider=Agnes / AGNES_TOKEN；
- switchable 恒 False（唯一模型无切换，E19）。
"""
from __future__ import annotations

import pytest

from engine import errors


# ---------------------------------------------------------------------------
# 矩阵本身：类别集与 (switchable, retry) 逐格对照
# ---------------------------------------------------------------------------
def test_matrix_categories_match_v2():
    assert set(errors.MATRIX) == {
        "invalid_params", "mcp_auth", "upstream_auth", "rate_limit",
        "upstream_error", "content_policy", "upstream_422", "persist_failed",
        "busy", "task_not_found",
    }


def test_matrix_values_match_spec():
    assert errors.MATRIX[errors.INVALID_PARAMS] == (False, "fixthenretry")
    assert errors.MATRIX[errors.MCP_AUTH] == (False, "no")
    assert errors.MATRIX[errors.UPSTREAM_AUTH] == (False, "no")
    assert errors.MATRIX[errors.TASK_NOT_FOUND] == (False, "fixthenretry")
    assert errors.MATRIX[errors.RATE_LIMIT] == (False, "yes")
    assert errors.MATRIX[errors.UPSTREAM_ERROR] == (False, "yes")
    assert errors.MATRIX[errors.CONTENT_POLICY] == (False, "no")
    assert errors.MATRIX[errors.UPSTREAM_422] == (False, "no")
    assert errors.MATRIX[errors.BUSY] == (False, "yes")


def test_matrix_has_402_removed():
    """402 不再是限流触发（Agnes 无限期间免费）——落入未定义状态兜底。"""
    info = errors.classify_http_status(402, "err")
    assert info.category == errors.UPSTREAM_ERROR   # 不再是 rate_limit
    assert info.retry == "yes"
    assert "402" in info.reason                      # 未定义状态显式透传码值


def test_switchable_is_always_false():
    """E19：唯一模型无切换——所有构造入口的 switchable 恒 False。"""
    infos = [
        errors.invalid_params("size", "bad"),
        errors.mcp_auth_error(),
        errors.busy_error(1.0),
        errors.deadline_exhausted(1.0),
        errors.task_not_found_error("t1"),
        errors.circuit_open_error(300),
        errors.video_failed_error("x"),
    ]
    for info in infos:
        assert info.switchable is False


# ---------------------------------------------------------------------------
# classify_http_status：状态码 → 五元组
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400])
def test_classify_400_content_policy_with_hint(status):
    info = errors.classify_http_status(
        status, "content policy violation: prompt rejected"
    )
    assert info.category == errors.CONTENT_POLICY
    assert info.retry == "no"


def test_classify_400_is_content_policy_regardless_of_detail():
    """400 统一内容审核（V1 语义保留：只发生 1 次调用，改 prompt 再来）。"""
    info = errors.classify_http_status(400, "bad size value")
    assert info.category == errors.CONTENT_POLICY
    assert info.retry == "no"


@pytest.mark.parametrize("status", [401, 403])
def test_classify_upstream_auth_mentions_agnes(status):
    """上游 401/403 注明 provider=Agnes，指向 AGNES_TOKEN。"""
    info = errors.classify_http_status(status, "Invalid token")
    assert info.category == errors.UPSTREAM_AUTH
    assert info.retry == "no"
    assert "Agnes" in info.reason
    assert "AGNES_TOKEN" in info.suggestion


@pytest.mark.parametrize("status", [404, 405])
def test_classify_4xx_no_retry(status):
    """未定义状态码按可重试类兜底（upstream_error, retry=yes）。"""
    info = errors.classify_http_status(status, "err")
    assert info.category == errors.UPSTREAM_ERROR
    assert info.retry == "yes"
    assert info.switchable is False


def test_classify_422_is_upstream_422():
    info = errors.classify_http_status(422, "validation failed")
    assert info.category == errors.UPSTREAM_422


def test_classify_429_rate_limit():
    info = errors.classify_http_status(429, "rate limited")
    assert info.category == errors.RATE_LIMIT
    assert info.retry == "yes"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_classify_5xx_upstream_error(status):
    info = errors.classify_http_status(status, "boom")
    assert info.category == errors.UPSTREAM_ERROR
    assert info.retry == "yes"


# ---------------------------------------------------------------------------
# 构造函数
# ---------------------------------------------------------------------------
def test_task_not_found_error():
    """R16 三分支（TTL/逐出/未知）统一契约：fixthenretry，reason 含 task_id。"""
    info = errors.task_not_found_error("task-123")
    assert info.category == errors.TASK_NOT_FOUND
    assert info.retry == "fixthenretry"
    assert "task-123" in info.reason


def test_task_not_found_without_id():
    info = errors.task_not_found_error(None)
    assert info.retry == "fixthenretry"


def test_circuit_open_error():
    """E24：熔断 OPEN 快失败 = rate_limit + retry=yes + 约 X 秒。"""
    info = errors.circuit_open_error(317.2)
    assert info.category == errors.RATE_LIMIT
    assert info.retry == "yes"
    assert "约 317" in info.reason
    assert "Agnes" in info.reason


def test_video_failed_error():
    info = errors.video_failed_error("sensitive content")
    assert info.category == errors.UPSTREAM_ERROR
    assert info.retry == "yes"
    assert "sensitive content" in info.reason


def test_invalid_params_names_parameter():
    info = errors.invalid_params("size", "malformed")
    assert info.category == errors.INVALID_PARAMS
    assert "size" in info.reason
    assert "malformed" in info.reason


def test_busy_error():
    info = errors.busy_error(30.0)
    assert info.category == errors.BUSY
    assert info.retry == "yes"
    assert "30.0" in info.reason


def test_deadline_exhausted_retry_yes():
    info = errors.deadline_exhausted(199.5)
    assert info.category == errors.UPSTREAM_ERROR
    assert info.retry == "yes"
    assert "199.5" in info.reason


def test_persist_failed_note_is_not_an_error_row():
    """R5：persist 失败降级，不产生 [error] 行。"""
    info = errors.persist_failed_note("download 404")
    assert info.category == errors.PERSIST_FAILED
    assert info.retry is None


def test_error_info_is_immutable():
    info = errors.busy_error(1.0)
    with pytest.raises(Exception):
        info.retry = "no"  # type: ignore[misc]
