"""出站 URL 拼接：把能力模板里的 path 并到用户填的「接口地址」上。

为什么值得单独一个模块（用户实测踩到）
--------------------------------------
面板「接口地址」那一格，用户按上游文档的习惯粘的是**完整端点 URL**：

    https://api.agnes-ai.cn/v1/videos

而能力模板里的 ``request.path`` 是 ``/v1/videos``。两者直接相接就变成

    /v1/videos/v1/videos          → 上游 404，
    轮询更明显：/v1/videos/v1/videos/query

httpx 自己的 ``base_url`` 合并同样是「路径直接相接」（0.28.1 实测：
``base_url=https://h/v1/videos`` + ``path=/v1/videos/query``
→ ``https://h/v1/videos/v1/videos/query``），所以指望不上，必须在出站前收口。

规则
----
**base 路径的尾部与 path 的头部的最长重合段视为重复，只保留一份。**

这样三种常见写法都能拼对，且都不需要用户改输入：

    域名            https://h            + /v1/videos       → /v1/videos
    完整端点 URL    https://h/v1/videos  + /v1/videos       → /v1/videos
    完整端点 URL    https://h/v1/videos  + /v1/videos/query → /v1/videos/query
    带前缀的网关    https://h/openai/v1  + /v1/chat         → /openai/v1/chat
    前缀不重合      https://h/api        + /v1/models       → /api/v1/models

刻意不做的事：不猜「哪一段是前缀」、不吞掉不重合的段落——只去掉重合。
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _segments(text: str) -> list:
    return [seg for seg in (text or "").split("/") if seg]


def merge_paths(base_path: str, req_path: str) -> str:
    """把 ``req_path`` 并到 ``base_path`` 之后，去掉首尾重合的段落。"""
    base_segs = _segments(base_path)
    req_segs = _segments(req_path)
    overlap = 0
    if base_segs and req_segs:
        for n in range(min(len(base_segs), len(req_segs)), 0, -1):
            if base_segs[len(base_segs) - n:] == req_segs[:n]:
                overlap = n
                break
    merged = base_segs + req_segs[overlap:]
    return "/" + "/".join(merged)


def split_endpoint(raw: str) -> tuple:
    """把用户粘的接口地址拆成 (base_url, path)。

    **用户就是按「完整端点 URL」给地址的**（如
    ``https://api.example.cn/v1/images/generations``），而不是只给域名。
    所以必须支持直接用完整 URL：把路径部分摘出来当 path，域名当 base_url，
    免得再去猜「这段路径是干吗的」。

    无路径（如 ``https://api.example.cn``）时 path 返回空串，由探测决定。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return "", ""
    # 去掉查询串/片段——不该进 base_url
    for sep in ("?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    scheme = ""
    rest = text
    for candidate in ("https://", "http://"):
        if text.lower().startswith(candidate):
            scheme = candidate
            rest = text[len(candidate):]
            break
    if "/" in rest:
        host, path = rest.split("/", 1)
        return f"{scheme}{host}", "/" + path.lstrip("/")
    return f"{scheme}{rest}", ""


def is_endpoint_suffix(base_path: str, candidates) -> bool:
    """``base_path`` 是不是某个候选路径的**重复端点**（而不是网关前缀）。

    判据：base 的尾部与候选中某一条的尾部完全重合。

    - ``/v1/videos`` vs ``/v1/videos`` → True（用户把端点粘进了接口地址）
    - ``/v1/images/generations`` vs ``/v1/images/generations`` → True
    - ``/openai/v1`` vs ``/v1/chat`` → False（这是网关前缀，不能丢）
    - ``/v1`` vs ``/v1/images/generations`` → False（同上）

    为什么非要区分：base_url 一旦带着端点路径，就**没法表达兄弟端点**
    （用户实测：查询通道 ``/agnesapi`` 与提交端点 ``/v1/videos`` 是兄弟，
    会把 /agnesapi 接成 /v1/videos/agnesapi）。重复端点要拆掉，前缀要留着。
    """
    base_segs = _segments(base_path)
    if not base_segs:
        return False
    for cand in candidates or ():
        segs = _segments(cand)
        if not segs:
            continue
        n = min(len(base_segs), len(segs))
        if base_segs[len(base_segs) - n:] == segs[len(segs) - n:]:
            return True
    return False


def resolve_url(base_url: str, path: str) -> str:
    """→ 最终请求 URL。``path`` 已是绝对 URL 时原样返回。"""
    raw = str(path or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    path_only, sep, query = raw.partition("?")
    base = str(base_url or "").strip()
    if not base:
        return raw or "/"
    parts = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        # 非绝对地址（正常校验不会放过）→ 退回朴素拼接，别抛出
        joined = base.rstrip("/") + (
            path_only if path_only.startswith("/") else "/" + path_only)
        return joined + sep + query
    merged = merge_paths(parts.path, path_only)
    return urlunsplit((parts.scheme, parts.netloc, merged, query, ""))
