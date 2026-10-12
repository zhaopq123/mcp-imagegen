#!/usr/bin/env python3
"""真机联调脚本（V2 第六轮；管理员派单五项逐项计时/记录）。

与单测/e2e 的边界：本脚本打**真实上游**（Agnes AI，端点与密钥来自 env
文件），不复用 pytest 收集（文件名不带 test_ 前缀，pytest.ini testpaths
亦限定 tests/）。可重复执行；每次运行结果**追加**写入
scripts/live_check_results.json（run_id 区分）。

阶段（--only 逗号分隔可选；默认 all 顺序执行）：
    raw_img_default   ① 直连生图默认档 1024x1024（计时 + 响应顶层结构）
    raw_img_2k        ① 直连生图 2K 档（官方推荐 size="2K"+ratio="16:9"）
    raw_img_bad       ① 直连生图非法 size（验证 400 错误体 message）
    svc_image         ① 项目链路生图默认档（MCP 协议全链，计时 + 契约行）
    svc_image_bad     ① 项目链路非法 size "99999x99999"（400 透传观察）
    svc_persist       ② 项目链路 persist=true（/files URL + GET 字节一致
                        + 与上游 URL 下载字节对照）
    svc_video         ③ 视频全流程（提交 → 轮询/续查至 completed → 顶层
                        url 验证 → query 链路；遇 503 记录重试行为）
    raw_429           ④ 直连连发生图（观察 429 与 Retry-After 头）

响应解析验证（⑤）隐含在各生图阶段：记录真实响应顶层键（data / images）
与项目链路解析结果（双兼容 E14 走通）。

用法：
    python scripts/live_check.py --env-file /home/z/envs/mcp-imagegen.env
    python scripts/live_check.py --only svc_video
    python scripts/live_check.py --only raw_429 --burst 10

纪律：Key 仅从 env 文件注入进程环境（os.environ），结果 JSON / 日志零
Key 落盘；上游短效 URL 允许记录（对账证据，非凭据）。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from config import load_config  # noqa: E402
from server import Components, build_app  # noqa: E402

RESULTS_PATH = ROOT / "scripts" / "live_check_results.json"
PROTOCOL_VERSION = "2025-06-18"
IMAGE_MODEL = "agnes-image-2.5-flash"
VIDEO_MODEL = "agnes-video-2.5-flash"
VIDEO_QUERY_TIMEOUT_TOTAL_SEC = 1800   # 视频续查外层上限（30 分钟）
VIDEO_QUERY_INTERVAL_SEC = 10.0         # query_video 外层续查间隔


# ---------------------------------------------------------------------------
# 环境加载（env 文件 → os.environ；凭据不落任何输出）
# ---------------------------------------------------------------------------
def load_env_file(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
def load_results() -> dict:
    if RESULTS_PATH.exists():
        with RESULTS_PATH.open() as f:
            return json.load(f)
    return {"runs": []}


def save_results(data: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(RESULTS_PATH)  # 原子写（root 只读场景同铁律）


def scrub(obj):
    """递归脱敏：任何 sk- 开头字符串只留前 6 字符（防日志/嵌套泄漏）。"""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("sk-"):
        return obj[:6] + "***REDACTED***"
    return obj


# ---------------------------------------------------------------------------
# 直连层（raw httpx；三端点全显式）
# ---------------------------------------------------------------------------
async def raw_generate(token: str, base: str, *, size: str, ratio: str | None,
                       prompt: str, read_sec: float = 360.0) -> dict:
    """直连生图：返回 {status, elapsed, top_keys, url, error…}。

    200 记录顶层键/URL；400/429 等记录错误体（error.message 透传验证）。
    """
    payload = {"model": IMAGE_MODEL, "prompt": prompt, "size": size}
    if ratio is not None:
        payload["ratio"] = ratio
    timeout = httpx.Timeout(connect=10.0, read=read_sec, write=30.0, pool=10.0)
    async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
        t0 = time.monotonic()
        try:
            resp = await client.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            elapsed = round(time.monotonic() - t0, 3)
            out = {
                "status": resp.status_code,
                "elapsed_sec": elapsed,
                "retry_after": resp.headers.get("retry-after"),
            }
            try:
                body = resp.json()
            except ValueError:
                out["body_prefix"] = resp.text[:200]
                return out
            out["top_keys"] = sorted(body.keys()) if isinstance(body, dict) else None
            if resp.status_code == 200 and isinstance(body, dict):
                items = body.get("data") or body.get("images") or []
                out["item_count"] = len(items)
                if items and isinstance(items[0], dict):
                    out["item_keys"] = sorted(items[0].keys())
                    url = items[0].get("url") or ""
                    out["url"] = url
            else:
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    out["error"] = {
                        "code": err.get("code"),
                        "message": err.get("message"),
                        "type": err.get("type"),
                    }
                else:
                    out["body_prefix"] = json.dumps(body, ensure_ascii=False)[:300]
            return out
        except httpx.HTTPError as exc:
            return {
                "status": None,
                "elapsed_sec": round(time.monotonic() - t0, 3),
                "exception": type(exc).__name__,
                "detail": str(exc)[:200],
            }


# ---------------------------------------------------------------------------
# 服务层（真实 uvicorn + 真实上游 + MCP streamable-http 协议）
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def spawn_service(env_map: dict):
    """起一套完整真机服务（独立临时落盘目录；真实 transport 不注入 mock）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="mcp-live-"))
    svc_env = dict(env_map)
    svc_env["IMAGE_DIR"] = str(tmpdir / "images")
    svc_env["VIDEO_DIR"] = str(tmpdir / "videos")
    (tmpdir / "images").mkdir(parents=True, exist_ok=True)
    (tmpdir / "videos").mkdir(parents=True, exist_ok=True)
    cfg = load_config(svc_env)
    components = Components.build(cfg)  # 真实上游 client + 真实 persist
    await components.persist.start()
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
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        timeout=httpx.Timeout(connect=10, read=400, write=30, pool=30),
    )
    try:
        yield cfg, components, client, tmpdir
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
        await components.persist.aclose()
        await components.api_client.aclose()


