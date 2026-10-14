"""管理面板 REST API。

接口一览（全部挂在 ``/admin/api/*``，除登录外都要求会话 + CSRF 头）::

    GET  /admin/api/state                查看状态（是否已初始化/是否已登录）
    POST /admin/api/setup               首次初始化：设置管理员密码
    POST /admin/api/auth/login          登录
    POST /admin/api/auth/logout         登出

    GET  /admin/api/dashboard           总览：指标/泳道/服务提供方/磁盘/任务
    GET  /admin/api/logs                最近日志（环形缓冲）

    GET  /admin/api/providers           列表（密钥脱敏）
    POST /admin/api/providers           新建
    GET  /admin/api/providers/<id>      详情（含密钥）
    PUT  /admin/api/providers/<id>      更新
    DEL  /admin/api/providers/<id>      删除
    POST /admin/api/providers/<id>/toggle    启/停
    POST /admin/api/providers/<id>/test      联通性自检
    POST /admin/api/providers/reload        热加载（等同上面的改动生效）
    GET  /admin/api/providers/export        导出
    POST /admin/api/providers/import        导入

    GET  /admin/api/tokens              令牌列表
    POST /admin/api/tokens              新建（服务端生成随机令牌）
    PUT  /admin/api/tokens/<id>         改名/改配额/改绑定
    DEL  /admin/api/tokens/<id>         删除
    POST /admin/api/tokens/<id>/rotate  重新生成令牌串

    GET  /admin/api/tasks               视频作业快照
    GET  /admin/api/settings            运行时参数（可热更新部分）
    PUT  /admin/api/settings            更新

约定：写操作一律要求 ``X-Admin-Request: 1`` 头；响应统一
``{"ok": true, ...}`` 或 ``{"ok": false, "error": "code", "message": "..."}``。
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import auth as auth_mod
from .page import PAGE_HTML
from providers import schema
from providers.store import new_token

logger = logging.getLogger("mcp_imagegen.admin")

# 探针自检参数（真实调用，故尽量小 / 短）
PROBE_PROMPT = "a tiny black dot on white background"
PROBE_SIZE = "512x512"
PROBE_DEADLINE_SEC = 30.0
PROBE_VIDEO_ID = "__connectivity_probe__"

# 列表里的密钥预览：只留"能认出是哪一把钥匙"的信息量
_PREVIEW_MIN_LEN = 8      # 短于此一律全遮（短串遮一半等于没遮）
_PREVIEW_HEAD = 3         # 只露前缀（sk- / gsk_ 这类类型标识）


def _token_preview(token: str) -> str:
    """列表视图的密钥预览：露类型前缀，不露尾巴。

    尾巴是高熵段，露出等于白送；前缀（sk- / gsk_ / xai-）只是"哪家/哪种
    钥匙"的标识，运维要在一堆 provider 里认人，这点信息量必须有。
    """
    if not token:
        return ""
    if len(token) < _PREVIEW_MIN_LEN:
        return "*" * len(token)
    return token[:_PREVIEW_HEAD] + "…" + "*" * 4


class RingLogHandler(logging.Handler):
    """把最近若干条日志留在内存里，供面板「日志」页查看。"""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__(level=logging.INFO)
        self.records: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({
                "ts": round(time.time(), 3),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record)[:800],
            })
        except Exception:  # noqa: BLE001 —— 日志器绝不能反过来杀死业务
            pass

    def dump(self, limit: int = 200, level: Optional[str] = None) -> list:
        items = list(self.records)
        if level:
            items = [i for i in items if i["level"] == level.upper()]
        return items[-limit:]


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
class AdminContext:
    """面板需要的全部依赖（server 装配时注入）。"""

    def __init__(
        self,
        cfg,
        store,
        gateway,
        pool,
        video_pipeline,
        persist,
        sessions: auth_mod.SessionManager,
        metrics,
        ring: RingLogHandler,
        *,
        apply_settings: Optional[Callable[[dict], Any]] = None,
        version: str = "0.0.0",
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.pool = pool
        self.video_pipeline = video_pipeline
        self.persist = persist
        self.sessions = sessions
        self.metrics = metrics
        self.ring = ring
        self.apply_settings = apply_settings
        self.version = version


class AdminAPI:
    """ASGI handler（挂在 fastmcp custom_route 上）。"""

    def __init__(self, ctx: AdminContext, prefix: str = "/admin") -> None:
        self.ctx = ctx
        self.prefix = prefix.rstrip("/") or "/admin"

    # ------------------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        """ASGI 入口。

        必须自己实现三参 ASGI：Starlette 的 Route 只对「函数/方法」做
        request_response 包装，**类实例会被原样当 ASGI app** 以
        (scope, receive, send) 调用——挂在 custom_route 上时走的就是这条路。
        只写 handle(Request) 的话面板 100% 报错，而且异常发生在路由层，
        单测（直接调 handle）根本测不出来——必须由端到端用例兜住。
        """
        if scope.get("type") != "http":
            return
        request = Request(scope, receive)
        response = await self.handle(request)
        await response(scope, receive, send)

    async def handle(self, request: Request) -> Response:
        """按请求分发（单测直接调它，绕开 ASGI 装配）。"""
        rest = (request.path_params.get("rest") or "").strip("/")
        if rest in ("", "index.html"):
            root = request.scope.get("root_path", "")
            html = PAGE_HTML.replace("{{PREFIX}}", f"{root}{self.prefix}")
            return Response(html, media_type="text/html; charset=utf-8")
        if not rest.startswith("api/"):
            return JSONResponse(
                {"ok": False, "error": "not_found"}, status_code=404)
        method = request.method
        tail = rest[len("api/"):]
        parts = [p for p in tail.split("/") if p]
        try:
            return await self._dispatch(method, parts, request)
        except auth_mod.SessionError as exc:
            return self._fail(exc.status, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 —— 面板任何异常都要变成 JSON
            logger.exception("[admin] %s /%s 处理异常", method, tail)
            return self._fail(500, "internal", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    async def _dispatch(self, method: str, parts: list,
                        request: Request) -> Response:
        head = parts[0] if parts else ""
        if head == "state":
            return self._state(request)
        if head == "setup":
            return await self._setup(request)
        if head == "auth":
            return await self._auth(parts[1:], request)
        # ---- 以下均需登录 ----
        self.ctx.sessions.authenticate(
            request.headers.raw, write=False)
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            self.ctx.sessions.require_write(request.headers.raw)

        if head == "dashboard":
            return self._dashboard()
        if head == "logs":
            return self._logs(request)
        if head == "providers":
            return await self._providers(method, parts[1:], request)
        if head == "tokens":
            return await self._tokens(method, parts[1:], request)
        if head == "tasks":
            return self._tasks(request)
        if head == "settings":
            return await self._settings(method, request)
        return self._fail(404, "not_found", f"未知接口 {head}")

    # ------------------------------------------------------------------
    # 基础响应
    # ------------------------------------------------------------------
    @staticmethod
    def _ok(payload: dict = None, status: int = 200) -> JSONResponse:
        return JSONResponse({"ok": True, **(payload or {})}, status_code=status)

    @staticmethod
    def _fail(status: int, code: str, message: str = "") -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": code, "message": message or code},
            status_code=status)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------
    def _state(self, request: Request) -> JSONResponse:
        sid = self.ctx.sessions.cookie_of(request.headers.raw)
        return JSONResponse({
            "ok": True,
            "initialized": self.ctx.sessions.initialized,
            "authenticated": self.ctx.sessions.valid(sid),
            "version": self.ctx.version,
            "capabilities": list(schema.CAPABILITIES),
        })

    async def _setup(self, request: Request) -> JSONResponse:
        if self.ctx.sessions.initialized:
            return self._fail(409, "already_initialized", "管理员已初始化")
        body = await self._json_body(request)
        password = str(body.get("password") or "")
        if len(password) < 8:
            return self._fail(400, "weak_password", "管理员密码至少 8 位")
        self.ctx.store.set_admin_password(password)
        logger.warning("[admin] 管理员密码已设置（首次初始化完成）")
        return self._ok({"initialized": True})

    async def _auth(self, parts: list, request: Request) -> JSONResponse:
        action = parts[0] if parts else ""
        if action == "login":
            body = await self._json_body(request)
            password = str(body.get("password") or "")
            if not self.ctx.sessions.initialized:
                return self._fail(409, "not_initialized", "尚未设置管理员密码")
            if not self.ctx.store.check_admin_password(password):
                return self._fail(401, "bad_credentials", "密码不正确")
            sid = self.ctx.sessions.issue()
            secure = request.url.scheme == "https"
            response = self._ok({"authenticated": True})
            response.set_cookie(
                auth_mod.SESSION_COOKIE, sid, httponly=True,
                max_age=int(auth_mod.ADMIN_SESSION_TTL_SEC),
                samesite="strict", secure=secure, path=self.prefix)
            return response
        if action == "logout":
            sid = self.ctx.sessions.cookie_of(request.headers.raw)
            self.ctx.sessions.revoke(sid)
            response = self._ok({"authenticated": False})
            response.delete_cookie(auth_mod.SESSION_COOKIE, path=self.prefix)
            return response
        return self._fail(404, "not_found", action)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def _dashboard(self) -> JSONResponse:
        ctx = self.ctx
        return self._ok({
            "metrics": ctx.metrics.snapshot(),
            "lanes": ctx.pool.stats() if hasattr(ctx.pool, "stats") else {},
            "providers": ctx.gateway.stats(),
            "counts": {
                "providers": len(ctx.store.providers),
                "providers_enabled": sum(
                    1 for p in ctx.store.providers if p.enabled),
                "tokens": len(ctx.store.tokens),
                "tokens_enabled": sum(1 for t in ctx.store.tokens if t.enabled),
                "video_jobs": len(ctx.video_pipeline.list_jobs(limit=1000)),
            },
            "disk_ratio": ctx.persist.disk_usage_ratio() if ctx.persist else None,
            "capabilities": {
                kind: ctx.gateway.has_capability(kind)
                for kind in schema.CAPABILITIES
            },
        })

    def _logs(self, request: Request) -> JSONResponse:
        level = request.query_params.get("level") or None
        limit = int(request.query_params.get("limit") or 200)
        return self._ok({"items": self.ctx.ring.dump(limit=limit, level=level)})

    def _tasks(self, request: Request) -> JSONResponse:
        limit = int(request.query_params.get("limit") or 50)
        return self._ok({"items": self.ctx.video_pipeline.list_jobs(limit=limit)})

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------
    def _masked(self, spec: schema.ProviderConfig) -> dict:
        data = spec.to_dict()
        token = data.get("auth", {}).get("token") or ""
        data["auth"]["token_set"] = bool(token)
        data["auth"]["token_preview"] = _token_preview(token)
        data["auth"].pop("token", None)
        data["health"] = self.ctx.gateway.stats().get(spec.id, {})
        return data

    async def _providers(self, method: str, parts: list,
                         request: Request) -> JSONResponse:
        store = self.ctx.store
        pid = parts[0] if parts else ""

        if method == "GET" and not pid:
            return self._ok({
                "items": [self._masked(p) for p in store.list_providers()],
                "presets": list(schema.PRESETS),
            })
        if method == "GET" and pid == "export":
            return JSONResponse({
                "ok": True, "version": 1,
                "providers": [p.to_dict() for p in store.list_providers()],
            })
        if method == "POST" and pid == "import":
            return await self._import_providers(request)
        if method == "GET" and pid == "stub":
            preset = request.query_params.get("preset") or "custom"
            name = request.query_params.get("id") or "provider"
            return self._ok({"item": schema.new_provider_stub(name, name, preset)})
        if method == "POST" and pid == "reload":
            added, removed = self.ctx.gateway.reload()
            return self._ok({"added": added, "removed": removed})
        if method == "POST" and not pid:
            return await self._write_provider(request, None)
        if pid:
            if method == "GET":
                spec = store.get_provider(pid)
                if spec is None:
                    return self._fail(404, "not_found", pid)
                return self._ok({"item": spec.to_dict()})
            if method == "PUT":
                return await self._write_provider(request, pid)
            if method == "DELETE":
                # 最后一个 provider 删掉后服务无能力，但这是用户的明确操作
                self.ctx.gateway.reload()
                removed = store.delete_provider(pid)
                if removed:
                    self.ctx.gateway.reload()
                return self._ok({"removed": removed})
            if len(parts) >= 2:
                action = parts[1]
                spec = store.get_provider(pid)
                if spec is None:
                    return self._fail(404, "not_found", pid)
                if action == "toggle":
                    spec.enabled = bool(not spec.enabled)
                    store.upsert_provider(spec)
                    self.ctx.gateway.reload()
                    return self._ok({"enabled": spec.enabled})
                if action == "test":
                    return await self._test_provider(spec, request)
        return self._fail(404, "not_found", "/".join(parts))

    async def _write_provider(self, request: Request,
                              pid: Optional[str]) -> JSONResponse:
        body = await self._json_body(request)
        if not isinstance(body, dict):
            return self._fail(400, "bad_body", "请求体必须是对象")
        target_id = pid or str(body.get("id") or "").strip()
        if not target_id:
            return self._fail(400, "bad_id", "缺少 id")
        body["id"] = target_id
        try:
            spec = schema.ProviderConfig.from_dict(body)
        except Exception as exc:  # noqa: BLE001 —— SchemaError / ValueError 都在此收口
            return self._fail(400, "invalid", f"{type(exc).__name__}: {exc}")
        existing = self.ctx.store.get_provider(target_id)
        if pid is None and existing is not None:
            return self._fail(409, "duplicate", f"id {target_id!r} 已存在")
        # 明细页未重填密钥时保留旧值（前端传空串 = 不修改）
        if existing is not None and not spec.auth.token:
            spec.auth.token = existing.auth.token
        self.ctx.store.upsert_provider(spec)
        self.ctx.gateway.reload()
        return self._ok({"item": spec.to_dict()})

    async def _import_providers(self, request: Request) -> JSONResponse:
        body = await self._json_body(request)
        items = body.get("providers") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return self._fail(400, "bad_body", "需要一个 providers 数组")
        added, updated = 0, 0
        for raw in items:
            try:
                spec = schema.ProviderConfig.from_dict(raw)
            except Exception as exc:  # noqa: BLE001
                return self._fail(400, "invalid", f"{type(exc).__name__}: {exc}")
            existed = self.ctx.store.get_provider(spec.id) is not None
            self.ctx.store.upsert_provider(spec)
            updated += 1 if existed else 0
            added += 0 if existed else 1
        self.ctx.gateway.reload()
        return self._ok({"added": added, "updated": updated})

    async def _test_provider(self, spec: schema.ProviderConfig,
                             request: Request) -> JSONResponse:
        """联通性自检：发一次真实的最小请求（会产生一次小额额度消耗）。"""
        import asyncio

        from infra.budget import Deadline
        body = await self._json_body(request) if request.method == "POST" else {}
        kind = str(body.get("capability") or
                   (schema.CAP_IMAGE if spec.has(schema.CAP_IMAGE)
                    else schema.CAP_VIDEO))
        started = time.monotonic()
        deadline = Deadline(PROBE_DEADLINE_SEC)
        try:
            if kind == schema.CAP_IMAGE:
                result = await asyncio.wait_for(
                    self.ctx.gateway.generate_image(
                        prompt=PROBE_PROMPT, size=PROBE_SIZE,
                        deadline=deadline, provider=spec.id),
                    timeout=PROBE_DEADLINE_SEC + 2)
            else:
                result = await asyncio.wait_for(
                    self.ctx.gateway.query_video(
                        video_id=PROBE_VIDEO_ID, deadline=deadline,
                        provider=spec.id),
                    timeout=PROBE_DEADLINE_SEC + 2)
        except asyncio.TimeoutError:
            return self._fail(504, "timeout", "探测请求超时")
        except Exception as exc:  # noqa: BLE001
            return self._fail(502, "probe_failed", f"{type(exc).__name__}: {exc}")
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        payload: dict = {
            "capability": kind, "latency_ms": elapsed_ms,
            "status_code": result.status_code, "detail": result.error_detail[:400],
        }
        if kind == schema.CAP_IMAGE:
            payload["urls"] = list(result.urls)[:2]
        else:
            payload["video_status"] = result.video_status
        # 鉴权未配置导致的必然失败要单独点明（最常见的配置错误）
        if not _resolve_auth_ok(spec):
            payload["warning"] = "未配置可用密钥（留空或 env: 变量不存在）"
        return JSONResponse({
            "ok": True, "reachable": bool(result.status_code) or bool(result.ok),
            **payload,
        })

    # ------------------------------------------------------------------
    # Token CRUD
    # ------------------------------------------------------------------
    async def _tokens(self, method: str, parts: list,
                      request: Request) -> JSONResponse:
        store = self.ctx.store
        tid = parts[0] if parts else ""

        if method == "GET" and not tid:
            items = []
            for record in store.tokens:
                data = record.to_dict()
                data.pop("token", None)
                items.append(data)
            return self._ok({"items": items})
        if method == "POST" and not tid:
            body = await self._json_body(request)
            tid = str(body.get("id") or "").strip()
            if not tid:
                return self._fail(400, "bad_id", "缺少 id")
            if store.get_token(tid) is not None:
                return self._fail(409, "duplicate", f"id {tid!r} 已存在")
            record = schema.TokenRecord(
                id=tid, name=str(body.get("name") or tid),
                token=new_token(),
                enabled=bool(body.get("enabled", True)),
                default_provider=str(body.get("default_provider") or ""),
                allowed_capabilities=list(
                    body.get("allowed_capabilities") or schema.CAPABILITIES),
                max_concurrency=int(body.get("max_concurrency") or 0),
                note=str(body.get("note") or ""),
                created_at=time.time(),
            )
            store.upsert_token(record)
            return self._ok({"item": record.to_dict()}, status=201)
        record = store.get_token(tid) if tid else None
        if record is None:
            return self._fail(404, "not_found", tid)
        if method == "PUT":
            body = await self._json_body(request)
            for key in ("name", "note", "default_provider"):
                if key in body:
                    setattr(record, key, str(body[key]))
            if "enabled" in body:
                record.enabled = bool(body["enabled"])
            if "max_concurrency" in body:
                record.max_concurrency = int(body["max_concurrency"] or 0)
            if "allowed_capabilities" in body:
                record.allowed_capabilities = list(
                    body["allowed_capabilities"] or schema.CAPABILITIES)
            store.upsert_token(record)
            return self._ok({"item": record.to_dict()})
        if method == "DELETE":
            if len(store.tokens) <= 1:
                return self._fail(
                    400, "last_token",
                    "至少保留一个访问令牌（删光了 MCP 客户端就全部连不上）")
            return self._ok({"removed": store.delete_token(tid)})
        if len(parts) >= 2 and parts[1] == "rotate":
            record.token = new_token()
            store.upsert_token(record)
            return self._ok({"item": record.to_dict()})
        if method == "GET":
            return self._ok({"item": record.to_dict()})
        return self._fail(404, "not_found", "/".join(parts))

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    async def _settings(self, method: str, request: Request) -> JSONResponse:
        cfg = self.ctx.cfg
        if method == "GET":
            return self._ok({
                "hot": cfg.hot_fields(),
                "readonly": {
                    "port": cfg.port, "image_dir": cfg.image_dir,
                    "video_dir": cfg.video_dir, "store_path": cfg.store_path,
                    "total_budget_sec": cfg.total_budget_sec,
                    "circuit_hard_sec": cfg.circuit_hard_sec,
                    "circuit_soft_sec": cfg.circuit_soft_sec,
                },
                "stored": self.ctx.store.settings,
            })
        if method != "PUT":
            return self._fail(405, "method_not_allowed", method)
        patch = await self._json_body(request)
        if not isinstance(patch, dict):
            return self._fail(400, "bad_body", "需要一个对象")
        if self.ctx.apply_settings is None:
            return self._fail(503, "not_supported", "本实例不支持热更新")
        try:
            new_cfg = self.ctx.apply_settings(patch)
        except Exception as exc:  # noqa: BLE001
            return self._fail(400, "invalid", f"{type(exc).__name__}: {exc}")
        # 热更新换的是 Config 实例（frozen dataclass），必须把引用换掉，
        # 否则面板继续读旧对象 → 显示"改了没生效"。
        if new_cfg is not None:
            self.ctx.cfg = new_cfg
        self.ctx.store.set_settings(patch)
        return self._ok({"hot": self.ctx.cfg.hot_fields()})

    # ------------------------------------------------------------------
    @staticmethod
    async def _json_body(request: Request) -> Any:
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001
            return {}
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}


def _resolve_auth_ok(spec: schema.ProviderConfig) -> bool:
    """密钥是否"大概率可用"（本地配置的非空 或 env: 变量有值）。"""
    from providers.gateway import _resolve_secret
    token = _resolve_secret(spec.auth.token)
    return bool(token) or spec.auth.kind == schema.AUTH_NONE
