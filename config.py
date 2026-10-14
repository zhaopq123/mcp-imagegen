"""配置加载与启动自检。

环境变量总表
------------
服务端口 / 落盘目录::

    PORT                            8000
    IMAGE_DIR                       <root>/data/images
    VIDEO_DIR                       <root>/data/videos
    STORE_PATH                      <root>/data/store.json（Provider / 令牌 /
                                    管理员凭据 / 运行时覆写）
    MCPACCESS_TOKEN                 兼容旧部署：非空即作为「默认」访问令牌

运行时预算（嵌套不等式在启动时自检，违例 fail-fast）::

    TOTALBUDGETSEC                  200（单请求总预算）
    AGENT_TIMEOUT_ADVICE            常量 210（> 总预算，Agent 侧建议超时）
    PERSIST_TIMEOUT_IMAGE_SEC       30
    PERSIST_TIMEOUT_VIDEO_SEC       120
    CIRCUITHARDSEC / CIRCUITSOFTSEC 600 / 300

生图 / 视频::

    TIMEOUT_UPSTREAM_IMAGE_SEC      60（生图单次调用 read 档）
    VIDEO_SUBMIT_TIMEOUT_SEC        30
    VIDEO_QUERY_TIMEOUT_SEC         10
    VIDEO_POLL_INTERVAL_SEC         2
    IMAGE_MAX_CONCURRENCY / VIDEO_MAX_CONCURRENCY      8 / 4
    IMAGE_QUEUE_WAIT_SEC / VIDEO_QUEUE_WAIT_SEC        30 / 30
    IMAGE_QUEUE_MAX / VIDEO_QUEUE_MAX                  64 / 16
    IMAGE_BATCH_CONCURRENCY         4
    VIDEO_JOB_MAX_WAIT_SEC          1800（后台作业超出单请求预算的续命窗口）
    VIDEO_JOB_MAX / VIDEO_JOB_CONCURRENCY              24 / 8
    VIDEO_SUBMIT_QUEUE_WAIT_SEC     600（上游队列满时的服务端代排队窗口）
    VIDEO_SUBMIT_RETRY_MIN_SEC / _MAX_SEC              20 / 90

HTTP 客户端::

    HTTPXCONNECTSEC / HTTPXWRITESEC / HTTPXPOOLSEC     5 / 5 / 5
    HTTPXMAXCONNECTIONS / HTTPXMAXKEEPALIVECONNECTIONS 10 / 4
    HTTPXKEEPALIVESEC               25
    CLIENT_ROTATE_SEC               86400（热重建上游连接池；0 = 关闭）

参考图 / 上传::

    REFERENCE_MAX_MB                10（单张参考图上限）
    IMAGE_REFERENCE_MAX             4
    VIDEO_REFERENCE_MAX             5
    UPLOAD_MAX_MB                   15
    REF_CACHE_MAX_MB                32（参考图 → data URI 的解析缓存）

关于「零内建默认」
-----------------
本文件**不含**任何上游厂商的地址、模型名与密钥——那些属于 Provider，
由使用者在管理面板或 ``store.json`` 里定义。没有配置 Provider 时服务照常
启动，调用生图/视频工具会明确返回「没有可用服务提供方」而不是悄悄失败。
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Mapping

# ---------------------------------------------------------------------------
# 部署层常量（非环境变量；散文中定死、不宜暴露为变量的参数集中此处）
# ---------------------------------------------------------------------------
AGENT_TIMEOUT_ADVICE_SEC = 210.0   # Agent 客户端建议超时（≥ 总预算 + 错峰 10s）
SYSTEMD_TIMEOUT_STOP_SEC = 220.0   # systemd TimeoutStopSec（排空窗口）
HEARTBEAT_INTERVAL_SEC = 15.0      # watchdog 心跳间隔（< WatchdogSec 的一半）
WATCHDOG_SEC = 30.0                # systemd WatchdogSec
DAILY_REBUILD_HOUR = 4             # 每日连接池重建时刻（CLIENT_ROTATE_SEC>0 时改用间隔）
REQUEST_GUARD_SLACK_SEC = 1.0      # 整体 asyncio.timeout 护栏松弛量
DISK_WATERMARK_RATIO = 0.90        # 磁盘水位阈值（超线只 WARN + meta 提示）
BATCH_MAX = 4                      # batch 合法区间上限
ADMIN_SESSION_TTL_SEC = 12 * 3600.0  # 管理面板会话有效期


class ConfigError(Exception):
    """配置缺失 / 类型非法 / 嵌套不等式违例（启动 fail-fast）。"""


# ---------------------------------------------------------------------------
# .env 自动加载（零新依赖；已存在的环境变量一律不覆盖）
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_ROOT, ".env")


def _load_dotenv(path: str = _ENV_FILE) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip()
    except OSError:
        pass


_load_dotenv()


def _default_dir(name: str) -> str:
    return os.path.join(_ROOT, "data", name)


# 环境变量名 -> (类型, 默认值)
_ENV_SPEC: dict[str, tuple[type, object]] = {
    "PORT": (int, 8000),
    "IMAGE_DIR": (str, _default_dir("images")),
    "VIDEO_DIR": (str, _default_dir("videos")),
    "STORE_PATH": (str, os.path.join(_ROOT, "data", "store.json")),
    "ADMIN_PATH_PREFIX": (str, "/admin"),
    # MCPACCESS_TOKEN 为兼容旧部署的入口；新部署请在面板里管理令牌
    "MCPACCESS_TOKEN": (str, ""),
    "MAXCONCURRENCY": (int, 3),
    "QUEUEWAIT_SEC": (float, 30.0),
    "TOTALBUDGETSEC": (float, 200.0),
    "PERSIST_TIMEOUT_IMAGE_SEC": (float, 30.0),
    "PERSIST_TIMEOUT_VIDEO_SEC": (float, 120.0),
    "CIRCUITHARDSEC": (float, 600.0),
    "CIRCUITSOFTSEC": (float, 300.0),
    "HTTPXKEEPALIVESEC": (float, 25.0),
    "HTTPXCONNECTSEC": (float, 5.0),
    "HTTPXWRITESEC": (float, 5.0),
    "HTTPXPOOLSEC": (float, 5.0),
    "HTTPXMAXCONNECTIONS": (int, 10),
    "HTTPXMAXKEEPALIVE": (int, 4),
    "CLIENT_ROTATE_SEC": (float, 86400.0),
    # ---- 生图 / 视频 ----
    "TIMEOUT_UPSTREAM_IMAGE_SEC": (float, 60.0),
    "VIDEO_SUBMIT_TIMEOUT_SEC": (float, 30.0),
    "VIDEO_POLL_INTERVAL_SEC": (float, 2.0),
    "VIDEO_QUERY_TIMEOUT_SEC": (float, 10.0),
    "TASK_TTL_SEC": (float, 7200.0),
    "TASK_MAX": (int, 100),
    "IMAGE_MAX_CONCURRENCY": (int, 8),
    "VIDEO_MAX_CONCURRENCY": (int, 4),
    "IMAGE_QUEUE_WAIT_SEC": (float, 30.0),
    "VIDEO_QUEUE_WAIT_SEC": (float, 30.0),
    "IMAGE_QUEUE_MAX": (int, 64),
    "VIDEO_QUEUE_MAX": (int, 16),
    "IMAGE_BATCH_CONCURRENCY": (int, 4),
    "VIDEO_JOB_MAX_WAIT_SEC": (float, 1800.0),
    "VIDEO_JOB_MAX": (int, 24),
    "VIDEO_JOB_CONCURRENCY": (int, 8),
    "VIDEO_SUBMIT_QUEUE_WAIT_SEC": (float, 600.0),
    "VIDEO_SUBMIT_RETRY_MIN_SEC": (float, 20.0),
    "VIDEO_SUBMIT_RETRY_MAX_SEC": (float, 90.0),
    # ---- 参考图 / 上传 ----
    "REFERENCE_MAX_MB": (float, 10.0),
    "IMAGE_REFERENCE_MAX": (int, 4),
    "VIDEO_REFERENCE_MAX": (int, 5),
    "UPLOAD_MAX_MB": (float, 15.0),
    "REF_CACHE_MAX_MB": (float, 32.0),
}

# 可在运行时热更新的字段（管理面板改完立刻生效，无需重启）
_HOT_FIELDS: tuple[str, ...] = (
    "image_max_concurrency", "video_max_concurrency",
    "image_queue_wait_sec", "video_queue_wait_sec",
    "image_queue_max", "video_queue_max",
    "image_batch_concurrency", "video_job_max_wait_sec",
    "video_job_max", "video_job_concurrency",
    "video_submit_queue_wait_sec", "video_submit_retry_min_sec",
    "video_submit_retry_max_sec",
    "reference_max_mb", "image_reference_max", "video_reference_max",
    "upload_max_mb", "video_poll_interval_sec",
)


@dataclass(frozen=True)
class Config:
    """只读配置对象（极少数字段支持热更新，其余改动需重启）。"""

    port: int = 8000
    image_dir: str = ""
    video_dir: str = ""
    store_path: str = ""
    admin_path_prefix: str = "/admin"
    mcp_access_token: str = ""

    max_concurrency: int = 3
    queue_wait_sec: float = 30.0
    total_budget_sec: float = 200.0
    persist_timeout_image_sec: float = 30.0
    persist_timeout_video_sec: float = 120.0
    circuit_hard_sec: float = 600.0
    circuit_soft_sec: float = 300.0

    httpx_keepalive_sec: float = 25.0
    httpx_connect_sec: float = 5.0
    httpx_write_sec: float = 5.0
    httpx_pool_sec: float = 5.0
    httpx_max_connections: int = 10
    httpx_max_keepalive: int = 4
    client_rotate_sec: float = 86400.0

    timeout_upstream_image_sec: float = 60.0
    video_submit_timeout_sec: float = 30.0
    video_poll_interval_sec: float = 2.0
    video_query_timeout_sec: float = 10.0
    task_ttl_sec: float = 7200.0
    task_max: int = 100

    image_max_concurrency: int = 8
    video_max_concurrency: int = 4
    image_queue_wait_sec: float = 30.0
    video_queue_wait_sec: float = 30.0
    image_queue_max: int = 64
    video_queue_max: int = 16
    image_batch_concurrency: int = 4
    video_job_max_wait_sec: float = 1800.0
    video_job_max: int = 24
    video_job_concurrency: int = 8
    video_submit_queue_wait_sec: float = 600.0
    video_submit_retry_min_sec: float = 20.0
    video_submit_retry_max_sec: float = 90.0

    reference_max_mb: float = 10.0
    image_reference_max: int = 4
    video_reference_max: int = 5
    upload_max_mb: float = 15.0
    ref_cache_max_mb: float = 32.0

    # ------------------------------------------------------------------
    # 派生
    # ------------------------------------------------------------------
    @property
    def reference_max_bytes(self) -> int:
        return int(self.reference_max_mb * 1024 * 1024)

    @property
    def upload_max_bytes(self) -> int:
        return int(self.upload_max_mb * 1024 * 1024)

    @property
    def ref_cache_max_bytes(self) -> int:
        return int(self.ref_cache_max_mb * 1024 * 1024)

    # ------------------------------------------------------------------
    # 热更新
    # ------------------------------------------------------------------
    def with_overrides(self, values: Mapping[str, object]) -> "Config":
        """返回应用了热更新字段的**新** Config（原对象不可变）。

        只接受 _HOT_FIELDS 内的键；非法值抛 ConfigError（不静默丢弃——
        面板上改了却没生效比报错更糟）。
        """
        unknown = [k for k in (values or {}) if k not in _HOT_FIELDS]
        if unknown:
            raise ConfigError(f"以下字段不支持热更新：{', '.join(sorted(unknown))}")
        return Config(**{**self.__dict__, **dict(values)})

    def hot_fields(self) -> "dict[str, object]":
        return {k: getattr(self, k) for k in _HOT_FIELDS}

    # ------------------------------------------------------------------
    # 启动自检：嵌套不等式
    # ------------------------------------------------------------------
    def self_check_report(self) -> list[str]:
        problems: list[str] = []

        def require(cond: bool, message: str) -> None:
            if not cond:
                problems.append(message)

        tier_max = max(self.timeout_upstream_image_sec,
                       self.video_submit_timeout_sec,
                       self.video_query_timeout_sec,
                       self.persist_timeout_image_sec,
                       self.persist_timeout_video_sec)
        if tier_max > self.total_budget_sec:
            require(
                False,
                f"调用档最大值 {tier_max}s > 总预算 {self.total_budget_sec}s"
                "（破坏 read ≤ 分档 ≤ 总预算）")
        require(self.total_budget_sec < AGENT_TIMEOUT_ADVICE_SEC,
                f"TOTALBUDGETSEC={self.total_budget_sec}s 必须 < "
                f"Agent 建议超时 {AGENT_TIMEOUT_ADVICE_SEC}s")
        require(AGENT_TIMEOUT_ADVICE_SEC < SYSTEMD_TIMEOUT_STOP_SEC,
                "Agent 建议超时必须 < systemd TimeoutStopSec")
        require(self.queue_wait_sec <= self.total_budget_sec,
                f"QUEUEWAIT_SEC={self.queue_wait_sec}s 超出总预算")
        require(self.max_concurrency >= 1, "MAXCONCURRENCY 必须 ≥ 1")
        require(self.video_query_timeout_sec <= self.video_submit_timeout_sec,
                f"VIDEO_QUERY_TIMEOUT_SEC={self.video_query_timeout_sec}s 必须 ≤ "
                f"VIDEO_SUBMIT_TIMEOUT_SEC={self.video_submit_timeout_sec}s")
        for name, value in (
            ("VIDEO_POLL_INTERVAL_SEC", self.video_poll_interval_sec),
            ("TASK_TTL_SEC", self.task_ttl_sec),
        ):
            require(value > 0, f"{name} 必须 > 0")
        require(self.task_max >= 1, "TASK_MAX 必须 ≥ 1")

        for name, value in (
            ("IMAGE_MAX_CONCURRENCY", self.image_max_concurrency),
            ("VIDEO_MAX_CONCURRENCY", self.video_max_concurrency),
            ("VIDEO_JOB_CONCURRENCY", self.video_job_concurrency),
            ("VIDEO_JOB_MAX", self.video_job_max),
        ):
            require(value >= 1, f"{name} 必须 ≥ 1")
        for name, value in (
            ("IMAGE_QUEUE_WAIT_SEC", self.image_queue_wait_sec),
            ("VIDEO_QUEUE_WAIT_SEC", self.video_queue_wait_sec),
            ("VIDEO_JOB_MAX_WAIT_SEC", self.video_job_max_wait_sec),
        ):
            require(value > 0, f"{name} 必须 > 0")
        require(
            max(self.image_queue_wait_sec, self.video_queue_wait_sec)
            <= self.total_budget_sec,
            "泳道排队等待上限不得超过总预算（排队计入预算）")
        require(self.image_queue_max >= 1 and self.video_queue_max >= 1,
                "IMAGE_QUEUE_MAX / VIDEO_QUEUE_MAX 必须 ≥ 1")
        require(1 <= self.image_batch_concurrency <= BATCH_MAX,
                f"IMAGE_BATCH_CONCURRENCY 必须落在 1–{BATCH_MAX}")
        require(self.video_submit_queue_wait_sec >= 0,
                "VIDEO_SUBMIT_QUEUE_WAIT_SEC 必须 ≥ 0（0 = 关闭代排队）")
        require(self.video_submit_retry_min_sec > 0,
                "VIDEO_SUBMIT_RETRY_MIN_SEC 必须 > 0")
        require(self.video_submit_retry_max_sec >= self.video_submit_retry_min_sec,
                "VIDEO_SUBMIT_RETRY_MAX_SEC 必须 ≥ VIDEO_SUBMIT_RETRY_MIN_SEC")

        require(self.reference_max_mb > 0, "REFERENCE_MAX_MB 必须 > 0")
        require(self.upload_max_mb >= self.reference_max_mb,
                f"UPLOAD_MAX_MB={self.upload_max_mb} 必须 ≥ "
                f"REFERENCE_MAX_MB={self.reference_max_mb}"
                "（否则上传成功的图无法用作参考图）")
        require(self.image_reference_max >= 1, "IMAGE_REFERENCE_MAX 必须 ≥ 1")
        require(1 <= self.video_reference_max <= 5,
                f"VIDEO_REFERENCE_MAX={self.video_reference_max} 必须落在 1–5")

        require(bool(self.store_path), "STORE_PATH 不能为空")
        require(bool(self.image_dir) and bool(self.video_dir),
                "IMAGE_DIR / VIDEO_DIR 不能为空")

        if problems:
            raise ConfigError("嵌套不等式自检失败：\n  - " + "\n  - ".join(problems))

        return [
            "[config] 预算自检: "
            f"调用档 max={tier_max:g}s ≤ 总预算 {self.total_budget_sec:g}s "
            f"< Agent建议 {AGENT_TIMEOUT_ADVICE_SEC:g}s "
            f"< TimeoutStop {SYSTEMD_TIMEOUT_STOP_SEC:g}s ✓",
            "[config] 泳道自检: "
            f"生图 并发{self.image_max_concurrency}/等待{self.image_queue_wait_sec:g}s/"
            f"队深{self.image_queue_max} | "
            f"视频 并发{self.video_max_concurrency}/等待{self.video_queue_wait_sec:g}s/"
            f"队深{self.video_queue_max} | batch 并发 {self.image_batch_concurrency} ✓",
            "[config] 视频后台自检: "
            f"作业上限 {self.video_job_max} / 轮询并发 {self.video_job_concurrency} / "
            f"跟踪窗口 {self.video_job_max_wait_sec:g}s | 提交代排队 "
            f"{self.video_submit_queue_wait_sec:g}s ✓",
            "[config] 参考图自检: "
            f"单张 ≤{self.reference_max_mb:g}MB | 生图 ≤{self.image_reference_max} 张 | "
            f"视频 ≤{self.video_reference_max} 张 | 上传 ≤{self.upload_max_mb:g}MB ✓",
            "[config] 配置仓库: "
            f"{self.store_path}（Provider / 令牌 / 管理员凭据均由你自己配置）",
        ]


# 环境变量名 → 字段名
_FIELD_NAMES: dict[str, str] = {
    "PORT": "port",
    "IMAGE_DIR": "image_dir",
    "VIDEO_DIR": "video_dir",
    "STORE_PATH": "store_path",
    "ADMIN_PATH_PREFIX": "admin_path_prefix",
    "MCPACCESS_TOKEN": "mcp_access_token",
    "MAXCONCURRENCY": "max_concurrency",
    "QUEUEWAIT_SEC": "queue_wait_sec",
    "TOTALBUDGETSEC": "total_budget_sec",
    "PERSIST_TIMEOUT_IMAGE_SEC": "persist_timeout_image_sec",
    "PERSIST_TIMEOUT_VIDEO_SEC": "persist_timeout_video_sec",
    "CIRCUITHARDSEC": "circuit_hard_sec",
    "CIRCUITSOFTSEC": "circuit_soft_sec",
    "HTTPXKEEPALIVESEC": "httpx_keepalive_sec",
    "HTTPXCONNECTSEC": "httpx_connect_sec",
    "HTTPXWRITESEC": "httpx_write_sec",
    "HTTPXPOOLSEC": "httpx_pool_sec",
    "HTTPXMAXCONNECTIONS": "httpx_max_connections",
    "HTTPXMAXKEEPALIVE": "httpx_max_keepalive",
    "CLIENT_ROTATE_SEC": "client_rotate_sec",
    "TIMEOUT_UPSTREAM_IMAGE_SEC": "timeout_upstream_image_sec",
    "VIDEO_SUBMIT_TIMEOUT_SEC": "video_submit_timeout_sec",
    "VIDEO_POLL_INTERVAL_SEC": "video_poll_interval_sec",
    "VIDEO_QUERY_TIMEOUT_SEC": "video_query_timeout_sec",
    "TASK_TTL_SEC": "task_ttl_sec",
    "TASK_MAX": "task_max",
    "IMAGE_MAX_CONCURRENCY": "image_max_concurrency",
    "VIDEO_MAX_CONCURRENCY": "video_max_concurrency",
    "IMAGE_QUEUE_WAIT_SEC": "image_queue_wait_sec",
    "VIDEO_QUEUE_WAIT_SEC": "video_queue_wait_sec",
    "IMAGE_QUEUE_MAX": "image_queue_max",
    "VIDEO_QUEUE_MAX": "video_queue_max",
    "IMAGE_BATCH_CONCURRENCY": "image_batch_concurrency",
    "VIDEO_JOB_MAX_WAIT_SEC": "video_job_max_wait_sec",
    "VIDEO_JOB_MAX": "video_job_max",
    "VIDEO_JOB_CONCURRENCY": "video_job_concurrency",
    "VIDEO_SUBMIT_QUEUE_WAIT_SEC": "video_submit_queue_wait_sec",
    "VIDEO_SUBMIT_RETRY_MIN_SEC": "video_submit_retry_min_sec",
    "VIDEO_SUBMIT_RETRY_MAX_SEC": "video_submit_retry_max_sec",
    "REFERENCE_MAX_MB": "reference_max_mb",
    "IMAGE_REFERENCE_MAX": "image_reference_max",
    "VIDEO_REFERENCE_MAX": "video_reference_max",
    "UPLOAD_MAX_MB": "upload_max_mb",
    "REF_CACHE_MAX_MB": "ref_cache_max_mb",
}


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """从环境变量加载配置（类型非法 → ConfigError）。

    注意：**没有必填项**。上游 API 的地址与密钥不在环境变量里，而在
    store.json（或管理面板）——这是刻意的：环境变量适合部署参数，
    不适合承载「用户还想临时改来改去」的业务配置。
    """
    env = os.environ if env is None else env
    values: dict[str, object] = {}
    for name, (typ, default) in _ENV_SPEC.items():
        raw = env.get(name)
        if raw is None or raw == "":
            values[_FIELD_NAMES[name]] = default
            continue
        try:
            values[_FIELD_NAMES[name]] = _cast(typ, raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"环境变量 {name}={raw!r} 无法解析为 {typ.__name__}") from exc
    cfg = Config(**values)  # type: ignore[arg-type]
    cfg.self_check_report()
    return cfg


def _cast(typ: type, raw: str):
    if typ is bool:
        return ast.literal_eval(raw) if raw in ("True", "False") else bool(raw)
    return typ(raw)
