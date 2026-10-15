"""管理面板 API 门禁：初始化 / 登录 / CSRF / CRUD / 热更新。

用纯 asyncio 方式直接驱动 AdminAPI（不走 TestClient）——面板与 MCP 服务
共享同一个事件循环，用线程跑 HTTP 测试会把 asyncio 原语带到另一个循环上，
测出来的东西没有意义。
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.requests import Request
from starlette.responses import Response

from admin.api import AdminAPI, AdminContext
from admin.auth import SessionManager, WRITE_HEADER
from conftest import make_config
from providers import schema
from providers.store import Store
from server import Components

PREFIX = "/admin"


# ---------------------------------------------------------------------------
# 驱动辅助
# ---------------------------------------------------------------------------
def _request(method: str, rest: str, *, body=None, headers=None,
             query: str = "", page: bool = False) -> Request:
    """page=True → 请求面板页面本体（rest 为空，不进 /api 子树）。"""
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1"))
           for k, v in (headers or {}).items()]
    tail = "" if page else f"/api/{rest}"
    scope = {
        "type": "http",
        "method": method,
        "path": f"{PREFIX}{tail}",
        "path_params": {"rest": "" if page else f"api/{rest}"},
        "headers": raw,
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    return Request(scope, receive)


def _cookie_header(sid: str) -> dict:
    return {"cookie": f"mi_admin={sid}"}


def _write_headers(sid: str) -> dict:
    return {**_cookie_header(sid), WRITE_HEADER: "1"}


async def _call(api: AdminAPI, method: str, rest: str, **kw) -> "tuple[int, dict]":
    response = await api.handle(_request(method, rest, **kw))
    payload = json.loads(response.body or b"{}")
    return response.status_code, payload


async def _call_page(api: AdminAPI) -> Response:
    return await api.handle(_request("GET", "", page=True))


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    cfg = make_config(
        image_dir=str(tmp_path / "images"),
        video_dir=str(tmp_path / "videos"),
        store_path=str(tmp_path / "store.json"),
    )
    store = Store(cfg.store_path)
    store.load()
    components = Components.build(cfg, store=store)
    api = AdminAPI(AdminContext(
        cfg=components.cfg, store=store, gateway=components.gateway,
        pool=components.pool, video_pipeline=components.video_pipeline,
        persist=components.persist, sessions=components.sessions,
        metrics=components.metrics, ring=components.ring,
        apply_settings=components.apply_settings, version="test",
    ), prefix=PREFIX)
    return {"cfg": cfg, "store": store, "api": api,
            "sessions": components.sessions, "components": components}


# ---------------------------------------------------------------------------
# 初始化与登录
# ---------------------------------------------------------------------------
async def test_state_before_init(env):
    status, body = await _call(env["api"], "GET", "state")
    assert status == 200
    assert body["initialized"] is False
    assert body["authenticated"] is False


async def test_setup_rejects_weak_password(env):
    status, body = await _call(env["api"], "POST", "setup",
                               body=json.dumps({"password": "123"}).encode())
    assert status == 400
    assert body["error"] == "weak_password"


async def test_setup_then_login_roundtrip(env):
    api = env["api"]
    status, _ = await _call(api, "POST", "setup",
                            body=json.dumps({"password": "good-pass"}).encode())
    assert status == 200
    assert env["store"].admin_ready is True

    status, body = await _call(api, "POST", "auth/login",
                               body=json.dumps({"password": "wrong"}).encode())
    assert status == 401 and body["error"] == "bad_credentials"

    response = await api.handle(_request(
        "POST", "auth/login",
        body=json.dumps({"password": "good-pass"}).encode()))
    assert response.status_code == 200
    cookies = [v for k, v in response.raw_headers if k == b"set-cookie"]
    assert cookies and b"HttpOnly" in cookies[0] and b"SameSite=strict" in cookies[0]


async def test_setup_twice_is_conflict(env):
    env["store"].set_admin_password("good-pass")
    status, body = await _call(env["api"], "POST", "setup",
                               body=json.dumps({"password": "good-pass"}).encode())
    assert status == 409


async def test_unauthenticated_read_is_rejected(env):
    status, body = await _call(env["api"], "GET", "dashboard")
    assert status == 401
    assert body["error"] == "unauthorized"


async def test_write_without_csrf_header_is_forbidden(env):
    sid = env["sessions"].issue()
    status, body = await _call(
        env["api"], "POST", "providers",
        headers=_cookie_header(sid),
        body=json.dumps({"id": "p"}).encode())
    assert status == 403 and body["error"] == "csrf"


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------
async def test_provider_crud_lifecycle(env):
    api, sid = env["api"], env["sessions"].issue()
    spec = {
        "id": "demo", "name": "Demo", "enabled": True,
        "base_url": "https://api.example.com",
        "auth": {"kind": "bearer", "token": "sk-demo-0123456789"},
        "capabilities": {
            "image": {"enabled": True, "model": "img-1",
                      "request": {"method": "POST", "path": "/v1/images",
                                  "body": {"prompt": "{prompt}"}},
                      "extract": {"urls": "data[].url"}},
        },
    }
    status, body = await _call(api, "POST", "providers", headers=_write_headers(sid),
                               body=json.dumps(spec).encode())
    assert status == 200 and body["item"]["id"] == "demo"

    # 列表脱敏：密钥不出现在列表里
    status, body = await _call(api, "GET", "providers", headers=_cookie_header(sid))
    assert body["items"][0]["auth"].get("token") is None
    assert body["items"][0]["auth"]["token_set"] is True
    assert body["items"][0]["auth"]["token_preview"].startswith("sk-")

    # 详情给明文（编辑页要用）
    status, body = await _call(api, "GET", "providers/demo",
                               headers=_cookie_header(sid))
    assert body["item"]["auth"]["token"] == "sk-demo-0123456789"

    # 更新：密钥留空 = 不修改
    spec2 = dict(spec, auth={"kind": "bearer", "token": ""}, name="Demo2")
    await _call(api, "PUT", "providers/demo", headers=_write_headers(sid),
                body=json.dumps(spec2).encode())
    status, body = await _call(api, "GET", "providers/demo",
                               headers=_cookie_header(sid))
    assert body["item"]["name"] == "Demo2"
    assert body["item"]["auth"]["token"] == "sk-demo-0123456789"

    # 启停
    status, body = await _call(api, "POST", "providers/demo/toggle",
                              headers=_write_headers(sid), body=b"{}")
    assert body["enabled"] is False

    # 删除
    status, body = await _call(api, "DELETE", "providers/demo",
                               headers=_write_headers(sid))
    assert body["removed"] is True
    status, body = await _call(api, "GET", "providers", headers=_cookie_header(sid))
    assert body["items"] == []


async def test_duplicate_provider_id_conflict(env):
    api, sid = env["api"], env["sessions"].issue()
    payload = json.dumps({"id": "dup", "base_url": "https://x"}).encode()
    await _call(api, "POST", "providers", headers=_write_headers(sid), body=payload)
    status, body = await _call(api, "POST", "providers",
                               headers=_write_headers(sid), body=payload)
    assert status == 409 and body["error"] == "duplicate"


async def test_invalid_provider_rejected(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(
        api, "POST", "providers", headers=_write_headers(sid),
        body=json.dumps({"id": "bad", "base_url": "ftp://nope"}).encode())
    assert status == 400 and body["error"] == "invalid"


async def test_export_and_import(env):
    api, sid = env["api"], env["sessions"].issue()
    await _call(api, "POST", "providers", headers=_write_headers(sid),
                body=json.dumps({"id": "ex", "base_url": "https://x"}).encode())
    status, body = await _call(api, "GET", "providers/export",
                               headers=_cookie_header(sid))
    assert [p["id"] for p in body["providers"]] == ["ex"]
    status, body = await _call(
        api, "POST", "providers/import", headers=_write_headers(sid),
        body=json.dumps({"providers": [
            {"id": "ex", "base_url": "https://y"},
            {"id": "new", "base_url": "https://z"},
        ]}).encode())
    assert body["added"] == 1 and body["updated"] == 1


# ---------------------------------------------------------------------------
# 令牌 CRUD
# ---------------------------------------------------------------------------
async def test_token_crud_and_rotation(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(
        api, "POST", "tokens", headers=_write_headers(sid),
        body=json.dumps({"id": "laptop", "name": "笔记本",
                         "max_concurrency": 2}).encode())
    assert status == 201
    token = body["item"]["token"]
    assert len(token) > 20

    # 列表不含明文
    status, body = await _call(api, "GET", "tokens", headers=_cookie_header(sid))
    assert all("token" not in item for item in body["items"])

    # 改名与配额
    await _call(api, "PUT", "tokens/laptop", headers=_write_headers(sid),
                body=json.dumps({"name": "新名字", "max_concurrency": 5}).encode())
    status, body = await _call(api, "GET", "tokens/laptop",
                               headers=_cookie_header(sid))
    assert body["item"]["name"] == "新名字"
    assert body["item"]["max_concurrency"] == 5

    # 轮换
    status, body = await _call(api, "POST", "tokens/laptop/rotate",
                               headers=_write_headers(sid), body=b"{}")
    assert body["item"]["token"] != token

    # 删除（最后一个令牌不允许删）
    status, body = await _call(api, "DELETE", "tokens/laptop",
                               headers=_write_headers(sid))
    assert status == 400 and body["error"] == "last_token"


async def test_find_token_by_secret_after_create(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(api, "POST", "tokens", headers=_write_headers(sid),
                               body=json.dumps({"id": "t"}).encode())
    token = body["item"]["token"]
    assert env["store"].find_token_by_secret(token).id == "t"


# ---------------------------------------------------------------------------
# 其它页面
# ---------------------------------------------------------------------------
async def test_dashboard_shape(env):
    api, sid = env["api"], env["sessions"].issue()
    await _call(api, "POST", "providers", headers=_write_headers(sid),
                body=json.dumps({
                    "id": "d1", "base_url": "https://x",
                    "capabilities": {"image": {
                        "enabled": True,
                        "request": {"method": "POST", "path": "/v1/images"},
                        "extract": {}}},
                }).encode())
    status, body = await _call(api, "GET", "dashboard", headers=_cookie_header(sid))
    assert status == 200
    assert body["counts"]["providers"] == 1
    assert body["capabilities"]["image"] is True
    assert body["capabilities"]["video"] is False
    assert "latency_ms" in body["metrics"]
    assert "image" in body["lanes"]


async def test_hot_settings_update(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(api, "GET", "settings", headers=_cookie_header(sid))
    assert body["hot"]["image_max_concurrency"] == env["cfg"].image_max_concurrency
    assert body["readonly"]["port"] == env["cfg"].port

    status, body = await _call(
        api, "PUT", "settings", headers=_write_headers(sid),
        body=json.dumps({"image_max_concurrency": 7}).encode())
    assert status == 200
    assert body["hot"]["image_max_concurrency"] == 7
    assert env["components"].cfg.image_max_concurrency == 7
    assert env["components"].pool.lane("image").size == 7   # 泳道同步重建
    assert env["store"].get_setting("image_max_concurrency") == 7   # 已持久化


async def test_invalid_hot_setting_rejected(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(
        api, "PUT", "settings", headers=_write_headers(sid),
        body=json.dumps({"port": 9999}).encode())
    assert status == 400 and body["error"] == "invalid"


async def test_tasks_and_logs_endpoints(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(api, "GET", "tasks", query="limit=5",
                               headers=_cookie_header(sid))
    assert status == 200 and body["items"] == []
    status, body = await _call(api, "GET", "logs", query="limit=5",
                               headers=_cookie_header(sid))
    assert status == 200 and "items" in body


async def test_unknown_api_returns_404(env):
    api, sid = env["api"], env["sessions"].issue()
    status, body = await _call(api, "GET", "nope", headers=_cookie_header(sid))
    assert status == 404 and body["error"] == "not_found"


async def test_page_html_is_served(env):
    api = env["api"]
    response = await _call_page(api)
    # rest 为空 → 返回页面（scope 里 path_params 需为空）
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "{{PREFIX}}" not in response.body.decode("utf-8")  # 占位符已替换


# ---------------------------------------------------------------------------
# 装配层回归：面板必须是一个真正的 ASGI 应用
# ---------------------------------------------------------------------------
async def test_admin_is_real_asgi_app_when_mounted(env):
    """挂在 Starlette 路由上时，面板必须是三参 ASGI。

    真实 bug 回归：Starlette 的 Route 只对「函数/方法」做 request_response
    包装，**类实例会被原样当 ASGI app** 以 (scope, receive, send) 调用。
    只实现 handle(Request) 时，单测（直接调 handle）全绿，但线上 /admin 与
    /admin/api/* 一律 TypeError——异常发生在路由层，单测看不见。
    """
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    api, sid = env["api"], env["sessions"].issue()
    app = Starlette(routes=[
        Route(PREFIX, api, methods=["GET"]),
        Route(PREFIX + "/{rest:path}", api,
              methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ])
    cookies = {"cookie": f"mi_admin={sid}"}
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://panel") as client:
        page = await client.get("/admin")
        state = await client.get("/admin/api/state")
        listed = await client.get("/admin/api/providers", headers=cookies)

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert state.status_code == 200 and state.json()["ok"] is True
    assert listed.status_code == 200 and "items" in listed.json()


def test_admin_page_script_is_syntactically_valid():
    """管理面板内联 JS 必须语法合法（白屏回归）。

    背景：2026-10-15 用户报 /admin 打开空白。根因是 openImport() 里
    textarea 的 placeholder 属性用单引号，而整段 HTML 又被包在 JS 的
    单引号字符串里 → JS 字符串提前闭合 → 整个 <script> 语法错误 →
    浏览器拒绝执行 → 白屏。**Python 侧完全正常**（返回 200 + 58KB HTML），
    只有真实浏览器才暴露，pytest 无法覆盖。

    做法：把内联 JS 落到临时文件交给 node --check（真实 JS 语法器）。
    环境无 node 时跳过——不能因为缺工具就放行。
    """
    import re
    import shutil
    import subprocess
    import tempfile

    from admin.page import PAGE_HTML

    m = re.search(r"<script>([\s\S]*?)</script>", PAGE_HTML)
    assert m, "页面里应内联 <script>"
    code = m.group(1)
    assert len(code) > 1000, "内联脚本不应为空"

    # 关键入口必须存在，否则页面无交互
    for fn in ("function bootstrap", "function render(", "function openImport"):
        assert fn in code, f"缺少关键函数 {fn}"

    node = shutil.which("node")
    if not node:
        pytest.skip("无 node，跳过 JS 语法校验")

    # 包一层 async IIFE：页面脚本用了顶层 await
    with tempfile.TemporaryDirectory() as td:
        js = os.path.join(td, "page.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write("(async () => {\n" + code + "\n})();\n")
        r = subprocess.run([node, "--check", js], capture_output=True,
                           text=True)
        assert r.returncode == 0, (
            "管理面板内联 JS 语法错误（浏览器会白屏）：\n"
            + (r.stderr or "").strip()[:500]
        )
