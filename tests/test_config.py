"""S1 门禁：config.py 覆盖规格书第七节全部变量（名与默认值原样）、
必填缺失 fail-fast、嵌套不等式启动自检（红线 B5/B6）。
"""
from __future__ import annotations

import pytest

from config import (
    AGENT_TIMEOUT_ADVICE_SEC,
    SYSTEMD_TIMEOUT_STOP_SEC,
    Config,
    ConfigError,
    load_config,
)

VALID_ENV = {
    "MCPACCESS_TOKEN": "mcp-token",
    "AGNES_TOKEN": "agnes-token",
}


# ---------------------------------------------------------------------------
# 变量总表：变量名与默认值逐一对照（V2：旧上游五变量已删）
# ---------------------------------------------------------------------------
SPEC_SECTION7_DEFAULTS = {
    "PORT": ("port", 8000),
    "IMAGE_DIR": ("image_dir", "/var/lib/mcp-imagegen/images"),
    "MAXCONCURRENCY": ("max_concurrency", 3),
    "QUEUEWAIT_SEC": ("queue_wait_sec", 30.0),
    "TOTALBUDGETSEC": ("total_budget_sec", 200.0),
    "PERSIST_TIMEOUT_IMAGE_SEC": ("persist_timeout_image_sec", 30.0),  # V2 更名（纯改名）
    "CIRCUITHARDSEC": ("circuit_hard_sec", 600.0),
    "CIRCUITSOFTSEC": ("circuit_soft_sec", 300.0),
    "HTTPXKEEPALIVESEC": ("httpx_keepalive_sec", 25.0),
}


@pytest.mark.parametrize("env_name, attr, default", [
    (k, v[0], v[1]) for k, v in SPEC_SECTION7_DEFAULTS.items()
])
def test_section7_default_values(env_name, attr, default):
    cfg = load_config(dict(VALID_ENV))
    assert getattr(cfg, attr) == default


def test_section7_env_names_map_exactly():
    """环境变量名逐一设置后字段必须跟随（证明变量名与规格书逐字一致）。"""
    env = dict(VALID_ENV)
    expected = {}
    for env_name, (attr, default) in SPEC_SECTION7_DEFAULTS.items():
        # int 类字段换成可区分的整数，float 类换成可区分的浮点（满足不等式）
        if isinstance(default, int):
            value = {"PORT": 9000, "MAXCONCURRENCY": 2}.get(env_name, default)
        else:
            value = default
        if env_name == "TOTALBUDGETSEC":
            value = 200.0
        if env_name == "IMAGE_DIR":
            value = "/tmp/mcp-imagegen-test"
        env[env_name] = str(value)
        expected[attr] = value
    cfg = load_config(env)
    for attr, value in expected.items():
        assert getattr(cfg, attr) == value


def test_derived_httpx_defaults():
    """httpx 四段超时与连接池（规格书五节，配置化派生项）。"""
    cfg = load_config(dict(VALID_ENV))
    assert cfg.httpx_connect_sec == 5.0
    assert cfg.httpx_write_sec == 5.0
    assert cfg.httpx_pool_sec == 5.0
    assert cfg.httpx_max_connections == 10
    assert cfg.httpx_max_keepalive_connections == 4


def test_env_overrides_derived_httpx():
    env = dict(VALID_ENV, HTTPXCONNECTSEC="9", HTTPXMAXCONNECTIONS="7")
    cfg = load_config(env)
    assert cfg.httpx_connect_sec == 9.0
    assert cfg.httpx_max_connections == 7


# ---------------------------------------------------------------------------
# 必填密钥 fail-fast（B5；V2：旧上游密钥变量已删）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["MCPACCESS_TOKEN", "AGNES_TOKEN"])
def test_missing_required_token_fails_fast(missing):
    env = dict(VALID_ENV)
    env.pop(missing)
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_missing_both_tokens_lists_all():
    with pytest.raises(ConfigError) as excinfo:
        load_config({})
    msg = str(excinfo.value)
    assert "MCPACCESS_TOKEN" in msg and "AGNES_TOKEN" in msg


@pytest.mark.parametrize("token", ["MCPACCESS_TOKEN", "AGNES_TOKEN"])
def test_empty_token_fails_fast(token):
    with pytest.raises(ConfigError, match=token):
        load_config(dict(VALID_ENV, **{token: "  "}))


def test_bad_number_fails_fast():
    with pytest.raises(ConfigError, match="PORT"):
        load_config(dict(VALID_ENV, PORT="not-a-number"))


# ---------------------------------------------------------------------------
# 嵌套不等式启动自检（B6）：read ≤ 分档 ≤ 200 < 210 建议 < 220
# ---------------------------------------------------------------------------
def test_defaults_pass_inequality_chain():
    cfg = load_config(dict(VALID_ENV))
    report = cfg.self_check_report()
    assert any("200" in line for line in report)
    assert any("210" in line for line in report)
    assert any("220" in line for line in report)


