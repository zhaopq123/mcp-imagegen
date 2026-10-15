"""上游「自动装配」：只给 base_url + 密钥 + 模型名，自动探出可用的请求形状。

为什么需要这个
--------------
V5 把上游配置全开放给面板，灵活但把门槛也抬高了：使用者要自己填
请求路径、请求体模板、响应抽取路径。可现实里 95% 的服务是「OpenAI
兼容」——路径 `/v1/images/generations`、请求体 `{model, prompt}`、
响应 `data[].url`。让人手填这些纯属仪式。

所以做**探测式装配**：给 base_url + 密钥 + 模型名，程序按候选清单挨个试，
用**最小代价**的探测请求判断哪个能用，再把成功形状回填成 CapabilitySpec。
探测不出来才需要手工介入。

设计约束（重要）
----------------
1. **零厂商常量**：本模块不含任何具体厂商的域名/专属路径。候选路径是
   业界通用的几种形状，真实地址永远来自用户输入的 base_url。
2. **不产生大额消耗**：探测用 n=1、最小 size、短超时。
3. **可解释**：记录试了哪些 path、各自HTTP 状态与错误摘要；失败时如实
   告知，不用"看起来像"的降级。
4. **拿不到成功响应就只做降级装配**：只在真正探测到东西时才产出配置；
   唯一的例外是"路径存在 + 上游明确说繁忙（503/429）"——此时按最可能的
   路径**降级**装配（结果里 `degraded=True`），由面板显式告知"未验证"，
   绝不伪装成探测成功。上游高峰时这是用户"只给模型名+API+Key"唯一能
   接上的窗口。
5. **轮询端点必须实证**：视频的轮询端点只有拿到**真实 task_id** 才探得
   出来（假 id 下路径式/查询式候选全都"存在"，都回「任务不存在」，根本
   区分不出）。探不到就留空 + 明确提示手工填，不猜。

关于「什么算路径存在」
---------------------
刻意把 401/403/400/405/422 都算作「路径存在」：很多服务路径对、只是
参数校验严（缺 size/model）或没权限，这恰恰是最常见、最可修复的情形。
只排除 404 与非 JSON 报错页。503/429 同样算「存在」，且额外作为降级
装配的依据（见约束 4）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import schema
from .urlmerge import split_endpoint
from .template import (
    _B64_KEYS, _CONTAINER_KEYS, _ID_KEYS, _STATUS_KEYS, _URL_KEYS,
    guess_urls, normalize_status,
)

log = logging.getLogger(__name__)

# ── 候选路径（通用形状，非厂商白名单）──
# 顺序即尝试顺序：越靠前越标准，命中概率最高。
_IMAGE_PATHS: tuple[str, ...] = (
    "/v1/images/generations",
    "/images/generations",
    "/v1/image/generations",
    "/openai/v1/images/generations",
    "/v1/images",
    "/v1/text-to-image",
    "/sdapi/v1/txt2img",
)
_VIDEO_SUBMIT_PATHS: tuple[str, ...] = (
    "/v1/videos",
    "/videos",
    "/v1/video/generations",
    "/openai/v1/videos",
    "/v1/video",
)
# 轮询候选形状：路径式 + 查询式（**通用形状，零厂商常量**——绝不含某家
# 上游的专属路径）。拿真实 task_id 逐个试，认得出 status 的才算数。
_VIDEO_POLL_CANDIDATES: tuple[dict, ...] = (
    {"path": "/v1/videos/{task_id}"},
    {"path": "/v1/videos/{task_id}/status"},
    {"path": "/v1/video/{task_id}"},
    {"path": "/v1/tasks/{task_id}"},
    {"path": "/v1/videos/query",
     "query": {"task_id": "{task_id}", "model": "{model}"}},
    {"path": "/v1/video/query",
     "query": {"task_id": "{task_id}", "model": "{model}"}},
)
_MODEL_PATHS: tuple[str, ...] = ("/v1/models", "/models", "/openai/v1/models")

# 上游「在线但没余量」类状态。这是**强**路径信号：打错路径一般回 404，
# 不会回 503/429；但拿不到成功响应 → 只能降级装配，并在结果里标 degraded，
# 由面板明确告知"未验证"（绝不假装成功——"看起来像"的降级比失败更难查）。
_TRANSIENT_STATUS: tuple[int, ...] = (429, 503)

_PROBE_PROMPT = "a red apple"
_PROBE_SIZE = "256x256"
_PROBE_TIMEOUT = 12.0


# ────────────────────────── 基础工具 ──────────────────────────

def _join(base: str, path: str) -> str:
    return f"{(base or '').rstrip('/')}{path}"


# split_endpoint 挪到 urlmerge（schema 也要用它拆 base_url，而 schema 不能
# 反向依赖本模块），本模块仍从那里导入，`autodetect.split_endpoint` 照旧可用。


def _auth_headers(spec: schema.ProviderConfig, secret: str) -> dict[str, str]:
    auth = spec.auth
    headers: dict[str, str] = {"Content-Type": "application/json",
                               "Accept": "application/json"}
    kind = auth.kind if auth else schema.AUTH_BEARER
    if kind == schema.AUTH_NONE or not secret:
        return headers
    if kind == schema.AUTH_BEARER:
        headers[auth.name or "Authorization"] = f"{auth.prefix}{secret}"
    elif kind == schema.AUTH_HEADER:
        headers[auth.name or "X-API-Key"] = f"{auth.prefix}{secret}"
    return headers


def _auth_query(spec: schema.ProviderConfig, secret: str) -> dict[str, str]:
    auth = spec.auth
    if not secret or not auth or auth.kind != schema.AUTH_QUERY:
        return {}
    return {auth.name or "api_key": secret}


def _parse_body(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        try:
            return resp.text
        except Exception:  # noqa: BLE001
            return None


def _body_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body[:400].lower()
    try:
        return json.dumps(body, ensure_ascii=False)[:600].lower()
    except (TypeError, ValueError):
        return str(body)[:400].lower()


def _path_exists(status: int, body: Any) -> bool:
    """响应是否说明「路径存在」（含参数错/无权限，只排除 404 与 HTML 报错页）。"""
    if 200 <= status < 300:
        return True
    if status == 404:
        return False
    if status in (401, 403, 405, 400, 422):
        return True
    # 其他状态：若是 HTML 错误页则不算，JSON 错误体算
    text = _body_text(body)
    return bool(text) and not text.lstrip().startswith("<")


def _summarize(body: Any, limit: int = 160) -> str:
    text = _body_text(body).replace("\n", " ").strip()
    return text[:limit]


def _is_transient(status: int) -> bool:
    """上游「在线但没余量」（503/429）——路径存在的强信号，但拿不到成功响应。"""
    try:
        return int(status) in _TRANSIENT_STATUS
    except (TypeError, ValueError):
        return False


def _field_path(body: Any, keys: tuple) -> str:
    """字段**路径**（而非值）：顶层优先，再下钻一层 dict 容器。

    与 template.guess_field 的查找顺序保持一致——那边靠这份顺序猜值，
    这边靠同一份顺序生成抽取路径，两边漂移就会推出抽取不到东西的配置。
    """
    if not isinstance(body, dict):
        return ""
    for key in keys:
        value = body.get(key)
        if isinstance(value, (str, int)) and str(value):
            return key
    for container in _CONTAINER_KEYS:
        node = body.get(container)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return f"{container}.{key}"
    return ""


def _derive_poll_extract(body: Any) -> schema.ExtractSpec:
    """轮询响应 → poll_extract（状态字段必填，URL 尽力而为）。

    URL 抽不到就留空，由 gateway 在 status=completed 时走 guess_url 兜底
    （那条兜底路径实测可靠，见 gateway._parse_query）。
    """
    status_path = _field_path(body, _STATUS_KEYS)
    url_path = ""
    if isinstance(body, dict):
        for key in _URL_KEYS:
            if isinstance(body.get(key), str) and body[key]:
                url_path = key
                break
        if not url_path:
            # 一层容器：先用与 guess_urls 共享的容器名，再兜底扫**任意**一级
            # dict 值——轮询响应常把结果裹在自有包装里（如 metadata.url），
            # 不认就会白丢一个可用的抽取路径（gateway 虽有 guess_url 兜底，
            # 但显式路径更稳、也便于用户在面板上核对该字段）。
            ordered = [k for k in _CONTAINER_KEYS if isinstance(body.get(k), dict)]
            ordered += [k for k, v in body.items()
                        if isinstance(v, dict) and k not in ordered]
            for container in ordered:
                node = body[container]
                for key in _URL_KEYS:
                    if isinstance(node.get(key), str) and node[key]:
                        url_path = f"{container}.{key}"
                        break
                if url_path:
                    break
    return schema.ExtractSpec(status=status_path, url=url_path)


def _fill(text: str, task_id: str, model: str) -> str:
    return (str(text).replace("{task_id}", task_id).replace("{model}", model))


def _fill_query(query: Optional[dict], task_id: str, model: str) -> dict:
    return {k: _fill(v, task_id, model) for k, v in (query or {}).items()}


async def _probe_poll(client: Any, spec: schema.ProviderConfig, secret: str,
                      task_id: str, model: str,
                      timeout: float) -> tuple[Optional[dict], list[dict]]:
    """拿**真实** task_id 试探轮询端点。

    这是轮询端点唯一靠谱的探测时机：没有真任务号时，路径式与查询式候选
    全都"存在"（都回「任务不存在」），根本区分不出来（2026-10-15 真机实测：
    路径式、查询式、以及上游自有的查询路由三条全不 404，全回任务不存在）。

    返回 (命中项, tried)；命中项 = {path, query, poll_extract}。
    """
    headers = _auth_headers(spec, secret)
    auth_q = _auth_query(spec, secret)
    tried: list[dict] = []
    for cand in _VIDEO_POLL_CANDIDATES:
        path_tpl = str(cand.get("path") or "")
        query_tpl = cand.get("query") or {}
        path = _fill(path_tpl, task_id, model)
        shown = path + (("?" + "&".join(
            f"{k}={v}" for k, v in _fill_query(query_tpl, task_id, model).items()))
            if query_tpl else "")
        entry: dict[str, Any] = {"path": shown, "slot": "poll"}
        params = dict(auth_q)
        params.update(_fill_query(query_tpl, task_id, model))
        try:
            resp = await client.get(_join(spec.base_url, path), headers=headers,
                                    params=params or None, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = type(exc).__name__
            tried.append(entry)
            continue
        entry["status"] = resp.status_code
        body = _parse_body(resp)
        entry["detail"] = _summarize(body, 100)
        tried.append(entry)
        if not (200 <= resp.status_code < 300):
            continue
        # 认得出「任务状态」才算轮询端点：要有状态字段，且值能归一成
        # running/completed/failed 之一。否则只是个 200（首页 / 网关页 /
        # 同名但语义不同的接口）——宁可判定"探不到"，也不能配一个错的。
        if not _looks_like_poll_body(body):
            continue
        return ({"path": path_tpl, "query": dict(query_tpl),
                 "poll_extract": _derive_poll_extract(body)}), tried
    return None, tried


def _looks_like_poll_body(body: Any) -> bool:
    """响应是否像「任务状态查询」：有状态字段且值可归一。"""
    if not _field_path(body, _STATUS_KEYS):
        return False
    value = _field_value(body, _STATUS_KEYS)
    return normalize_status(value) in ("running", "completed", "failed")


def _field_value(body: Any, keys: tuple) -> str:
    """取 _field_path 命中的那个字段的值（查找顺序与 _field_path 一致）。"""
    path = _field_path(body, keys)
    if not path:
        return ""
    node = body
    for part in path.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return str(node) if node is not None else ""


# ────────────────────────── 模型列表 ──────────────────────────

def _parse_model_ids(body: Any) -> list[str]:
    """从 /models 响应抠模型 id（形状不统一，宽松解析）。"""
    out: list[str] = []

    def take(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for mk in ("id", "name", "model"):
                if isinstance(node.get(mk), str):
                    out.append(node[mk])
                    return

    if isinstance(body, dict):
        for key in ("data", "models", "result", "items", "list"):
            node = body.get(key)
            if isinstance(node, list):
                for item in node:
                    take(item)
                if out:
                    break
    elif isinstance(body, list):
        for item in body:
            take(item)
    seen, uniq = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq[:200]


async def list_models(client: Any, spec: schema.ProviderConfig,
                      secret: str, timeout: float = _PROBE_TIMEOUT) -> dict:
    """探测上游可用模型列表（best-effort）。拿到后面板可给下拉框。"""
    headers = _auth_headers(spec, secret)
    query = _auth_query(spec, secret)
    tried: list[dict] = []
    for path in _MODEL_PATHS:
        entry: dict[str, Any] = {"path": path}
        try:
            resp = await client.get(_join(spec.base_url, path), headers=headers,
                                    params=query or None, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = type(exc).__name__
            tried.append(entry)
            continue
        entry["status"] = resp.status_code
        body = _parse_body(resp)
        if 200 <= resp.status_code < 300:
            ids = _parse_model_ids(body)
            if ids:
                return {"ok": True, "models": ids, "tried": tried}
        else:
            entry["detail"] = _summarize(body, 80)
        tried.append(entry)
    return {"ok": False, "models": [], "tried": tried}


# ────────────────────────── 形状反推 ──────────────────────────

def _derive_extract(body: Any) -> schema.ExtractSpec:
    """从成功响应反推抽取路径（拿不到就留空 = 自动猜测）。

    键名清单**必须复用 template._URL_KEYS / _CONTAINER_KEYS**，不能自己另写一套
    —— 否则会推出一个 guess_urls 根本不认的路径（如 `images[].uri`：
    uri 不在 _URL_KEYS 里），探测能过、真正调用时却抽不到图。
    2026-10-15 端到端测试正是踩了这个坑。
    """
    if not guess_urls(body):
        return schema.ExtractSpec()
    if isinstance(body, dict):
        for container in _CONTAINER_KEYS:
            node = body.get(container)
            if isinstance(node, list) and node and isinstance(node[0], dict):
                for key in _URL_KEYS + _B64_KEYS:
                    if node[0].get(key):
                        return schema.ExtractSpec(
                            urls=f"{container}[].{key}", b64=key in _B64_KEYS)
            if isinstance(node, dict):
                for key in _URL_KEYS:
                    if isinstance(node.get(key), str):
                        return schema.ExtractSpec(urls=f"{container}.{key}")
        for key in _URL_KEYS:
            if isinstance(body.get(key), str):
                return schema.ExtractSpec(urls=key)
    return schema.ExtractSpec()


def _derive_body(path: str, body: Any, status: int,
                 model: str) -> dict[str, Any]:
    """构造请求体模板：默认最小字段集，按报错补齐必需项。

    ⚠️ 关键：探测用的是**真实探测值**（prompt="a red apple"），但回填时
    必须换回**占位符** `{prompt}` / `{size}` —— 否则用户保存后每次生图
    都会返回同一张探测图。这是有测试兜着的硬约束。
    """
    payload: dict[str, Any] = {"model": "{model}", "prompt": "{prompt}"}
    text = _body_text(body)
    if status == 400:
        if "size" in text or "width" in text:
            payload["size"] = "{size}"
        if '"n"' in text or "n is required" in text or "n必须" in text:
            payload["n"] = 1
    return payload


# ────────────────────────── 主流程 ──────────────────────────

async def autodetect(client: Any, spec: schema.ProviderConfig, secret: str,
                     kind: str = schema.CAP_IMAGE, model: str = "",
                     timeout: float = _PROBE_TIMEOUT) -> dict:
    """探测并回填能力配置。

    返回 ``{ok, capability, tried, models?, detail, degraded?}``：
    - ok=True  → capability 是可直接用的 CapabilitySpec.to_dict()
    - ok=False → tried 列出每个候选 path 的状态/摘要，供面板提示用户
    - degraded=True → 只拿到「路径存在」的非成功响应（上游 503/429 繁忙），
      按最可能的路径装配但**未验证成功响应**——面板必须显式提示用户
    """
    kind = kind or schema.CAP_IMAGE
    model = (model or "").strip()
    headers = _auth_headers(spec, secret)
    query = _auth_query(spec, secret)
    tried: list[dict] = []
    chosen: Optional[dict] = None
    transient: list[dict] = []
    seen_paths: set = set()

    # 用户粘的是完整端点 URL 时，**先用它自己的路径**，不通再回退候选清单。
    # 这条优先级最高：用户已经明确告诉我们要打哪了，不该替他猜。
    explicit_path = str(getattr(spec, "_explicit_path", "") or "").strip()
    if explicit_path:
        seen_paths.add(explicit_path)
        entry: dict[str, Any] = {"path": explicit_path, "source": "用户填写"}
        payload = {"model": model or "{model}", "prompt": _PROBE_PROMPT}
        try:
            resp = await client.post(_join(spec.base_url, explicit_path),
                                     headers=headers, params=query or None,
                                     json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = type(exc).__name__
            entry["detail"] = "按你填的完整地址请求失败"
            tried.append(entry)
        else:
            status = resp.status_code
            body = _parse_body(resp)
            entry["status"] = status
            entry["path_exists"] = _path_exists(status, body)
            entry["detail"] = "可用" if 200 <= status < 300 else _summarize(body, 100)
            tried.append(entry)
            if 200 <= status < 300:
                chosen = {"path": explicit_path, "status": status, "body": body}
            elif _is_transient(status):
                # 用户明确给的路 + 上游回"我很忙"：最强的降级信号
                transient.append({"path": explicit_path, "status": status,
                                  "body": body, "source": "用户填写"})

    candidates = (_IMAGE_PATHS if kind == schema.CAP_IMAGE
                  else _VIDEO_SUBMIT_PATHS)
    if chosen:
        candidates = ()

    for path in candidates:
        if path in seen_paths:
            # 用户填的完整端点 URL 已经探过这个路径了——**不能再探一次**：
            # 否则同一条路径会往 transient 里塞两条，len==2 就被误判成
            # "多个候选都忙"而放弃降级装配（真机 503 场景下必然踩到）。
            continue
        seen_paths.add(path)
        entry: dict[str, Any] = {"path": path}
        payload = {"model": model or "probe-model", "prompt": _PROBE_PROMPT}
        try:
            resp = await client.post(_join(spec.base_url, path),
                                     headers=headers, params=query or None,
                                     json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = type(exc).__name__
            tried.append(entry)
            continue
        status = resp.status_code
        body = _parse_body(resp)
        entry["status"] = status
        exists = _path_exists(status, body)
        entry["path_exists"] = exists
        if not exists:
            entry["detail"] = _summarize(body, 80)
            tried.append(entry)
            continue
        # 路径存在：若已成功则立即采用；否则记录（可能是参数问题，继续试下一个）
        if 200 <= status < 300:
            entry["detail"] = "可用"
            tried.append(entry)
            chosen = {"path": path, "status": status, "body": body}
            break
        entry["detail"] = _summarize(body, 100)
        tried.append(entry)
        if _is_transient(status):
            transient.append({"path": path, "status": status, "body": body})

    degraded = False
    if chosen is None and len(transient) == 1:
        # 上游在线但没余量：**恰好一个**候选回 503/429 → 几乎可以断定它才是真
        # 路径（打错路径一般回 404，不会回 503/429）。但拿不到成功响应，所以
        # 只做**降级**装配并标 degraded，由面板明说"未验证"——绝不假装成功。
        # 两个以上候选都"忙"则视为整站/网关级故障，路径无法判别 → 不装配。
        chosen = {**transient[0], "degraded": True}
        degraded = True

    if chosen is None:
        if transient:
            detail = ("多个候选路径都返回上游繁忙（503/429），无法判断哪个是正确"
                      "的请求路径；请等上游空闲后重试，或手工指定请求路径与请求体")
        else:
            detail = "所有候选路径都未命中；请手工指定请求路径与请求体"
        return {"ok": False, "tried": tried, "detail": detail}

    path = chosen["path"]
    body = chosen["body"]
    status = chosen["status"]
    spec_req = schema.RequestSpec(method="POST", path=path,
                                  body=_derive_body(path, body, status, model))
    notes: list[str] = []
    if degraded:
        notes.append(
            f"探测时上游返回 {status}（繁忙），已按最可能的路径 {path} 装配，"
            "但**没有拿到成功响应**——请用一次真实调用验证")
    else:
        notes.append(f"已自动匹配 {path}")

    if kind == schema.CAP_IMAGE:
        cap = schema.CapabilitySpec(
            enabled=True, model=model,
            request=spec_req, extract=_derive_extract(body))
    else:
        cap = await _assemble_video_cap(client, spec, secret, body, model, timeout,
                                        spec_req, notes, tried)

    return {
        "ok": True,
        "capability": cap.to_dict(),
        "tried": tried,
        "detail": "；".join(notes),
        "degraded": degraded,
    }


async def _assemble_video_cap(client: Any, spec: schema.ProviderConfig, secret: str,
                              body: Any, model: str, timeout: float,
                              spec_req: schema.RequestSpec, notes: list,
                              tried: list) -> schema.CapabilitySpec:
    """视频能力装配：任务号抽取路径 + 轮询端点（**探测**而非猜）。

    轮询端点只有拿到真实 task_id 才探得出来（假 id 下路径式/查询式候选全都
    "存在"）。所以：提交成功（2xx，手里有真任务号）→ 逐个试候选；探不到就
    **留空**并在 detail 里明说要手工填，绝不静默配一个错的（错的 poll 会让
    用户每次查询都撞 task_not_exist，比"没配"更难查）。
    """
    task_id_path = _field_path(body, _ID_KEYS)
    cap = schema.CapabilitySpec(
        enabled=True, model=model, request=spec_req,
        extract=schema.ExtractSpec(task_id=task_id_path))
    task_id = ""
    if task_id_path:
        node: Any = body
        for part in task_id_path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        task_id = str(node) if node is not None else ""

    if not task_id:
        notes.append("提交响应里认不出任务号，无法探测轮询端点；"
                     "请到「高级」手工填写轮询端点与任务号抽取路径")
        return cap

    found, poll_tried = await _probe_poll(client, spec, secret, task_id, model,
                                          timeout)
    tried.extend(poll_tried)
    if found is None:
        notes.append("已提交探测任务但逐个候选都认不出任务状态，未能探测到轮询端点；"
                     "请到「高级」手工填写轮询端点与响应抽取路径"
                     "（该上游可能用的是自有的查询接口）")
        return cap
    cap.poll = schema.RequestSpec(method="GET", path=found["path"],
                                 query=dict(found["query"]))
    cap.poll_extract = found["poll_extract"]
    notes.append(f"轮询端点已探测为 {found['path']}")
    return cap


async def detect_all(client: Any, spec: schema.ProviderConfig, secret: str,
                     kind: str = schema.CAP_IMAGE, model: str = "",
                     timeout: float = _PROBE_TIMEOUT) -> dict:
    """装配 = 探测形状 + 拉模型列表（模型列表失败不影响装配成功）。"""
    result = await autodetect(client, spec, secret, kind, model, timeout)
    result["models"] = []
    try:
        m = await list_models(client, spec, secret, timeout)
        result["models"] = m.get("models") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("[autodetect] 模型列表探测失败（忽略）：%s", type(exc).__name__)
    return result