def _rpc_headers(token: str, session_id: str | None) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        h["mcp-session-id"] = session_id
    return h


def _parse_rpc(response: httpx.Response, msg_id: int) -> dict:
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                ev = json.loads(line[len("data:"):].strip())
                if isinstance(ev, dict) and ev.get("id") == msg_id:
                    return ev
        raise AssertionError(f"SSE 中未找到 id={msg_id}：{response.text[:300]}")
    body = response.json()
    if isinstance(body, dict) and body.get("id") == msg_id:
        return body
    if isinstance(body, list):
        for ev in body:
            if isinstance(ev, dict) and ev.get("id") == msg_id:
                return ev
    raise AssertionError(f"非预期响应形态：{str(body)[:300]}")


async def mcp_call(client: httpx.AsyncClient, token: str,
                   method: str, params: dict | None = None,
                   session: list | None = None) -> dict:
    """MCP JSON-RPC；session=[session_id] 可变单元素容器（initialize 回填）。"""
    rid = uuid.uuid4().int % 100000 + 1
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    headers = _rpc_headers(token, session[0] if session else None)
    resp = await client.post("/mcp", headers=headers, json=body)
    assert resp.status_code == 200, f"{method} HTTP {resp.status_code}: {resp.text[:300]}"
    payload = _parse_rpc(resp, rid)
    assert "error" not in payload, f"{method} JSON-RPC 错误: {payload}"
    if session is not None and session[0] is None:
        session[0] = resp.headers.get("mcp-session-id")
    return payload["result"]


async def call_tool(client: httpx.AsyncClient, token: str, session: list,
                    name: str, **arguments) -> str:
    result = await mcp_call(client, token, "tools/call",
                            {"name": name, "arguments": arguments},
                            session=session)
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    return texts[0] if texts else ""


async def mcp_initialize(client: httpx.AsyncClient, token: str, session: list) -> None:
    await mcp_call(client, token, "initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "live-check", "version": "1.0"},
    }, session=session)
    headers = _rpc_headers(token, session[0])
    await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------