def test_tier_above_budget_rejected():
    """V2：唯一生图档 TIMEOUT_AGNES_IMAGE_SEC(250) > TOTALBUDGETSEC(100) → 拒绝。"""
    with pytest.raises(ConfigError, match="TIMEOUT_AGNES_IMAGE_SEC"):
        load_config(dict(VALID_ENV, TIMEOUT_AGNES_IMAGE_SEC="250", TOTALBUDGETSEC="100"))


def test_budget_above_agent_advice_rejected():
    """TOTALBUDGETSEC(215) ≥ Agent 建议 210 → 拒绝启动。"""
    with pytest.raises(ConfigError, match="210"):
        load_config(dict(VALID_ENV, TOTALBUDGETSEC="215"))


def test_queue_wait_above_budget_rejected():
    with pytest.raises(ConfigError, match="QUEUEWAIT_SEC"):
        load_config(dict(VALID_ENV, QUEUEWAIT_SEC="250"))


def test_persist_timeout_above_budget_rejected():
    with pytest.raises(ConfigError, match="PERSIST_TIMEOUT_IMAGE_SEC"):
        load_config(dict(VALID_ENV, PERSIST_TIMEOUT_IMAGE_SEC="300"))


def test_boundary_exact_budget_passes():
    """TOTALBUDGETSEC 恰等于最大分档 120：分档 ≤ 200 边界成立，可启动。"""
    cfg = load_config(dict(VALID_ENV, TOTALBUDGETSEC="120"))
    assert cfg.total_budget_sec == 120.0


def test_boundary_budget_just_below_210_passes():
    cfg = load_config(dict(VALID_ENV, TOTALBUDGETSEC="209.5"))
    assert cfg.total_budget_sec == 209.5


def test_deploy_constants_locked():
    """部署层常量：Agent 建议 210 < TimeoutStop 220（规格书二/五节）。"""
    assert AGENT_TIMEOUT_ADVICE_SEC == 210.0
    assert SYSTEMD_TIMEOUT_STOP_SEC == 220.0
    assert AGENT_TIMEOUT_ADVICE_SEC < SYSTEMD_TIMEOUT_STOP_SEC


# ---------------------------------------------------------------------------
# 分档查表（规格书五节：API 调用超时上限）
# ---------------------------------------------------------------------------
def test_api_tier_lookup_removed():
    """V2：分档查表 api_tier_timeout 已随旧上游删除（唯一档=TIMEOUT_AGNES_IMAGE_SEC）。"""
    cfg = load_config(dict(VALID_ENV))
    assert not hasattr(cfg, "api_tier_timeout")
    assert cfg.timeout_agnes_image_sec == 60.0  # 唯一 read 档（D5：30→60）


def test_config_is_frozen():
    cfg = load_config(dict(VALID_ENV))
    with pytest.raises(Exception):
        cfg.port = 9999  # type: ignore[misc]


def test_load_config_without_env_uses_os_environ(monkeypatch):
    """无参 load_config() 回退到 os.environ（from_env 入口）。"""
    monkeypatch.setenv("MCPACCESS_TOKEN", "y")
    monkeypatch.setenv("AGNES_TOKEN", "z")
    cfg = load_config()
    assert cfg.mcp_access_token == "y"
    assert cfg.agnes_token == "z"
    assert isinstance(cfg, Config)


# ===========================================================================
# V2 增量（T1）：Agnes / 视频 / 任务表新变量 + 更名 + 嵌套不等式自检
# ===========================================================================
V2_DEFAULTS = {
    "AGNES_BASE_URL": ("agnes_base_url", "https://api.agnes-ai.cn"),
    "VIDEO_DIR": ("video_dir", "/var/lib/mcp-imagegen/videos"),
    "TIMEOUT_AGNES_IMAGE_SEC": ("timeout_agnes_image_sec", 60.0),  # D5
    "VIDEO_SUBMIT_TIMEOUT_SEC": ("video_submit_timeout_sec", 30.0),
    "VIDEO_POLL_INTERVAL_SEC": ("video_poll_interval_sec", 2.0),   # D7
    "VIDEO_QUERY_TIMEOUT_SEC": ("video_query_timeout_sec", 10.0),
    "PERSIST_TIMEOUT_IMAGE_SEC": ("persist_timeout_image_sec", 30.0),
    "PERSIST_TIMEOUT_VIDEO_SEC": ("persist_timeout_video_sec", 120.0),
    "TASK_TTL_SEC": ("task_ttl_sec", 7200.0),
    "TASK_MAX": ("task_max", 100),
}


@pytest.mark.parametrize("env_name, attr, default", [
    (k, v[0], v[1]) for k, v in V2_DEFAULTS.items()
])
def test_v2_default_values(env_name, attr, default):
    cfg = load_config(dict(VALID_ENV))
    assert getattr(cfg, attr) == default


