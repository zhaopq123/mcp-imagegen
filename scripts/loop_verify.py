"""V4 完整出片闭环验证（mock 上游，零额度消耗）。

链路全程跟真实调用一致，唯一把上游换成 mock：

    本机参考图 → POST /upload
      → generate_image(image=[产物])          参考图生图 V4 新能力
      → generate_video(first_frame=<上一步产物>) 图生视频 V4 新能力
      → 上游队列满(503) → 服务端代排队 → 退避重试提交
      → 轮询 → completed → persist 落盘
      → query_video / list_video_tasks 取终态

用法（容器内）：./venv/bin/python loop_verify.py
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8002"
TOKEN = "stresstoken"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001")

_session: str | None = None


def post(path: str, payload=None, raw: bytes | None = None):
    global _session
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
    if raw is not None:
        headers["Content-Type"] = "image/png"
    if _session:
        headers["Mcp-Session-Id"] = _session
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        sess = resp.headers.get("Mcp-Session-Id")
        if sess:
            _session = sess
        return resp.read().decode("utf-8", "replace")


def rpc(method: str, params: dict) -> dict:
    text = post("/mcp", {"jsonrpc": "2.0", "id": 1,
                         "method": method, "params": params})
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {}


def call(name: str, args: dict) -> str:
    res = rpc("tools/call", {"name": name, "arguments": args})
    result = res.get("result", {})
    return "\n".join(c.get("text", "")
                     for c in result.get("content", []) if isinstance(c, dict))


def mock_stats() -> dict:
    with urllib.request.urlopen(
            "http://127.0.0.1:8001/mock-stats", timeout=10) as resp:
        return json.loads(resp.read().decode())


def check(title: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK  ' if ok else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "loop", "version": "1"}})
    passed = True

    # ---- 1. 上传参考图（本机 → 服务端）----
    up = json.loads(post("/upload", raw=PNG))
    passed &= check("1. 参考图上传 → /files/<name>",
                    bool(up.get("url", "").startswith("/files/")), str(up)[:120])
    ref_name = up["name"]

    # ---- 2. 参考图生图 ----
    img = call("generate_image", {
        "prompt": "mock 场景合成测试", "image": [ref_name],
        "size": "1K", "persist": True})
    passed &= check("2. 参考图生图出图", img.startswith("http") and "refs=1" in img,
                    img.replace("\n", " | ")[:130])
    produced = img.split("/files/")[-1].split()[0] if "/files/" in img else ""

    # ---- 3. 图生视频（first_frame），走代排队路径 ----
    t0 = time.time()
    vid = call("generate_video", {
        "prompt": "让这张图缓慢动起来",
        "first_frame": produced or ref_name,
        "seconds": 5, "aspect_ratio": "16:9", "persist": True})
    passed &= check("3. 首帧图生视频受理（含代排队）",
                    ("status=queued" in vid) or vid.startswith("http"),
                    vid.replace("\n", " | ")[:130])

    # ---- 4. 轮询到终态 ----
    task_id = vid.split("task_id=")[1].split()[0].strip("；;, ") if "task_id=" in vid else ""
    final = vid
    if task_id:
        for _ in range(30):
            time.sleep(2)
            body = call("query_video", {"task_id": task_id})
            if ".mp4" in body or "completed" in body.lower() \
                    or "[error]" in body:
                final = body
                break
            final = body
    passed &= check("4. 轮询到终态并出片",
                    ".mp4" in final or "/files/" in final,
                    final.replace("\n", " | ")[:160])
    print(f"      出片耗时 {time.time() - t0:.1f}s")

    # ---- 5. 代排队确实发生过（mock 记录）----
    stats = mock_stats()
    subs = stats.get("submits", [])
    passed &= check("5. 上游先队列满后代排队重试成功",
                    subs[:2] == ["503-queue-full", subs[1]] and subs[1].startswith("200"),
                    f"submits={subs}")

    # ---- 6. 终态可观测（工具面）----
    listing = call("list_video_tasks", {"limit": 3})
    passed &= check("6. list_video_tasks 可观测", "tasks" in listing, listing[:160])

    print("\n=== 闭环结论:", "PASS" if passed else "FAIL", "===")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
