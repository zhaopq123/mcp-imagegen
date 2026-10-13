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
# V3：batch 合法区间上限（与 app/pipeline.BATCH_MAX 同源；config 不反向依赖 app，
# 故在此单一出处定义，pipeline 从 config 导入以杜绝双头常量）
BATCH_MAX = 4


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
    # ---- V3 增量：多队列 / 并发 / 视频后台异步（IMAGE=生图泳道，
    #      VIDEO=视频泳道；两泳道独立额度，互不饿死）----
    "IMAGE_MAX_CONCURRENCY": (int, 3),      # 生图泳道并发槽
    "VIDEO_MAX_CONCURRENCY": (int, 3),      # 视频泳道并发槽（提交+单查 IO 期间占用）
    "IMAGE_QUEUE_WAIT_SEC": (float, 30.0),  # 生图排队等待上限（计入 deadline）
    "VIDEO_QUEUE_WAIT_SEC": (float, 30.0),  # 视频排队等待上限（计入 deadline）
    "IMAGE_QUEUE_MAX": (int, 64),           # 生图泳道排队深度上限（超限 → busy）
    "VIDEO_QUEUE_MAX": (int, 16),           # 视频泳道排队深度上限
    "IMAGE_BATCH_CONCURRENCY": (int, 4),    # 单次请求内 batch 拆分的并发度（1–4）
    # 视频后台作业（脱离请求 deadline，由后台 worker 轮询到终态）
    "VIDEO_JOB_MAX_WAIT_SEC": (float, 1800.0),  # 后台轮询总上限（> deadline，跨请求续命）
    "VIDEO_JOB_MAX": (int, 24),                 # 同时跟踪的后台作业数上限
    "VIDEO_JOB_CONCURRENCY": (int, 8),          # 后台同时轮询的作业数上限
    # ---- V3：上游速率限制（压测实证：放开并发会撞 429 → 硬熔断 10min，
    #      不如自己排队慢慢发）----
    "UPSTREAM_RPS": (float, 1.0),               # 上游请求匀速上限（令牌/秒）
    "UPSTREAM_BURST": (int, 2),                 # 允许的短突发（桶容量）
    "UPSTREAM_COOLDOWN_SEC": (float, 15.0),     # 收到 429/503 后的冻结时长
    # ---- V4：参考图（图生图 / 图生视频）----
    # 参考图统一转 data URI 内联发给上游（实测可用）。上限取官方参考媒体
    # 限制的保守侧：图片单张 <15MB；本服务再收紧到 10MB，避免 base64
    # 膨胀 33% 后撞上游 50MB 总限，也避免手机内存被单请求吃干。
    "REFERENCE_MAX_MB": (float, 10.0),          # 单张参考图大小上限（MB）
    "IMAGE_REFERENCE_MAX": (int, 4),            # 生图参考图数量上限
    "VIDEO_REFERENCE_MAX": (int, 5),            # 视频参考图上限（Flash 官方=5）
    "UPLOAD_MAX_MB": (float, 15.0),             # /upload 单文件上限（官方单张 15MB）
    # ---- V4：视频提交排队（上游队列满=常态，免费额度 RPM=1）----
    # 撞 503 video_queue_full 不再立刻失败，改为服务端代为排队重试：
    # 把「用户/Agent 反复手动重试」变成「提交一次，好了来拿」。
    "VIDEO_SUBMIT_QUEUE_WAIT_SEC": (float, 600.0),  # 代排队总时长上限
    "VIDEO_SUBMIT_RETRY_MIN_SEC": (float, 20.0),    # 排队重试最小间隔
    "VIDEO_SUBMIT_RETRY_MAX_SEC": (float, 90.0),    # 排队重试最大间隔（指数退避封顶）
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
    # ---- V3（多队列 / 并发 / 视频后台异步）----
    image_max_concurrency: int
    video_max_concurrency: int
    image_queue_wait_sec: float
    video_queue_wait_sec: float
    image_queue_max: int
    video_queue_max: int
    image_batch_concurrency: int
    video_job_max_wait_sec: float
    video_job_max: int
    video_job_concurrency: int
    # ---- V3：上游速率限制 ----
    upstream_rps: float
    upstream_burst: int
    upstream_cooldown_sec: float
    # ---- V4：参考图（图生图 / 图生视频）----
    reference_max_mb: float
    image_reference_max: int
    video_reference_max: int
    upload_max_mb: float
    # ---- V4：视频提交排队 ----
    video_submit_queue_wait_sec: float
    video_submit_retry_min_sec: float
    video_submit_retry_max_sec: float

    # ------------------------------------------------------------------
    # V4 派生：参考图上限（字节）
    # ------------------------------------------------------------------
    @property
    def reference_max_bytes(self) -> int:
        return int(self.reference_max_mb * 1024 * 1024)

    @property
    def upload_max_bytes(self) -> int:
        return int(self.upload_max_mb * 1024 * 1024)

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

        # ---- V3 增量自检（多队列 / 并发 / 后台作业）----
        if self.image_max_concurrency < 1:
            problems.append(
                f"IMAGE_MAX_CONCURRENCY={self.image_max_concurrency} 必须 ≥ 1")
        if self.video_max_concurrency < 1:
            problems.append(
                f"VIDEO_MAX_CONCURRENCY={self.video_max_concurrency} 必须 ≥ 1")
        if self.image_queue_wait_sec <= 0:
            problems.append(
                f"IMAGE_QUEUE_WAIT_SEC={self.image_queue_wait_sec}s 必须 > 0")
        if self.video_queue_wait_sec <= 0:
            problems.append(
                f"VIDEO_QUEUE_WAIT_SEC={self.video_queue_wait_sec}s 必须 > 0")
        if self.image_queue_wait_sec > self.total_budget_sec:
            problems.append(
                f"IMAGE_QUEUE_WAIT_SEC={self.image_queue_wait_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（排队计入预算）")
        if self.video_queue_wait_sec > self.total_budget_sec:
            problems.append(
                f"VIDEO_QUEUE_WAIT_SEC={self.video_queue_wait_sec}s > "
                f"TOTALBUDGETSEC={self.total_budget_sec}s（排队计入预算）")
        if self.image_queue_max < 1:
            problems.append(f"IMAGE_QUEUE_MAX={self.image_queue_max} 必须 ≥ 1")
        if self.video_queue_max < 1:
            problems.append(f"VIDEO_QUEUE_MAX={self.video_queue_max} 必须 ≥ 1")
        if not 1 <= self.image_batch_concurrency <= BATCH_MAX:
            problems.append(
                f"IMAGE_BATCH_CONCURRENCY={self.image_batch_concurrency} 必须落在 "
                f"1–{BATCH_MAX}（与 batch_size 合法区间一致）")
        if self.video_job_max_wait_sec <= 0:
            problems.append(
                f"VIDEO_JOB_MAX_WAIT_SEC={self.video_job_max_wait_sec}s 必须 > 0")
        if self.video_job_max < 1:
            problems.append(f"VIDEO_JOB_MAX={self.video_job_max} 必须 ≥ 1")
        if self.video_job_concurrency < 1:
            problems.append(
                f"VIDEO_JOB_CONCURRENCY={self.video_job_concurrency} 必须 ≥ 1")
        if self.upstream_rps <= 0:
            problems.append(f"UPSTREAM_RPS={self.upstream_rps} 必须 > 0")
        if self.upstream_burst < 1:
            problems.append(f"UPSTREAM_BURST={self.upstream_burst} 必须 ≥ 1")
        if self.upstream_cooldown_sec < 0:
            problems.append(
                f"UPSTREAM_COOLDOWN_SEC={self.upstream_cooldown_sec} 必须 ≥ 0")

        # ---- V4 增量自检（参考图 / 视频提交排队）----
        if self.reference_max_mb <= 0:
            problems.append(
                f"REFERENCE_MAX_MB={self.reference_max_mb} 必须 > 0")
        if self.upload_max_mb <= 0:
            problems.append(f"UPLOAD_MAX_MB={self.upload_max_mb} 必须 > 0")
        # /upload 上限必须 ≥ 参考图上限：否则「上传成功却当不了参考图」，
        # 用户拿到一张用不了的图，属于自相矛盾的配置
        if self.upload_max_mb < self.reference_max_mb:
            problems.append(
                f"UPLOAD_MAX_MB={self.upload_max_mb} 必须 ≥ "
                f"REFERENCE_MAX_MB={self.reference_max_mb}"
                "（否则上传成功的图无法用作参考图）")
        if self.image_reference_max < 1:
            problems.append(
                f"IMAGE_REFERENCE_MAX={self.image_reference_max} 必须 ≥ 1")
        if not 1 <= self.video_reference_max <= 5:
            # Flash 上游硬限 5 张（官方文档：images length must not exceed 5）
            problems.append(
                f"VIDEO_REFERENCE_MAX={self.video_reference_max} 必须落在 1–5"
                "（agnes-video-2.5-flash 上游硬限 5 张）")
        if self.video_submit_queue_wait_sec < 0:
            problems.append(
                f"VIDEO_SUBMIT_QUEUE_WAIT_SEC={self.video_submit_queue_wait_sec}"
                " 必须 ≥ 0（0 = 关闭代排队，退回立即失败）")
        if self.video_submit_retry_min_sec <= 0:
            problems.append(
                f"VIDEO_SUBMIT_RETRY_MIN_SEC={self.video_submit_retry_min_sec}"
                " 必须 > 0")
        if self.video_submit_retry_max_sec < self.video_submit_retry_min_sec:
            problems.append(
                f"VIDEO_SUBMIT_RETRY_MAX_SEC={self.video_submit_retry_max_sec} "
                f"必须 ≥ VIDEO_SUBMIT_RETRY_MIN_SEC="
                f"{self.video_submit_retry_min_sec}")

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
            "[config] V3 多队列自检: "
            f"生图泳道 并发{self.image_max_concurrency}/等待{self.image_queue_wait_sec:g}s/"
            f"队列深{self.image_queue_max} | "
            f"视频泳道 并发{self.video_max_concurrency}/等待{self.video_queue_wait_sec:g}s/"
            f"队列深{self.video_queue_max} | "
            f"batch 并发 {self.image_batch_concurrency} ✓ | "
            f"视频后台作业 上限{self.video_job_max}/轮询并发{self.video_job_concurrency}/"
            f"总等待{self.video_job_max_wait_sec:g}s ✓",
            "[config] V3 上游限速自检: "
            f"{self.upstream_rps:g} req/s（突发 {self.upstream_burst}）| "
            f"过载冻结 {self.upstream_cooldown_sec:g}s ✓",
            "[config] V4 参考图自检: "
            f"单张上限 {self.reference_max_mb:g}MB | 生图上限 "
            f"{self.image_reference_max} 张 | 视频上限 "
            f"{self.video_reference_max} 张（上游硬限 5）| 上传上限 "
            f"{self.upload_max_mb:g}MB ✓",
            "[config] V4 视频提交排队自检: "
            f"代排队上限 {self.video_submit_queue_wait_sec:g}s | 重试退避 "
            f"{self.video_submit_retry_min_sec:g}–"
            f"{self.video_submit_retry_max_sec:g}s ✓",
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
        # ---- V3（多队列 / 并发 / 视频后台异步）----
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
        # ---- V3：上游速率限制 ----
        "UPSTREAM_RPS": "upstream_rps",
        "UPSTREAM_BURST": "upstream_burst",
        "UPSTREAM_COOLDOWN_SEC": "upstream_cooldown_sec",
        # ---- V4：参考图（图生图 / 图生视频）----
        "REFERENCE_MAX_MB": "reference_max_mb",
        "IMAGE_REFERENCE_MAX": "image_reference_max",
        "VIDEO_REFERENCE_MAX": "video_reference_max",
        "UPLOAD_MAX_MB": "upload_max_mb",
        # ---- V4：视频提交排队 ----
        "VIDEO_SUBMIT_QUEUE_WAIT_SEC": "video_submit_queue_wait_sec",
        "VIDEO_SUBMIT_RETRY_MIN_SEC": "video_submit_retry_min_sec",
        "VIDEO_SUBMIT_RETRY_MAX_SEC": "video_submit_retry_max_sec",
    }
    return special[env_name]
