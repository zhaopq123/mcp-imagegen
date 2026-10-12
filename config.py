"""MCP 图片生成 Server · 配置加载与启动自检。

V2 变量总表（方案 V2 三节；硅基五变量已随 Provider 删除）：
    MCPACCESS_TOKEN                     MCP 层密钥（必填）
    PORT                                8000
    IMAGE_DIR                           /var/lib/mcp-imagegen/images
    MAXCONCURRENCY / QUEUEWAIT_SEC      3 / 30
    TOTALBUDGETSEC                      200
    PERSIST_TIMEOUT_IMAGE_SEC           30（V2 更名自 PERSISTTIMEOUTSEC，纯改名）
    CIRCUITHARDSEC / CIRCUITSOFTSEC     600 / 300
    HTTPXKEEPALIVESEC                   25

补充（规格书五节 httpx 四段超时与连接池上限，"所有超时为配置项不硬编码"）：
    HTTPXCONNECTSEC / HTTPXWRITESEC / HTTPXPOOLSEC   5 / 5 / 5
    HTTPXMAXCONNECTIONS / HTTPXMAXKEEPALIVECONNECTIONS   10 / 4
    （方案 V2 之 HTTPX_CONNECT_TIMEOUT_SEC(5) 即此三段：V1 已配置化，
     不重复增设别名变量，避免同一超时双头控制。）

V2 增量（增量设计方案 V2 三节；T1）：
    AGNES_TOKEN                          Agnes 上游密钥（必填）
    AGNES_BASE_URL                        https://api.agnes-ai.cn（V2 第六轮
                                         修正：官方文档端点，原 apihub 子域已废弃）
    VIDEO_DIR                             /var/lib/mcp-imagegen/videos
    TIMEOUT_AGNES_IMAGE_SEC               60（生图 read 档；D5 调整自 30）
    VIDEO_SUBMIT_TIMEOUT_SEC              30（视频提交 read 档）
    VIDEO_POLL_INTERVAL_SEC               2（轮询间隔；D7 官方建议 1–2s，原 5）
    VIDEO_QUERY_TIMEOUT_SEC               10（视频单查 read 档）
    PERSIST_TIMEOUT_VIDEO_SEC            120（视频下载 read 档）
    TASK_TTL_SEC                          7200（任务表 TTL）
    TASK_MAX                              100（任务表容量）

启动自检（嵌套不等式，红线 B6，违例 fail-fast）：
    read ≤ 分档 ≤ deadline 200 < Agent 建议 210 < TimeoutStop 220
    V2 新增嵌套：视频单查(10) ≤ 提交(30) ≤ deadline；各 read 档 ≤ deadline；
    视频 persist(120) ≤ deadline；轮询间隔 / TTL / 容量必须为正。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

# ---------------------------------------------------------------------------
# 部署层常量（非第七节环境变量；S4 交付物 systemd 单元 / Agent 配置与之对齐）
#
# 红线 B（数值无硬编码）：本项目全部"魔法数值"集中在 config.py——
# 第七节环境变量走 _ENV_SPEC；规格书散文中定死、不宜暴露为环境变量的
# 运行参数集中在本节命名常量（单一出处、可审计）。
# ---------------------------------------------------------------------------
AGENT_TIMEOUT_ADVICE_SEC = 210.0  # Agent 客户端建议超时（规格书二：≥210s，错峰缓冲 10s）
SYSTEMD_TIMEOUT_STOP_SEC = 220.0  # systemd TimeoutStopSec（规格书五、6.2：排空窗口）
HEARTBEAT_INTERVAL_SEC = 15.0     # watchdog 心跳间隔（规格书 6.1：15s < WatchdogSec 30s 的一半）
WATCHDOG_SEC = 30.0               # systemd WatchdogSec（6.1；须 > 2×心跳）
DAILY_REBUILD_HOUR = 4             # 每日客户端重建调度时刻（6.2：每日 04:00 本地时间）
REQUEST_GUARD_SLACK_SEC = 1.0     # 整体 asyncio.timeout 护栏 = TOTALBUDGETSEC + 本松弛量（6.1 自救兜底）
DISK_WATERMARK_RATIO = 0.90       # 磁盘水位阈值（6.2 超线只 WARN + meta 提示；规格书未定数值，工程默认 90%）


class ConfigError(Exception):
    """配置缺失 / 类型非法 / 嵌套不等式违例（启动 fail-fast）。"""


# ---------------------------------------------------------------------------
# .env 自动加载（用户 2026-10-10 指示"Key 做成环境变量只换一次"）
# 纯标准库实现，零新依赖；规则：
#   - 仅当项目根存在 .env 时加载（不存在则零副作用）；
#   - 已存在的环境变量一律不覆盖（进程级 export / systemd 优先级更高）；
#   - 只解析 KEY=VALUE 行，忽略空行与 # 注释；值两侧空白剥离，不去引号
#     （.env 由本项目维护，约定不使用引号包裹）。
# ---------------------------------------------------------------------------
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


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
        pass  # 文件不存在 / 不可读：静默跳过，按纯环境变量模式运行


_load_dotenv()


# 第七节变量总表：环境变量名 -> (类型, 默认值)。名称与默认值原样，不得改动。
_ENV_SPEC: dict[str, tuple[type, object]] = {
    "PORT": (int, 8000),
    "IMAGE_DIR": (str, "/var/lib/mcp-imagegen/images"),
    "MAXCONCURRENCY": (int, 3),
    "QUEUEWAIT_SEC": (float, 30.0),
    "TOTALBUDGETSEC": (float, 200.0),
    "PERSIST_TIMEOUT_IMAGE_SEC": (float, 30.0),  # V2 更名自 PERSISTTIMEOUTSEC（纯改名）
    "CIRCUITHARDSEC": (float, 600.0),
    "CIRCUITSOFTSEC": (float, 300.0),
    "HTTPXKEEPALIVESEC": (float, 25.0),
    # 规格书五节 httpx 数值（connect/write/pool 5s；Limits 10/4），按
    # "所有超时为配置项不硬编码"（五节校准闭环）配置化，属第七节之外的派生项。
    # V2 方案的 HTTPX_CONNECT_TIMEOUT_SEC(5) 即此三段，不重复增设别名变量。
    "HTTPXCONNECTSEC": (float, 5.0),
    "HTTPXWRITESEC": (float, 5.0),
    "HTTPXPOOLSEC": (float, 5.0),
    "HTTPXMAXCONNECTIONS": (int, 10),
    "HTTPXMAXKEEPALIVECONNECTIONS": (int, 4),
    # ---- V2 增量（增量设计方案 V2 三节；T1）----
    "AGNES_BASE_URL": (str, "https://api.agnes-ai.cn"),
    # 上游模型 ID（可自定，不写死）：上游新增/更名模型时改这两项即可，
    # 无需改代码。留空则回落到内置默认（见 infra/agnes.py DEFAULT_MODEL_IDS）。
    "AGNES_IMAGE_MODEL": (str, ""),
    "AGNES_VIDEO_MODEL": (str, ""),
    "VIDEO_DIR": (str, "/var/lib/mcp-imagegen/videos"),
    "TIMEOUT_AGNES_IMAGE_SEC": (float, 60.0),  # D5：30→60（4K 档实测 9s+，留裕量）
    "VIDEO_SUBMIT_TIMEOUT_SEC": (float, 30.0),
    "VIDEO_POLL_INTERVAL_SEC": (float, 2.0),   # D7：5→2（官方建议 1–2s 轮询）
    "VIDEO_QUERY_TIMEOUT_SEC": (float, 10.0),
    "PERSIST_TIMEOUT_VIDEO_SEC": (float, 120.0),
    "TASK_TTL_SEC": (float, 7200.0),
    "TASK_MAX": (int, 100),
}

# 必填密钥（缺失/为空 → 启动 fail-fast）
_REQUIRED_TOKENS: tuple[str, ...] = ("MCPACCESS_TOKEN", "AGNES_TOKEN")


@dataclass(frozen=True)
class Config:
    """只读配置对象。改动配置需 restart 生效（不做热更新）。"""

    mcp_access_token: str
    port: int
    image_dir: str
    max_concurrency: int
    queue_wait_sec: float
    total_budget_sec: float
    persist_timeout_image_sec: float  # V2 更名自 persist_timeout_sec（纯改名）
    circuit_hard_sec: float
    circuit_soft_sec: float
    httpx_keepalive_sec: float
    httpx_connect_sec: float
    httpx_write_sec: float
    httpx_pool_sec: float
    httpx_max_connections: int
    httpx_max_keepalive_connections: int
    # ---- V2（T1 增量；硅基五变量已随 Provider 删除）----
    agnes_base_url: str
    agnes_image_model: str  # 空串 = 用内置默认
    agnes_video_model: str  # 空串 = 用内置默认
    agnes_token: str
    video_dir: str
    timeout_agnes_image_sec: float
    video_submit_timeout_sec: float
    video_poll_interval_sec: float
    video_query_timeout_sec: float
    persist_timeout_video_sec: float
    task_ttl_sec: float
    task_max: int

    # ------------------------------------------------------------------
    # 启动自检：嵌套不等式（红线 B6）
    # ------------------------------------------------------------------
    def self_check_report(self) -> list[str]:
        """返回自检报告行；任何违例直接抛 ConfigError（fail-fast）。"""
        tier_max = self.timeout_agnes_image_sec  # 唯一生图 read 档（V2）
        problems: list[str] = []

        if tier_max > self.total_budget_sec:
            problems.append(
                f"分档上限最大值 {tier_max}s > 总预算 TOTALBUDGETSEC={self.total_budget_sec}s"
                "（破坏 read ≤ 分档 ≤ 200）"
            )
        if self.total_budget_sec >= AGENT_TIMEOUT_ADVICE_SEC:
            problems.append(
                f"TOTALBUDGETSEC={self.total_budget_sec}s 必须 < Agent 建议超时 "
                f"{AGENT_TIMEOUT_ADVICE_SEC}s"
            )
        if AGENT_TIMEOUT_ADVICE_SEC >= SYSTEMD_TIMEOUT_STOP_SEC:
            problems.append(
                f"Agent 建议超时 {AGENT_TIMEOUT_ADVICE_SEC}s 必须 < TimeoutStopSec "
                f"{SYSTEMD_TIMEOUT_STOP_SEC}s"
            )
        if self.queue_wait_sec > self.total_budget_sec:
            problems.append(
                f"QUEUEWAIT_SEC={self.queue_wait_sec}s > TOTALBUDGETSEC={self.total_budget_sec}s"
                "（排队等待计入预算，等待上限不得超过总预算）"
            )
        if self.persist_timeout_image_sec > self.total_budget_sec:
            problems.append(
                f"PERSIST_TIMEOUT_IMAGE_SEC={self.persist_timeout_image_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（下载计入预算）"
            )
        if self.max_concurrency < 1:
            problems.append(f"MAXCONCURRENCY={self.max_concurrency} 必须 ≥ 1")

        # ---- V2 增量嵌套不等式（T1）----
        # 各调用档 read ≤ deadline：
        if self.timeout_agnes_image_sec > self.total_budget_sec:
            problems.append(
                f"TIMEOUT_AGNES_IMAGE_SEC={self.timeout_agnes_image_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（生图 read 档 ≤ deadline）"
            )
        if self.video_submit_timeout_sec > self.total_budget_sec:
            problems.append(
                f"VIDEO_SUBMIT_TIMEOUT_SEC={self.video_submit_timeout_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（提交 read 档 ≤ deadline）"
            )
        # 提交 30 / 单查 10 嵌套校验：单查 read ≤ 提交 read（由传递性亦有 单查 ≤ deadline）。
        if self.video_query_timeout_sec > self.video_submit_timeout_sec:
            problems.append(
                f"VIDEO_QUERY_TIMEOUT_SEC={self.video_query_timeout_sec}s > "
                f"VIDEO_SUBMIT_TIMEOUT_SEC={self.video_submit_timeout_sec}s"
                "（单查 read ≤ 提交 read，破坏 10 ≤ 30 嵌套）"
            )
        if self.video_query_timeout_sec > self.total_budget_sec:
            problems.append(
                f"VIDEO_QUERY_TIMEOUT_SEC={self.video_query_timeout_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（单查 read 档 ≤ deadline）"
            )
        if self.video_poll_interval_sec <= 0:
            problems.append(
                f"VIDEO_POLL_INTERVAL_SEC={self.video_poll_interval_sec}s 必须 > 0"
            )
        if self.persist_timeout_video_sec > self.total_budget_sec:
            problems.append(
                f"PERSIST_TIMEOUT_VIDEO_SEC={self.persist_timeout_video_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（视频下载计入预算）"
            )
        if self.task_ttl_sec <= 0:
            problems.append(f"TASK_TTL_SEC={self.task_ttl_sec}s 必须 > 0")
        if self.task_max < 1:
            problems.append(f"TASK_MAX={self.task_max} 必须 ≥ 1")

        if problems:
            raise ConfigError(
                "嵌套不等式自检失败（read ≤ 分档 ≤ 200 < 210 建议 < 220）：\n  - "
                + "\n  - ".join(problems)
            )

        return [
            "[config] 嵌套不等式自检: "
            f"read ≤ 分档(max={tier_max:g}s) ≤ deadline({self.total_budget_sec:g}s) "
            f"< Agent建议({AGENT_TIMEOUT_ADVICE_SEC:g}s) < TimeoutStop({SYSTEMD_TIMEOUT_STOP_SEC:g}s) ✓",
            f"[config] 队列等待 {self.queue_wait_sec:g}s ≤ 总预算 ✓ | "
            f"persist 下载 图{self.persist_timeout_image_sec:g}s/视频{self.persist_timeout_video_sec:g}s ≤ 总预算 ✓ | "
            f"并发槽 {self.max_concurrency} | 熔断 硬{self.circuit_hard_sec:g}s/软{self.circuit_soft_sec:g}s",
            "[config] V2 Agnes read 档自检: "
            f"生图 {self.timeout_agnes_image_sec:g}s / 提交 {self.video_submit_timeout_sec:g}s / "
            f"单查 {self.video_query_timeout_sec:g}s ≤ 单查 ≤ 提交 ≤ deadline "
            f"({self.total_budget_sec:g}s) ✓ | 轮询 {self.video_poll_interval_sec:g}s | "
            f"任务表 TTL {self.task_ttl_sec:g}s / 容量 {self.task_max} ✓",
        ]


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """从环境变量加载配置。

    - 必填密钥缺失/为空 → ConfigError（fail-fast，一次性列出全部缺失项）
    - 数值类型非法 → ConfigError
    - 嵌套不等式违例 → ConfigError
    """
    env = os.environ if env is None else env

    missing = [name for name in _REQUIRED_TOKENS if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"必填环境变量缺失: {', '.join(missing)}（密钥项不可为空）")

    values: dict[str, object] = {
        "mcp_access_token": env["MCPACCESS_TOKEN"].strip(),
        "agnes_token": env["AGNES_TOKEN"].strip(),
    }

    for name, (typ, default) in _ENV_SPEC.items():
        raw = env.get(name)
        if raw is None or raw == "":
            value: object = default
        else:
            try:
                value = typ(raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"环境变量 {name}={raw!r} 无法解析为 {typ.__name__}") from exc
        values[_field_name(name)] = value

    cfg = Config(**values)  # type: ignore[arg-type]
    cfg.self_check_report()  # 启动即自检，违例 fail-fast
    return cfg


def _field_name(env_name: str) -> str:
    """环境变量名 → Config 字段名（仅用于第七节总表与派生项的确定性映射）。"""
    special = {
        "MCPACCESS_TOKEN": "mcp_access_token",
        "PORT": "port",
        "IMAGE_DIR": "image_dir",
        "MAXCONCURRENCY": "max_concurrency",
        "QUEUEWAIT_SEC": "queue_wait_sec",
        "TOTALBUDGETSEC": "total_budget_sec",
        "PERSIST_TIMEOUT_IMAGE_SEC": "persist_timeout_image_sec",
        "CIRCUITHARDSEC": "circuit_hard_sec",
        "CIRCUITSOFTSEC": "circuit_soft_sec",
        "HTTPXKEEPALIVESEC": "httpx_keepalive_sec",
        "HTTPXCONNECTSEC": "httpx_connect_sec",
        "HTTPXWRITESEC": "httpx_write_sec",
        "HTTPXPOOLSEC": "httpx_pool_sec",
        "HTTPXMAXCONNECTIONS": "httpx_max_connections",
        "HTTPXMAXKEEPALIVECONNECTIONS": "httpx_max_keepalive_connections",
        # ---- V2 增量（T1）----
        "AGNES_BASE_URL": "agnes_base_url",
        "AGNES_IMAGE_MODEL": "agnes_image_model",
        "AGNES_VIDEO_MODEL": "agnes_video_model",
        "VIDEO_DIR": "video_dir",
        "TIMEOUT_AGNES_IMAGE_SEC": "timeout_agnes_image_sec",
        "VIDEO_SUBMIT_TIMEOUT_SEC": "video_submit_timeout_sec",
        "VIDEO_POLL_INTERVAL_SEC": "video_poll_interval_sec",
        "VIDEO_QUERY_TIMEOUT_SEC": "video_query_timeout_sec",
        "PERSIST_TIMEOUT_VIDEO_SEC": "persist_timeout_video_sec",
        "TASK_TTL_SEC": "task_ttl_sec",
        "TASK_MAX": "task_max",
    }
    return special[env_name]
