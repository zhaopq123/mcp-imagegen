"""模板渲染与响应抽取：让「任意 HTTP API」可被配置描述。

占位符
------
```
{name}    字符串插值：把变量 str() 后嵌进字符串
{{name}}  原样注入：整个值必须是单个占位符，变量按原类型放入
```

渲染规则

- 叶子值是 str → 先查整体占位符（``{{x}}``），命中即替换整值；否则做
  ``str.format`` 风格的插值；
- 叶子值是 dict/list → 递归；
- 整体为单个占位符而变量缺失（None）→ **删除该键**（不向 API 注入 null，
  实测大量 OpenAI 兼容实现对 `"seed": null` 直接 400）。

路径表达式
----------
```
data[].url        → data 数组中每项取 url（返回列表）
images[0].url     → 按下标
$.a.b             → 顶层 $ 可选，按点分段
result.video_id   → 嵌套取值
```
取值失败返回 None（调用方据此判定「抽取失败」而非抛异常）。
"""
from __future__ import annotations

import re
from typing import Any, Optional

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_INTERP = re.compile(r"\{(\w+)\}")

MISSING = object()


def _is_empty(value: Any) -> bool:
    """「变量实质缺失」判定。

    显式区分 0 / False 与真空值——`{{seed}}` 传 0 是合法参数，
    只有 None、空白串、空列表/字典才算没给。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_value(value: Any, ctx: dict) -> Any:
    """渲染模板中的单个叶子节点。返回 MISSING 表示该键应被丢弃。"""
    if isinstance(value, str):
        stripped = value.strip()
        whole = _PLACEHOLDER.fullmatch(stripped)
        if whole:
            name = whole.group(1)
            if name not in ctx or _is_empty(ctx[name]):
                return MISSING
            return ctx[name]
        # 「整个值就是一个 {x}」且变量缺失/为空 → 也删键。
        # 只按 {{x}} 判会漏掉最常见的写法：绝大多数 API 模板里
        # "ratio": "{ratio}" 用插值而不是原样注入，可选参数没传时
        # 会变成 "ratio": ""，上游轻则忽略、重则校验失败。
        single = _INTERP.fullmatch(stripped)
        if single and _is_empty(ctx.get(single.group(1))):
            return MISSING
        if "{" not in value:
            return value

        def _sub(match: "re.Match[str]") -> str:
            name = match.group(1)
            raw = ctx.get(name)
            if raw is None:
                return ""
            if isinstance(raw, (list, tuple, dict)):
                import json
                return json.dumps(raw, ensure_ascii=False)
            if isinstance(raw, bool):
                return "true" if raw else "false"
            return str(raw)

        return _INTERP.sub(_sub, value)
    return value


def render_node(node: Any, ctx: dict) -> Any:
    """递归渲染（dict 键可能整体消失；list 的原样占位符会被摊平/删除）。"""
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            rendered = render_node(value, ctx)
            if rendered is MISSING:
                continue
            rendered_key = render_value(key, ctx)
            if rendered_key is MISSING:
                continue
            out[str(rendered_key)] = rendered
        return out
    if isinstance(node, list):
        out_list: list = []
        for item in node:
            rendered = render_node(item, ctx)
            if rendered is MISSING:
                # 列表里出现单占位符且变量缺失 → 摊平注入（数组直接展开）
                continue
            if isinstance(rendered, list):
                out_list.extend(rendered)
            else:
                out_list.append(rendered)
        return out_list
    return render_node_leaf(node, ctx)


def render_node_leaf(node: Any, ctx: dict) -> Any:
    if isinstance(node, str):
        return render_value(node, ctx)
    return node


def render_mapping(mapping: Optional[dict], ctx: dict) -> dict:
    """渲染 headers / query 这类扁平映射。"""
    if not mapping:
        return {}
    rendered = render_node(mapping, ctx)
    if not isinstance(rendered, dict):  # 防御：模板结构异常
        return {}
    return {str(k): _stringify(v) for k, v in rendered.items()}


def _stringify(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    import json
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
_SEG = re.compile(r"^([^\[\]]+)((?:\[\*?\]|\[\d+\])*)$")


def _segments(path: str) -> list:
    """'a.b[].c[0]' → ['a', 'b', '*', 'c', 0]"""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    path = path.lstrip(".")
    segs: list = []
    for part in path.split("."):
        if not part:
            continue
        m = _SEG.match(part)
        if not m:
            segs.append(part)
            continue
        name, idx = m.group(1), m.group(2)
        if name:
            segs.append(name)
            for one in re.findall(r"\[\*?\]|\[\d+\]", idx or ""):
                # "[]" 与 "[*]" 等价：都是展开数组（写 [] 更自然，别让人去记 *）
                if one in ("[*]", "[]"):
                    segs.append("*")
                else:
                    segs.append(int(one[1:-1]))
    return segs


def extract(obj: Any, path: str) -> Any:
    """按路径表达式取值；失败返回 None。空路径返回原对象。"""
    if not path:
        return obj
    current = obj
    try:
        for seg in _segments(path):
            if seg == "*":
                if not isinstance(current, list):
                    return None
                results = []
                for item in current:
                    results.append(item)
                current = results
                continue
            if isinstance(seg, int):
                if not isinstance(current, list) or len(current) <= seg:
                    return None
                current = current[seg]
                continue
            if isinstance(current, dict):
                if seg not in current:
                    return None
                current = current[seg]
            else:
                return None
    except (TypeError, KeyError, IndexError):
        return None
    return current


def extract_list(obj: Any, path: str) -> list:
    """抽取列表值：路径含 [*] / [] 时展开成扁平列表。

    返回值为 None 或单个元素时归一成列表；空列表即「没抽到」。
    """
    if not path:
        return []
    raw = obj
    segs = _segments(path)
    # 逐段推进，遇到通配就展开
    pending: list = [obj]
    for seg in segs:
        nxt: list = []
        for item in pending:
            if seg == "*":
                if isinstance(item, list):
                    nxt.extend(item)
                continue
            if isinstance(seg, int):
                if isinstance(item, list) and len(item) > seg:
                    nxt.append(item[seg])
                continue
            if isinstance(item, dict) and seg in item:
                nxt.append(item[seg])
        pending = nxt
        if not pending:
            return []
    raw = pending
    if isinstance(raw, list):
        return [v for v in raw if v is not None]
    return [raw] if raw is not None else []


# ---------------------------------------------------------------------------
# 自动探测（未配置抽取路径时的兜底）
# ---------------------------------------------------------------------------
_URL_KEYS = ("url", "image_url", "image", "src", "link", "download_url")
# base64 图片的键名。单独提出为常量，供 autodetect 反推抽取路径时复用——
# 两边必须认同一份清单，否则会推出 guess_urls 不认的路径。
_B64_KEYS = ("b64_json", "b64", "base64")
_ID_KEYS = ("video_id", "task_id", "id", "job_id", "request_id", "uuid")
_STATUS_KEYS = ("status", "state", "task_status")
_CONTAINER_KEYS = ("data", "images", "results", "items", "output", "videos")


def _looks_like_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://", "data:"))


def guess_urls(body: Any) -> list:
    """从任意响应体里猜出图片 URL 列表（深度 4 层，广度优先）。

    覆盖三种常见形态：
        {"images": [{"url": ...}]}   {"data": [{"url": ...}]}
        {"data": [{"b64_json": ...}]}  {"url": "..."}   [...]（裸数组）
    """
    found: list = []
    queue: list = [body]
    depth = 0
    while queue and depth < 4 and not found:
        nxt: list = []
        for node in queue:
            if isinstance(node, dict):
                # 1) 直接含 url 类键 → 一条图片
                #    放宽到「非空字符串」：上游返回相对路径（/files/x.png）或
                #    自定义 URI 形态很常见，卡 http(s) 前缀会把它们静默丢掉，
                #    用户只看到"生成成功但没有图"，比报错更难查。
                for key in _URL_KEYS:
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        found.append(value.strip())
                        break
                else:
                    for key in _B64_KEYS:
                        if isinstance(node.get(key), str) and node[key]:
                            found.append(node[key])
                            break
                for key in (*_CONTAINER_KEYS, "result", "results"):
                    if key in node:
                        nxt.append(node[key])
            elif isinstance(node, list):
                nxt.extend(node)
        queue = nxt
        depth += 1
    # 去重保序
    seen, out = set(), []
    for url in found:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def guess_field(body: Any, keys: tuple, exclude: tuple = ()) -> Optional[str]:
    """按候选键名猜一个标量字段（顶层优先，再下钻一层容器）。"""
    if not isinstance(body, dict):
        return None
    for key in keys:
        value = body.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    for container in _CONTAINER_KEYS:
        node = body.get(container)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return str(value)
    return None


def guess_task_id(body: Any) -> Optional[str]:
    value = guess_field(body, _ID_KEYS)
    if value and not _looks_like_url(value):
        return value
    return value


def guess_status(body: Any) -> Optional[str]:
    return guess_field(body, _STATUS_KEYS)


def guess_url(body: Any) -> Optional[str]:
    urls = guess_urls(body)
    return urls[0] if urls else None


def normalize_status(raw: str, status_map: Optional[dict] = None) -> str:
    """把上游状态字符串归一成内部状态（running/completed/failed）。

    status_map 优先（面板可配）；其次按常见词面匹配。
    """
    if not isinstance(raw, str):
        return "running"
    key = raw.strip().lower()
    if status_map:
        mapped = status_map.get(raw) or status_map.get(key)
        if isinstance(mapped, str) and mapped:
            return mapped
    if key in ("running", "pending", "queued", "processing", "in_progress", "waiting"):
        return "running"
    if key in ("completed", "succeeded", "success", "done", "finished", "complete"):
        return "completed"
    if key in ("failed", "failure", "error", "cancelled", "canceled", "rejected"):
        return "failed"
    return raw
