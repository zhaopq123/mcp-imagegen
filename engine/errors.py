"""错误分类矩阵（V2 改造版；红线 C3、R10；E19/E24）。

纯查表，无副作用；返回五元组 (category, retry, switchable, reason, suggestion)。
任何处理动作（重试/熔断）都不在本模块。

V2 变更（增量设计方案一节/三节）：
- 删 402 分支：Agnes 无限期间免费，402 不可能出现（规格书 4.1"仅硅基侧"）；
- 加 task_not_found（fixthenretry）：视频任务表过期/逐出/不存在（R16）；
- 视频失败按原因映射：video_failed_error（E19 唯一模型无切换，
  失败分类与生图共用本矩阵，retry 语义不变）；
- 上游 401/403 注明 provider=Agnes（Agnes 侧配置错误，指向 AGNES_TOKEN）；
- switchable 列随单 Provider 失去消费者，矩阵保留列但恒无意义
  （唯一模型无切换，E19）——列值统一 False，防旧语义残留。

V2 第七轮（官方文档对齐 D6）：上游错误体双格式解析纯函数
upstream_error_message（顶层 {code,message,data} 优先、嵌套
{"error":{…}} 兜底，真机 F10/F3 实证两种并存）；503 video_queue_full
经 5xx 分支归 upstream_error、retry=yes（无需新增类别）。

| 类别              | retry         | 说明                                   |
|-------------------|---------------|----------------------------------------|
| 参数错            | fixthenretry  | 指明哪个参数                           |
| MCP 鉴权 401      | no            | 找管理员（MCPACCESS_TOKEN）            |
| 上游鉴权 401/403  | no            | 服务器配置错（AGNES_TOKEN，provider=Agnes） |
| 任务不存在        | fixthenretry  | 视频任务表未命中（TTL/逐出/未知）     |
| 限流 429          | yes           | 硬熔断 10min                           |
| 上游 5xx/超时/网络| yes           | 重试 1 次；连败 2 次软熔断 5min        |
| 内容审核 400      | no            | 改 prompt 再来                         |
| 上游 422          | no            | 归一层 bug，报错误码反馈               |
| persist 失败      | —（非错误行） | 降级返原 URL，WARN 日志                |
| 并发繁忙          | yes           | 稍后重调                               |
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------
# 类别码（[error] category=<码> 的取值）
# --------------------------------------------------------------------------
INVALID_PARAMS = "invalid_params"      # 参数错
MCP_AUTH = "mcp_auth"                  # MCP 层鉴权 401（MCPACCESS_TOKEN，找管理员）
UPSTREAM_AUTH = "upstream_auth"        # 上游鉴权 401/403（AGNES_TOKEN，provider=Agnes）
RATE_LIMIT = "rate_limit"              # 限流 429（402 已删：Agnes 无限期间免费）
UPSTREAM_ERROR = "upstream_error"       # 上游 5xx/超时/网络
CONTENT_POLICY = "content_policy"      # 内容审核 400
UPSTREAM_422 = "upstream_422"          # 上游 422（归一层 bug）
PERSIST_FAILED = "persist_failed"      # persist 失败（降级，不产生 error 行）
BUSY = "busy"                          # 并发繁忙
TASK_NOT_FOUND = "task_not_found"      # 视频任务表未命中（V2 新增，R16）

# retry 三值语义（红线 A4）：Agent 读 retry 字段即知动作
RETRY_NO = "no"
RETRY_YES = "yes"
RETRY_FIX_THEN_RETRY = "fixthenretry"

# 矩阵：category -> (switchable, retry)。None = 不适用（persist 失败不是错误行）
# switchable 列 V2 起恒 False（唯一模型无切换，E19）；保留列供契约兼容。
MATRIX: dict[str, tuple[Optional[bool], Optional[str]]] = {
    INVALID_PARAMS: (False, RETRY_FIX_THEN_RETRY),
    MCP_AUTH: (False, RETRY_NO),
    UPSTREAM_AUTH: (False, RETRY_NO),
    RATE_LIMIT: (False, RETRY_YES),
    UPSTREAM_ERROR: (False, RETRY_YES),
    CONTENT_POLICY: (False, RETRY_NO),
    UPSTREAM_422: (False, RETRY_NO),
    PERSIST_FAILED: (None, None),
    BUSY: (False, RETRY_YES),
    TASK_NOT_FOUND: (False, RETRY_FIX_THEN_RETRY),
}


@dataclass(frozen=True)
class ErrorInfo:
    """错误分类结果（五元组，设计方案 2.4 契约）。"""

    category: str
    retry: Optional[str]         # None = 不适用（persist_failed）
    switchable: Optional[bool]    # None = 不适用（persist_failed）
    reason: str
    suggestion: str


# --------------------------------------------------------------------------
# 分类入口（纯查表）
# --------------------------------------------------------------------------

def _build(category: str, reason: str, suggestion: str, detail: str = "") -> ErrorInfo:
    switchable, retry = MATRIX[category]
    full_reason = f"{reason} | {detail}" if detail else reason
    return ErrorInfo(category, retry, switchable, full_reason, suggestion)


def classify_http_status(status_code: int, detail: str = "") -> ErrorInfo:
    """上游 HTTP 状态 → 分类（规格书 4.1 的 V2 版；402 已删）。

    规格书未明确列出的状态码（如 404）按最接近的可重试类 upstream_error 处理。
    注意：MCP 层 401 与上游 401 严格区分（R10）——本函数只处理上游响应，
    MCP 层鉴权失败请用 mcp_auth_error()。
    """
    if status_code == 400:
        return _build(CONTENT_POLICY, "内容审核未通过(400)", "修改 prompt 后重试", detail)
    if status_code in (401, 403):
        return _build(
            UPSTREAM_AUTH,
            f"上游鉴权失败({status_code}, provider=Agnes)",
            "服务器配置错误，请检查 AGNES_TOKEN",
            detail,
        )
    if status_code == 429:
        return _build(RATE_LIMIT, "上游限流(429)", "稍后重试", detail)
    if status_code == 422:
        return _build(UPSTREAM_422, "上游 422：归一层参数 bug", "请将错误码反馈给开发者", detail)
    if 500 <= status_code <= 599:
        return _build(UPSTREAM_ERROR, f"上游服务错误({status_code})", "稍后重试", detail)
    return _build(
        UPSTREAM_ERROR,
        f"上游返回未定义状态({status_code})，按可重试类处理",
        "稍后重试",
        detail,
    )


def classify_exception(exc: BaseException) -> ErrorInfo:
    """网络/超时异常 → upstream_error（重试 1 次动作在调用方）。"""
    name = type(exc).__name__
    return _build(UPSTREAM_ERROR, f"网络/超时异常({name})", "稍后重试", str(exc))


def upstream_error_message(body) -> Optional[str]:
    """上游错误体 → message 提取（D6 双格式；纯函数无 IO）。

    真机实证两种格式并存（V2 第六轮 F3/F10）：
    - 顶层三段式：{"code": "video_queue_full", "message": "...",
      "data": null}——503 视频队列满实测（F10）；
    - 嵌套式：{"error": {code, message, type}}——401 实测（F3）。

    顶层 message 优先（D6 定案）；无 message / 格式不识别 → None
    （调用方回退响应体截断文本）。类别归属不变：HTTP 状态码仍是
    分类唯一依据（503 含 video_queue_full → upstream_error、retry=yes，
    经 classify_http_status 5xx 分支，无需新增类别）。
    """
    if not isinstance(body, dict):
        return None
    # 顶层三段式（F10：503 video_queue_full 等真机实证）
    msg = body.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg
    # 嵌套式（F3：{"error":{code,message,type}} 真机实证）
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg
    return None


def network_error(detail: str = "") -> ErrorInfo:
    """网络/超时类失败（重试 1 次后仍失败；供 pipeline 从 ApiCallResult 构造）。"""
    return _build(
        UPSTREAM_ERROR, "网络/超时异常(重试1次后仍失败)", "稍后重试", detail
    )


def internal_error(exc: BaseException) -> ErrorInfo:
    """服务内部异常兜底（理论上不可达）。

    4.1 无"内部错误"类别；按"上游 422：归一层 bug，报错误码反馈"的语义
    就近映射（服务端 bug → 反馈开发者），retry=no 不引导 Agent 撞墙。
    """
    return _build(
        UPSTREAM_422,
        f"服务内部异常({type(exc).__name__})",
        "请将日志中的 req_id 反馈给开发者",
        str(exc),
    )


# --------------------------------------------------------------------------
# 工厂（供 pipeline / budget / persist / video_pipeline 各阶段直接构造）
# --------------------------------------------------------------------------

def invalid_params(param: str, detail: str) -> ErrorInfo:
    """参数错：必须指明哪个参数（fixthenretry）。"""
    return _build(INVALID_PARAMS, f"参数校验失败: {param}", "修正参数后重试", detail)


def mcp_auth_error() -> ErrorInfo:
    """MCP 层鉴权 401（Bearer MCPACCESS_TOKEN，找管理员；与上游 401 严格区分）。"""
    return _build(MCP_AUTH, "MCP 层鉴权失败(401)", "请联系管理员检查 MCPACCESS_TOKEN")


def busy_error(waited_sec: float) -> ErrorInfo:
    return _build(BUSY, f"并发繁忙(等待 {waited_sec:.1f}s 超限)", "稍后重调")


def deadline_exhausted(elapsed: float = 0.0) -> ErrorInfo:
    """预算耗尽 / 余额不足快失败（规格书 3.3：快失败返超时错误，retry=yes）。"""
    detail = f"已耗时 {elapsed:.1f}s" if elapsed else "预算耗尽即止，快失败"
    return _build(
        UPSTREAM_ERROR, "请求预算耗尽(deadline)", "稍后重试（Agent 侧 retry=yes）", detail
    )


def persist_failed_note(reason: str) -> ErrorInfo:
    """persist 失败：不产生 [error] 行，降级返原 URL + meta 追加 persist_failed（R5）。"""
    return _build(PERSIST_FAILED, "persist 失败（降级返回原 URL）", "-", reason)


def task_not_found_error(task_id: str = "") -> ErrorInfo:
    """视频任务不存在 / TTL 过期 / 容量逐出（R16 三分支统一契约，V2 新增）。

    fixthenretry：Agent 应换 prompt 重新提交（新 task_id）——与"参数错"
    同语义（Agent 侧可自愈，无需人工）。
    """
    detail = f"task_id={task_id}" if task_id else "task_id 未提供"
    return _build(
        TASK_NOT_FOUND,
        "视频任务不存在（TTL 过期/容量逐出/未知 task_id）",
        "请重新提交生成（新 task_id）",
        detail,
    )


def circuit_open_error(retry_after_sec: float) -> ErrorInfo:
    """E24：唯一生图模型熔断 OPEN 期，auto 请求快失败（rate_limit, retry=yes）。"""
    return _build(
        RATE_LIMIT,
        f"熔断中（provider=Agnes），约 {retry_after_sec:.0f} 秒后重试",
        "稍后重试（Agent 侧 retry=yes）",
    )


def video_failed_error(detail: str = "") -> ErrorInfo:
    """视频任务失败（E19：唯一模型无切换，按原因映射共用本矩阵）。

    失败原因细分由上游 status/错误体透传进 detail；类别按最接近语义
    upstream_error（可稍后重试）——审核类 400 / 鉴权类 401/403 走
    classify_http_status 获得各自类别，本函数仅兜底"上游明确说 failed"。
    """
    return _build(
        UPSTREAM_ERROR,
        "视频生成失败(上游 failed)",
        "修改 prompt 后重试（retry=yes）",
        detail,
    )
