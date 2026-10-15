"""代码生图 / 代码生视频：用代码渲染出图和视频。

为什么有这套工具
----------------
`generate_image` 是「文字→AI 画图」，画不了精确的东西：图表、示意图、
带文字的海报、需要精确排版的封面——这些用代码画反而更快更准。
所以这里提供一条**代码直出**的通道：给一段 SVG（或一段生成 SVG 的 Python），
服务端渲染成 PNG/视频返回。

设计要点
--------
1. **SVG 是代码载体**：SVG 本身就是代码，天然适合「用代码生成图」，
   且不需要浏览器/无头内核，容器里 ImageMagick 就能渲染。
2. **纯离线渲染**：不消耗任何上游额度，不联网。
3. **可复现**：同一段代码永远渲染出同一张图，适合模板化/批量的产出。
4. **安全边界**：不执行任意代码。用户传的是 SVG 或一段受限的 Python，
   后者只在受限命名空间里跑，禁止 import / 打开文件 / 网络，
   超时与输出大小都有上限（见 sandbox）。

渲染后端优先级：
1. ImageMagick `convert`（容器自带，够用）
2. cairosvg（若装了，质量更好）
缺失时给出可执行的安装提示，而不是抛栈。
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── 限制（防止把服务当渲染农场用）──
MAX_SVG_BYTES = 512 * 1024          # 单帧 SVG 上限 512KB
MAX_PNG_BYTES = 32 * 1024 * 1024    # 单张成图上限 32MB
MAX_FRAMES = 120                    # 视频帧数上限（约 6s @20fps）
MAX_RENDER_SEC = 20.0                # 单帧渲染超时
MAX_TOTAL_SEC = 90.0                 # 一次视频请求的总时长上限（防渲染农场）
DEFAULT_SIZE = (1024, 1024)
DEFAULT_VIDEO_SIZE = (720, 720)
VIDEO_FORMATS = ("auto", "mp4", "gif")

# SVG 根标签检查用的宽松正则
_SVG_ROOT_RE = re.compile(r"<svg\b", re.IGNORECASE)

# ── Python 沙箱：白名单 ──────────────────────────────────────────
# 用户代码只在「显式白名单 builtins」的命名空间里跑：
# 没有 open / eval / exec / compile / __import__ / getattr / globals …
# 注意 __build_class__ 必须留（否则 `class Foo:` 直接崩，报错还很难懂）。
_SANDBOX_BUILTINS = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "chr", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "hash", "hex", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "oct", "ord", "pow",
    "range", "repr", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip", "object", "super", "type", "print",
    "Exception", "ArithmeticError", "AttributeError", "AssertionError",
    "IndexError", "KeyError", "LookupError", "MemoryError", "NameError",
    "NotImplementedError", "OSError", "OverflowError", "RecursionError",
    "RuntimeError", "StopIteration", "TypeError", "UnboundLocalError",
    "ValueError", "ZeroDivisionError", "__build_class__",
)
# 预置模块：画图常用（数学/取色/随机），全部是纯计算模块，不带 IO
_SANDBOX_MODULES = ("math", "json", "random", "statistics", "colorsys")

# 静态门禁：命中即拒（明确报错，比让它在运行时莫名其妙地崩好）
# 1) dunder 逃逸面（().__class__.__bases__[0].__subclasses__() 这类经典越狱）
_SANDBOX_BLOCKED_ATTR = re.compile(
    r"__(?:class|bases|base|subclasses|mro|globals|builtins|import|loader|"
    r"spec|dict|code|closure|getattribute|getattr|reduce|init_subclass)__"
)
# 2) 危险调用（白名单里本来就没有，这里提前拦，错误信息更直白）
_SANDBOX_BLOCKED_CALL = re.compile(
    r"\b(?:open|eval|exec|compile|globals|locals|vars|input|breakpoint|"
    r"exit|quit|help|memoryview|getattr|setattr|delattr)\s*\("
)


def _which(*names: str) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# 同名程序很可能是别的东西（Windows 的 C:\Windows\System32\convert.exe 是
# FAT→NTFS 转换工具，参数完全对不上）。所以「有没有」不能只看 which，
# 必须跑一次 `-version` 认亲，认错等于把故障推给用户（报"渲染失败"却看不出原因）。
_MAGICK_CACHE: list = []
_FFMPEG_CACHE: list = []


def _probe(cmd: list[str], marker: str) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001 —— 起不来就当没有
        return False
    blob = (proc.stdout or b"") + (proc.stderr or b"")
    return marker.encode() in blob


def _magick() -> Optional[str]:
    """真正的 ImageMagick 可执行文件路径；无则 None（结果缓存）。"""
    if _MAGICK_CACHE:
        return _MAGICK_CACHE[0]
    found = None
    for name in ("magick", "convert"):
        path = shutil.which(name)
        if path and _probe([path, "-version"], "ImageMagick"):
            found = path
            break
    _MAGICK_CACHE.append(found)
    return found


def _ffmpeg() -> Optional[str]:
    """真正的 ffmpeg 路径；无则 None（结果缓存）。"""
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    path = shutil.which("ffmpeg")
    found = path if path and _probe([path, "-version"], "ffmpeg") else None
    _FFMPEG_CACHE.append(found)
    return found


def renderer_available() -> Optional[str]:
    """返回可用的渲染器名；都没有则返回 None。"""
    if _magick():
        return "imagemagick"
    try:
        import cairosvg  # noqa: F401
        return "cairosvg"
    except Exception:  # noqa: BLE001
        return None


# ── 中文字体（不指定 → IM 静默不画中文）────────────────────────────
# 线上实测：容器里有 DroidSansFallbackFull.ttf（覆盖中日韩），
# fontconfig 也认得它，但 ImageMagick 的**内置 SVG 渲染器不做字体回退**——
# <text> 没写 font-family 时它用自带默认字体（只有拉丁字形），
# 中文/日文直接不画、也不报错，用户只会看到"图上少了字"。
# 解药是显式 `-font <能覆盖中文的字体文件>`（实测有效；family 名不管用）。
_FONT_CACHE: list = []
_FONT_HINTS = ("wqy", "microhei", "zenhei", "sourcehan", "source han",
               "noto", "cjk", "droid", "fallback", "simhei", "simsun",
               "msyh", "arphic", "uming", "ukai", "heiti", "songti")
_FONT_DIRS = (
    "/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/fonts/truetype",
    "/usr/share/fonts/opentype", "/System/Library/Fonts",  # macOS（本地调试）
)


def _font_hint_rank(path: str) -> int:
    low = path.lower()
    for i, hint in enumerate(_FONT_HINTS):
        if hint in low:
            return i
    return len(_FONT_HINTS)


def _fc_list_zh() -> list:
    """问 fontconfig 要「覆盖中文」的字体文件（有 fc-list 就优先用它）。"""
    exe = shutil.which("fc-list")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, ":lang=zh", "file"],
                              capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        path = line.split(":")[0].strip()
        if path and os.path.isfile(path):
            out.append(path)
    return out


def _scan_font_dirs() -> list:
    """兜底：翻字体目录找名字像中文的字体的文件。"""
    found = []
    for root in _FONT_DIRS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if len(found) > 40:
                break
            for name in files:
                low = name.lower()
                if low.endswith((".ttc", ".ttf", ".otf")) and any(
                        h.replace(" ", "") in low.replace(" ", "").replace("-", "")
                        for h in _FONT_HINTS):
                    found.append(os.path.join(dirpath, name))
    return found


def cjk_font() -> Optional[str]:
    """给 ImageMagick 用的中文字体路径；找不到返回 None（结果缓存）。

    优先级：环境变量 CODERENDER_FONT / WS_RENDER_FONT（运维可覆盖）
    → fc-list 的中文库 → 字体目录名匹配。候选按「好看程度」投票
    （文泉驿/思源 > Noto > Droid 兜底）。
    """
    if _FONT_CACHE:
        return _FONT_CACHE[0]
    found: Optional[str] = None
    for env in ("CODERENDER_FONT", "WS_RENDER_FONT"):
        cand = (os.environ.get(env) or "").strip()
        if cand and os.path.isfile(cand):
            found = cand
            break
    if not found:
        for source in (_fc_list_zh(), _scan_font_dirs()):
            if source:
                found = sorted(source, key=_font_hint_rank)[0]
                break
    _FONT_CACHE.append(found)
    if not found:
        log.warning(
            "[coderender] 未找到中文字体：SVG 里的中文会被 ImageMagick "
            "静默丢掉（只画拉丁字形）。装一个即可，例如 "
            "apt-get install -y fonts-droid-fallback（或用 CODERENDER_FONT "
            "指定字体文件绝对路径）")
    return found


def _normalize_svg(code: str, width: int, height: int) -> str:
    """把用户的 SVG 补齐成可渲染的完整文档（缺尺寸/命名空间就补上）。"""
    text = (code or "").strip()
    if "<svg" not in text.lower():
        # 允许用户只给一段形状片段：包一层
        text = ('<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}">' + text + "</svg>")
        return text
    # 已有 <svg>：补 viewBox / 尺寸（只在缺失时补，不覆盖用户的）
    def _ensure(attr: str, value: str) -> str:
        nonlocal text
        if not re.search(rf"\b{attr}\s*=", text, re.IGNORECASE):
            text = re.sub(r"(<svg\b)", rf'\1 {attr}="{value}"', text, count=1,
                          flags=re.IGNORECASE)
    _ensure("width", str(width))
    _ensure("height", str(height))
    if not re.search(r"\bviewBox\s*=", text, re.IGNORECASE):
        text = re.sub(r"(<svg\b)", rf'\1 viewBox="0 0 {width} {height}"', text,
                      count=1, flags=re.IGNORECASE)
    return text


def _check_size(svg: str) -> None:
    n = len(svg.encode("utf-8"))
    if n > MAX_SVG_BYTES:
        raise ValueError(
            f"SVG 过大：{n // 1024}KB > {MAX_SVG_BYTES // 1024}KB 上限")



def _assert_source_allowed(code: str) -> None:
    """静态门禁：命中即拒（明确报错，比运行时莫名其妙地崩好）。

    R2（本项目铁律：禁线程）的连带影响：这里不能靠线程池去隔离执行，
    只能靠子进程——顺带把「启动子进程」这件事本身当作隔离面。
    """
    if len(code.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError("代码过长")
    hit = _SANDBOX_BLOCKED_ATTR.search(code) or _SANDBOX_BLOCKED_CALL.search(code)
    if hit:
        raise ValueError(
            f"代码包含沙箱禁止的写法：{hit.group(0)}——"
            "本沙箱不开放文件/网络/内省能力，请只用数学与字符串拼出 SVG")


def _sandbox_runner_source(code: str, width: int, height: int) -> str:
    """拼出子进程要跑的执行器源码（SRC 用 repr 注入，不做字符串格式化）。"""
    prelude = (
        "_SAFE_NAMES = " + repr(_SANDBOX_BUILTINS) + "\n"
        "_SAFE_MODULES = " + repr(_SANDBOX_MODULES) + "\n"
        f"_WIDTH = {int(width)}\n_HEIGHT = {int(height)}\n"
        "SRC = " + repr(code) + "\n"
    )
    return prelude + _SANDBOX_RUNNER


def python_to_svg(code: str, width: int, height: int,
                  timeout: float = 5.0) -> str:
    """在受限沙箱里跑用户 Python，取它产出的 SVG 字符串（**同步门面**）。

    服务内（异步上下文）请用 `await python_to_svg_async(...)`：本函数
    内部自己起事件循环，在已有循环里调用会直接报错（R2 禁线程，没有
    别的隔离手段可用）。

    沙箱边界（重要，别削弱）：
    - 独立子进程 + `timeout` 硬杀，防死循环
    - `__builtins__` **白名单**（显式列举）：没有 open / eval / exec /
      compile / __import__ 之外的危险项，也没有 getattr / globals
    - 源码静态门禁：dunder 逃逸面与危险调用**命中即拒**
    - `-I` 隔离模式 + 干净 env（不继承 HOME/密钥等环境变量）
    - 只接收产出：变量 `svg`/`out`/`result`/`image`/`output`，或 print 出来

    ⚠️ 这是**软边界**：CPython 层做沙箱挡得住误用与直接攻击，但挡不住
    铁了心研究解释器漏洞的人。服务本身有 Bearer 令牌门（/mcp 需鉴权），
    不要在无鉴权环境里暴露本工具。
    """
    _assert_source_allowed(code)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_sandbox_exec(code, width, height, timeout))
    raise RuntimeError(
        "python_to_svg 是同步门面，事件循环里请用 await python_to_svg_async")


async def python_to_svg_async(code: str, width: int, height: int,
                              timeout: float = 5.0) -> str:
    """沙箱执行的异步入口（服务内一律走这条；见 python_to_svg 说明）。"""
    _assert_source_allowed(code)
    return await _sandbox_exec(code, width, height, timeout)


async def _sandbox_exec(code: str, width: int, height: int,
                        timeout: float) -> str:
    """起子进程跑用户代码，拿它 stdout 里的 SVG。

    用 asyncio 子进程 API 而不是 subprocess.run：本项目 R2 禁线程，
    也没有线程池可用，阻塞式调用会把整个事件循环（含看门狗心跳）钉住。
    """
    runner = _sandbox_runner_source(code, width, height)
    workdir = tempfile.mkdtemp(prefix="sandbox-")
    try:
        py = os.path.join(workdir, "gen.py")
        with open(py, "w", encoding="utf-8") as fh:
            fh.write(runner)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", py,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=workdir, env=_sandbox_env(workdir))
        except (FileNotFoundError, NotImplementedError) as exc:
            raise ValueError(f"无法启动 Python 子进程：{exc}")
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill(proc)
            await _reap(proc)
            raise ValueError(
                f"代码执行超时（>{timeout:.0f}s），可能有死循环")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out = out_b.decode("utf-8", "replace").strip()
    if not out or "<svg" not in out.lower():
        err = err_b.decode("utf-8", "replace").strip()[:300]
        raise ValueError(
            "代码没有产出 SVG。请 print 出来，或赋值给 svg / out / result "
            "变量（返回值需含 <svg>）"
            + (f"（stderr: {err}）" if err else ""))
    return out


def _kill(proc) -> None:
    """尽力杀死子进程（已经退出则忽略）。"""
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def _reap(proc) -> None:
    """回收被杀的子进程，避免僵尸与 unclosed transport 告警。"""
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


# 子进程跑的执行器：SRC 由外面拼到最前面（repr 注入，不做字符串格式化）
_SANDBOX_RUNNER = r'''
import io, sys, builtins

safe = {n: getattr(builtins, n) for n in _SAFE_NAMES if hasattr(builtins, n)}

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """只放行纯计算白名单模块；其余一律 ImportError。

    喂白名单 builtins 会让 `import x` 变成 `__import__ not found`——报错
    莫名其妙。给一个只会拒绝的 __import__，错误信息就能说人话。
    """
    root = str(name).split(".")[0]
    if level == 0 and root in _SAFE_MODULES:
        return __import__(name, globals, locals, fromlist, level)
    raise ImportError(
        "沙箱不提供 %r（可用：%s）" % (name, ", ".join(_SAFE_MODULES)))

safe["__import__"] = _safe_import

g = {"__builtins__": safe, "__name__": "__main__",
     "width": _WIDTH, "height": _HEIGHT}
for _m in _SAFE_MODULES:
    try:
        g[_m] = __import__(_m)
    except Exception:
        pass
if "random" in g:
    g["random"].seed(0)          # 固定种子：同一段代码永远渲染出同一张图

_buf = io.StringIO()
_old = sys.stdout
sys.stdout = _buf
_error = ""
try:
    exec(SRC, g, g)
except BaseException as _exc:
    _error = "%s: %s" % (type(_exc).__name__, _exc)
finally:
    sys.stdout = _old

_result = None
for _k in ("svg", "out", "result", "image", "output", "code"):
    _v = g.get(_k)
    if isinstance(_v, str) and "<svg" in _v.lower():
        _result = _v
        break
if _result is None:
    _text = _buf.getvalue().strip()
    if "<svg" in _text.lower():
        _result = _text
if _result:
    sys.stdout.write(_result)
else:
    sys.stderr.write(_error or "代码跑完了，但没有产出含 <svg> 的字符串")
'''


def _sandbox_env(workdir: str) -> dict:
    """子进程环境：只留跑 Python 必需的，不带出宿主环境变量。"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": workdir,
        "TMPDIR": workdir, "TEMP": workdir, "TMP": workdir,
        "PYTHONIOENCODING": "utf-8",   # 中文 SVG 不被平台默认编码搞坏
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",  # 别在临时目录里留 __pycache__
    }
    for keep in ("SYSTEMROOT", "WINDIR"):  # Windows 子进程起不来就是这个缺了
        if os.environ.get(keep):
            env[keep] = os.environ[keep]
    return env


