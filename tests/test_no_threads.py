"""R2 静态门禁：全项目禁线程（规格书 6.1；铁律 R2）。

R2：禁 threading / asyncio.to_thread / run_in_executor 等一切线程构造
    （含间接）。僵死自愈完全依赖事件循环内心跳 + systemd Watchdog。

实现：tokenize 剥离注释与字符串字面量后做模式扫描——docstring 中
"禁 threading / asyncio.to_thread" 这类说明文字不会误报；真正的 import /
调用即便藏进多行字符串拼接也逃不掉（字符串字面量本身不构成可执行代码，
但为防"eval 字符串拼代码"绕过，导入语句与调用样式一律禁止出现在代码层）。

新文件加入项目后自动纳入扫描（glob 全 .py），无需登记。
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 禁用构造（代码层）：import / 属性访问 / 调用三种形态
FORBIDDEN_PATTERNS = [
    (r"\bimport\s+threading\b", "import threading"),
    (r"\bfrom\s+threading\b", "from threading import ..."),
    (r"\bthreading\.", "threading.* 属性访问"),
    (r"\basyncio\.to_thread\b", "asyncio.to_thread"),
    (r"\bto_thread\s*\(", "to_thread(...)"),
    (r"\brun_in_executor\s*\(", "run_in_executor(...)"),
    (r"\bstart_new_thread\b", "start_new_thread"),
    (r"\bfrom\s+concurrent\b", "from concurrent.futures import ..."),
    (r"\bconcurrent\.futures\b", "concurrent.futures"),
    (r"\bthreading\.Thread\s*\(", "threading.Thread(...)"),
    (r"\bThread\s*\(\s*target\s*=", "Thread(target=...)"),
]


def _strip_comments_and_strings(source: str) -> str:
    """tokenize 剥离 COMMENT 与 STRING/追尾字符串 → 只留可执行代码流。"""
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        # 语法异常时退回原文本（宁可误报不可漏报）
        return source
    return " ".join(out)


def _python_files() -> list[Path]:
    files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        parts = path.relative_to(PROJECT_ROOT).parts
        if any(seg in ("venv", ".venv", "__pycache__", ".git", "node_modules")
               for seg in parts):
            continue
        files.append(path)
    assert files, "扫描器异常：一个 .py 都没找到"
    return files


def test_no_thread_constructs_anywhere():
    """全项目（app/engine/infra/ops/tests/config/server）零线程构造。"""
    violations: list[str] = []
    for path in _python_files():
        code = _strip_comments_and_strings(path.read_text(encoding="utf-8"))
        for pattern, desc in FORBIDDEN_PATTERNS:
            for m in re.finditer(pattern, code):
                snippet = code[max(0, m.start() - 30):m.end() + 30]
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}: 发现 {desc} … {snippet!r}"
                )
    assert not violations, "R2 铁律违例（禁线程）：\n" + "\n".join(violations)


def test_no_thread_import_in_dependencies_comment_only():
    """佐证扫描有效：liveness.py docstring 提及禁用清单（字符串），代码层零命中。"""
    src = (PROJECT_ROOT / "ops" / "liveness.py").read_text(encoding="utf-8")
    code = _strip_comments_and_strings(src)
    assert "threading" in src  # docstring 提及（中文说明）
    assert "threading" not in code  # 代码层未出现（被 tokenize 剥离）
