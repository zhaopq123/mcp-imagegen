"""配置层门禁：默认值、类型解析、嵌套不等式自检、热更新边界。

V5 与旧版最大的差别：**没有必填的上游密钥**。上游地址 / 模型 / 密钥全部
落在 Provider 配置（store.json 或管理面板）里，环境变量只管部署参数。
"""
from __future__ import annotations

import pytest

from config import (
    AGENT_TIMEOUT_ADVICE_SEC,
    BATCH_MAX,
    SYSTEMD_TIMEOUT_STOP_SEC,
    Config,
    ConfigError,
    load_config,
)

# 变量总表：环境变量名 → (字段, 默认值)
SPEC_DEFAULTS = {
    "PORT": ("port", 8000),
    "MAXCONCURRENCY": ("max_concurrency", 3),
    "QUEUEWAIT_SEC": ("queue_wait_sec", 30.0),
    "TOTALBUDGETSEC": ("total_budget_sec", 200.0),
    "PERSIST_TIMEOUT_IMAGE_SEC": ("persist_timeout_image_sec", 30.0),
    "PERSIST_TIMEOUT_VIDEO_SEC": ("persist_timeout_video_sec", 120.0),
    "CIRCUITHARDSEC": ("circuit_hard_sec", 600.0),
    "CIRCUITSOFTSEC": ("circuit_soft_sec", 300.0),
    "HTTPXKEEPALIVESEC": ("httpx_keepalive_sec", 25.0),
    "HTTPXCONNECTSEC": ("httpx_connect_sec", 5.0),
    "HTTPXWRITESEC": ("httpx_write_sec", 5.0),
    "HTTPXPOOLSEC": ("httpx_pool_sec", 5.0),
    "HTTPXMAXCONNECTIONS": ("httpx_max_connections", 10),
    "HTTPXMAXKEEPALIVE": ("httpx_max_keepalive", 4),
    "TIMEOUT_UPSTREAM_IMAGE_SEC": ("timeout_upstream_image_sec", 60.0),
    "VIDEO_SUBMIT_TIMEOUT_SEC": ("video_submit_timeout_sec", 30.0),
    "VIDEO_POLL_INTERVAL_SEC": ("video_poll_interval_sec", 2.0),
    "VIDEO_QUERY_TIMEOUT_SEC": ("video_query_timeout_sec", 10.0),
    "TASK_TTL_SEC": ("task_ttl_sec", 7200.0),
    "TASK_MAX": ("task_max", 100),
    "IMAGE_MAX_CONCURRENCY": ("image_max_concurrency", 8),
    "VIDEO_MAX_CONCURRENCY": ("video_max_concurrency", 4),
    "IMAGE_QUEUE_WAIT_SEC": ("image_queue_wait_sec", 30.0),
    "VIDEO_QUEUE_WAIT_SEC": ("video_queue_wait_sec", 30.0),
    "IMAGE_QUEUE_MAX": ("image_queue_max", 64),
    "VIDEO_QUEUE_MAX": ("video_queue_max", 16),
    "IMAGE_BATCH_CONCURRENCY": ("image_batch_concurrency", 4),
    "VIDEO_JOB_MAX_WAIT_SEC": ("video_job_max_wait_sec", 1800.0),
    "VIDEO_JOB_MAX": ("video_job_max", 24),
    "VIDEO_JOB_CONCURRENCY": ("video_job_concurrency", 8),
    "VIDEO_SUBMIT_QUEUE_WAIT_SEC": ("video_submit_queue_wait_sec", 600.0),
    "VIDEO_SUBMIT_RETRY_MIN_SEC": ("video_submit_retry_min_sec", 20.0),
    "VIDEO_SUBMIT_RETRY_MAX_SEC": ("video_submit_retry_max_sec", 90.0),
    "REFERENCE_MAX_MB": ("reference_max_mb", 10.0),
    "IMAGE_REFERENCE_MAX": ("image_reference_max", 4),
    "VIDEO_REFERENCE_MAX": ("video_reference_max", 5),
    "UPLOAD_MAX_MB": ("upload_max_mb", 15.0),
}


@pytest.mark.parametrize("env_name, attr, default",
                         [(k, v[0], v[1]) for k, v in SPEC_DEFAULTS.items()])
def test_default_values(env_name, attr, default):
    """无环境变量 → 取规格默认值（可审计的单一出处）。"""
    cfg = load_config({})
    assert getattr(cfg, attr) == default