async def phase_raw_img_default(env_map: dict, run: dict, prompt: str) -> None:
    r = await raw_generate(env_map["AGNES_TOKEN"], env_map["AGNES_BASE_URL"],
                           size="1024x1024", ratio=None, prompt=prompt)
    run["raw_img_default"] = r


async def phase_raw_img_2k(env_map: dict, run: dict, prompt: str) -> None:
    r = await raw_generate(env_map["AGNES_TOKEN"], env_map["AGNES_BASE_URL"],
                           size="2K", ratio="16:9", prompt=prompt)
    run["raw_img_2k"] = r


async def phase_raw_img_bad(env_map: dict, run: dict, prompt: str) -> None:
    r = await raw_generate(env_map["AGNES_TOKEN"], env_map["AGNES_BASE_URL"],
                           size="banana", ratio=None, prompt=prompt)
    run["raw_img_bad"] = r


async def phase_svc_image(env_map: dict, run: dict, prompt: str) -> None:
    async with spawn_service(env_map) as (cfg, comp, client, tmpdir):
        session: list = [None]
        await mcp_initialize(client, env_map["MCPACCESS_TOKEN"], session)
        t0 = time.monotonic()
        text = await call_tool(client, env_map["MCPACCESS_TOKEN"], session,
                               "generate_image", prompt=prompt)
        run["svc_image"] = {
            "elapsed_sec": round(time.monotonic() - t0, 3),
            "reply": text,
            "timeout_tier": cfg.timeout_agnes_image_sec,
        }


async def phase_svc_image_bad(env_map: dict, run: dict, prompt: str) -> None:
    async with spawn_service(env_map) as (cfg, comp, client, tmpdir):
        session: list = [None]
        await mcp_initialize(client, env_map["MCPACCESS_TOKEN"], session)
        t0 = time.monotonic()
        text = await call_tool(client, env_map["MCPACCESS_TOKEN"], session,
                               "generate_image", prompt=prompt,
                               size="99999x99999")
        run["svc_image_bad"] = {
            "elapsed_sec": round(time.monotonic() - t0, 3),
            "reply": text,
        }


async def phase_svc_persist(env_map: dict, run: dict, prompt: str) -> None:
    async with spawn_service(env_map) as (cfg, comp, client, tmpdir):
        session: list = [None]
        await mcp_initialize(client, env_map["MCPACCESS_TOKEN"], session)
        t0 = time.monotonic()
        text = await call_tool(client, env_map["MCPACCESS_TOKEN"], session,
                               "generate_image", prompt=prompt, persist=True)
        elapsed = round(time.monotonic() - t0, 3)
        lines = text.splitlines()
        url = lines[0] if lines else ""
        out = {"elapsed_sec": elapsed, "reply": text}
        if url.startswith("http://127.0.0.1:") and "/files/" in url:
            path = url[url.index("/files/"):]
            resp = await client.get(path)
            out["files_get_status"] = resp.status_code
            out["files_get_bytes"] = len(resp.content)
            out["files_content_type"] = resp.headers.get("content-type")
            disk = list((tmpdir / "images").glob("*"))
            out["disk_files"] = [p.name for p in disk]
            if disk:
                out["bytes_match_disk"] = resp.content == disk[0].read_bytes()
        elif url.startswith("http"):
            out["note"] = "persist 降级返回上游 URL（persist_failed）"
        run["svc_persist"] = out


