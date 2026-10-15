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
4. **拿不到成功响应就不回填**：只在真正探测到东西时才产出配置。

关于「什么算路径存在」
---------------------
刻意把 401/403/400/405/422 都算作「路径存在」：很多服务路径对、只是
参数校验严（缺 size/model）或没权限，这恰恰是最常见、最可修复的情形。
只排除 404 与非 JSON 报错页。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import schema
from .template import _B64_KEYS, _CONTAINER_KEYS, _URL_KEYS, guess_urls

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
_VIDEO_POLL_PATHS: tuple[str, ...] = (
    "/v1/videos/{task_id}",
    "/v1/videos/{task_id}/status",
    "/v1/video/{task_id}",
)
_MODEL_PATHS: tuple[str, ...] = ("/v1/models", "/models", "/openai/v1/models")

_PROBE_PROMPT = "a red apple"
_PROBE_SIZE = "256x256"
_PROBE_TIMEOUT = 12.0


# ────────────────────────── 基础工具 ──────────────────────────

def _join(base: str, path: str) -> str:
    return f"{(base or '').rstrip('/')}{path}"


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

    返回 ``{ok, capability, tried, models?, detail}``：
    - ok=True  → capability 是可直接用的 CapabilitySpec.to_dict()
    - ok=False → tried 列出每个候选 path 的状态/摘要，供面板提示用户
    """
    kind = kind or schema.CAP_IMAGE
    model = (model or "").strip()
    candidates = _IMAGE_PATHS if kind == schema.CAP_IMAGE else _VIDEO_SUBMIT_PATHS
    headers = _auth_headers(spec, secret)
    query = _auth_query(spec, secret)
    tried: list[dict] = []
    chosen: Optional[dict] = None

    for path in candidates:
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

    if chosen is None:
        return {"ok": False, "tried": tried,
                "detail": "所有候选路径都未命中；请手工指定请求路径与请求体"}

    path = chosen["path"]
    body = chosen["body"]
    status = chosen["status"]
    spec_req = schema.RequestSpec(method="POST", path=path,
                                  body=_derive_body(path, body, status, model))
    if kind == schema.CAP_IMAGE:
        cap = schema.CapabilitySpec(
            enabled=True, model=model,
            request=spec_req, extract=_derive_extract(body))
    else:
        cap = schema.CapabilitySpec(
            enabled=True, model=model, request=spec_req,
            extract=schema.ExtractSpec(task_id=""),
            poll=schema.RequestSpec(method="GET", path=_VIDEO_POLL_PATHS[0]),
            poll_extract=schema.ExtractSpec(status="", url=""))
    return {
        "ok": True,
        "capability": cap.to_dict(),
        "tried": tried,
        "detail": f"已自动匹配 {path}",
    }


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
