"""Gitee 建仓 + 推送 + 设公开（token 不落盘不输出）。

流程（SOP 九点五）：POST 建仓（默认私有）→ git push（一次性 URL）→ PATCH 公开（必须带 name）。
用法：python gitee_publish.py <repo_root> <repo_name>
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CRED = Path.home() / ".dsh" / ".credentials.yaml"


def get_token() -> str:
    """宽松正则取 GITEE_TOKEN，只返回长度校验，不打印明文。"""
    text = CRED.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"GITEE_TOKEN\s*:\s*(\S+)", text)
    if not match:
        raise SystemExit("credentials.yaml 中未找到 GITEE_TOKEN")
    token = match.group(1).strip("\"'")
    if len(token) < 20:
        raise SystemExit(f"GITEE_TOKEN 长度异常: {len(token)}")
    return token


def api(method: str, path: str, token: str, payload: dict | None = None):
    url = f"https://gitee.com/api/v5{path}"
    if payload is None:
        payload = {"access_token": token}
    else:
        payload["access_token"] = token
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]


def run(cmd: list[str], cwd: str, redact: str) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).replace(redact, "[TOKEN-REDACTED]")
    return proc.returncode, out


def main() -> int:
    repo_root = sys.argv[1]
    repo_name = sys.argv[2]
    token = get_token()
    owner = "zhaopq"  # Gitee 账号（≠ GitHub zhaopq123）

    # 1. 建仓（默认私有，先 push 再改公开——空仓库不能设公开）
    code, resp = api("POST", "/user/repos", token, {
        "name": repo_name,
        "description": "图片/视频生成 MCP Server：文生图/参考图生图/图生视频，"
                       "服务端代排队与熔断限速，510 项测试",
        "has_issues": True, "has_wiki": False,
    })
    print(f"建仓: HTTP {code} "
          f"{'(201 已建 / 已存在见下)' if code in (201, 400) else ''}")
    if code not in (201, 400):
        print("  ", str(resp)[:200])
        return 1

    # 2. push（一次性 URL，不写进 .git/config）
    url = f"https://oauth2:{token}@gitee.com/{owner}/{repo_name}.git"
    code, out = run(["git", "push", url, "main:main", "--force"],
                    repo_root, token)
    print(f"push: exit {code}\n  {out.strip()[-300:]}")
    if code != 0:
        return 1

    # 3. 确认 remote 未污染
    cfg = (Path(repo_root) / ".git" / "config").read_text()
    assert "oauth2" not in cfg, ".git/config 泄漏 token！"
    print("防泄漏检查: .git/config 无 oauth2 ✓")

    # 4. 改公开（PATCH 必须带 name）
    code, resp = api("PATCH", f"/repos/{owner}/{repo_name}", token, {
        "name": repo_name, "private": False,
        "description": "图片/视频生成 MCP Server：文生图/参考图生图/图生视频，"
                       "服务端代排队与熔断限速，510 项测试",
    })
    private = resp.get("private", "?") if isinstance(resp, dict) else "?"
    print(f"设公开: HTTP {code} private={private}")
    return 0 if (code == 200 and private is False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
