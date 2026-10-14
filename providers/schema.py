"""Provider 配置模型：把「上游生成 API」抽象成可增删改查的配置对象。

设计目标
--------
1. **零内建默认**：本包不含任何厂商的 base_url、模型名或密钥常量。
   没有配置就没有能力——服务不会悄悄退化到某个写死的上游。
2. **能描述任意 HTTP API**：请求侧是模板（method / path / headers /
   query / body），响应侧是路径表达式抽取。视频类 API 天生是「提交 +
   轮询」两段异步模型，故视频能力持有 submit 与 poll 两个请求模板。
3. **preset 只是填表助手**：内置 preset 给出常见协议（OpenAI 风格生图、
   通用异步视频）的默认模板，用户在面板上仍能逐字段改。删掉 preset
   字段，配置依然完整可读——preset 不承载运行时逻辑。

占位符语法（见 providers/template.py）
------------------------------------
body / query / path / headers 的字符串值里：

    {prompt}      → 替换为对应变量的字符串形态
    {{prompt}}    → 原样注入（保持变量原本类型：数组/对象/数字/布尔）

单个值若**整体**就是一个占位符，且该变量缺失，则整个键被删除
（不向上游注入 `null`——大多数 API 会因 `"seed": null` 报 400）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 能力类型（Provider 自检与路由的单位）
CAP_IMAGE = "image"
CAP_VIDEO = "video"
CAPABILITIES = (CAP_IMAGE, CAP_VIDEO)

# 鉴权方式
AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_HEADER = "header"
AUTH_QUERY = "query"
AUTH_KINDS = (AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_QUERY)

# 请求体编码
BODY_JSON = "json"
BODY_FORM = "form"
BODY_KINDS = (BODY_JSON, BODY_FORM)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SchemaError(Exception):
    """配置非法（面板提交 / 配置文件加载时的校验失败）。"""


def check_id(value: str, what: str = "id") -> str:
    """id 合法性：字母数字开头，限长。"""
    if not isinstance(value, str) or not _ID_RE.match(value or ""):
        raise SchemaError(
            f"{what} 非法：{value!r}（须以字母或数字开头，"
            "仅含字母/数字/下划线/连字符，长度 1–64）"
        )
    return value


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
@dataclass
class AuthSpec:
    """上游鉴权。token 支持 ``env:VAR_NAME`` 形态引用环境变量。

    引用环境变量是推荐的用法：密钥不落配置文件，容器环境变量或
    systemd EnvironmentFile 注入即可。
    """

    kind: str = AUTH_BEARER
    token: str = ""
    # header 模式：Authorization / X-API-Key 等；query 模式：key / api_key
    name: str = "Authorization"
    prefix: str = "Bearer "  # 值前缀（bearer 模式默认 "Bearer "）

    def __post_init__(self) -> None:
        if self.kind not in AUTH_KINDS:
            raise SchemaError(f"auth.kind 非法：{self.kind!r}（应为 {AUTH_KINDS} 之一）")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "token": self.token,
            "name": self.name, "prefix": self.prefix,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AuthSpec":
        data = dict(data or {})
        if "token" not in data:
            # 兼容简写：{"Authorization": "Bearer xxx"} 之类直接给头名
            data.setdefault("token", "")
        return cls(
            kind=data.get("kind", AUTH_BEARER),
            token=str(data.get("token", "")),
            name=str(data.get("name") or "Authorization"),
            prefix=str(data.get("prefix") or "Bearer "),
        )


# ---------------------------------------------------------------------------
# 请求模板 / 响应抽取
# ---------------------------------------------------------------------------
@dataclass
class RequestSpec:
    """一次上游调用的形状。所有字符串值支持占位符。"""

    method: str = "POST"
    path: str = ""
    headers: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    body: Optional[Any] = None  # dict / None（GET 通常为 None）
    body_encoding: str = BODY_JSON

    def __post_init__(self) -> None:
        if self.method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise SchemaError(f"request.method 非法：{self.method!r}")
        if self.body_encoding not in BODY_KINDS:
            raise SchemaError(f"request.body_encoding 非法：{self.body_encoding!r}")
        self.method = self.method.upper()

    def to_dict(self) -> dict:
        return {
            "method": self.method, "path": self.path,
            "headers": dict(self.headers or {}),
            "query": dict(self.query or {}),
            "body": self.body,
            "body_encoding": self.body_encoding,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RequestSpec":
        data = dict(data or {})
        return cls(
            method=str(data.get("method") or "POST"),
            path=str(data.get("path") or ""),
            headers=dict(data.get("headers") or {}),
            query=dict(data.get("query") or {}),
            body=data.get("body"),
            body_encoding=str(data.get("body_encoding") or BODY_JSON),
        )


@dataclass
class ExtractSpec:
    """从上游响应里抽取目标字段的路径表达式。

    路径语法（providers/template.extract）：

        data[].url        → 取 data 数组每个元素的 url
        images[0].url     → 按下标取
        $.result.video_id → 顶层 $ 可选

    留空 = 走内置「猜测」逻辑：在响应里找第一个含目标字段的容器（兼容
    ``images[]`` / ``data[]`` 双形态，绝大多数 API 都能命中）。
    """

    urls: str = ""      # 生图：图片 URL 列表（或 base64 列表）
    task_id: str = ""   # 视频提交：上游任务号
    status: str = ""    # 视频查询：任务状态字符串
    url: str = ""       # 视频查询：结果视频 URL
    b64: bool = False   # 生图抽取结果视为 base64（自动补 data URI 头）
    status_map: dict = field(default_factory=dict)  # 上游状态 → 标准状态
    error_message: str = "message"  # 错误体 message 抽取路径

    def to_dict(self) -> dict:
        return {
            "urls": self.urls, "task_id": self.task_id, "status": self.status,
            "url": self.url, "b64": self.b64,
            "status_map": dict(self.status_map or {}),
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExtractSpec":
        data = dict(data or {})
        return cls(
            urls=str(data.get("urls") or ""),
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or ""),
            url=str(data.get("url") or ""),
            b64=bool(data.get("b64", False)),
            status_map=dict(data.get("status_map") or {}),
            error_message=str(data.get("error_message") or "message"),
        )


@dataclass
class CapabilitySpec:
    """一项能力（image 或 video）。"""

    enabled: bool = True
    model: str = ""                      # 写进 body 的 {model} 值
    request: RequestSpec = field(default_factory=RequestSpec)
    extract: ExtractSpec = field(default_factory=ExtractSpec)
    # 视频：轮询模板（提交返回任务号后反复调用直到终态）
    poll: Optional[RequestSpec] = None
    poll_extract: Optional[ExtractSpec] = None
    # video 专属：是否走「提交 + 轮询」异步模型（False = 提交即出结果）
    async_poll: bool = True
    request_interval_sec: float = 0.0    # 提交前的礼貌间隔（下游合作 source）

    def to_dict(self) -> dict:
        data: dict = {
            "enabled": self.enabled, "model": self.model,
            "request": self.request.to_dict(), "extract": self.extract.to_dict(),
        }
        if self.poll is not None:
            data["poll"] = self.poll.to_dict()
            data["poll_extract"] = (
                self.poll_extract.to_dict() if self.poll_extract else None)
            data["async_poll"] = self.async_poll
        data["request_interval_sec"] = self.request_interval_sec
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CapabilitySpec":
        data = dict(data or {})
        poll_raw = data.get("poll")
        poll_extract_raw = data.get("poll_extract")
        return cls(
            enabled=bool(data.get("enabled", True)),
            model=str(data.get("model") or ""),
            request=RequestSpec.from_dict(data.get("request")),
            extract=ExtractSpec.from_dict(data.get("extract")),
            poll=RequestSpec.from_dict(poll_raw) if poll_raw else None,
            poll_extract=(
                ExtractSpec.from_dict(poll_extract_raw) if poll_extract_raw else None
            ),
            async_poll=bool(data.get("async_poll", True)),
            request_interval_sec=float(data.get("request_interval_sec") or 0.0),
        )


@dataclass
class LimitSpec:
    """本 provider 的上游限速（令牌桶 + 过载冻结）。

    默认 rps=10 / burst=20：允许 20 张内的短突发一次性放出，之后以 10 req/s
    匀速补充。这样既给足并发（不把上游当串行队列），又在真实上游承压时通过
    令牌桶与 429 冻结窗口兜底，避免撞 429 被硬熔断。脆弱上游可在管理面板把
    rps 调小（如 1~2），无需改代码。
    """

    rps: float = 10.0
    burst: int = 20
    cooldown_sec: float = 15.0

    def __post_init__(self) -> None:
        if self.rps <= 0:
            raise SchemaError("limits.rps 必须 > 0")
        if self.burst < 1:
            raise SchemaError("limits.burst 必须 ≥ 1")
        if self.cooldown_sec < 0:
            raise SchemaError("limits.cooldown_sec 必须 ≥ 0")

    def to_dict(self) -> dict:
        return {"rps": self.rps, "burst": self.burst, "cooldown_sec": self.cooldown_sec}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "LimitSpec":
        data = dict(data or {})
        return cls(
            rps=float(data.get("rps", 1.0)),
            burst=int(data.get("burst", 2)),
            cooldown_sec=float(data.get("cooldown_sec", 15.0)),
        )


@dataclass
class PoolSpec:
    """本 provider 独占的 HTTP 连接池参数（默认继承全局，可覆盖）。"""

    max_connections: int = 0          # 0 = 用全局
    max_keepalive_connections: int = 0
    connect_sec: float = 0.0
    write_sec: float = 0.0
    pool_sec: float = 0.0
    read_sec: float = 0.0             # read 档（0 = 用全局生图/视频档）

    def to_dict(self) -> dict:
        return {
            "max_connections": self.max_connections,
            "max_keepalive_connections": self.max_keepalive_connections,
            "connect_sec": self.connect_sec, "write_sec": self.write_sec,
            "pool_sec": self.pool_sec, "read_sec": self.read_sec,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PoolSpec":
        data = dict(data or {})
        return cls(
            max_connections=int(data.get("max_connections") or 0),
            max_keepalive_connections=int(
                data.get("max_keepalive_connections") or 0),
            connect_sec=float(data.get("connect_sec") or 0.0),
            write_sec=float(data.get("write_sec") or 0.0),
            pool_sec=float(data.get("pool_sec") or 0.0),
            read_sec=float(data.get("read_sec") or 0.0),
        )


@dataclass
class ProviderConfig:
    """一个上游供应商的完整配置（面板 CRUD 的单位）。"""

    id: str
    name: str = ""
    enabled: bool = True
    preset: str = "custom"
    base_url: str = ""
    auth: AuthSpec = field(default_factory=AuthSpec)
    limits: LimitSpec = field(default_factory=LimitSpec)
    pool: PoolSpec = field(default_factory=PoolSpec)
    capabilities: dict = field(default_factory=dict)  # str -> CapabilitySpec
    # 优先级：同一能力多个 provider 时，数字小的先；同优先级轮询
    priority: int = 100
    tags: list = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        check_id(self.id, "provider.id")
        if self.base_url:
            if not self.base_url.startswith(("http://", "https://")):
                raise SchemaError(
                    f"provider.base_url 必须以 http:// 或 https:// 开头："
                    f"{self.base_url!r}"
                )
            self.base_url = self.base_url.rstrip("/")

    # ------------------------------------------------------------------
    def cap(self, kind: str) -> Optional[CapabilitySpec]:
        """取可用能力（不存在 / 未启用 / base_url 缺省 → None）。"""
        if not self.enabled or not self.base_url:
            return None
        spec = self.capabilities.get(kind)
        if spec is None or not spec.enabled:
            return None
        if not spec.request.path:
            return None
        return spec

    def has(self, kind: str) -> bool:
        return self.cap(kind) is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "preset": self.preset, "base_url": self.base_url,
            "auth": self.auth.to_dict(), "limits": self.limits.to_dict(),
            "pool": self.pool.to_dict(), "priority": self.priority,
            "tags": list(self.tags), "notes": self.notes,
            "capabilities": {k: v.to_dict() for k, v in self.capabilities.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        data = dict(data or {})
        pid = str(data.get("id") or "").strip()
        if not pid:
            raise SchemaError("provider.id 不能为空")
        caps: dict = {}
        for kind, raw in dict(data.get("capabilities") or {}).items():
            if kind not in CAPABILITIES:
                raise SchemaError(
                    f"未知能力 {kind!r}（应为 {CAPABILITIES} 之一）")
            caps[kind] = CapabilitySpec.from_dict(raw)
        return cls(
            id=pid,
            name=str(data.get("name") or pid),
            enabled=bool(data.get("enabled", True)),
            preset=str(data.get("preset") or "custom"),
            base_url=str(data.get("base_url") or ""),
            auth=AuthSpec.from_dict(data.get("auth")),
            limits=LimitSpec.from_dict(data.get("limits")),
            pool=PoolSpec.from_dict(data.get("pool")),
            capabilities=caps,
            priority=int(data.get("priority", 100)),
            tags=list(data.get("tags") or []),
            notes=str(data.get("notes") or ""),
        )


# ---------------------------------------------------------------------------
# 访问令牌（MCP 层；多会话 / 多工作空间隔离的单位）
# ---------------------------------------------------------------------------
@dataclass
class TokenRecord:
    """一个 MCP 访问令牌。

    多工作空间隔离靠它：每个会话/工作空间发一个 token，各自绑定默认
    provider 与并发配额，互不挤占。
    """

    id: str
    name: str = ""
    token: str = ""
    enabled: bool = True
    default_provider: str = ""          # 空 = 走全局路由（按 priority）
    allowed_capabilities: list = field(default_factory=lambda: list(CAPABILITIES))
    max_concurrency: int = 0            # 0 = 不限（走全局泳道额度）
    note: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0

    def __post_init__(self) -> None:
        check_id(self.id, "token.id")

    def allows(self, kind: str) -> bool:
        return kind in (self.allowed_capabilities or CAPABILITIES)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "token": self.token,
            "enabled": self.enabled, "default_provider": self.default_provider,
            "allowed_capabilities": list(self.allowed_capabilities),
            "max_concurrency": self.max_concurrency, "note": self.note,
            "created_at": self.created_at, "last_used_at": self.last_used_at,
            "use_count": self.use_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenRecord":
        data = dict(data or {})
        tid = str(data.get("id") or "").strip()
        if not tid:
            raise SchemaError("token.id 不能为空")
        return cls(
            id=tid,
            name=str(data.get("name") or tid),
            token=str(data.get("token") or ""),
            enabled=bool(data.get("enabled", True)),
            default_provider=str(data.get("default_provider") or ""),
            allowed_capabilities=list(
                data.get("allowed_capabilities") or CAPABILITIES),
            max_concurrency=int(data.get("max_concurrency") or 0),
            note=str(data.get("note") or ""),
            created_at=float(data.get("created_at") or 0.0),
            last_used_at=float(data.get("last_used_at") or 0.0),
            use_count=int(data.get("use_count") or 0),
        )


# ---------------------------------------------------------------------------
# 预设模板（面板「新建」时的默认形状；不含任何厂商地址）
# ---------------------------------------------------------------------------
def preset_capability(preset: str, kind: str) -> CapabilitySpec:
    """按 preset + 能力给出一份可编辑的默认模板。

    注意：**只有模板，没有厂商**。用户仍需填 base_url、密钥与模型名。
    """
    if kind == CAP_IMAGE:
        if preset == "openai":
            return CapabilitySpec(
                model="",
                request=RequestSpec(
                    method="POST", path="/v1/images/generations",
                    body={"model": "{model}", "prompt": "{prompt}",
                          "n": "{{n}}", "size": "{size}"},
                ),
                extract=ExtractSpec(urls="data[].url", b64=False),
            )
        # generic：同步返回图片
        return CapabilitySpec(
            model="",
            request=RequestSpec(
                method="POST", path="/v1/images/generations",
                body={"model": "{model}", "prompt": "{prompt}",
                      "size": "{size}", "ratio": "{ratio}",
                      "image": "{{images}}"},
            ),
            extract=ExtractSpec(urls="", b64=False),
        )

    # ---- video ----
    if preset == "openai":
        return CapabilitySpec(
            model="",
            request=RequestSpec(
                method="POST", path="/v1/videos",
                body={"model": "{model}", "prompt": "{prompt}"},
            ),
            extract=ExtractSpec(task_id="id"),
            poll=RequestSpec(method="GET", path="/v1/videos/{{task_id}}"),
            poll_extract=ExtractSpec(status="status", url="url"),
        )
    return CapabilitySpec(
        model="",
        request=RequestSpec(
            method="POST", path="/v1/videos",
            body={"model": "{model}", "prompt": "{prompt}",
                  "seconds": "{seconds}", "aspect_ratio": "{aspect_ratio}",
                  "seed": "{{seed}}", "image": "{{images}}"},
        ),
        extract=ExtractSpec(task_id=""),
        poll=RequestSpec(
            method="GET", path="/v1/videos/query",
            query={"task_id": "{task_id}", "model": "{model}"},
        ),
        poll_extract=ExtractSpec(status="", url=""),
    )


PRESETS = ("custom", "openai")


def new_provider_stub(pid: str, name: str = "", preset: str = "custom") -> dict:
    """面板「新建 provider」的初始对象（未填的地址/密钥均为空）。"""
    caps = {
        CAP_IMAGE: preset_capability(preset, CAP_IMAGE),
        CAP_VIDEO: preset_capability(preset, CAP_VIDEO),
    }
    return ProviderConfig(
        id=pid, name=name or pid, preset=preset, base_url="",
        auth=AuthSpec(kind=AUTH_BEARER, token=""),
        capabilities=caps,
    ).to_dict()
