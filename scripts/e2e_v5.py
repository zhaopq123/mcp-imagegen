"""V5 端到端自检：真起服务 + 真起 mock 上游，全程走 HTTP（不用单测替身）。

与单测的分工
------------
单测（tests/）验证单元契约，替身挡在 app 层之下；本脚本验证**装配之后**
还成立不成立：ASGI 门禁、MCP 协议、管理面板、以及"配了 provider 才可用"
这条 V5 的主线。

跑法::

    python scripts/e2e_v5.py                 # 全部用例
    python scripts/e2e_v5.py -k provider     # 只跑名字含 provider 的用例

退出码：0 全通过 / 1 有失败。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# 若整脚本运行超过 6 分钟仍未退出，说明有真死锁——把主协程栈转储出来，
# 而不是默默卡死让调用方（后台任务）拿不到任何现场。
faulthandler.dump_traceback_later(360, exit=False)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers import schema as p_schema          # noqa: E402
from providers.store import Store                 # noqa: E402

ADMIN_PASSWORD = "e2e-admin-pass-2026"
MCP_TOKEN = "e2e-mcp-token"
PROVIDER_ID = "mock-local"
PNG = b"\x89PNG\r\n\x1a\n" + b"e2e-png-payload"

# 健康等待上限（Windows 上子进程起 uvicorn 略慢）
BOOT_TIMEOUT_SEC = 60.0


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, timeout: float = BOOT_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.closing(socket.socket()) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def spawn(args: list, env: dict, log_path: Path) -> subprocess.Popen:
    # 关键：子进程日志落到文件，绝不走 PIPE。否则父进程不读、64KB 管道缓冲
    # 一满，子进程 print/logging 阻塞 → 服务无响应 → 整脚本看起来"卡死"。
    # 文件重定向没有缓冲上限，彻底排除这类伪死锁。
    return subprocess.Popen(
        args, cwd=str(ROOT),
        env={**os.environ, **env},
        stdout=open(log_path, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT)


def dump_log(log_path: Path, label: str, lines: int = 60) -> None:
    """把子进程日志尾部吐出来——挂起类故障只有它能解释。"""
    if log_path is None or not Path(log_path).exists():
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-lines:])
    except Exception:  # noqa: BLE001
        tail = ""
    print(f"[log] ---- {label} 日志尾部（{log_path}）----\n{tail}"
          f"\n[log] ---- 结束 ----")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# 用例登记
# ---------------------------------------------------------------------------
RESULTS: list = []


def record(no: str, title: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((no, title, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {no} {title}")
    if not ok and detail:
        for line in str(detail).split("\n")[:8]:
            print(f"         {line}")


# ---------------------------------------------------------------------------
# MCP 协议助手（streamable-http 可能回 SSE）
# ---------------------------------------------------------------------------
def mcp_payload(response: httpx.Response) -> dict:
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                if body and body != "[DONE]":
                    return json.loads(body)
        return {}
    return response.json()


def tool_text(response: httpx.Response) -> str:
    data = mcp_payload(response)
    result = data.get("result") or {}
    chunks = result.get("content") or []
    return "\n".join(c.get("text", "") for c in chunks if isinstance(c, dict))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main(pattern: str) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mcp-imagegen-e2e-"))
    mock_port, srv_port = free_port(), free_port()
    mock_base = f"http://127.0.0.1:{mock_port}"
    srv_base = f"http://127.0.0.1:{srv_port}"
    mock_log, serve_log = tmp / "mock.log", tmp / "serve.log"

    mock_proc = srv_proc = None
    try:
        # ---- 1. 起 mock 上游（轮询快速出片，别让 E2E 干等）----
        mock_proc = spawn(
            [sys.executable, str(ROOT / "scripts" / "mock_upstream.py"), str(mock_port)],
            {"MOCK_IMAGE_DELAY": "0.2", "MOCK_VIDEO_DELAY": "0.1",
             "MOCK_VIDEO_POLLS": "1"}, mock_log)
        print(f"[boot] mock 上游 :{mock_port}", end=" ")

        # ---- 2. 预置 store：一个 provider + 一个访问令牌 ----
        store_path = tmp / "store.json"
        image_dir, video_dir = tmp / "images", tmp / "videos"
        for d in (image_dir, video_dir):
            d.mkdir(parents=True, exist_ok=True)

        store = Store(str(store_path))
        store.load()
        store.upsert_provider(p_schema.ProviderConfig(
            id=PROVIDER_ID, name="本地 mock", base_url=mock_base,
            auth=p_schema.AuthSpec(kind=p_schema.AUTH_BEARER, token="mock-sk-0000"),
            capabilities={
                "image": p_schema.CapabilitySpec(
                    model="mock-image-1",
                    request=p_schema.RequestSpec(
                        method="POST", path="/v1/images/generations",
                        body={"model": "{model}", "prompt": "{prompt}",
                              "size": "{size}", "ratio": "{ratio}"}),
                    extract=p_schema.ExtractSpec(urls="images[].url"),
                ),
                "video": p_schema.CapabilitySpec(
                    model="mock-video-1",
                    request=p_schema.RequestSpec(
                        method="POST", path="/v1/videos",
                        body={"model": "{model}", "prompt": "{prompt}",
                              "seconds": "{seconds}",
                              "aspect_ratio": "{aspect_ratio}"}),
                    extract=p_schema.ExtractSpec(task_id="video_id"),
                    poll=p_schema.RequestSpec(
                        method="GET", path="/v1/videos/query",
                        query={"video_id": "{task_id}"}),
                    poll_extract=p_schema.ExtractSpec(status="status", url="url"),
                ),
            }))
        store.upsert_token(p_schema.TokenRecord(
            id="e2e", name="e2e 客户端", token=MCP_TOKEN,
            max_concurrency=0))
        store.save()

        # ---- 3. 起服务 ----
        srv_proc = spawn(
            [sys.executable, str(ROOT / "server.py")],
            {"PORT": str(srv_port), "STORE_PATH": str(store_path),
             "IMAGE_DIR": str(image_dir), "VIDEO_DIR": str(video_dir),
             "MCPACCESS_TOKEN": MCP_TOKEN,
             "VIDEO_POLL_INTERVAL_SEC": "1",
             # 日志走文件（见 spawn）；这里仍关掉 Python 行缓冲，让写盘即时
             "PYTHONUNBUFFERED": "1",
             "PYTHONIOENCODING": "utf-8"}, serve_log)
        print(f"| 服务 :{srv_port}")

        if not (wait_port(mock_port) and wait_port(srv_port)):
            print("[boot] 子进程未能就绪")
            dump_log(serve_log, "服务")
            dump_log(mock_log, "mock 上游")
            return 1

        async with httpx.AsyncClient(base_url=srv_base, timeout=60.0) as c:
            await run_checks(c, srv_base, mock_base, pattern)
    except Exception as exc:  # noqa: BLE001 —— 自检脚本要把现场吐出来
        print(f"\n[abort] 自检中断：{type(exc).__name__}: {exc}")
        # 先分清「整机卡死」与「单个请求卡死」——两者的排查方向完全不同
        try:
            async with httpx.AsyncClient(base_url=srv_base, timeout=6.0) as probe:
                hz = await probe.get("/healthz")
                st = await probe.get("/admin/api/state")
                print(f"[probe] 服务仍活着：/healthz={hz.status_code} "
                      f"/admin/api/state={st.status_code}（单个请求卡死）")
        except Exception as probe_exc:  # noqa: BLE001
            print(f"[probe] 服务无响应：{type(probe_exc).__name__}（整机卡死）")
        # 再看 mock 上游与本机：三者一起卡 → 是环境，不是服务
        try:
            async with httpx.AsyncClient(base_url=mock_base, timeout=6.0) as m:
                print(f"[probe] mock 上游 → {(await m.get('/mock-stats')).status_code}")
        except Exception as mock_exc:  # noqa: BLE001
            print(f"[probe] mock 上游无响应：{type(mock_exc).__name__}")
        try:
            done = subprocess.run([sys.executable, "-c", "print(1)"],
                                  timeout=10, capture_output=True)
            print(f"[probe] 本机子进程：rc={done.returncode}（环境正常）")
        except Exception as env_exc:  # noqa: BLE001
            print(f"[probe] 本机子进程超时：{type(env_exc).__name__}（环境问题）")
        dump_log(serve_log, "服务")
        dump_log(mock_log, "mock 上游")
        RESULTS.append(("X0", "自检未跑完", False, f"{type(exc).__name__}: {exc}"))
    finally:
        stop(srv_proc)
        stop(mock_proc)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[2]]
    print(f"\n{'=' * 60}")
    print(f"V5 端到端自检：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for no, title, _ok, detail in failed:
        print(f"  ✗ {no} {title}")
        if detail:
            print(f"      {str(detail).splitlines()[0][:120]}")
    return 1 if failed else 0


async def run_checks(c: httpx.AsyncClient, srv_base: str, mock_base: str,
                     pattern: str) -> None:
    def want(keyword: str) -> bool:
        # setup / login 是后续用例的前置条件，不能按关键字过滤掉
        if keyword in ("setup", "login"):
            return True
        if not pattern:
            return True
        # 支持逗号分隔的多个关键字（-k video,probe）
        keys = [k.strip().lower() for k in pattern.split(",") if k.strip()]
        return any(k in keyword.lower() for k in keys)

    auth = {"authorization": f"Bearer {MCP_TOKEN}"}

    async def session(token: str = MCP_TOKEN) -> dict:
        """开一个 MCP 会话，返回可复用的请求头。

        streamable-http 要求先 initialize 再发业务请求；没有会话就调
        tools/call 拿到的是协议层 400，不是工具结果——自检里最容易踩。
        """
        r = await c.post("/mcp", headers={"authorization": f"Bearer {token}"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "capabilities": {},
                                          "clientInfo": {"name": "e2e",
                                                         "version": "1"}}})
        s = r.headers.get("mcp-session-id", "")
        return {"authorization": f"Bearer {token}",
                **({"mcp-session-id": s} if s else {})}

    # ================= A. 启动与门禁 =================
    if want("healthz"):
        r = await c.get("/healthz")
        body = r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else {}
        record("A1", "/healthz 报告服务提供方能力", r.status_code == 200
               and body.get("status") == "ok"
               and body.get("capabilities", {}).get("image") is True
               and body.get("providers", 0) >= 1,
               f"status={r.status_code} body={body}")

    if want("gate"):
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})
        record("A2", "/mcp 无令牌 → 401", r.status_code == 401
               and "category=mcp_auth" in r.text, f"{r.status_code} {r.text[:120]}")

        r = await c.post("/upload", content=PNG)
        record("A3", "/upload 无令牌 → 401", r.status_code == 401
               and "category=mcp_auth" in r.text, f"{r.status_code} {r.text[:120]}")

    # ================= B. MCP 工具 =================
    sid = ""
    if want("mcp"):
        r = await c.post("/mcp", headers=auth, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "clientInfo": {"name": "e2e", "version": "1"}}})
        sid = r.headers.get("mcp-session-id", "")
        record("B1", "MCP initialize 返回会话", r.status_code == 200 and bool(sid),
               f"{r.status_code} sid={sid[:12]}")

    if want("tools/list"):
        headers = await session()
        r = await c.post("/mcp", headers=headers,
                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        payload = mcp_payload(r)
        names = sorted(t["name"] for t in payload.get("result", {}).get("tools", []))
        img = next((t for t in payload.get("result", {}).get("tools", [])
                    if t["name"] == "generate_image"), None)
        has_provider = bool(img and "provider" in (img.get("inputSchema", {})
                                                   .get("properties", {})))
        record("B2", "tools/list 四工具 + provider 参数",
               names == ["generate_image", "generate_video",
                         "list_video_tasks", "query_video"] and has_provider,
               f"names={names} provider={has_provider}")

    if want("generate_image"):
        headers = await session()
        r = await c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "generate_image",
                       "arguments": {"prompt": "一只在窗台上的猫",
                                     "size": "1024x1024"}}})
        text = tool_text(r)
        first = text.split("\n")[0] if text else ""
        record("B3", "tools/call 生图返回 URL 契约行",
               first.startswith("http") and "[meta]" in text, text[:200])

    if want("provider_hint"):
        headers = await session()
        r = await c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "generate_image",
                       "arguments": {"prompt": "a cat", "size": "1024x1024",
                                     "provider": "no-such-provider"}}})
        text = tool_text(r)
        record("B4", "显式指定不存在的 provider → 明确报错（不静默兜底）",
               "[error]" in text and "no-such-provider" in text, text[:200])

    if want("generate_video"):
        headers = await session()
        r = await c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "generate_video",
                       "arguments": {"prompt": "a cat walking", "seconds": 5,
                                     "aspect_ratio": "16:9"}}})
        text = tool_text(r)
        ok = text.startswith("http") or "[meta]" in text
        record("B5", "tools/call 生视频（提交+轮询出片）", ok, text[:200])

    if want("upload"):
        r = await c.post("/upload", headers=auth, content=PNG)
        name = r.json().get("name", "") if r.status_code == 201 else ""
        ok = r.status_code == 201 and bool(name)
        record("B6", "/upload 带令牌落盘并返回文件名", ok,
               f"{r.status_code} {r.text[:120]}")
        if ok:
            r2 = await c.get(f"/files/{name}")
            record("B7", "/files 公开可读（不落门禁）",
                   r2.status_code == 200 and r2.content == PNG,
                   f"{r2.status_code} len={len(r2.content)}")

    # ================= C. 管理面板 =================
    cookie = ""
    if want("panel_page"):
        r = await c.get("/admin")
        record("C1", "/admin 页面 200（内联 SPA，零外部依赖）",
               r.status_code == 200
               and "text/html" in r.headers.get("content-type", "")
               and "{{PREFIX}}" not in r.text,
               f"{r.status_code} ctype={r.headers.get('content-type')}")

    if want("setup"):
        r = await c.get("/admin/api/state")
        init = r.json().get("initialized")
        r2 = await c.post("/admin/api/setup", json={"password": "123"})
        record("C2", "首次状态未初始化 + 弱口令被拒",
               init is False and r2.status_code == 400,
               f"state={r.status_code}/{init} setup={r2.status_code}")

        r3 = await c.post("/admin/api/setup", json={"password": ADMIN_PASSWORD})
        r4 = await c.post("/admin/api/setup", json={"password": ADMIN_PASSWORD})
        record("C3", "初始化成功 + 重复初始化 409",
               r3.status_code == 200 and r4.status_code == 409,
               f"{r3.status_code}/{r4.status_code}")

    if want("login"):
        r = await c.post("/admin/api/auth/login", json={"password": "wrong-pass"})
        ok_bad = r.status_code == 401

        r = await c.post("/admin/api/auth/login", json={"password": ADMIN_PASSWORD})
        raw = r.headers.get("set-cookie", "")
        cookie = raw.split("mi_admin=")[1].split(";")[0] if "mi_admin=" in raw else ""
        flags_ok = ("HttpOnly" in raw and "SameSite=strict" in raw)
        record("C4", "错密码 401 + 正确登录下发 HttpOnly/Strict Cookie",
               ok_bad and r.status_code == 200 and bool(cookie) and flags_ok,
               f"bad={ok_bad} ok={r.status_code} flags={flags_ok} raw={raw!r}")

    if not cookie:
        print("  [skip] 未取得会话，面板写操作用例跳过")
        return

    read_h = {"cookie": f"mi_admin={cookie}"}
    write_h = {**read_h, "X-Admin-Request": "1"}

    if want("guard"):
        # 换一个干净客户端：httpx 会记住登录拿到的 Cookie，用同一个 client
        # 测「未登录」是假阴性（请求其实带着会话）。
        async with httpx.AsyncClient(base_url=srv_base, timeout=30.0) as anon:
            r = await anon.get("/admin/api/providers")
            r2 = await anon.post("/admin/api/providers", headers=read_h,
                                 json={"id": "x"})
        record("C5", "未登录读 401 + 缺 CSRF 头写 403",
               r.status_code == 401 and r2.status_code == 403,
               f"read={r.status_code} write={r2.status_code}")

    if want("provider_crud"):
        spec = {
            "id": "e2e-created", "name": "E2E 新建", "enabled": True,
            "base_url": mock_base,
            "auth": {"kind": "bearer", "token": "sk-e2e-demo-0123456789"},
            "capabilities": {
                "image": {"enabled": True, "model": "mock-image-1",
                          "request": {"method": "POST",
                                      "path": "/v1/images/generations",
                                      "body": {"prompt": "{prompt}"}},
                          "extract": {"urls": "images[].url"}}},
        }
        r = await c.post("/admin/api/providers", headers=write_h, json=spec)
        ok_create = r.status_code == 200

        r = await c.get("/admin/api/providers", headers=read_h)
        items = r.json().get("items", [])
        masked = next((i for i in items if i["id"] == "e2e-created"), {})
        auth_part = masked.get("auth", {})
        ok_mask = (auth_part.get("token") is None
                   and auth_part.get("token_set") is True
                   and str(auth_part.get("token_preview", "")).startswith("sk-"))

        r = await c.delete("/admin/api/providers/e2e-created", headers=write_h)
        record("C6", "Provider 新建 / 列表脱敏 / 删除",
               ok_create and ok_mask and r.status_code == 200,
               f"create={ok_create} mask={ok_mask} del={r.status_code} "
               f"preview={auth_part.get('token_preview')}")

    if want("probe"):
        r = await c.post(f"/admin/api/providers/{PROVIDER_ID}/test",
                         headers=write_h, json={})
        body = r.json()
        record("C7", "联通性探测（真实打一次上游）",
               r.status_code == 200 and body.get("reachable") is True,
               f"{r.status_code} {json.dumps(body, ensure_ascii=False)[:200]}")

    if want("hot_settings"):
        before = (await c.get("/admin/api/settings", headers=read_h)).json()
        cur = before["hot"]["image_max_concurrency"]
        r = await c.put("/admin/api/settings", headers=write_h,
                        json={"image_max_concurrency": cur + 4})
        after = (await c.get("/admin/api/settings", headers=read_h)).json()
        record("C8", "运行参数热更新立即生效（无需重启）",
               r.status_code == 200
               and after["hot"]["image_max_concurrency"] == cur + 4,
               f"{cur} → {r.json()['hot']['image_max_concurrency']} → "
               f"{after['hot']['image_max_concurrency']}")
        # 还原，别把自检的改动留给后续用例
        print("      … C8 还原中")
        await c.put("/admin/api/settings", headers=write_h,
                    json={"image_max_concurrency": cur})
        print("      … C8 已还原")

    if want("tokens"):
        print("      … C9 建令牌")
        r = await c.post("/admin/api/tokens", headers=write_h,
                         json={"id": "e2e-tok", "name": "E2E 令牌"})
        print("      … C9 建令牌完成")
        created = r.json().get("item", {})
        secret = created.get("token", "")
        r2 = await c.get("/admin/api/tokens", headers=read_h)
        listed = any(t["id"] == "e2e-tok" for t in r2.json().get("items", []))
        r3 = await c.post("/admin/api/tokens/e2e-tok/rotate", headers=write_h,
                          json={})
        rotated = r3.json().get("item", {}).get("token", "")

        # 新签发的令牌必须真能过门禁（这条最容易漏，且必须在删除之前测）
        new_h = await session(rotated)
        r5 = await c.post("/mcp", headers=new_h,
                          json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        r4 = await c.delete("/admin/api/tokens/e2e-tok", headers=write_h)
        record("C9", "令牌新建/轮换/删除 + 新令牌真能过门禁",
               bool(secret) and listed and bool(rotated) and rotated != secret
               and r5.status_code == 200 and r4.status_code == 200,
               f"new={bool(secret)} listed={listed} rotated={bool(rotated)} "
               f"gate={r5.status_code} del={r4.status_code}")

    if want("dashboard"):
        r = await c.get("/admin/api/dashboard", headers=read_h)
        body = r.json()
        r2 = await c.get("/admin/api/logs?limit=5", headers=read_h)
        r3 = await c.get("/admin/api/tasks", headers=read_h)
        record("C10", "总览/日志/任务三张看板可读",
               r.status_code == 200 and r2.status_code == 200
               and r3.status_code == 200 and "metrics" in body,
               f"{r.status_code}/{r2.status_code}/{r3.status_code} "
               f"keys={sorted(body)[:6]}")

    # ================= D. 零配置契约（V5 主线）=================
    if want("not_configured"):
        r = await c.post(f"/admin/api/providers/{PROVIDER_ID}/toggle",
                         headers=write_h, json={})
        disabled = r.json().get("enabled") is False
        headers = await session()
        r2 = await c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "generate_image",
                       "arguments": {"prompt": "a cat", "size": "1024x1024"}}})
        text = tool_text(r2)
        ok = (disabled and "category=not_configured" in text
              and "管理面板" in text)
        # 复原
        await c.post(f"/admin/api/providers/{PROVIDER_ID}/toggle",
                     headers=write_h, json={})
        record("D1", "全部停用 → not_configured 且指向管理面板（非上游故障）",
               ok, f"disabled={disabled} text={text[:180]}")

        r3 = await c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "generate_image",
                       "arguments": {"prompt": "a cat", "size": "1024x1024"}}})
        record("D2", "重新启用后立刻恢复可用（热加载，无重启）",
               tool_text(r3).startswith("http"), tool_text(r3)[:160])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="V5 端到端自检")
    ap.add_argument("-k", dest="keyword", default="",
                    help="只跑名字含该关键字的用例")
    args = ap.parse_args()
    code = asyncio.run(main(args.keyword))
    sys.exit(code)
