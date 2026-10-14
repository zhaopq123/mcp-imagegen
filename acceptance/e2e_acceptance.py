#!/usr/bin/env python3
"""S5 端到端验收脚本（V2 全量三工具版；方案 V2 四节 31 项映射）。

驱动方式：真实 uvicorn（127.0.0.1 临时端口）+ 真实 MCP streamable-http
协议（initialize → tools/list → tools/call），上游由本地 mock 提供方代替
（生图/视频提交/视频查询三端点，VideoScript 分发 mock）与"persist
图片/视频下载"（注入脚本式/挂起式 mock transport）。

V2 场景映射（编号对齐方案 V2 四节 31 项验收清单）：
    ⑲ /healthz | ⑫ 错 Token 401 | ⑬ 错上游 Token 401（厂商中立文案）
    ① 握手恰 3 工具（名称集合精确匹配 + A1′/A2′/A3′ 全等）
    ㉖ 中英文→image 生成成功（route=auto + E25 三键锁定）
    ㉗ size 直传 | ㉕ url_expires | ⑥ batch=4 全成 | ㉙ batch 部分成功
    ⑩ 审核 400 | ⑪附 越界 invalid_params | ㉒ 10 连败槽不泄漏
    ⑨ 429 → OPEN_HARD → E24 快失败 → 半开探针恢复（改写）
    ⑪ 软熔断半开探针失败续期（改写：无切换，E19）
    ㉓ persist 成功/降级（图）| ㊟ 视频 persist 成功/降级
    ㉛ 视频同步完成返可播放 URL+meta | ㊝ query 不存在 task_id
    ㊞ 视频失败分类不切换（failed/400/500 三态）
    ㊜ 超预算 running → query_video 续接 | ⑳ 视频侧挂起（read 档先响）
    ㊱ 任务表 TTL 过期（TASK_TTL_SEC 调小）
    ⑳ 慢死路径 / ㉑ read 动态（--fast 跳过；代码保留）

用法：
    python acceptance/e2e_acceptance.py --fast   # 快速档（约 90 秒）
    python acceptance/e2e_acceptance.py          # 含 ⑳㉑ 长耗时项（约 5 分钟）

退出码：0 = 全部通过；1 = 存在失败项（零 bug 标准）。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import tool as tool_mod  # noqa: E402
from config import load_config  # noqa: E402
from server import Components, build_app  # noqa: E402

MCP_TOKEN = "e2e-mcp-token"
UPSTREAM_TOKEN = "e2e-upstream-token"
PROTOCOL_VERSION = "2025-06-18"

# ---------------------------------------------------------------------------
# 证据收集
# ---------------------------------------------------------------------------
RESULTS: list[dict] = []


def record(item: str, name: str, ok: bool, evidence: str) -> None:
    RESULTS.append(
        {"item": item, "name": name, "ok": ok, "evidence": evidence.strip()}
    )
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] 验收{item} {name}")
    for line in evidence.strip().splitlines():
        print(f"    {line}")
    print()


class LogTap(logging.Handler):
    """捕获 mcp_imagegen.* 全部日志（访问行/persist WARN 等 → 证据）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# ---------------------------------------------------------------------------
# mock 传输层（自包含，勿依赖 pytest）
# ---------------------------------------------------------------------------
def ok_response(urls=("https://img.example/a.png",)) -> httpx.Response:
    return httpx.Response(200, json={"images": [{"url": u} for u in urls]})


def status_response(status: int, body: str = "err") -> httpx.Response:
    return httpx.Response(status, text=body)


class Script(httpx.AsyncBaseTransport):
    """脚本式上游：按序返回预设响应；耗尽后返回 default（默认 200）。"""

    def __init__(self, script: list | None = None, default=None) -> None:
        self.script: list = list(script or [])
        self.default = default
        self.requests: list[httpx.Request] = []

    def push(self, item) -> None:
        self.script.append(item)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            item = httpx.Response(
                200, json={"images": [{"url": "https://img.example/default.png"}]},
                request=request,
            )
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        return item


