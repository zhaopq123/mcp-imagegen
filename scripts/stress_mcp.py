"""mcp-imagegen V3 压力测试：多会话 / 多队列 / 并发 / 异步视频。

真实链路压测（本机 → vivo 8000 → Agnes 上游），模拟多个 MCP 客户端
会话并发调用，测量：
  - 吞吐（请求/秒）、延迟 P50/P95、错误率（含 busy 占比）
  - 泳道隔离效果：视频在跑时生图是否仍能拿到额度（V2 会全部 busy）
  - 视频异步：提交后立刻返回 task_id，后台作业独立完成

用法：
  python stress_mcp.py [--sessions 6] [--images 2] [--batch 2] [--videos 2]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import httpx

BASE = os.environ.get("MCP_BASE", "http://127.0.0.1:8000/mcp")
TOKEN = os.environ.get("MCP_TOKEN", "a3f1c9d27e6b4805b12f8ce04d7a9163")


class Session:
    """一个 MCP 客户端会话（独立 Mcp-Session-Id）。"""

    def __init__(self, name: str, client: httpx.AsyncClient):
        self.name = name
        self.client = client
        self.sid: str | None = None
        self._id = 0

    async def open(self) -> None:
        r = await self.client.post(BASE, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": self.name, "version": "1"}},
        })
        r.raise_for_status()
        self.sid = r.headers.get("Mcp-Session-Id")
        # 通知已初始化（fastmcp 要求）
        await self.client.post(BASE, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": self.sid})

    def _headers(self) -> dict:
        return {"Mcp-Session-Id": self.sid or ""}

    async def call(self, tool: str, args: dict, timeout: float = 240.0) -> tuple[float, str, str]:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
        t0 = time.monotonic()
        try:
            r = await self.client.post(BASE, json=payload,
                                       headers=self._headers(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return time.monotonic() - t0, "network_error", str(exc)
        dt = time.monotonic() - t0
        if r.status_code != 200:
            return dt, "http_error", f"{r.status_code} {r.text[:120]}"
        body = r.text
        # SSE 形态（可能夹带 : ping 心跳注释行）→ 取带 jsonrpc 的 data 行
        for line in body.splitlines():
            if line.startswith("data: ") and "jsonrpc" in line:
                body = line[6:]
                break
        try:
            data = json.loads(body)
        except ValueError:
            return dt, "bad_json", body[:120]
        if "error" in data:
            return dt, "rpc_error", json.dumps(data["error"])[:160]
        content = data.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        return dt, "ok", text


def classify(text: str) -> str:
    """结果分类（契约文本 → 标签）。"""
    if text.startswith("[error]"):
        if "category=busy" in text:
            return "busy"
        if "category=rate_limit" in text:
            return "rate_limit"
        if "category=invalid_params" in text:
            return "invalid_params"
        return "error_other"
    if "status=running" in text and "task_id=" in text:
        return "deferred"
    return "ok"


async def worker(sess: Session, idx: int, n_images: int, batch: int,
                 results: list) -> None:
    prompts = [
        "一只橘猫在窗台上晒太阳，写实摄影",
        "极简风格的蓝色咖啡杯，白色背景，产品摄影",
        "未来城市夜景霓虹，赛博朋克风格",
        "秋天的森林小径，水彩画风格",
    ]
    for i in range(n_images):
        dt, kind, text = await sess.call("generate_image", {
            "prompt": prompts[(idx + i) % len(prompts)],
            "size": "1024x1024", "batch_size": batch, "persist": True,
        })
        results.append({
            "session": sess.name, "tool": "generate_image",
            "kind": kind, "label": classify(text) if kind == "ok" else kind,
            "sec": dt, "text": text[:200],
        })


async def video_worker(sess: Session, idx: int, results: list) -> None:
    dt, kind, text = await sess.call("generate_video", {
        "prompt": "海浪拍打礁石，慢镜头，4K",
        "seconds": 5, "aspect_ratio": "16:9", "persist": True,
    })
    label = classify(text) if kind == "ok" else kind
    results.append({"session": sess.name, "tool": "generate_video",
                    "kind": kind, "label": label, "sec": dt, "text": text[:200]})
    task_id = None
    if "task_id=" in text:
        task_id = text.split("task_id=", 1)[1].split(" ", 1)[0]
    if task_id:
        # 异步续查：后台作业应继续跑完
        for _ in range(40):
            await asyncio.sleep(3)
            _, qkind, qtext = await sess.call("query_video", {"task_id": task_id})
            lab = classify(qtext) if qkind == "ok" else qkind
            if lab == "ok":
                results.append({"session": sess.name, "tool": "query_video",
                                "kind": qkind, "label": "ok", "sec": dt,
                                "text": qtext[:200]})
                return
        results.append({"session": sess.name, "tool": "query_video",
                        "kind": qkind, "label": lab, "sec": 0,
                        "text": qtext[:200]})


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--images", type=int, default=2)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--videos", type=int, default=2)
    ap.add_argument("--token", default=None)
    ap.add_argument("--base", default=None)
    args = ap.parse_args()
    global BASE, TOKEN
    if args.base:
        BASE = args.base
    if args.token:
        TOKEN = args.token

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        limits=limits, timeout=httpx.Timeout(240.0),
    ) as client:
        sessions = [Session(f"s{i}", client) for i in range(args.sessions)]
        await asyncio.gather(*(s.open() for s in sessions))
        print(f"[setup] {len(sessions)} 个 MCP 会话已建立")

        results: list[dict] = []
        t0 = time.monotonic()
        tasks = []
        # 视频先发（占住 video 泳道），随后生图并发——验证泳道隔离
        for i in range(args.videos):
            tasks.append(video_worker(sessions[i % len(sessions)], i, results))
        await asyncio.sleep(1.0)   # 让视频先进入后台等待
        for i, s in enumerate(sessions):
            tasks.append(worker(s, i, args.images, args.batch, results))
        await asyncio.gather(*tasks)
        total = time.monotonic() - t0

        # 队列实况
        hz = await client.get(BASE.replace("/mcp", "/healthz"))
        print(f"[healthz] {hz.text}")

        # 作业总览（走最后一个会话）
        _, k, t = await sessions[0].call("list_video_tasks", {"limit": 10})
        print(f"[list_video_tasks] {k}: {t[:300]}")

    # ---- 汇总 ----
    ok = [r for r in results if r["label"] == "ok"]
    busy = [r for r in results if r["label"] == "busy"]
    err = [r for r in results if r["label"] not in ("ok", "busy")]
    secs = sorted(r["sec"] for r in results)
    print("\n================ 压测结果 ================")
    print(f"总请求 {len(results)}  总耗时 {total:.1f}s  "
          f"吞吐 {len(results)/total:.2f} req/s")
    print(f"成功 {len(ok)}  busy {len(busy)}  其他错误 {len(err)}")
    if secs:
        p50 = statistics.median(secs)
        p95 = secs[min(len(secs) - 1, int(len(secs) * 0.95))]
        print(f"延迟 P50={p50:.1f}s  P95={p95:.1f}s  max={max(secs):.1f}s")
    for r in results:
        if r["label"] != "ok":
            print(f"  ! {r['session']} {r['tool']} {r['label']}: {r['text'][:120]}")
    img = [r["sec"] for r in results if r["tool"] == "generate_image"]
    if img:
        print(f"生图 n={len(img)} 平均 {sum(img)/len(img):.1f}s  "
              f"并发有效加速比 ≈ {sum(img)/len(img) and (sum(img)/len(img)):.1f}")
    return 0 if not err else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