async def phase_svc_video(env_map: dict, run: dict, prompt: str) -> None:
    async with spawn_service(env_map) as (cfg, comp, client, tmpdir):
        session: list = [None]
        await mcp_initialize(client, env_map["MCPACCESS_TOKEN"], session)
        out: dict = {}
        # ---- 提交 + 服务内轮询（deadline 200s；超时返 running + task_id）----
        # 队列满观察（派单③）：遇 503 video_queue_full（服务内 C4 已重试 1 次
        # 后仍满）→ 外层等待后重提，记录重试次数与恢复耗时；上限后不阻塞。
        attempts = []
        submit_wait = 20.0      # 外层每轮等待（秒）
        submit_max = 6          # 外层最多 6 轮（120s 观察）
        text = ""
        t_all = time.monotonic()
        for i in range(1, submit_max + 1):
            t0 = time.monotonic()
            text = await call_tool(
                client, env_map["MCPACCESS_TOKEN"], session,
                "generate_video", prompt=prompt, seconds=5,
                aspect_ratio="16:9")
            att = {"attempt": i,
                   "elapsed_sec": round(time.monotonic() - t0, 3)}
            ok = ("503" not in text and "video_queue_full" not in text
                  and "error]" not in text)
            if ok:
                att["result"] = "submitted/poll-started"
                attempts.append(att)
                break
            att["result"] = "queue_full_or_error"
            att["reply_head"] = text[:180]
            attempts.append(att)
            if i < submit_max:
                print(f"[live] 视频队列满/失败（第 {i} 次），"
                      f"{submit_wait:.0f}s 后重提…", flush=True)
                await asyncio.sleep(submit_wait)
        out["submit_attempts"] = attempts
        out["submit_attempts_total_sec"] = round(time.monotonic() - t_all, 3)
        out["run_reply"] = text
        task_id = None
        for ln in text.splitlines():
            if "task_id=" in ln:
                for tok in ln.replace("]", " ").split():
                    if tok.startswith("task_id="):
                        task_id = tok.split("=", 1)[1]
        # ---- query 链路：外层续查至 completed / failed / 上限 ----
        if task_id:
            out["task_id"] = task_id
            queries = []
            t1 = time.monotonic()
            while time.monotonic() - t1 < VIDEO_QUERY_TIMEOUT_TOTAL_SEC:
                qtext = await call_tool(
                    client, env_map["MCPACCESS_TOKEN"], session,
                    "query_video", task_id=task_id)
                queries.append(qtext)
                if "completed" in qtext or qtext.lstrip().startswith("http"):
                    break
                if "failed" in qtext:
                    break
                await asyncio.sleep(VIDEO_QUERY_INTERVAL_SEC)
            out["query_count"] = len(queries)
            out["query_total_sec"] = round(time.monotonic() - t1, 3)
            out["query_first_reply"] = queries[0] if queries else None
            out["query_last_reply"] = queries[-1] if queries else None
            # completed → 顶层 url 字段验证（可下载性）
            last = queries[-1] if queries else ""
            vurl = None
            for ln in (last or text).splitlines():
                if ln.startswith("http"):
                    vurl = ln
                    break
            if vurl:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
                    ) as vc:
                        vr = await vc.get(vurl)
                        out["video_url_check"] = {
                            "status": vr.status_code,
                            "bytes": len(vr.content),
                            "content_type": vr.headers.get("content-type"),
                        }
                except httpx.HTTPError as exc:
                    out["video_url_check"] = {
                        "exception": type(exc).__name__,
                        "detail": str(exc)[:150],
                    }
        run["svc_video"] = out


async def phase_raw_429(env_map: dict, run: dict, prompt: str, burst: int,
                        concurrent: bool = False) -> None:
    results = []
    async with httpx.AsyncClient(
        base_url=env_map["AGNES_BASE_URL"],
        timeout=httpx.Timeout(connect=10, read=360, write=30, pool=10),
    ) as client:
        async def one(i: int) -> dict:
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    "/v1/images/generations",
                    headers={"Authorization":
                             f"Bearer {env_map['AGNES_TOKEN']}"},
                    json={"model": IMAGE_MODEL, "prompt": f"{prompt} #{i}",
                          "size": "1024x1024"},
                )
                item = {
                    "i": i, "status": resp.status_code,
                    "elapsed_sec": round(time.monotonic() - t0, 3),
                    "retry_after": resp.headers.get("retry-after"),
                    "ratelimit_headers": {
                        k: v for k, v in resp.headers.items()
                        if "rate" in k.lower() or "limit" in k.lower()
                    },
                }
                if resp.status_code != 200:
                    try:
                        item["error_body"] = resp.json()
                    except ValueError:
                        item["body_prefix"] = resp.text[:200]
                return item
            except httpx.HTTPError as exc:
                return {
                    "i": i, "status": None,
                    "elapsed_sec": round(time.monotonic() - t0, 3),
                    "exception": type(exc).__name__,
                }
        if concurrent:
            results = list(await asyncio.gather(*[
                one(i) for i in range(burst)]))
            results.sort(key=lambda x: x["i"])
        else:
            for i in range(burst):
                results.append(await one(i))
    run["raw_429"] = {
        "burst": burst,
        "mode": "concurrent" if concurrent else "serial",
        "results": results,
        "saw_429": any(r.get("status") == 429 for r in results),
    }