class NeverRelease(httpx.AsyncBaseTransport):
    """永久挂起上游（慢死路径）：read 超时到期抛 httpx.ReadTimeout。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._never = asyncio.Event()  # 永不 set

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        spec = (request.extensions or {}).get("timeout") or {}
        read = spec.get("read") if isinstance(spec, dict) else None
        try:
            await asyncio.wait_for(self._never.wait(),
                                   timeout=float(read) if read else None)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout(
                f"Read timed out after {read:.3f}s（e2e mock 模拟 httpcore）",
                request=request,
            )
        raise AssertionError("unreachable")


class VideoScript(Script):
    """三端点分发 mock（V2 T10）：生图/视频提交/视频查询各自脚本。

    - POST /v1/images/generations → 沿用父类 Script 队列（生图场景兼容）；
    - POST /v1/videos → video_script 队列（dict=200 JSON；耗尽→自增
      video_id 200，如 vid-e2e-001）；
    - GET /v1/videos/query?video_id=… → queries[video_id] 队列（dict=200 JSON；
      HANG 哨兵=挂起至 read 超时；耗尽→{"status": "running"}）。
    requests 三端点统一记录（环境内全局共享）。
    """

    HANG = object()  # 查询挂起哨兵（视频侧慢死路径 mock）

    def __init__(self) -> None:
        super().__init__()
        self.video_script: list = []
        self.queries: dict[str, list] = {}
        self._seq = 0

    def push_video(self, item) -> None:
        self.video_script.append(item)

    def push_query(self, video_id: str, item) -> None:
        self.queries.setdefault(video_id, []).append(item)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/videos":
            self.requests.append(request)
            if self.video_script:
                item = self.video_script.pop(0)
            else:
                self._seq += 1
                item = {"video_id": f"vid-e2e-{self._seq:03d}"}
            return self._materialize(item, request)
        if path == "/v1/videos/query":
            self.requests.append(request)
            vid = request.url.params.get("video_id", "")
            queue = self.queries.get(vid)
            item = queue.pop(0) if queue else {"status": "running"}
            if item is VideoScript.HANG:
                return await self._hang(request)
            return self._materialize(item, request)
        return await super().handle_async_request(request)

    def _materialize(self, item, request: httpx.Request) -> httpx.Response:
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item, request=request)

    @staticmethod
    async def _hang(request: httpx.Request) -> httpx.Response:
        """挂起至 read 超时（与 NeverRelease 同纪律，作用于查询端点）。"""
        spec = (request.extensions or {}).get("timeout") or {}
        read = spec.get("read") if isinstance(spec, dict) else None
        try:
            await asyncio.wait_for(asyncio.Event().wait(),
                                   timeout=float(read) if read else None)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout(
                f"Read timed out after {read:.3f}s（e2e mock 模拟 httpcore）",
                request=request,
            )
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# 环境装配
# ---------------------------------------------------------------------------
class Env:
    def __init__(self, base_url: str, client: httpx.AsyncClient,
                 components: Components, api, persist_transport,
                 logtap: LogTap, tmpdir: Path) -> None:
        self.base = base_url
        self.client = client
        self.components = components
        self.api = api
        self.persist_transport = persist_transport
        self.log = logtap
        self.tmpdir = tmpdir
        self.session_id: str | None = None


async def _reap_sse_starlette_state() -> None:
    """每个"模拟进程"边界复位 sse_starlette 进程级停机状态（详见 V1 验收报告）。"""
    from sse_starlette import sse as _sse_mod

    current = asyncio.current_task()
    stale = [
        t for t in asyncio.all_tasks()
        if t is not current
        and getattr(t.get_coro(), "__qualname__", "") == "_shutdown_watcher"
    ]
    for t in stale:
        t.cancel()
    for t in stale:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    _sse_mod.AppStatus.should_exit = False
    try:
        _sse_mod._get_shutdown_state().watcher_started = False
    except Exception:  # pragma: no cover
        pass


@contextlib.asynccontextmanager
async def spawn_env(*, cfg_overrides: dict | None = None, api=None,
                    persist_transport=None):
    """起一套完整服务（独立端口/独立组件状态，场景间互不污染）。"""
    await _reap_sse_starlette_state()
    tmpdir = Path(tempfile.mkdtemp(prefix="mcp-e2e-"))
    env_map = {
        "MCPACCESS_TOKEN": MCP_TOKEN,
        "UPSTREAM_TOKEN": UPSTREAM_TOKEN,
        "IMAGE_DIR": str(tmpdir / "images"),
        "PORT": "0",
    }
    for k, v in (cfg_overrides or {}).items():
        env_map[k.upper()] = str(v)
    cfg = load_config(env_map)
    (tmpdir / "images").mkdir(parents=True, exist_ok=True)

    api = api if api is not None else Script()
    components = Components.build(
        cfg, transport=api, persist_transport=persist_transport,
    )
    app = build_app(cfg, components)

    uconfig = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning",
        access_log=False, lifespan="on",
    )
    server = uvicorn.Server(uconfig)
    task = asyncio.create_task(server.serve())
    for _ in range(400):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn 未能启动"
    port = server.servers[0].sockets[0].getsockname()[1]

    logtap = LogTap()
    _pkg_logger = logging.getLogger("mcp_imagegen")
    _pkg_logger.setLevel(logging.INFO)
    _pkg_logger.addHandler(logtap)
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        timeout=httpx.Timeout(connect=10, read=240, write=30, pool=30),
    )
    try:
        yield Env(client.base_url, client, components, api, persist_transport,
                  logtap, tmpdir)
    finally:
        logging.getLogger("mcp_imagegen").removeHandler(logtap)
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)


# ---------------------------------------------------------------------------
# MCP streamable-http 客户端（协议级，最小实现）
# ---------------------------------------------------------------------------
def _json_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _parse_rpc(response: httpx.Response, msg_id: int) -> dict:
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        events = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
        for ev in events:
            if isinstance(ev, dict) and ev.get("id") == msg_id:
                return ev
        raise AssertionError(f"SSE 中未找到 id={msg_id} 的响应：{events}")
    body = response.json()
    if isinstance(body, dict) and body.get("id") == msg_id:
        return body
    if isinstance(body, list):
        for ev in body:
            if isinstance(ev, dict) and ev.get("id") == msg_id:
                return ev
    raise AssertionError(f"非预期响应形态：{body!r}")


async def mcp_initialize(env: Env) -> dict:
    resp = await env.client.post("/mcp", headers=_json_headers(MCP_TOKEN), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "e2e-acceptance", "version": "1.0"},
        },
    })
    assert resp.status_code == 200, f"initialize HTTP {resp.status_code}: {resp.text[:300]}"
    env.session_id = resp.headers.get("mcp-session-id")
    payload = _parse_rpc(resp, 1)
    assert "error" not in payload, f"initialize 报错: {payload}"
    await env.client.post("/mcp", headers=_json_headers(MCP_TOKEN), json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    return payload["result"]


_RPC_ID = 10


async def mcp_rpc(env: Env, method: str, params: dict | None = None) -> dict:
    global _RPC_ID
    _RPC_ID += 1
    headers = _json_headers(MCP_TOKEN)
    if env.session_id:
        headers["mcp-session-id"] = env.session_id
    body = {"jsonrpc": "2.0", "id": _RPC_ID, "method": method}
    if params is not None:
        body["params"] = params
    resp = await env.client.post("/mcp", headers=headers, json=body)
    assert resp.status_code == 200, f"{method} HTTP {resp.status_code}: {resp.text[:300]}"
    payload = _parse_rpc(resp, _RPC_ID)
    assert "error" not in payload, f"{method} JSON-RPC 错误: {payload}"
    return payload["result"]


async def call_tool_named(env: Env, name: str, **arguments) -> str:
    result = await mcp_rpc(env, "tools/call", {
        "name": name, "arguments": arguments,
    })
    assert result.get("isError") is not True, f"工具 {name} isError: {result}"
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    assert len(texts) == 1, f"期望恰好一段文本内容: {result}"
    return texts[0]


async def call_tool(env: Env, **arguments) -> str:
    return await call_tool_named(env, "generate_image", **arguments)


async def call_video(env: Env, **arguments) -> str:
    return await call_tool_named(env, "generate_video", **arguments)


async def call_query_video(env: Env, task_id: str) -> str:
    return await call_tool_named(env, "query_video", task_id=task_id)


def upstream_payload(env: Env, idx: int = -1) -> dict:
    return json.loads(env.api.requests[idx].content)


def access_line(env: Env, since: int) -> str:
    lines = [ln for ln in env.log.lines[since:] if "req_id=" in ln and "route=" in ln]
    assert lines, f"未捕获访问日志行：{env.log.lines[since:]}"
    return lines[-1]


# ===========================================================================
# 场景（每函数一个验收项；断言失败即该项 FAIL，不中断其余项）
# ===========================================================================
async def s_healthz(env: Env) -> None:
    """⑲ /healthz：无鉴权探活，返回 200（C7：不参与僵死判据）。"""
    resp = await env.client.get("/healthz")
    ok = resp.status_code == 200 and resp.json() == {"status": "ok"}
    record("⑲", "/healthz 探活 200", ok,
           f"GET /healthz → {resp.status_code} {resp.text}")


async def s_auth_401(env: Env) -> None:
    """⑫ 错误 Token → HTTP 401 + 返回体即 [error] mcp_auth 契约行。"""
    resp = await env.client.post("/mcp", headers=_json_headers("wrong-token"), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "e2e", "version": "1.0"}},
    })
    expected = ("[error] category=mcp_auth retry=no "
                "reason=MCP 层鉴权失败(401) suggestion=请联系管理员检查 MCPACCESS_TOKEN")
    body = resp.text.strip()
    ok = (resp.status_code == 401 and body == expected
          and resp.headers.get("www-authenticate") == "Bearer")
    record("⑫", "错 Token 401 + mcp_auth 契约行", ok, "\n".join([
        f"HTTP {resp.status_code}  www-authenticate={resp.headers.get('www-authenticate')}",
        f"body: {body}",
        f"expected: {expected}",
    ]))


async def s_handshake(env: Env) -> None:
    """① MCP 握手：tools/list 恰 3 工具（名称集合精确匹配）；
    A1′/A2′/A3′ 三段 description 全等；Q1=A 四参数 / task_id 单参数 schema。"""
    result = await mcp_initialize(env)
    assert result["serverInfo"]["name"] == "mcp-imagegen", result
    tools = (await mcp_rpc(env, "tools/list", {}))["tools"]
    names = [t["name"] for t in tools]
    assert len(tools) == 3 and set(names) == {
        "generate_image", "generate_video", "query_video",
    }, f"应恰 3 工具（T8 三工具注册）: {names}"
    by = {t["name"]: t for t in tools}
    gi, gv, qv = by["generate_image"], by["generate_video"], by["query_video"]
    gi_names = tuple(gi["inputSchema"]["properties"].keys())
    gv_names = tuple(gv["inputSchema"]["properties"].keys())
    qv_names = tuple(qv["inputSchema"]["properties"].keys())
    ok = (
        gi["description"] == tool_mod.TOOL_DESCRIPTION
        and gi_names == tool_mod.TOOL_PARAMS
        and gi["inputSchema"].get("required") == ["prompt"]
        and gv["description"] == tool_mod.TOOL_DESCRIPTION_VIDEO
        and gv_names == tool_mod.TOOL_PARAMS_VIDEO
        and gv["inputSchema"].get("required") == ["prompt"]
        and qv["description"] == tool_mod.TOOL_DESCRIPTION_QUERY
        and qv_names == ("task_id",)
        and qv["inputSchema"].get("required") == ["task_id"]
    )
    record("①", "握手三工具 + A1′/A2′/A3′ 全等", ok, "\n".join([
        f"serverInfo.name = {result['serverInfo']['name']}",
        f"protocolVersion = {result.get('protocolVersion')}",
        f"session_id = {str(env.session_id)[:8]}…",
        f"tools = {names}（恰 3，名称集合精确匹配）",
        f"generate_image: A1′ 全等="
        f"{gi['description'] == tool_mod.TOOL_DESCRIPTION}；"
        f"params({len(gi_names)}) = {', '.join(gi_names)}",
        f"generate_video: A2′ 全等="
        f"{gv['description'] == tool_mod.TOOL_DESCRIPTION_VIDEO}；"
        f"params({len(gv_names)}) = {', '.join(gv_names)}",
        f"query_video: A3′ 全等="
        f"{qv['description'] == tool_mod.TOOL_DESCRIPTION_QUERY}；"
        f"params = task_id（必填单参数）",
    ]))


async def s_single_model_and_e25(env: Env) -> None:
    """㉖ 中英文 prompt → image 生成成功（Q1=A 无 model 参数，auto
    直通即显式 image）；E25 payload 三键锁定。"""
    for prompt in ("一只戴墨镜的橘猫，赛博朋克风格", "a cyberpunk orange cat"):
        env.api.push(ok_response())
        since = len(env.log.lines)
        text = await call_tool(env, prompt=prompt)
        line = access_line(env, since)
        payload = upstream_payload(env)
        meta = text.splitlines()[-1]
        ok = ("model=image" in meta and "route=auto" in line
              and set(payload.keys()) == {"model", "prompt", "size"}
              and payload["model"] == "example-image-1"
              and len(env.log.lines[since:]) == 1)
        assert ok, f"E25/单模型断言失败: prompt={prompt!r} payload={payload}"
    record("㉖", "中英文→image 生成成功 + E25 三键", True, "\n".join([
        "中文/英文 prompt 均 route=auto → model=image（Q1=A：无 model 参数，"
        "直通唯一生图模型）",
        f"上游 payload 键集 = {set(upstream_payload(env).keys())}（E25：仅 model/prompt/size，"
        "quality/steps/guidance/seed 一律不注入）",
        f"真实模型 id = {upstream_payload(env)['model']}（别名映射）",
        f"访问日志恰好 1 行: ✓",
    ]))


async def s_upstream_auth_401(env: Env) -> None:
    """⑬ 错 UPSTREAM_TOKEN → 上游 401 → [error] upstream_auth（retry=no，
    文案只指向管理面板，不归属任何厂商）；C4 零重试（恰 1 次上游调用）。"""
    before = len(env.api.requests)
    env.api.push(httpx.Response(
        401, json={"error": {"code": "invalid_api_key",
                             "message": "Invalid token (req 3998)",
                             "type": "authentication_error"}}))
    text = await call_tool(env, prompt="a cat with a bad upstream token")
    calls = len(env.api.requests) - before
    ok = ("[error] category=upstream_auth" in text
          and "retry=no" in text
          and "provider=" not in text
          and "UPSTREAM_TOKEN" in text
          and calls == 1)
    record("⑬", "错上游 Token 401（厂商中立文案）", ok, "\n".join([
        f"返回体: {text}",
        f"上游调用次数 = {calls}（C4：401 零重试，E19 无切换）",
        "错误体 error.message 透传（内含请求 id，便于对账）",
    ]))


async def s_size_passthrough(env: Env) -> None:
    """㉗ size 直传：768x1344 原样到上游 payload（E12：无吸附）。"""
    env.api.push(ok_response())
    text = await call_tool(env, prompt="a tall cat", size="768x1344")
    payload = upstream_payload(env)
    meta = text.splitlines()[-1]
    ok = (payload["size"] == "768x1344" and "size=768x1344" in meta
          and payload["size"] == "768x1344")
    record("㉗", "size 直传（无吸附）", ok, "\n".join([
        f"返回体: {text}",
        f"上游 payload size = {payload['size']}（原样直传，无档位吸附）",
    ]))


async def s_url_expires(env: Env) -> None:
    """㉕ 不 persist → meta url_expires=1h（E13 短效警示）。"""
    env.api.push(ok_response())
    text = await call_tool(env, prompt="an ephemeral cat", persist=False)
    meta = text.splitlines()[-1]
    ok = "url_expires=1h" in meta and "persisted" not in meta
    record("㉕", "不 persist → url_expires=1h", ok, "\n".join([
        f"返回体: {text}",
        f"meta 短效警示存在: {'url_expires=1h' in meta}（E13：上游未标注时效按短效对待）",
    ]))


async def s_batch_split(env: Env) -> None:
    """⑥ batch=4 → 服务端拆分 4 次串行调用；4 行 URL + meta（无原生 batch）。"""
    before = len(env.api.requests)
    for i in range(4):
        env.api.push(ok_response((f"https://img.example/b{i}.png",)))
    text = await call_tool(env, prompt="four cats", batch_size=4)
    lines = text.splitlines()
    url_lines, meta = lines[:-1], lines[-1]
    calls = len(env.api.requests) - before  # 增量（env 全局 mock 共享）
    payloads = [json.loads(r.content) for r in env.api.requests[before:]]
    ok = (calls == 4 and len(url_lines) == 4
          and all(f"https://img.example/b{i}.png" in url_lines for i in range(4))
          and "model=image" in meta
          and "requested=" not in meta  # 全成功：无部分成功标记（R17）
          and all(p.get("size") == "1024x1024" for p in payloads)
          and all("batch_size" not in p for p in payloads))
    record("⑥", "batch=4 拆分串行 4 调用", ok, "\n".join([
        f"上游调用 = {calls} 次（拆分串行，每次 1 张）",
        f"返回体（{len(url_lines)} 行 URL + 1 行 meta）:",
        text,
        "每次 payload 均无 batch_size 字段（单张语义）",
        "meta 无 requested/delivered（全成功不走部分成功契约）",
    ]))


async def s_batch_partial(env: Env) -> None:
    """㉙ R17 部分成功：batch=2 拆分串行 1 成 1 败 → 1 行 URL +
    meta requested=2 delivered=1（非错误）；再证单次失败不中断后续
    （batch=3，第 2 次 400 后第 3 次仍发起且成功）。"""
    # (a) 1 成 1 败 → requested=2 delivered=1
    before = len(env.api.requests)
    env.api.push(ok_response(("https://img.example/p1.png",)))
    env.api.push(status_response(400, "rejected"))
    text = await call_tool(env, prompt="two cats, one rejected", batch_size=2)
    lines = text.splitlines()
    calls = len(env.api.requests) - before
    meta = lines[-1]
    ok_a = (calls == 2 and len(lines) == 2
            and lines[0] == "https://img.example/p1.png"
            and "requested=2" in meta and "delivered=1" in meta
            and "[error]" not in text)
    # (b) 失败不中断后续：batch=3 = [成, 400, 成] → delivered=2、恰 3 次调用
    before = len(env.api.requests)
    env.api.push(ok_response(("https://img.example/q1.png",)))
    env.api.push(status_response(400, "rejected again"))
    env.api.push(ok_response(("https://img.example/q2.png",)))
    text2 = await call_tool(env, prompt="three cats, middle rejected",
                            batch_size=3)
    lines2 = text2.splitlines()
    calls2 = len(env.api.requests) - before
    ok_b = (calls2 == 3 and len(lines2) == 3
            and lines2[0] == "https://img.example/q1.png"
            and lines2[1] == "https://img.example/q2.png"
            and "requested=3" in lines2[-1] and "delivered=2" in lines2[-1]
            and "[error]" not in text2)
    record("㉙", "batch 部分成功（requested/delivered）", ok_a and ok_b, "\n".join([
        f"(a) 1 成 1 败: {text}",
        f"    上游调用 = {calls} 次",
        f"(b) 失败不中断后续: {text2}",
        f"    上游调用 = {calls2} 次（第 2 次 400 后第 3 次仍发起并成功）",
    ]))


async def s_content_policy(env: Env) -> None:
    """⑩ 内容审核 400 → 不重试、仅 1 次上游调用、[error] content_policy。"""
    env.api.push(status_response(400, "content policy violation"))
    before = len(env.api.requests)
    since = len(env.log.lines)
    text = await call_tool(env, prompt="中文敏感词测试")
    line = access_line(env, since)
    calls = len(env.api.requests) - before
    ok = ("[error] category=content_policy" in text and "retry=no" in text
          and calls == 1 and "error:content_policy" in line)
    record("⑩", "审核 400 仅 1 次 API 调用", ok, "\n".join([
        f"返回体: {text}", f"访问日志: {line}",
        f"上游调用次数 = {calls}（应恰好 1 次：C4 零重试，E19 无切换）",
    ]))


async def s_invalid_params(env: Env) -> None:
    """⑪附 A2 红线：范围越界走 [error] invalid_params 契约行（非 schema 报错）。"""
    cases = [
        ({"prompt": "cat", "batch_size": 99}, "batch_size"),
        ({"prompt": "cat", "size": "abc"}, "size"),
    ]
    outs = []
    allok = True
    for args, param in cases:
        before = len(env.api.requests)
        text = await call_tool(env, **args)
        nocall = len(env.api.requests) == before
        good = ("[error] category=invalid_params" in text
                and "retry=fixthenretry" in text and nocall)
        allok = allok and good
        outs.append(f"{param} 越界 → {text}  上游调用={len(env.api.requests) - before}")
    record("⑪附", "范围越界 → invalid_params 契约行（A2）", allok, "\n".join(outs))


async def s_ten_failures(env: Env) -> None:
    """㉒ 连续 10 次失败后第 11 次仍得槽执行（槽不泄漏，R1）。

    V2 单模型语义下选 400 审核类失败做压力源：零重试（每次恰 1 次
    上游调用）、不写熔断（C4：审核类无熔断副作用）——10 连败后第 11
    次照常得槽成功，验证槽归还无泄漏。
    （500 类连败会触发软熔断 OPEN→E24 快失败，属⑨场景语义。）
    """
    env.api.default = status_response(400, "rejected")
    err_count = 0
    upstream_calls = 0
    for i in range(10):
        before = len(env.api.requests)
        text = await call_tool(env, prompt=f"cat {i}")
        upstream_calls += len(env.api.requests) - before
        if "[error]" in text:
            err_count += 1
    env.api.default = None
    env.api.push(ok_response())
    text11 = await call_tool(env, prompt="cat 11th")
    sem_value = getattr(env.components.pool._sem, "_value", None)
    ok = (err_count == 10 and "[error]" not in text11
          and "https://img.example" in text11 and sem_value == 3)
    record("㉒", "10 次失败后第 11 次仍得槽成功", ok, "\n".join([
        f"前 10 次均结构化报错: {err_count}/10（共 {upstream_calls} 次上游调用，"
        "400 审核类零重试，每失败请求恰 1 次）",
        f"第 11 次返回体: {text11}",
        f"并发槽回收校验: semaphore 值 = {sem_value}（满额 3，无泄漏）",
    ]))


async def env_hard_circuit() -> None:
    """⑨改 429 → OPEN_HARD；auto 请求窗口内快失败 E24；到期半开探针恢复。"""
    async with spawn_env(cfg_overrides={"CIRCUITHARDSEC": 2}) as env:
        await mcp_initialize(env)
        # 429 → 零重试 1 次调用 → OPEN_HARD
        env.api.push(status_response(429, "rate limited"))
        since = len(env.log.lines)
        text = await call_tool(env, prompt="额度测试猫")
        line = access_line(env, since)
        calls = len(env.api.requests)
        ok8 = ("[error] category=rate_limit" in text and "retry=yes" in text
               and calls == 1 and "error:rate_limit" in line)
        record("⑨", "429 → 硬熔断（零重试）", ok8, "\n".join([
            f"返回体: {text}", f"访问日志: {line}",
            f"上游调用 = {calls}（429 零重试）",
            f"熔断状态: {env.components.circuit.snapshot()}",
        ]))
        # E24：OPEN 窗口内 auto 请求 → 快失败（不发上游调用）
        before = len(env.api.requests)
        since = len(env.log.lines)
        text2 = await call_tool(env, prompt="熔断窗口测试猫")
        line2 = access_line(env, since)
        nocall = len(env.api.requests) == before
        ok9a = ("[error] category=rate_limit" in text2 and "retry=yes" in text2
                and "秒后重试" in text2 and nocall
                and "route=circuit_open" in line2)
        record("⑨", "OPEN 窗口 auto 快失败（E24）", ok9a, "\n".join([
            f"返回体: {text2}", f"访问日志: {line2}",
            f"窗口内上游调用 = 0 次: {nocall}（快失败，零 IO）",
        ]))
        # 到期半开：探针请求 → 成功 → CLOSED
        await asyncio.sleep(2.6)
        env.api.push(ok_response())
        since = len(env.log.lines)
        text3 = await call_tool(env, prompt="探针恢复猫")
        line3 = access_line(env, since)
        snap = env.components.circuit.snapshot()
        ok9b = ("https://img.example" in text3 and "route=auto" in line3
                and snap.get("image") == "CLOSED")
        record("⑨", "熔断到期半开探针→成功→CLOSED", ok9b, "\n".join([
            f"返回体: {text3}", f"访问日志: {line3}",
            f"探针后熔断状态: {snap}",
        ]))


async def env_probe_renewal() -> None:
    """⑪改 软熔断半开探针失败 → 状态回 OPEN 续期一轮（E19：无切换）。"""
    async with spawn_env(cfg_overrides={"CIRCUITSOFTSEC": 2}) as env:
        await mcp_initialize(env)
        # 连败 2 次 → OPEN_SOFT（每失败请求 = 500 首调 + C4 重试 = 2 次调用）
        for _ in range(2):
            env.api.push(status_response(500, "err"))
            env.api.push(status_response(500, "err"))
            await call_tool(env, prompt="软熔断猫")
        snap1 = env.components.circuit.snapshot()
        # 到期半开 → 探针失败（500×2）→ 续期 OPEN_SOFT
        await asyncio.sleep(2.6)
        opened_before = env.components.circuit._entries["image"].opened_at
        env.api.push(status_response(500, "probe fail"))
        env.api.push(status_response(500, "probe fail"))
        text = await call_tool(env, prompt="探针失败猫")
        snap2 = env.components.circuit.snapshot()
        opened_after = env.components.circuit._entries["image"].opened_at
        # 再到期 → 探针成功 → CLOSED（证明续期未卡死半开）
        await asyncio.sleep(2.6)
        env.api.push(ok_response())
        text2 = await call_tool(env, prompt="探针成功猫")
        snap3 = env.components.circuit.snapshot()
        ok = (snap1.get("image") == "OPEN_SOFT"
              and snap2.get("image") == "OPEN_SOFT"
              and opened_after > opened_before
              and snap3.get("image") == "CLOSED"
              and "[error]" in text and "[error]" not in text2)
        record("⑪", "半开探针失败→OPEN 续期一轮", ok, "\n".join([
            f"连败 2 次后: {snap1}",
            f"半开探针失败后: {snap2}（opened_at {opened_before:.1f} → {opened_after:.1f}，续期）",
            f"再到期探针成功后: {snap3}",
        ]))


async def env_persist_ok() -> None:
    """㉓ persist 成功：/files URL + persisted；GET 取回字节一致。"""
    png = b"\x89PNG\r\n\x1a\n" + b"e2e-persist-bytes" * 8
    persist_transport = Script([
        httpx.Response(200, content=png,
                       headers={"content-type": "image/png"}),
    ])
    async with spawn_env(persist_transport=persist_transport) as env:
        await mcp_initialize(env)
        env.api.push(ok_response(("https://img.example/orig.png",)))
        text = await call_tool(env, prompt="a cat to persist", persist=True)
        meta = text.splitlines()[-1]
        url = text.splitlines()[0]
        file_ok = url.startswith("http://127.0.0.1:") and "/files/" in url
        path = url[url.index("/files/"):]
        resp = await env.client.get(path)
        same = resp.content == png
        await asyncio.sleep(1)
        resp2 = await env.client.get(path)
        disk = list((env.tmpdir / "images").glob("*"))
        ok = ("persisted" in meta and file_ok and resp.status_code == 200
              and same and resp2.status_code == 200 and len(disk) == 1)
        record("㉓", "persist 成功→/files URL + 持久可访问", ok, "\n".join([
            f"返回体: {text}",
            f"GET {path} → {resp.status_code}，字节与源一致: {same}",
            f"1 秒后再次 GET → {resp2.status_code}（文件长期保留）",
            f"落盘: {[p.name for p in disk]}",
        ]))


async def env_persist_fail() -> None:
    """㉓ persist 降级：下载失败 → 原上游 URL + persist_failed（无 [error] 行）。"""
    persist_transport = Script([status_response(502, "download fail")])
    async with spawn_env(persist_transport=persist_transport) as env:
        await mcp_initialize(env)
        env.api.push(ok_response(("https://img.example/orig2.png",)))
        since = len(env.log.lines)
        text = await call_tool(env, prompt="a cat persist fail", persist=True)
        meta = text.splitlines()[-1]
        warns = [ln for ln in env.log.lines[since:] if "persist" in ln.lower()]
        ok = (text.splitlines()[0] == "https://img.example/orig2.png"
              and "persist_failed" in meta and "[error]" not in text)
        record("㉓", "persist 失败→原URL+persist_failed 降级", ok, "\n".join([
            f"返回体: {text}",
            f"[error] 行不存在: {'[error]' not in text}（C6：额度已花，降级出图优于报错）",
            f"服务端 WARN 日志: {warns[:1]}",
        ]))


async def env_deadline_truncation() -> None:
    """⑳ 慢死路径（生图侧）：上游全挂起 → ≤200s（+护栏余量）返回结构化超时错误（非断连）。"""
    api = NeverRelease()
    async with spawn_env(api=api) as env:
        await mcp_initialize(env)
        t0 = time.monotonic()
        text = await call_tool(env, prompt="a hanging cat")
        elapsed = time.monotonic() - t0
        ok = ("[error] category=upstream_error" in text and "retry=yes" in text
              and elapsed <= 201.5)
        record("⑳", "慢死路径 ≤200s 截断（结构化超时）", ok, "\n".join([
            f"返回体: {text}",
            f"耗时: {elapsed:.1f}s（预算 200s；收到完整 JSON-RPC 结果=非断连）",
            f"上游挂起请求数: {len(api.requests)}",
        ]))


async def env_dynamic_read() -> None:
    """㉑ read 动态：注入 20s 档挂 21s → read 超时 → C4 重试 1 次成功（calls==2）。

    V2 验收轮（芳 2026-10-10）：场景原按默认 30s 档设计（挂 31s）；D5 已将
    TIMEOUT_UPSTREAM_IMAGE_SEC 默认调至 60，31s 挂起在 60s 窗口内首次即成功
    （calls=1，行为无 bug，断言口径过时）。改为显式注入 20s 档 + 挂 21s，
    回归"read 档先响 + 重试 1 次"语义，不再耦合全局默认值。
    """
    import asyncio as _aio

    class Hold21(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self._released = _aio.Event()
            _aio.get_running_loop().create_task(self._timer(21.0))

        async def _timer(self, hold: float) -> None:
            await _aio.sleep(hold)
            self._released.set()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            spec = (request.extensions or {}).get("timeout") or {}
            read = spec.get("read") if isinstance(spec, dict) else None
            try:
                await _aio.wait_for(self._released.wait(),
                                    timeout=float(read) if read else None)
            except _aio.TimeoutError:
                raise httpx.ReadTimeout("read timed out (e2e)", request=request)
            return ok_response()

    async with spawn_env(api=Hold21(),
                         cfg_overrides={"TIMEOUT_UPSTREAM_IMAGE_SEC": 20}) as env:
        await mcp_initialize(env)
        t0 = time.monotonic()
        text = await call_tool(env, prompt="slow cat")
        e = time.monotonic() - t0
        calls = len(env.api.requests)
        ok = ("[error]" not in text and calls == 2 and 20.0 <= e < 40)
        record("㉑", "read 档边界 + C4 重试 1 次（20s 档挂 21s）", ok, "\n".join([
            f"返回体: {text}",
            f"耗时 {e:.1f}s，上游调用 {calls} 次（read=20s 先响 → 重试 1 次成功）",
        ]))


# ===========================================================================
# 视频场景（V2 T10：三端点 VideoScript mock 状态机）
# ===========================================================================
async def s_video_sync_success(env: Env) -> None:
    """㉛ 视频同步完成：返回可播放上游 URL + meta（model/seconds/
    aspect_ratio/elapsed/url_expires）；E21 提交体六键锁定（seconds
    int→str、mode=text、size=720P）；查询参数 video_id/model_name 对账。"""
    env.api.push_video({"video_id": "vid-sync-001"})
    env.api.push_query("vid-sync-001",
                       {"status": "completed",
                        "url": "https://video.example/sync001.mp4"})
    since = len(env.log.lines)
    text = await call_video(env, prompt="一只冲浪的橘猫", seconds=5,
                            aspect_ratio="16:9")
    lines = text.splitlines()
    url, meta = lines[0], lines[-1]
    submit_payload = json.loads(env.api.requests[-2].content)  # [-1] 为查询 GET
    query_req = env.api.requests[-1]
    line = access_line(env, since)
    ok = (
        len(lines) == 2
        and url == "https://video.example/sync001.mp4"
        and "model=video" in meta and "seconds=5" in meta
        and "aspect_ratio=16:9" in meta and "elapsed=" in meta
        and "url_expires=1h" in meta and "persisted" not in meta
        and set(submit_payload.keys())
        == {"model", "prompt", "seconds", "mode", "size", "aspect_ratio"}
        and submit_payload["seconds"] == "5"   # E21：int → str
        and submit_payload["mode"] == "text" and submit_payload["size"] == "720P"
        and submit_payload["model"] == "example-video-1"
        and query_req.url.params["video_id"] == "vid-sync-001"
        and query_req.url.params["model_name"] == "example-video-1"
        and "task_id=vid-sync-001" in line and "result=ok" in line
        and "poll_count=1" in line
        and "[error]" not in text
    )
    record("㉛", "视频同步完成返可播放 URL+meta", ok, "\n".join([
        f"返回体: {text}",
        f"E21 提交体: {submit_payload}（seconds int→str）",
        f"查询参数: video_id={query_req.url.params['video_id']} "
        f"model_name={query_req.url.params['model_name']}",
        f"访问日志: {line}",
    ]))


async def s_video_task_not_found(env: Env) -> None:
    """㊝ query_video 不存在 task_id → task_not_found（fixthenretry；
    表未命中零上游 IO）。"""
    before = len(env.api.requests)
    since = len(env.log.lines)
    text = await call_query_video(env, "ghost-task-404")
    nocall = len(env.api.requests) == before
    line = access_line(env, since)
    ok = ("[error] category=task_not_found" in text
          and "retry=fixthenretry" in text
          and "TTL 过期/容量逐出/未知 task_id" in text
          and "请重新提交生成" in text
          and "ghost-task-404" in text
          and nocall and "error:task_not_found" in line)
    record("㊝", "query_video 不存在 task_id", ok, "\n".join([
        f"返回体: {text}",
        f"上游调用次数 = {len(env.api.requests) - before}（表未命中即返回，零 IO）",
        f"访问日志: {line}",
    ]))


async def s_video_failed_classifications(env: Env) -> None:
    """㊞ 视频失败分类不切换（E19）：上游 failed→video_failed
    （upstream_error, retry=yes）；查询 400→content_policy；
    查询 500→C4 重试 1 次后 upstream_error。熔断快照零视频键（无切换
    无降级语义，唯一模型一次定生死）。"""
    # (a) 上游 status=failed
    env.api.push_video({"video_id": "vid-fail-001"})
    env.api.push_query("vid-fail-001", {"status": "failed"})
    text_a = await call_video(env, prompt="a doomed video", seconds=4)
    # (b) 轮询查询 400（审核类）
    env.api.push_video({"video_id": "vid-400-001"})
    env.api.push_query("vid-400-001", httpx.Response(
        400, json={"error": {"code": "content_policy_violation",
                             "message": "prompt contains sensitive content",
                             "type": "invalid_request_error"}}))
    text_b = await call_video(env, prompt="an audited video", seconds=4)
    # (c) 轮询查询 500（C4：5xx 恰重试 1 次）
    env.api.push_video({"video_id": "vid-500-001"})
    env.api.push_query("vid-500-001", status_response(500, "boom"))
    env.api.push_query("vid-500-001", status_response(500, "boom"))
    before_c = len(env.api.requests)
    text_c = await call_video(env, prompt="a broken upstream", seconds=4)
    calls_c = len(env.api.requests) - before_c
    snapshot = env.components.circuit.snapshot()
    joined = text_a + text_b + text_c
    ok = (
        "[error] category=upstream_error" in text_a
        and "retry=yes" in text_a
        and "视频生成失败(上游 failed)" in text_a
        and "task_id=vid-fail-001" in text_a
        and "[error] category=content_policy" in text_b
        and "retry=no" in text_b
        and "task_id=vid-400-001" in text_b
        and "[error] category=upstream_error" in text_c
        and "retry=yes" in text_c and "上游服务错误(500)" in text_c
        and calls_c == 3   # 1 提交 + 2 查询（C4 恰重试 1 次）
        and "video" not in snapshot  # 视频路径零熔断写入（E19）
        and "切换" not in joined and "fallback" not in joined
    )
    record("㊞", "视频失败分类不切换（failed/400/500）", ok, "\n".join([
        f"(a) 上游 failed: {text_a}",
        f"(b) 查询 400: {text_b}",
        f"(c) 查询 500: {text_c}",
        f"(c) 上游调用 = {calls_c} 次（1 提交 + 2 查询，C4 重试 1 次）",
        f"熔断快照: {snapshot}（无 video 键：视频路径不写熔断）",
        "三态均无 切换/fallback 字样（E19：唯一视频模型）",
    ]))


async def s_video_persist(env: Env, vdir: Path) -> None:
    """㊟ 视频 persist（T7c）：成功 → /files/*.mp4 可播放（GET 字节一致，
    video/mp4）+ meta persisted；下载失败 → 原上游 URL + persist_failed
    （㉓ 视频部分，C6：非 error 行）。"""
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"e2e-video-bytes" * 64  # ~1KB mock 视频
    env.persist_transport.push(httpx.Response(
        200, content=mp4, headers={"content-type": "video/mp4"}))
    env.api.push_video({"video_id": "vid-per-001"})
    env.api.push_query("vid-per-001",
                       {"status": "completed",
                        "url": "https://video.example/per001.mp4"})
    text = await call_video(env, prompt="a video to persist", seconds=5,
                            persist=True)
    lines = text.splitlines()
    files_url, meta = lines[0], lines[-1]
    resp = await env.client.get(files_url)
    disk = sorted(p.name for p in vdir.glob("*.mp4"))
    ok_a = (files_url.startswith(f"{env.base}/files/")
            and files_url.endswith(".mp4")
            and "persisted" in meta and "url_expires" not in meta
            and resp.status_code == 200 and resp.content == mp4
            and "video/mp4" in resp.headers.get("content-type", "")
            and len(disk) == 1)
    # 降级：下载 502 → 原上游 URL + persist_failed（C6 永不报错）
    env.persist_transport.push(status_response(502, "bad gateway"))
    env.api.push_video({"video_id": "vid-per-002"})
    env.api.push_query("vid-per-002",
                       {"status": "completed",
                        "url": "https://video.example/per002.mp4"})
    text2 = await call_video(env, prompt="a degraded video", seconds=5,
                             persist=True)
    lines2 = text2.splitlines()
    ok_b = (lines2[0] == "https://video.example/per002.mp4"
            and "persist_failed" in lines2[-1]
            and "[error]" not in text2)
    record("㊟", "视频 persist 成功/降级（/files 可播放）", ok_a and ok_b, "\n".join([
        f"(a) 成功返回体: {text}",
        f"    GET {files_url.replace(str(env.base), '')} → {resp.status_code}，"
        f"字节一致={resp.content == mp4}，content-type="
        f"{resp.headers.get('content-type')}",
        f"    VIDEO_DIR 落盘: {disk}",
        f"(b) 降级返回体: {text2}",
        "    （原上游 URL + persist_failed，无 [error] 行，C6/R5）",
    ]))


async def env_video_standard() -> None:
    """环境 6：视频标准预算（轮询 50ms；VIDEO_DIR 独立临时目录）。"""
    vdir = Path(tempfile.mkdtemp(prefix="mcp-e2e-videos-")) / "videos"
    api = VideoScript()
    persist_transport = Script()
    async with spawn_env(
        cfg_overrides={"VIDEO_POLL_INTERVAL_SEC": 0.05, "VIDEO_DIR": str(vdir)},
        api=api, persist_transport=persist_transport,
    ) as env:
        await mcp_initialize(env)
        await s_video_sync_success(env)               # ㉛
        await s_video_task_not_found(env)             # ㊝
        await s_video_failed_classifications(env)     # ㊞
        await s_video_persist(env, vdir)              # ㊟ + ㉓（视频）


async def env_video_overbudget() -> None:
    """环境 7：视频超预算（deadline 4s / 提交 2s / 单查 1s / 轮询 0.2s；
    嵌套不等式自检通过为前提）。"""
    api = VideoScript()
    async with spawn_env(
        cfg_overrides={
            "TOTALBUDGETSEC": 4, "QUEUEWAIT_SEC": 2,
            "TIMEOUT_UPSTREAM_IMAGE_SEC": 2,
            "VIDEO_SUBMIT_TIMEOUT_SEC": 2, "VIDEO_QUERY_TIMEOUT_SEC": 1,
            "PERSIST_TIMEOUT_IMAGE_SEC": 2, "PERSIST_TIMEOUT_VIDEO_SEC": 2,
            "VIDEO_POLL_INTERVAL_SEC": 0.2,
        },
        api=api,
    ) as env:
        await mcp_initialize(env)
        # ---- ㊜ 超预算：running → task_id 返回 → query_video 续接 ----
        env.api.push_video({"video_id": "vid-run-001"})
        # vid-run-001 的查询队列留空 → 恒 running，轮询至 deadline
        t0 = time.monotonic()
        text = await call_video(env, prompt="a long video", seconds=8)
        deferred_sec = time.monotonic() - t0
        lines = text.splitlines()
        ok_a = (len(lines) == 1
                and lines[0].startswith("[meta] task_id=vid-run-001 ")
                and "status=running" in text and "已等待" in text
                and "query_video" in text and "[error]" not in text)
        slot_free = getattr(env.components.pool._sem, "_value", None) == 3
        poll_line = [ln for ln in env.log.lines
                     if "task_id=vid-run-001" in ln and "result=running" in ln]
        ok_line = (len(poll_line) == 1
                   and "poll_count=" in poll_line[0]
                   and "poll_count=0" not in poll_line[0])
        # 续接：上游完成后 query_video（同一 task_id）拿到可播放 URL
        env.api.push_query("vid-run-001",
                           {"status": "completed",
                            "url": "https://video.example/done001.mp4"})
        t2 = await call_query_video(env, "vid-run-001")
        ok_b = t2 == "https://video.example/done001.mp4"
        record("㊜", "超预算 running → query_video 续接成功",
               ok_a and ok_b and slot_free and ok_line, "\n".join([
                   f"generate_video 返回体: {text}",
                   f"超预算耗时 {deferred_sec:.1f}s（deadline 4s，轮询间隔 0.2s）",
                   f"访问日志: {poll_line[0] if poll_line else '（未捕获）'}",
                   f"槽释放（R15）: semaphore = "
                   f"{getattr(env.components.pool._sem, '_value', None)}/3",
                   f"query_video 续接返回: {t2}（恰 URL，无 meta 行）",
               ]))
        # ---- ⑳ 视频侧慢死：轮询单查挂起 → read 档（1s）先响 →
        #      C4 重试 1 次仍挂起 → 结构化错误（有界、非断连、槽归还）----
        env.api.push_video({"video_id": "vid-hang-001"})
        env.api.push_query("vid-hang-001", VideoScript.HANG)
        env.api.push_query("vid-hang-001", VideoScript.HANG)
        t0 = time.monotonic()
        text2 = await call_video(env, prompt="a video on a hung upstream",
                                 seconds=5)
        hang_sec = time.monotonic() - t0
        hang_calls = [r for r in env.api.requests
                      if r.url.path == "/v1/videos/query"
                      and r.url.params.get("video_id") == "vid-hang-001"]
        slot_free2 = getattr(env.components.pool._sem, "_value", None) == 3
        ok2 = ("[error] category=upstream_error" in text2
               and "retry=yes" in text2 and "ReadTimeout" in text2
               and "task_id=vid-hang-001" in text2
               and len(hang_calls) == 2
               and hang_sec < 4 + 10   # 远低于护栏（deadline+SLACK），有界
               and slot_free2)
        record("⑳", "视频侧挂起→结构化错误（read 档先响）", ok2, "\n".join([
            f"返回体: {text2}",
            f"耗时 {hang_sec:.1f}s（单查 read=1s ×2 + 退避 ≪ 护栏；非断连）",
            f"挂起查询请求数 = {len(hang_calls)}（C4：超时恰重试 1 次）",
            f"槽归还（㉒ 视频路径）: semaphore = "
            f"{getattr(env.components.pool._sem, '_value', None)}/3",
        ]))


async def env_video_ttl() -> None:
    """环境 8：任务表 TTL（TASK_TTL_SEC=1；get 惰性判过期，不依赖清理节拍）。"""
    api = VideoScript()
    async with spawn_env(
        cfg_overrides={"VIDEO_POLL_INTERVAL_SEC": 0.05, "TASK_TTL_SEC": 1},
        api=api,
    ) as env:
        await mcp_initialize(env)
        env.api.push_video({"video_id": "vid-ttl-001"})
        env.api.push_query("vid-ttl-001",
                           {"status": "completed",
                            "url": "https://video.example/ttl001.mp4"})
        text = await call_video(env, prompt="a short-lived record", seconds=4)
        ok_sync = "https://video.example/ttl001.mp4" in text
        await asyncio.sleep(1.3)  # TTL 1s 过界
        text2 = await call_query_video(env, "vid-ttl-001")
        ok = (ok_sync
              and "[error] category=task_not_found" in text2
              and "TTL 过期" in text2)
        record("㊱", "TTL 过期 → task_not_found", ok, "\n".join([
            f"提交后同步完成: {text}",
            f"等待 1.3s（TASK_TTL_SEC=1）后再查: {text2}",
            "TTL 过期视为未命中（与容量逐出同一契约，R16）",
        ]))


# ===========================================================================
# 主流程
# ===========================================================================
async def run_all(fast: bool) -> int:
    # ---- 环境 1：标准配置（契约/鉴权/健康/E25/拆分串行/部分成功）----
    async with spawn_env() as env:
        await s_healthz(env)                       # ⑲
        await s_auth_401(env)                      # ⑫
        await s_handshake(env)                     # ①
        await s_single_model_and_e25(env)          # ㉖
        await s_upstream_auth_401(env)             # ⑬
        await s_size_passthrough(env)              # ㉗
        await s_url_expires(env)                    # ㉕
        await s_batch_split(env)                    # ⑥
        await s_batch_partial(env)                  # ㉙
        await s_content_policy(env)                # ⑩
        await s_invalid_params(env)                # ⑪附（A2）
        await s_ten_failures(env)                  # ㉒

    # ---- 环境 2/3：熔断（E24 快失败 + 半开探针）----
    await env_hard_circuit()                       # ⑨
    await env_probe_renewal()                      # ⑪

    # ---- 环境 4/5：persist 成功与降级（㉓ 图）----
    await env_persist_ok()
    await env_persist_fail()

    # ---- 环境 6：视频标准预算（㉛ ㊝ ㊞ ㊟/㉓ 视频）----
    await env_video_standard()

    # ---- 环境 7：视频超预算（㊜ + ⑳ 视频侧 + ㉒ 视频槽归还）----
    await env_video_overbudget()

    # ---- 环境 8：任务表 TTL（㊱）----
    await env_video_ttl()

    # ---- 环境 9/10：长耗时项 ----
    if not fast:
        await env_deadline_truncation()            # ⑳ 生图侧（~200s）
        await env_dynamic_read()                   # ㉑（~62s）

    return 0 if all(r["ok"] for r in RESULTS) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="S5 端到端验收（V2 三工具全量）")
    parser.add_argument("--fast", action="store_true",
                        help="跳过 ⑳生图侧/㉑ 长耗时项")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp_imagegen").setLevel(logging.INFO)

    rc = asyncio.run(run_all(args.fast))

    print("=" * 72)
    print(f"验收场景汇总：{len(RESULTS)} 项，"
          f"通过 {sum(1 for r in RESULTS if r['ok'])}，"
          f"失败 {sum(1 for r in RESULTS if not r['ok'])}")
    for r in RESULTS:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['item']:<4} {r['name']}")
    out = ROOT / "acceptance" / "e2e_results.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n证据 JSON 已写入: {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
