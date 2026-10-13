"""开源前安全扫描（SOP 第一节）：密钥/内网IP/主机名/个人信息/生产信息。

用法：python security_scan.py <repo_root>
只报告不修改。命中列出 文件:行号:内容预览（预览做脱敏截断）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    ("sk-密钥", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("ghp令牌", r"ghp_[A-Za-z0-9]{20,}"),
    ("AKIA", r"AKIA[0-9A-Z]{16}"),
    ("私钥头", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("内网IP-192", r"192\.168\.\d+\.\d+"),
    ("内网IP-10", r"(?<![0-9.])10\.\d+\.\d+\.\d+(?![0-9.])"),
    ("内网IP-172", r"172\.(1[6-9]|2[0-9]|3[01])\.\d+\.\d+"),
    ("环回/链路", r"127\.0\.0\.1|169\.254\.\d+\.\d+"),
    ("主机名", r"DESKTOP-[A-Z0-9]{7}"),
    ("真实邮箱", r"[A-Za-z0-9._%+\-]+@(qq|163|gmail|outlook|foxmail)\.(com|net)"),
    ("Bearer硬编码", r"Bearer [A-Za-z0-9_\-]{16,}"),
    ("token赋值", r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(root), capture_output=True, text=True
    ).stdout.split()
    return out


def mask(line: str, pat: str) -> str:
    def repl(match: re.Match) -> str:
        value = match.group(0)
        return value[:4] + f"...({len(value)}字符,已遮蔽)"

    return re.sub(pat, repl, line)[:160]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    files = tracked_files(root)
    hits: list[str] = []
    scanned = 0
    for name in files:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, PermissionError):
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pat in PATTERNS:
                if re.search(pat, line):
                    hits.append(
                        f"{name}:{lineno} [{label}] {mask(line, pat)}")
    print(f"扫描文本文件 {scanned}/{len(files)} 个，命中 {len(hits)} 处")
    for h in hits:
        print("  " + h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