def test_v2_env_names_map_exactly():
    """V2 环境变量名逐一设置后字段必须跟随（变量名与方案 V2 三节逐字一致）。"""
    env = dict(VALID_ENV)
    expected = {}
    for env_name, (attr, default) in V2_DEFAULTS.items():
        if isinstance(default, int):
            value = 50  # TASK_MAX：可区分整数
        elif isinstance(default, float):
            value = {"TASK_TTL_SEC": 3600.0, "PERSIST_TIMEOUT_VIDEO_SEC": 110.0}.get(env_name, default)
        else:
            value = {"AGNES_BASE_URL": "https://agnes.example", "VIDEO_DIR": "/tmp/videos"}.get(env_name, default)
        env[env_name] = str(value)
        expected[attr] = value
    cfg = load_config(env)
    for attr, value in expected.items():
        assert getattr(cfg, attr) == value


def test_v2_agnes_token_required_fails_fast():
    env = dict(VALID_ENV)
    env.pop("AGNES_TOKEN")
    with pytest.raises(ConfigError, match="AGNES_TOKEN"):
        load_config(env)


def test_v2_agnes_token_blank_fails_fast():
    with pytest.raises(ConfigError, match="AGNES_TOKEN"):
        load_config(dict(VALID_ENV, AGNES_TOKEN="   "))


def test_v2_old_persist_name_no_longer_recognized():
    """改名彻底：旧名 PERSISTTIMEOUTSEC 被无视（不再是合法配置入口）。"""
    cfg = load_config(dict(VALID_ENV, PERSISTTIMEOUTSEC="29"))
    assert cfg.persist_timeout_image_sec == 30.0


def test_v2_inequality_image_tier_above_budget_rejected():
    with pytest.raises(ConfigError, match="TIMEOUT_AGNES_IMAGE_SEC"):
        load_config(dict(VALID_ENV, TIMEOUT_AGNES_IMAGE_SEC="250"))


def test_v2_inequality_submit_above_budget_rejected():
    with pytest.raises(ConfigError, match="VIDEO_SUBMIT_TIMEOUT_SEC"):
        load_config(dict(VALID_ENV, VIDEO_SUBMIT_TIMEOUT_SEC="250"))


def test_v2_inequality_query_above_submit_rejected():
    """单查(35) > 提交(30)：破坏 10 ≤ 30 嵌套校验，拒绝启动。"""
    with pytest.raises(ConfigError, match="VIDEO_QUERY_TIMEOUT_SEC"):
        load_config(dict(VALID_ENV, VIDEO_QUERY_TIMEOUT_SEC="35"))


def test_v2_inequality_query_overshoot_capped_by_nesting():
    """单查(500) 远超提交档：由单查 ≤ 提交嵌套校验拦截（传递性含 ≤ deadline）。"""
    with pytest.raises(ConfigError, match="VIDEO_QUERY_TIMEOUT_SEC"):
        load_config(dict(VALID_ENV, VIDEO_QUERY_TIMEOUT_SEC="500"))


def test_v2_inequality_poll_interval_non_positive_rejected():
    with pytest.raises(ConfigError, match="VIDEO_POLL_INTERVAL_SEC"):
        load_config(dict(VALID_ENV, VIDEO_POLL_INTERVAL_SEC="0"))


def test_v2_inequality_video_persist_above_budget_rejected():
    with pytest.raises(ConfigError, match="PERSIST_TIMEOUT_VIDEO_SEC"):
        load_config(dict(VALID_ENV, PERSIST_TIMEOUT_VIDEO_SEC="250"))


def test_v2_inequality_task_ttl_non_positive_rejected():
    with pytest.raises(ConfigError, match="TASK_TTL_SEC"):
        load_config(dict(VALID_ENV, TASK_TTL_SEC="0"))


def test_v2_inequality_task_max_zero_rejected():
    with pytest.raises(ConfigError, match="TASK_MAX"):
        load_config(dict(VALID_ENV, TASK_MAX="0"))


def test_v2_boundary_query_equal_submit_passes():
    """边界：单查 == 提交（等号成立）可启动——不等式为 ≤ 而非 <。"""
    cfg = load_config(dict(VALID_ENV, VIDEO_QUERY_TIMEOUT_SEC="30"))
    assert cfg.video_query_timeout_sec == 30.0


def test_v2_boundary_tiers_equal_budget_pass():
    """边界：生图/提交档 == 总预算（等号成立）可启动。"""
    cfg = load_config(dict(
        VALID_ENV, TOTALBUDGETSEC="200",
        TIMEOUT_AGNES_IMAGE_SEC="200", VIDEO_SUBMIT_TIMEOUT_SEC="200",
        VIDEO_QUERY_TIMEOUT_SEC="200", PERSIST_TIMEOUT_VIDEO_SEC="200",
    ))
    assert cfg.timeout_agnes_image_sec == 200.0


def test_v2_defaults_report_mentions_video_tiers():
    """自检报告新增 V2 行：三 read 档 + 嵌套链 + 任务表参数。"""
    cfg = load_config(dict(VALID_ENV))
    report = cfg.self_check_report()
    joined = "\n".join(report)
    # D5/D7：生图 60 / 轮询 2（原 30 / 5）
    for needle in ("V2 Agnes read 档", "生图 60", "提交 30", "单查 10",
                   "轮询 2s", "TTL 7200", "容量 100"):
        assert needle in joined, f"自检报告缺少: {needle!r}\n{joined}"