# ────────────────────────── 渲染 ──────────────────────────

# cairosvg 的一次性渲染脚本（子进程运行；SVG/PNG 路径与目标尺寸走 argv）
_CAIROSVG_SCRIPT = (
    "import sys, cairosvg;"
    "cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2],"
    " output_width=int(sys.argv[3]), output_height=int(sys.argv[4]))"
)


async def _cairosvg_png(svg_path: str, png_path: str,
                        width: int, height: int) -> Optional[str]:
    """用 cairosvg 渲染（子进程）；不可用或失败 → None，由调用方回退。

    为什么要起子进程而不是 import 后直接调：cairosvg 是同步阻塞的 C 扩展，
    本项目 **R2 禁线程**（没有线程池可用），直接在协程里调会把事件循环
    （含看门狗心跳）钉死；子进程是唯一干净的隔离手段。
    """
    try:
        import cairosvg  # noqa: F401 —— 只为判断可用性，不在这里调用
    except Exception:  # noqa: BLE001
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _CAIROSVG_SCRIPT,
            svg_path, png_path, str(width), str(height),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=MAX_RENDER_SEC)
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        log.info("[coderender] cairosvg 子进程失败，回退 ImageMagick：%s", exc)
        return None
    if os.path.exists(png_path):
        return png_path
    log.info("[coderender] cairosvg 未产出 PNG，回退 ImageMagick：%s",
             err.decode("utf-8", "replace").strip()[:200])
    return None


