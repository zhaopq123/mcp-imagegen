# -*- coding: utf-8 -*-
"""上游自动装配（providers.autodetect）测试：全离线，不触网。

覆盖：候选路径顺序、404 跳过、401/400 视为「路径存在」、
从响应反推抽取路径、模型列表解析、失败时如实返回 tried。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from providers import autodetect, schema


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeClient:
    """按 path 返回预设响应；记录所有请求的 url 供断言。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    async def post(self, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "body": json,
                           "headers": headers or {}, "params": params or {}})
        path = url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        for pfx, resp in self.routes.items():
            if path == pfx or path.endswith(pfx):
                return resp() if callable(resp) else resp
        return FakeResp(404, {"error": "not found"})

    async def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url,
                           "headers": headers or {}, "params": params or {}})
        path = url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        for pfx, resp in self.routes.items():
            if path == pfx or path.endswith(pfx):
                return resp() if callable(resp) else resp
        return FakeResp(404, {"error": "not found"})


def _spec(base="https://api.example.com", token="sk-test", kind="bearer"):
    return schema.ProviderConfig(
        id="t", base_url=base,
        auth=schema.AuthSpec(kind=kind, token=token, name="Authorization",
                             prefix="Bearer "),
    )


@pytest.mark.asyncio
async def test_autodetect_picks_first_working_path():
    """第一个候选路径 200 就用它，不继续试后面的。"""
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "https://cdn.example/a.png"}]})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "gpt-image-1")
    assert res["ok"] is True
    cap = res["capability"]
    assert cap["request"]["path"] == "/v1/images/generations"
    assert cap["extract"]["urls"] == "data[].url"
    #只试了 1 个路径就命中
    assert len(c.calls) == 1
    # 模型名放在cap.model（用户可改），body 里必须是占位符
    assert cap["model"] == "gpt-image-1"
    assert cap["request"]["body"]["model"] == "{model}"


@pytest.mark.asyncio
async def test_autodetect_never_leaks_probe_prompt_into_template():
    """防回归：探测用的真实 prompt/size 不能被写进请求体模板。

    否则用户保存后每次生图都返回同一张探测图（a red apple）。
    """
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "u"}]})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    body = res["capability"]["request"]["body"]
    blob = json.dumps(body, ensure_ascii=False)
    assert "a red apple" not in blob, "探测文案泄漏进模板"
    assert "256x256" not in blob, "探测尺寸泄漏进模板"
    assert body["prompt"] == "{prompt}"


@pytest.mark.asyncio
async def test_autodetect_skips_404_then_uses_second():
    """404 视为路径不存在 → 跳到下一个候选。"""
    c = FakeClient({
        "/v1/image/generations": FakeResp(
            200, {"images": [{"url": "https://cdn/b.png"}]}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    assert res["ok"] is True
    assert res["capability"]["request"]["path"] == "/v1/image/generations"
    assert res["capability"]["extract"]["urls"] == "images[].url"
    # 至少试了 2 次
    assert len(c.calls) >= 2


@pytest.mark.asyncio
async def test_autodetect_401_counts_as_path_exists():
    """401 视为「路径存在只是没权限」—— 不应跳过后面的路径。"""
    c = FakeClient({"/v1/images/generations": FakeResp(401, {"error": "no key"})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    # 全部候选都 404/401 类，最后没有 2xx → 失败但tried 里有明细
    assert res["ok"] is False
    assert res["tried"], "失败时必须给出已试清单"
    assert any(t["path"] == "/v1/images/generations" for t in res["tried"])


@pytest.mark.asyncio
async def test_autodetect_sends_auth_header():
    """bearer 鉴权要真的带上Authorization 头。"""
    c = FakeClient({"/v1/images/generations": FakeResp(200, {"data": [{"url": "u"}]})})
    await autodetect.autodetect(c, _spec(), "sk-mykey", "image", "m")
    hdr = c.calls[0]["headers"]
    assert hdr.get("Authorization") == "Bearer sk-mykey"


@pytest.mark.asyncio
async def test_autodetect_header_auth_mode():
    """header 模式用指定头名（非 Authorization）。"""
    sp = schema.ProviderConfig(
        id="t", base_url="https://api.example.com",
        auth=schema.AuthSpec(kind="header", token="k", name="X-API-Key",
                             prefix=""))
    c = FakeClient({"/v1/images/generations": FakeResp(200, {"data": [{"url": "u"}]})})
    await autodetect.autodetect(c, sp, "k", "image", "m")
    assert c.calls[0]["headers"].get("X-API-Key") == "k"


@pytest.mark.asyncio
async def test_list_models_openai_shape():
    c = FakeClient({"/v1/models": FakeResp(
        200, {"data": [{"id": "gpt-image-1"}, {"id": "dall-e-3"}]})})
    res = await autodetect.list_models(c, _spec(), "sk-test")
    assert res["ok"] is True
    assert res["models"] == ["gpt-image-1", "dall-e-3"]


@pytest.mark.asyncio
async def test_list_models_alt_shape_and_failure():
    c = FakeClient({"/models": FakeResp(200, {"models": ["a", "b"]})})
    res = await autodetect.list_models(c, _spec(), "sk")
    assert res["ok"] is True and res["models"] == ["a", "b"]

    c2 = FakeClient({})  # 全404
    res2 = await autodetect.list_models(c2, _spec(), "sk")
    assert res2["ok"] is False and res2["models"] == []


def test_derive_extract_handles_b64():
    body = {"data": [{"b64_json": "iVBORw0KGgo="}]}
    ex = autodetect._derive_extract(body)
    assert ex.urls == "data[].b64_json"
    assert ex.b64 is True


def test_path_exists_rules():
    assert autodetect._path_exists(200, {}) is True
    assert autodetect._path_exists(404, {"e": "x"}) is False
    assert autodetect._path_exists(401, {}) is True
    assert autodetect._path_exists(405, {}) is True
    assert autodetect._path_exists(400, {"error": "size required"}) is True
    # HTML 报错页不算路径存在
    assert autodetect._path_exists(500, "<html>oops</html>") is False


def test_no_vendor_constants_in_source():
    """铁律：探测候选里不能出现任何具体厂商域名/专属路径常量。"""
    src = Path(autodetect.__file__).read_text(encoding="utf-8")
    for bad in ("siliconflow", "agnes", "openai.azure", "api.deepseek",
                "dashscope", "volces", "ark.cn", "aliyuncs"):
        assert bad not in src.lower(), f"探测模块不应硬编码厂商：{bad}"
