"""C3 / R10 门禁：错误分类矩阵（V2 改造版）。

V2 变更：
- 402 分支已删（该计费模型不适用）；
- task_not_found 新增（fixthenretry，R16）；
- upstream_auth 只指向「管理面板改服务提供方密钥」，不点名任何厂商；
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
        # V5：没有任何可用服务提供方（未配置 ≠ 上游故障，retry=no）
        "not_configured",
        # V5：上游账号额度/余额用尽（403；换 Key 修不好，retry=no）
        "insufficient_quota",
    }


def test_not_configured_is_not_retryable():
    """配置缺失必须 retry=no：没配好之前重试一万次也是同一个结果。"""
    info = errors.not_configured_error("image", "面板里没有启用任何提供方")
    assert info.category == errors.NOT_CONFIGURED
    assert info.retry == "no"
    assert "管理面板" in info.suggestion


def test_matrix_values_match_spec():
    assert errors.MATRIX[errors.INVALID_PARAMS] == (False, "fixthenretry")
    assert errors.MATRIX[errors.MCP_AUTH] == (False, "no")
    assert errors.MATRIX[errors.UPSTREAM_AUTH] == (False, "no")
    assert errors.MATRIX[errors.INSUFFICIENT_QUOTA] == (False, "no")
    assert errors.MATRIX[errors.TASK_NOT_FOUND] == (False, "fixthenretry")
    assert errors.MATRIX[errors.RATE_LIMIT] == (False, "yes")
    assert errors.MATRIX[errors.UPSTREAM_ERROR] == (False, "yes")
    assert errors.MATRIX[errors.CONTENT_POLICY] == (False, "no")
    assert errors.MATRIX[errors.UPSTREAM_422] == (False, "no")
    assert errors.MATRIX[errors.BUSY] == (False, "yes")


def test_matrix_has_402_removed():
    """402 不再是限流触发（该计费模型不适用）——落入未定义状态兜底。"""
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
def test_classify_upstream_auth_points_to_panel(status):
    """上游 401/403：指向「管理面板改服务提供方密钥」，不绑定任何厂商。"""
    info = errors.classify_http_status(status, "Invalid token")
    assert info.category == errors.UPSTREAM_AUTH
    assert info.retry == "no"
    assert "管理面板" in info.suggestion


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
    # 厂商中立：错误文案里不许出现任何 provider=<厂商名> 的归属
    assert "provider=" not in info.reason
    assert "provider=" not in info.suggestion


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


def test_classify_400_task_not_exist_is_not_content_policy():
    """上游 400 也可能是「任务不存在」——不能一律报内容审核。

    生产实测（2026-10-15）：轮询参数名与上游不符，上游回 400
    ``{"code":"task_not_exist","message":"任务不存在"}``，却被报成
    「内容审核未通过(400)」——排查方向被整个带偏。
    """
    info = errors.classify_http_status(
        400, "任务不存在 (request id: 202610150800499035830991XTGzzKt)")
    assert info.category == errors.TASK_NOT_FOUND
    assert info.retry == "fixthenretry"
    assert "参数名" in info.suggestion


@pytest.mark.parametrize("detail", [
    '{"code":"task_not_exist","message":"x"}',
    "TASK_NOT_EXIST",
    "Task Not Found",
])
def test_classify_400_task_missing_markers(detail):
    assert errors.classify_http_status(400, detail).category == errors.TASK_NOT_FOUND


@pytest.mark.parametrize("detail", [
    "内容审核未通过：prompt 含违规内容",
    "bad size value",
    "content policy violation",
])
def test_classify_400_other_business_errors_still_content_policy(detail):
    assert errors.classify_http_status(400, detail).category == errors.CONTENT_POLICY


# ---------------------------------------------------------------------------
# 403 分流：鉴权失败 vs 账号额度/余额用尽（2026-10-15 真机）
# ---------------------------------------------------------------------------
def test_classify_403_insufficient_quota_is_not_auth():
    """上游 403 也可能是「额度用尽」——不能一律报「密钥配错」。

    生产实测（2026-10-15）：上游 403
    ``{"code":"insufficient_user_quota","message":"用户额度不足，剩余额度: ￥0.000000"}``
    被报成「服务提供方的密钥有误或已失效」——用户按提示反复换 Key 也修不好，
    实际是账号余额为 0。
    """
    info = errors.classify_http_status(
        403, "用户额度不足，剩余额度: ￥0.000000 (request id: 20261015091303314597804f2gLK5LR)")
    assert info.category == errors.INSUFFICIENT_QUOTA
    assert info.retry == "no"
    assert "密钥有误" not in info.suggestion      # 不再误导到「换密钥」
    assert "充值" in info.suggestion or "更换" in info.suggestion


@pytest.mark.parametrize("detail", [
    "insufficient_user_quota",
    "Insufficient Quota",
    "quota_exceeded",
    "You exceeded your current quota",
    "insufficient_balance",
    "余额不足",
    "额度已用尽",
])
def test_classify_403_quota_markers(detail):
    assert errors.classify_http_status(403, detail).category == errors.INSUFFICIENT_QUOTA


@pytest.mark.parametrize("detail", [
    "Invalid token",
    "Authentication failed: bad api key",
    "forbidden",
    "",
])
def test_classify_403_auth_still_auth(detail):
    """非额度措辞的 403 仍是鉴权类（不误伤真·密钥错误）。"""
    assert errors.classify_http_status(403, detail).category == errors.UPSTREAM_AUTH


def test_classify_401_never_quota():
    """401 只可能是鉴权：即使正文里出现「额度」也不改判（防误伤）。"""
    assert errors.classify_http_status(
        401, "额度不足").category == errors.UPSTREAM_AUTH


def test_classify_429_stays_rate_limit_even_with_quota_wording():
    """429 一律 rate_limit：同家上游 429 = 速率限制，措辞里也带「额度」，
    若按标记分流会把「稍后重试」误判成「不可重试」（免费档 60~90s 放行一个）。"""
    info = errors.classify_http_status(429, "免费额度限流，请稍后重试")
    assert info.category == errors.RATE_LIMIT
    assert info.retry == "yes"