def _magick_cmd(conv: str, svg_path: str, png_path: str,
                width: int, height: int) -> list:
    """拼 ImageMagick 渲染命令（独立出来是为了能单测「有没有带 -font」）。"""
    # -font 必须在输入文件之前（IM6 里它是 setting，不是 operator）。
    # 不指定就可能整段中文不画——见 cjk_font() 的说明。
    cmd = [conv, "-background", "none"]
    font = cjk_font()
    if font:
        cmd += ["-font", font]
    cmd += [svg_path, "-resize", f"{width}x{height}!", png_path]
    return cmd


async def _render_svg_to_png(svg: str, width: int, height: int,
                             workdir: str, name: str) -> str:
    """SVG → PNG 文件路径。优先 cairosvg，回退 ImageMagick。"""
    _check_size(svg)
    svg_path = os.path.join(workdir, name + ".svg")
    png_path = os.path.join(workdir, name + ".png")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    # 先试 cairosvg（渲染质量更好、字体更准）——同样起子进程：cairosvg 是
    # 同步阻塞的 C 扩展调用，本项目禁线程，只能在子进程里跑。
    cairo = await _cairosvg_png(svg_path, png_path, width, height)
    if cairo is not None:
        return cairo

    conv = _magick()
    if not conv:
        raise RuntimeError(
            "服务端缺少渲染器。请在容器内安装其一："
            "`apt-get install -y imagemagick` 或 `pip install cairosvg`")
    cmd = _magick_cmd(conv, svg_path, png_path, width, height)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=MAX_RENDER_SEC)
    except asyncio.TimeoutError:
        _kill(proc)
        await _reap(proc)
        raise RuntimeError(f"渲染超时（>{MAX_RENDER_SEC:.0f}s）")
    if not os.path.exists(png_path):
        raise RuntimeError(
            "渲染失败：" + (err.decode("utf-8", "replace").strip()[:200] or "未知错误"))
    return png_path


