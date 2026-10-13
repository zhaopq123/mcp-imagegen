"""统一仓库行尾为 LF（历史 scp/tar 混入 CRLF 的根治）。

用法：python normalize_eol.py [--write]
不带 --write 只报告；带 --write 落盘改写。
"""
from __future__ import annotations

import sys
from pathlib import Path

TEXT_SUFFIXES = {
    ".py", ".sh", ".md", ".env", ".ini", ".cfg", ".txt", ".json", ".yml",
    ".yaml", ".toml", ".service", ".timer", ".gitignore", ".lock",
}


def find(root: Path):
    out = []
    for cmd in ("git ls-files",):
        pass
    import subprocess
    files = subprocess.run(
        cmd, shell=True, cwd=str(root), capture_output=True, text=True
    ).stdout.split()
    for name in files:
        path = root / name
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in (
                ".gitignore", "LICENSE", "Caddyfile"):
            continue
        raw = path.read_bytes()
        if b"\r\n" in raw or b"\r" in raw:
            out.append((name, raw.count(b"\r\n"), len(raw)))
    return out


def main():
    root = Path.cwd()
    write = "--write" in sys.argv
    hits = find(root)
    print(f"=== 含 CR 的跟踪文件 {len(hits)} 个 ===")
    for name, crlf_count, size in hits:
        print(f"  {name:52s} CRLF={crlf_count:5d} size={size}")
    if not write:
        print("\n（只报告。加 --write 落盘）")
        return
    for name, _c, _s in hits:
        path = root / name
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    print(f"\n已把 {len(hits)} 个文件统一为 LF")


if __name__ == "__main__":
    main()