PHASES = ("raw_img_default", "raw_img_2k", "raw_img_bad",
          "svc_image", "svc_image_bad", "svc_persist", "svc_video", "raw_429")


async def main_async(args) -> int:
    env_map = load_env_file(Path(args.env_file))
    for required in ("AGNES_TOKEN", "AGNES_BASE_URL", "MCPACCESS_TOKEN"):
        if not env_map.get(required):
            print(f"[live] env 缺少 {required}，中止", file=sys.stderr)
            return 2
    os.environ.update(env_map)  # 密钥只进进程环境

    wanted = list(PHASES) if args.only == "all" else [
        p.strip() for p in args.only.split(",") if p.strip()]
    unknown = [p for p in wanted if p not in PHASES]
    if unknown:
        print(f"[live] 未知阶段: {unknown}（可选: {PHASES}）", file=sys.stderr)
        return 2

    run = {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": env_map["AGNES_BASE_URL"],
        "phases_requested": wanted,
    }
    img_prompt = args.prompt or "一只戴圆框眼镜的橘猫坐在窗台看雨，暖色调胶片感"
    vid_prompt = "一只橘猫在冲浪板上乘风破浪，阳光海浪，动感镜头"

    for phase in wanted:
        print(f"[live] === 阶段 {phase} 开始 ===", flush=True)
        t0 = time.monotonic()
        try:
            if phase == "raw_img_default":
                await phase_raw_img_default(env_map, run, img_prompt)
            elif phase == "raw_img_2k":
                await phase_raw_img_2k(env_map, run, img_prompt)
            elif phase == "raw_img_bad":
                await phase_raw_img_bad(env_map, run, img_prompt)
            elif phase == "svc_image":
                await phase_svc_image(env_map, run, img_prompt)
            elif phase == "svc_image_bad":
                await phase_svc_image_bad(env_map, run, img_prompt)
            elif phase == "svc_persist":
                await phase_svc_persist(env_map, run, img_prompt)
            elif phase == "svc_video":
                await phase_svc_video(env_map, run, vid_prompt)
            elif phase == "raw_429":
                await phase_raw_429(env_map, run, img_prompt, args.burst,
                                    concurrent=args.concurrent)
        except Exception as exc:  # 单阶段失败不阻塞其余阶段（派单③纪律）
            run[phase] = {"phase_error": f"{type(exc).__name__}: {exc}"[:500]}
        print(f"[live] === 阶段 {phase} 结束（{time.monotonic() - t0:.1f}s）===",
              flush=True)

    run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data = load_results()
    data["runs"].append(scrub(run))
    save_results(data)
    print(f"[live] 完成。结果已追加 → {RESULTS_PATH}（run_id={run['run_id']}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Agnes 真机联调（V2 第六轮）")
    ap.add_argument("--env-file", default="/home/z/envs/mcp-imagegen.env",
                    help="env 文件路径（默认 /home/z/envs/mcp-imagegen.env）")
    ap.add_argument("--only", default="all",
                    help=f"阶段（逗号分隔；可选 {','.join(PHASES)}）")
    ap.add_argument("--burst", type=int, default=10,
                    help="429 连发次数（默认 10）")
    ap.add_argument("--concurrent", action="store_true",
                    help="429 连发改并发（gather 同时打满，提高触发概率）")
    ap.add_argument("--prompt", default=None, help="覆盖默认生图 prompt")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