def _png_size_bytes(data: bytes) -> tuple[int, int]:
    """从 PNG 字节读尺寸（只解析 IHDR，不依赖 PIL）。"""
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return (0, 0)
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    except Exception:  # noqa: BLE001
        return (0, 0)


def _png_size(path: str) -> tuple[int, int]:
    """读 PNG 尺寸（只解析 IHDR，不依赖 PIL）。"""
    try:
        with open(path, "rb") as fh:
            return _png_size_bytes(fh.read(26))
    except Exception:  # noqa: BLE001
        return (0, 0)

# ────────────────────────── 公开入口 ──────────────────────────

async def render_image_from_code(
    code: str,
    width: int = DEFAULT_SIZE[0],
    height: int = DEFAULT_SIZE[1],
    lang: str = "svg",
    timeout: float = MAX_RENDER_SEC,
) -> dict:
    """代码 → 一张PNG。返回 {ok, png_path, size, svg, error?}。

    lang:
      - "svg"    直接就是 SVG 代码（最常用）
      - "python"跑一段 Python 产出 SVG
    """
    if not renderer_available():
        return {"ok": False,
                "error": "服务端未安装渲染器。请在容器内执行："
                          "apt-get install -y imagemagick（或 pip install cairosvg）"}
    width = max(16, min(int(width or DEFAULT_SIZE[0]), 4096))
    height = max(16, min(int(height or DEFAULT_SIZE[1]), 4096))
    try:
        if lang == "python":
            svg = await python_to_svg_async(code, width, height, timeout)
        else:
            svg = code
        svg = _normalize_svg(svg, width, height)
        _check_size(svg)
        # 用 mkdtemp + 手动清理：TemporaryDirectory 上下文退出就把文件删了，
        # 调用方（存/转存）还没来得及用。
        workdir = tempfile.mkdtemp(prefix="coderender-")
        try:
            png = await _render_svg_to_png(svg, width, height, workdir, "out")
            with open(png, "rb") as fh:
                data = fh.read()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if len(data) > MAX_PNG_BYTES:
            return {"ok": False,
                    "error": f"成图过大：{len(data) // 1024 // 1024}MB"}
        w, h = _png_size_bytes(data)
        return {"ok": True, "png": data, "size": [w or width, h or height],
                "svg": svg}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("[coderender] 渲染异常")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def render_video_from_code(
    code: str,
    width: int = DEFAULT_VIDEO_SIZE[0],
    height: int = DEFAULT_VIDEO_SIZE[1],
    fps: int = 12,
    frames: int = 36,
    lang: str = "svg",
    video_format: str = "auto",
    timeout: float = MAX_RENDER_SEC,
) -> dict:
    """代码 → 一段视频（MP4 或 GIF）。

    实现：逐帧执行代码产出 SVG → 渲染 PNG → 交给 ffmpeg/ImageMagick 合成。
    帧数、单帧时长、总时长三重上限，防止把服务当渲染农场：

    - frames ≤ MAX_FRAMES（120）
    - 单帧 ≤ MAX_RENDER_SEC（20s）
    - 整次请求 ≤ MAX_TOTAL_SEC（90s）——单帧上限乘帧数会到 40 分钟，
      所以必须有总闸门

    video_format: auto（有 ffmpeg 出 mp4，否则 gif）/ mp4 / gif
    返回 {ok, path, format, frames, fps, size, workdir, error?}
    """
    if not renderer_available():
        return {"ok": False,
                "error": "服务端未安装渲染器（imagemagick / cairosvg）"}
    fmt = (video_format or "auto").strip().lower()
    if fmt not in VIDEO_FORMATS:
        return {"ok": False, "error": f"不支持的 format={video_format}"}
    if fmt == "mp4" and not _ffmpeg():
        return {"ok": False,
                "error": "服务端没有 ffmpeg，无法输出 mp4；请改用 format=gif"}
    fps = max(1, min(int(fps or 12), 30))
    frames = max(1, min(int(frames or 36), MAX_FRAMES))
    width = max(16, min(int(width or DEFAULT_VIDEO_SIZE[0]), 1920))
    height = max(16, min(int(height or DEFAULT_VIDEO_SIZE[1]), 1920))

    pngs: list[str] = []
    workdir = ""
    started = time.monotonic()
    try:
        workdir = tempfile.mkdtemp(prefix="codevideo-")
        for i in range(frames):
            left = frames - i
            budget = MAX_TOTAL_SEC - (time.monotonic() - started)
            if budget <= 0.5:
                return {"ok": False, "workdir": workdir,
                        "error": f"渲染总时长超过 {MAX_TOTAL_SEC:.0f}s 上限，"
                                 f"请减少 frames（当前 {frames}）或简化代码"}
            frame_timeout = max(1.0, min(timeout, budget / left * 2))
            frame_code = (
                _inject_frame(code, i, frames, width, height)
                if lang == "python"
                else _inject_frame_svg(code, i, frames, width, height))
            if lang == "python":
                svg = await python_to_svg_async(
                    frame_code, width, height, frame_timeout)
            else:
                svg = frame_code
            svg = _normalize_svg(svg, width, height)
            _check_size(svg)
            pngs.append(await _render_svg_to_png(svg, width, height,
                                                 workdir, f"f{i:04d}"))
        if not pngs:
            return {"ok": False, "workdir": workdir, "error": "没有渲染出任何帧"}

        out = await _encode_video(pngs, workdir, fps, fmt)
        if not out.get("ok"):
            out["workdir"] = workdir
            return out
        return {"ok": True, "path": out["path"], "format": out["format"],
                "frames": len(pngs), "fps": fps,
                "size": [width, height], "workdir": workdir}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "workdir": workdir}
    except Exception as exc:  # noqa: BLE001
        log.exception("[coderender] 视频渲染异常")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "workdir": workdir}


