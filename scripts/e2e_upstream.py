# -*- coding: utf-8 -*-
"""容器内端到端验证：**真实上游**的生图 / 生视频链路。

与 e2e_coderender.py 的分工：那个验「代码渲染」（纯本地、不耗额度），
这个验「配好上游之后到底通不通」——会真实消耗上游额度。

用法（容器内）::

    ./venv/bin/python scripts/e2e_upstream.py                # 图 + 视频
    ./venv/bin/python scripts/e2e_upstream.py --only image    # 只测生图
    ./venv/bin/python scripts/e2e_upstream.py --poll 240      # 视频轮询预算(秒)

只输出非敏感内容（token 从 store 读，绝不打印）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8000")
STORE = os.environ.get("E2E_STORE", "/opt/mcp-imagegen/data/store.json")
IMG_PROMPT = "一只橘猫坐在窗台上打盹，午后阳光洒进来，卡通插画风格"
VID_PROMPT = "一只橘猫在窗台上伸懒腰，阳光洒进来，镜头缓慢推近"


def _token() -> str:
    store = json.load(open(STORE))
    for item in store.get("tokens") or []:
        if isinstance(item, dict) and item.get("token"):
            return item["token"]
    raise SystemExit("store 里没有可用 token")


def _parse(resp: httpx.Response) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {}
    return resp.json()


class Mcp:
    def __init__(self, timeout: float = 120.0) -> None:
        self.c = httpx.Client(timeout=timeout)
        self.h = {
            "Authorization": "Bearer " + _token(),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        r = self.c.post(BASE + "/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "e2e-upstream", "version": "1"}}})
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.h["Mcp-Session-Id"] = sid
        self.c.post(BASE + "/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.rid = 10

    def call(self, name: str, args: dict) -> str:
        self.rid += 1
        r = self.c.post(BASE + "/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": self.rid, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        res = _parse(r)["result"]
        if res.get("content"):
            return res["content"][0]["text"]
        return json.dumps(res, ensure_ascii=False)

    def tools(self) -> list:
        self.rid += 1
        r = self.c.post(BASE + "/mcp", headers=self.h, json={
            "jsonrpc": "2.0", "id": self.rid, "method": "tools/list", "params": {}})
        return [t["name"] for t in _parse(r)["result"]["tools"]]

    def fetch(self, url: str) -> tuple:
        r = self.c.get(url)
        return r.status_code, len(r.content), r.content[:8]


def show(tag: str, text: str) -> str:
    print("\n[%s]\n%s" % (tag, text))
    return text


def first_url(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    return ""


def kv(text: str, key: str) -> str:
    """从 ``a=1 b=2`` 形态的 meta/错误行里取一个值。"""
    for token in text.replace("|", " ").split():
        if token.startswith(key + "="):
            return token[len(key) + 1:]
    return ""


def test_image(mcp: Mcp) -> bool:
    ok = True
    text = show("generate_image (persist=true)",
                mcp.call("generate_image",
                         {"prompt": IMG_PROMPT, "size": "1024x1024"}))
    url = first_url(text)
    if not url:
        print("  ✗ 无产物 URL"); return False
    code, size, magic = mcp.fetch(url)
    print("  取回: HTTP %s, %d bytes, magic=%r" % (code, size, magic))
    if code != 200 or magic[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0",
                                        b"\xff\xd8\xff\xe1"):
        print("  ✗ 产物不是有效图片"); ok = False
    else:
        print("  ✓ 生图链路通")

    text2 = show("generate_image (batch_size=2, persist=false)",
                 mcp.call("generate_image",
                          {"prompt": IMG_PROMPT, "size": "1024x1024",
                           "batch_size": 2, "persist": False}))
    urls = [ln.strip() for ln in text2.splitlines() if ln.strip().startswith("http")]
    print("  返图数: %d" % len(urls))
    if len(urls) < 2:
        print("  ✗ batch_size=2 没返回两张"); ok = False
    else:
        for u in urls[:2]:
            print("  直链: HTTP %s" % (mcp.fetch(u)[0],))
    return ok


def test_video(mcp: Mcp, poll_budget: float) -> bool:
    text = show("generate_video", mcp.call("generate_video", {
        "prompt": VID_PROMPT, "seconds": 5, "aspect_ratio": "16:9"}))
    # 形态一：预算内跑完 → 直接给成片 URL
    done = first_url(text)
    if done and "status=queued" not in text:
        code, size, _ = mcp.fetch(done)
        print("  取回: HTTP %s, %d bytes" % (code, size))
        print("  ✓ 生视频链路通（阻塞到成片）")
        return code == 200
    # 形态二：上游忙 → 服务端代排队，给排队号，之后轮询
    tid = kv(text, "task_id")
    if not tid:
        print("  ✗ 既没成片 URL 也没 task_id"); return False
    if tid.startswith("q-"):
        print("  ! 上游忙，服务端代排队（task_id=%s）—— 后台 worker 会代为重试提交" % tid)

    start = time.monotonic()
    last = text
    while time.monotonic() - start < poll_budget:
        q = mcp.call("query_video", {"task_id": tid})
        if q != last:
            print("  [%4.0fs] %s" % (time.monotonic() - start,
                                     q.replace("\n", " | ")[:200]))
            last = q
        if "status=completed" in q or "status=failed" in q:
            break
        time.sleep(5)

    print("\n[query_video 终态]\n%s" % last)
    if "status=completed" in last:
        url = first_url(last)
        if url:
            print("  取回: HTTP %s" % (mcp.fetch(url)[0],))
        print("  ✓ 生视频链路通")
        return True
    print("  ✗ 未拿到成片")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("image", "video"), default=None)
    ap.add_argument("--poll", type=float, default=300.0,
                    help="视频轮询预算（秒）")
    args = ap.parse_args()

    mcp = Mcp()
    names = mcp.tools()
    print("工具清单: %s" % names)

    results = {}
    if args.only != "video":
        results["image"] = test_image(mcp)
    if args.only != "image":
        results["video"] = test_video(mcp, args.poll)

    print("\n===== 汇总 =====")
    for key, value in results.items():
        print("  %-6s %s" % (key, "PASS" if value else "FAIL"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
