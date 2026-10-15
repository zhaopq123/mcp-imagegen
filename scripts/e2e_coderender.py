# -*- coding: utf-8 -*-
"""容器内端到端验证：MCP 层的代码生图 / 代码生视频。

只用非敏感输出（token 从 store 读取，绝不打印）。
"""
import json

import httpx

store = json.load(open("/opt/mcp-imagegen/data/store.json"))
tok = store["tokens"][0]["token"]
BASE = "http://127.0.0.1:8000"
H = {"Authorization": "Bearer " + tok, "Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}


def parse(r):
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {}
    return r.json()


SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300">'
    '<rect width="600" height="300" fill="#0d1117"/>'
    '<circle cx="150" cy="150" r="90" fill="#2f81f7"/>'
    '<text x="280" y="160" font-size="34" fill="#e6edf3">代码生图 OK</text>'
    '</svg>'
)

PYCODE = "\n".join([
    "import math",
    "bars = []",
    "for i in range(12):",
    "    h = int((math.sin(i / 2) + 1) * 60) + 10",
    "    bars.append('<rect x=\"%d\" y=\"%d\" width=\"24\" height=\"%d\" "
    "fill=\"#f78166\"/>' % (30 + i * 40, 200 - h, h))",
    "svg = ('<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"%d\" "
    "height=\"200\">%s</svg>' % (width, ''.join(bars)))",
])

ANIM = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200">'
    '<rect width="360" height="200" fill="#161b22"/>'
    '<rect x="0" y="80" width="60" height="40" rx="8" fill="#3fb950">'
    '<animate attributeName="height" values="10;40;10" dur="1s" '
    'repeatCount="indefinite"/></rect>'
    '<circle cx="90" cy="100" r="18" fill="#2f81f7"/>'
    '<text x="140" y="110" font-size="24" fill="#e6edf3">frame __FRAME__</text>'
    '</svg>'
)

ESCAPE = 'svg = str(().__class__.__bases__[0].__subclasses__())'

with httpx.Client(timeout=90) as c:
    r = c.post(BASE + "/mcp", headers=H, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "e2e", "version": "1"}}})
    H2 = dict(H)
    sid = r.headers.get("mcp-session-id")
    if sid:
        H2["Mcp-Session-Id"] = sid
    else:
        print("警告：initialize 未返回 Mcp-Session-Id")
    c.post(BASE + "/mcp", headers=H2, json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    r = c.post(BASE + "/mcp", headers=H2, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = parse(r)["result"]["tools"]
    names = [t["name"] for t in tools]
    print("工具清单:", names)
    print("两者已注册:", "render_image" in names, "render_video" in names)
    print("schema 无 anyOf:",
          all("anyOf" not in json.dumps(t["inputSchema"]) for t in tools))

    def call(name, args, rid):
        rr = c.post(BASE + "/mcp", headers=H2, json={
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": args}})
        res = parse(rr)["result"]
        if res.get("content"):
            return res["content"][0]["text"]
        return json.dumps(res, ensure_ascii=False)

    def show(tag, text):
        print("\n[%s]\n%s" % (tag, text))
        return text

    t1 = show("render_image svg", call(
        "render_image", {"code": SVG, "size": "600x300"}, 3))
    t2 = show("render_image python", call(
        "render_image", {"code": PYCODE, "size": "520x200"}, 4))
    t3 = show("render_video svg", call(
        "render_video", {"code": ANIM, "size": "360x200", "frames": 10,
                         "fps": 10}, 5))
    t4 = show("render_video python", call(
        "render_video", {"code": "svg = ('<svg xmlns=\"http://www.w3.org/2000/"
                         "svg\" width=\"240\" height=\"120\"><rect x=\"%d\" "
                         "y=\"40\" width=\"40\" height=\"40\" fill=\"#2f81f7\" "
                         "/></svg>' % (frame * 12))",
                         "size": "240x120", "frames": 8, "fps": 8}, 6))
    t5 = show("render_image 越界 size", call(
        "render_image", {"code": SVG, "size": "huge"}, 7))
    t6 = show("render_video 静态 svg", call(
        "render_video", {"code": "<svg><circle r=\"5\"/></svg>"}, 8))
    t7 = show("render_image 沙箱逃逸", call(
        "render_image", {"code": ESCAPE, "lang": "python"}, 9))

    # 取回产物，验证是真图
    for tag, text in (("image", t1), ("video", t3), ("gif-py", t4)):
        url = text.splitlines()[0].strip()
        if not url.startswith("http"):
            print("\n[取回 %s] 无 URL，跳过" % tag)
            continue
        rr = c.get(url)
        head = rr.content[:16]
        print("[取回 %s] HTTP %s, %d bytes, magic=%r" % (
            tag, rr.status_code, len(rr.content), head[:8]))
    # 清理：不删除产物（/files 供人工查看）
    print("\n完成。")