@pytest.mark.parametrize("env_name, attr, default",
                         [(k, v[0], v[1]) for k, v in SPEC_DEFAULTS.items()])
def test_env_overrides_field(env_name, attr, default):
    """环境变量设置后字段跟随（类型正确）。"""
    value = 2 if isinstance(default, int) else default
    cfg = load_config({env_name: str(value)})
    assert getattr(cfg, attr) == value


def test_no_required_tokens_anymore():
    """V5：空环境也能加载（上游凭据不在环境变量里）。"""
    cfg = load_config({})
    assert cfg.store_path
    assert cfg.mcp_access_token == ""


def test_default_dirs_are_under_project():
    cfg = load_config({})
    assert cfg.image_dir.endswith("images")
    assert cfg.video_dir.endswith("videos")
    assert cfg.store_path.endswith("store.json")


def test_bad_type_is_config_error():
    with pytest.raises(ConfigError):
        load_config({"PORT": "not-a-number"})


def test_self_check_rejects_tier_above_budget():
    """调用档 > 总预算 → fail-fast（read ≤ 分档 ≤ 总预算）。"""
    with pytest.raises(ConfigError):
        load_config({"TIMEOUT_UPSTREAM_IMAGE_SEC": "500"})


def test_self_check_rejects_budget_above_agent_advice():
    with pytest.raises(ConfigError):
        load_config({"TOTALBUDGETSEC": str(AGENT_TIMEOUT_ADVICE_SEC)})


def test_self_check_rejects_query_tier_above_submit_tier():
    """单查 ≤ 提交 的嵌套关系被破坏 → fail-fast。"""
    with pytest.raises(ConfigError):
        load_config({"VIDEO_QUERY_TIMEOUT_SEC": "31"})


def test_self_check_rejects_upload_smaller_than_reference():
    """上传上限 < 参考图上限：会出现「传上去了却用不了」的自相矛盾配置。"""
    with pytest.raises(ConfigError):
        load_config({"UPLOAD_MAX_MB": "5", "REFERENCE_MAX_MB": "10"})


def test_self_check_rejects_video_reference_over_five():
    with pytest.raises(ConfigError):
        load_config({"VIDEO_REFERENCE_MAX": "9"})


def test_self_check_rejects_batch_concurrency_out_of_range():
    with pytest.raises(ConfigError):
        load_config({"IMAGE_BATCH_CONCURRENCY": str(BATCH_MAX + 1)})


def test_self_check_rejects_zero_concurrency():
    with pytest.raises(ConfigError):
        load_config({"IMAGE_MAX_CONCURRENCY": "0"})


def test_agent_advice_below_systemd_stop():
    """常量链的硬约束：Agent 建议超时必须小于 systemd 停机超时。"""
    assert AGENT_TIMEOUT_ADVICE_SEC < SYSTEMD_TIMEOUT_STOP_SEC


def test_self_check_report_lines():
    cfg = load_config({})
    report = cfg.self_check_report()
    assert len(report) >= 4
    assert all(isinstance(line, str) and line for line in report)


# ---------------------------------------------------------------------------
# 热更新
# ---------------------------------------------------------------------------
def test_hot_fields_apply():
    cfg = load_config({})
    new = cfg.with_overrides({"image_max_concurrency": 8})
    assert new.image_max_concurrency == 8
    assert cfg.image_max_concurrency == 8  # 原对象不可变（默认 8）


def test_hot_update_rejects_readonly_field():
    cfg = load_config({})
    with pytest.raises(ConfigError):
        cfg.with_overrides({"port": 9000})


def test_hot_fields_snapshot_roundtrip():
    cfg = load_config({})
    snapshot = cfg.hot_fields()
    assert snapshot["image_batch_concurrency"] == cfg.image_batch_concurrency
    assert Config(**{**cfg.__dict__, **snapshot}).hot_fields() == snapshot


def test_hot_update_still_self_checked():
    """热更新后仍要过嵌套不等式（不能直接把坏值写进运行态）。"""
    cfg = load_config({})
    bad = cfg.with_overrides({"video_submit_retry_max_sec": 1.0})
    assert bad.video_submit_retry_max_sec < bad.video_submit_retry_min_sec
    with pytest.raises(ConfigError):
        bad.self_check_report()


def test_derived_byte_limits():
    cfg = load_config({"REFERENCE_MAX_MB": "2", "UPLOAD_MAX_MB": "4"})
    assert cfg.reference_max_bytes == 2 * 1024 * 1024
    assert cfg.upload_max_bytes == 4 * 1024 * 1024