def _inject_frame(code: str, index: int, total: int,
                  width: int, height: int) -> str:
    """给 Python 代码注入 frame/total/width/height 变量。"""
    pre = (f"frame = {index}\ntotal = {total}\n"
           f"width = {width}\nheight = {height}\n")
    return pre + code


def _inject_frame_svg(svg: str, index: int, total: int,
                      width: int, height: int) -> str:
    """给 SVG 注入一个可被动画引用的占位符：`__FRAME__` 替换成帧号。

    同时把帧号写进 <desc>，便于用户确认。
    """
    text = (svg or "").replace("__FRAME__", str(index))
    return text.replace("<svg", f"<svg data-frame=\"{index}\"", 1)


async def _encode_video(pngs: list[str], workdir: str, fps: int,
                        video_format: str = "auto") -> dict:
    """把帧序列合成视频：MP4（ffmpeg）优先，GIF（ImageMagick）兜底。

    video_format=mp4 且没 ffmpeg → 明确报错（不偷偷降级成 gif 让人以为
    拿到的是 mp4）；auto → 有 ffmpeg 用 mp4，没有就用 gif。
    """
    ff = _ffmpeg()
    if ff and video_format in ("auto", "mp4"):
        pattern = os.path.join(workdir, "f%04d.png")
        out = os.path.join(workdir, "out.mp4")
        cmd = [ff, "-y", "-framerate", str(fps), "-i", pattern,
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
               "-movflags", "+faststart", out]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(),
                                            timeout=MAX_RENDER_SEC * 2)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "视频合成超时"}
        if os.path.exists(out):
            return {"ok": True, "path": out, "format": "mp4"}
        detail = err.decode("utf-8", "replace").strip()[-200:]
        if video_format == "mp4":
            return {"ok": False, "error": f"ffmpeg 合成 mp4 失败：{detail}"}
        log.info("[coderender] ffmpeg 失败，回退 GIF：%s", detail)

    conv = _magick()
    if not conv:
        return {"ok": False, "error": "缺少 ffmpeg 与 ImageMagick，无法合成视频"}
    out = os.path.join(workdir, "out.gif")
    delay = str(max(2, round(100 / fps)))
    cmd = [conv, "-delay", delay, "-loop", "0"] + pngs + [out]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, gif_err = await asyncio.wait_for(proc.communicate(),
                                            timeout=MAX_RENDER_SEC * 2)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "GIF 合成超时"}
    if os.path.exists(out):
        return {"ok": True, "path": out, "format": "gif"}
    return {"ok": False,
            "error": "视频合成失败：" + (
                gif_err.decode("utf-8", "replace").strip()[-200:] or "未知错误")}
