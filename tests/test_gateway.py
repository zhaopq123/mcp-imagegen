"""网关门禁：请求模板 → 真实 HTTP 形状、响应抽取、多提供方路由、重试与限速。

全部走脚本式 transport（零真实网络）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from infra import budget
from providers import schema
from providers.gateway import ProviderGateway, compute_read_timeout
from providers.store import Store


class ScriptedTransport(httpx.AsyncBaseTransport):
    """按序返回预设响应/异常，并记录全部请求。"""

    def __init__(self, script=None, handler=None) -> None:
        self.script = list(script or [])
        self.handler = handler
        self.requests: list = []

    def push(self, item) -> None:
        self.script.append(item)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            item = self.handler(request)
        else:
            item = self.script.pop(0) if self.script else httpx.Response(
                200, json={"data": [{"url": "https://img/default.png"}]},
                request=request)
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        return item


def image_provider(pid="p1", **kw) -> schema.ProviderConfig:
    spec = schema.ProviderConfig(
        id=pid, name=pid, base_url="https://api.example.com",
        auth=schema.AuthSpec(kind=schema.AUTH_BEARER, token="sk-test"),
        capabilities={"image": schema.CapabilitySpec(
            model="img-1",
            request=schema.RequestSpec(
                method="POST", path="/v1/images/generations",
                body={"model": "{model}", "prompt": "{prompt}",
                      "size": "{size}", "ratio": "{ratio}",
                      "image": "{{images}}"}),
            extract=schema.ExtractSpec(urls="images[].url"),
        )})
    for key, value in kw.items():
        setattr(spec, key, value)
    return spec


def video_provider(pid="v1") -> schema.ProviderConfig:
    return schema.ProviderConfig(
        id=pid, name=pid, base_url="https://api.example.com",
        auth=schema.AuthSpec(kind=schema.AUTH_BEARER, token="sk-test"),
        capabilities={"video": schema.CapabilitySpec(
            model="vid-1",
            request=schema.RequestSpec(
                method="POST", path="/v1/videos",
                body={"model": "{model}", "prompt": "{prompt}",
                      "seconds": "{seconds}", "aspect_ratio": "{aspect_ratio}",
                      "seed": "{{seed}}"}),
            extract=schema.ExtractSpec(task_id="video_id"),
            poll=schema.RequestSpec(
                method="GET", path="/v1/videos/query",
                query={"video_id": "{task_id}", "model_name": "{model}"}),
            poll_extract=schema.ExtractSpec(status="status", url="url"),
        )})


def make_gateway(cfg, tmp_path, providers, transport, sleeper=None) -> ProviderGateway:
    store = Store(str(tmp_path / "store.json"))
    store.load()
    for spec in providers:
        store.upsert_provider(spec)
    return ProviderGateway(cfg, store, transport=transport, sleep=sleeper)


# ---------------------------------------------------------------------------
# 生图
# ---------------------------------------------------------------------------
async def test_generate_image_returns_urls(cfg, tmp_path):
    transport = ScriptedTransport([
        httpx.Response(200, json={"images": [{"url": "https://a/1.png"},
                                             {"url": "https://a/2.png"}]}),
    ])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        res = await gw.generate_image(
            prompt="a cat", size="1024x1024",
            deadline=budget.Deadline(60.0))
        assert res.ok is True
        assert res.urls == ("https://a/1.png", "https://a/2.png")
        assert res.provider_id == "p1"
    finally:
        await gw.aclose()


async def test_request_body_is_rendered_from_template(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        await gw.generate_image(
            prompt="a cat", size="1K", ratio="16:9", images=("data:image/png;base64,x",),
            deadline=budget.Deadline(60.0))
        request = transport.requests[0]
        body = json.loads(request.content)
        assert body == {"model": "img-1", "prompt": "a cat", "size": "1K",
                        "ratio": "16:9", "image": ["data:image/png;base64,x"]}
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.url.path == "/v1/images/generations"
    finally:
        await gw.aclose()


async def test_missing_optional_variable_is_not_sent(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        await gw.generate_image(prompt="x", size="1K",
                                deadline=budget.Deadline(60.0))
        body = json.loads(transport.requests[0].content)
        assert "ratio" not in body and "image" not in body
    finally:
        await gw.aclose()


async def test_base64_results_are_wrapped_as_data_uri(cfg, tmp_path):
    spec = image_provider()
    spec.capabilities["image"].extract = schema.ExtractSpec(
        urls="data[].b64_json", b64=True)
    transport = ScriptedTransport([httpx.Response(
        200, json={"data": [{"b64_json": "AAAA"}]})])
    gw = make_gateway(cfg, tmp_path, [spec], transport)
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.urls == ("data:image/png;base64,AAAA",)
    finally:
        await gw.aclose()


async def test_no_provider_returns_not_configured(cfg, tmp_path):
    gw = make_gateway(cfg, tmp_path, [], ScriptedTransport())
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.ok is False
        assert res.reason == "no_provider"
        assert res.status_code is None
    finally:
        await gw.aclose()


async def test_malformed_200_is_error(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(200, json={"nope": 1})])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.ok is False
        assert res.status_code == 200
        assert "抽取不到" in res.error_detail
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 重试 / 限速 / 错误分类
# ---------------------------------------------------------------------------
async def test_5xx_retried_once_then_failed(cfg, tmp_path, clock):
    from tests.conftest import FakeSleeper
    transport = ScriptedTransport([
        httpx.Response(503, json={"message": "busy"}),
        httpx.Response(503, json={"message": "busy again"}),
    ])
    sleeper = FakeSleeper()
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport, sleeper=sleeper)
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.ok is False and res.status_code == 503
        assert len(transport.requests) == 2      # 仅一次重试
        assert len(sleeper.calls) == 1           # 退避发生了
    finally:
        await gw.aclose()


async def test_429_not_retried_but_penalized(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(
        429, json={"message": "slow down"}, headers={"Retry-After": "3"})])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert len(transport.requests) == 1      # 零重试
        assert res.retry_after == 3.0
        runtime = gw.get("p1")
        assert runtime.limiter.blocked() > 0     # 进入惩罚窗口
    finally:
        await gw.aclose()


async def test_network_error_classified(cfg, tmp_path):
    transport = ScriptedTransport([
        lambda req: httpx.ConnectError("refused", request=req),
        lambda req: httpx.ConnectError("refused", request=req),
    ])
    from tests.conftest import FakeSleeper
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport,
                      sleeper=FakeSleeper())
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.ok is False and res.status_code is None
        assert "ConnectError" in res.error_detail
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 多提供方路由
# ---------------------------------------------------------------------------
async def test_route_by_priority(cfg, tmp_path):
    a = image_provider("a")
    a.priority = 10
    b = image_provider("b")
    b.priority = 1
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [a, b], transport)
    try:
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.provider_id == "b"           # 数字小者优先
    finally:
        await gw.aclose()


async def test_explicit_preference_wins(cfg, tmp_path):
    a = image_provider("a"); a.priority = 1
    b = image_provider("b"); b.priority = 5
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [a, b], transport)
    try:
        res = await gw.generate_image(prompt="x", size="1K", provider="b",
                                      deadline=budget.Deadline(60.0))
        assert res.provider_id == "b"
    finally:
        await gw.aclose()


async def test_open_provider_is_skipped(cfg, tmp_path):
    a = image_provider("a"); a.priority = 1
    b = image_provider("b"); b.priority = 5
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [a, b], transport)
    try:
        from infra.circuit import FailureKind
        gw.get("a").circuit.record_failure("a", FailureKind.HARD)
        res = await gw.generate_image(prompt="x", size="1K",
                                      deadline=budget.Deadline(60.0))
        assert res.provider_id == "b"           # 熔断中的 a 被跳过
    finally:
        await gw.aclose()


async def test_providers_have_isolated_limiter_and_pool(cfg, tmp_path):
    a = image_provider("a"); b = image_provider("b")
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [a, b], transport)
    try:
        ra, rb = gw.get("a"), gw.get("b")
        assert ra.limiter is not rb.limiter
        assert ra.circuit is not rb.circuit
        assert ra.client is not rb.client
    finally:
        await gw.aclose()


async def test_reload_picks_up_new_provider(cfg, tmp_path):
    store = Store(str(tmp_path / "store.json"))
    store.load()
    gw = ProviderGateway(cfg, store, transport=ScriptedTransport())
    try:
        assert gw.has_capability("image") is False
        store.upsert_provider(image_provider("late"))
        added, removed = gw.reload()
        assert added == ["late"] and removed == []
        assert gw.has_capability("image") is True
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 视频：提交 + 轮询
# ---------------------------------------------------------------------------
async def test_submit_video_returns_task_id(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(200, json={"video_id": "vid-9"})])
    gw = make_gateway(cfg, tmp_path, [video_provider()], transport)
    try:
        res = await gw.submit_video(
            prompt="a cat", seconds=5, aspect_ratio="16:9",
            deadline=budget.Deadline(30.0))
        assert res.ok is True and res.video_id == "vid-9"
        body = json.loads(transport.requests[0].content)
        assert body["seconds"] == "5"           # 字符串形态（主流上游约定）
        assert "seed" not in body
    finally:
        await gw.aclose()


async def test_query_video_normalizes_status(cfg, tmp_path):
    spec = video_provider()
    spec.capabilities["video"].poll_extract = schema.ExtractSpec(
        status="state", url="result.video_url",
        status_map={"DONE": "completed"})
    transport = ScriptedTransport([httpx.Response(200, json={
        "state": "DONE", "result": {"video_url": "https://v/1.mp4"}})])
    gw = make_gateway(cfg, tmp_path, [spec], transport)
    try:
        res = await gw.query_video(video_id="vid-9",
                                   deadline=budget.Deadline(30.0))
        assert res.ok is True
        assert res.video_status == "completed"
        assert res.video_url == "https://v/1.mp4"
        request = transport.requests[0]
        assert request.method == "GET"
        assert request.url.params["video_id"] == "vid-9"
        assert request.url.params["model_name"] == "vid-1"
    finally:
        await gw.aclose()


async def test_query_without_poll_endpoint_reports_config_error(cfg, tmp_path):
    spec = video_provider()
    spec.capabilities["video"].poll = None
    gw = make_gateway(cfg, tmp_path, [spec], ScriptedTransport())
    try:
        res = await gw.query_video(video_id="x",
                                   deadline=budget.Deadline(30.0))
        assert res.ok is False and res.reason == "poll_undefined"
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 鉴权形态
# ---------------------------------------------------------------------------
async def test_auth_header_mode(cfg, tmp_path):
    spec = image_provider()
    spec.auth = schema.AuthSpec(kind=schema.AUTH_HEADER, token="abc",
                                name="X-API-Key", prefix="")
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [spec], transport)
    try:
        await gw.generate_image(prompt="x", size="1K",
                                deadline=budget.Deadline(60.0))
        assert transport.requests[0].headers["x-api-key"] == "abc"
    finally:
        await gw.aclose()


async def test_auth_query_mode(cfg, tmp_path):
    spec = image_provider()
    spec.auth = schema.AuthSpec(kind=schema.AUTH_QUERY, token="abc", name="key")
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [spec], transport)
    try:
        await gw.generate_image(prompt="x", size="1K",
                                deadline=budget.Deadline(60.0))
        assert transport.requests[0].url.params["key"] == "abc"
    finally:
        await gw.aclose()


async def test_env_reference_secret(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "from-env")
    spec = image_provider()
    spec.auth = schema.AuthSpec(kind=schema.AUTH_BEARER, token="env:TEST_UPSTREAM_KEY")
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [spec], transport)
    try:
        await gw.generate_image(prompt="x", size="1K",
                                deadline=budget.Deadline(60.0))
        assert transport.requests[0].headers["authorization"] == "Bearer from-env"
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 纯函数与统计
# ---------------------------------------------------------------------------
def test_compute_read_timeout_clamped_by_remaining(clock):
    deadline = budget.Deadline(100.0, clock=clock)
    assert compute_read_timeout(30.0, deadline) == 30.0
    clock.advance(95.0)
    assert compute_read_timeout(30.0, deadline) == pytest.approx(5.0)
    clock.advance(10.0)
    assert compute_read_timeout(30.0, deadline) == 0.0


async def test_stats_expose_health(cfg, tmp_path):
    transport = ScriptedTransport([httpx.Response(
        200, json={"images": [{"url": "u"}]})])
    gw = make_gateway(cfg, tmp_path, [image_provider()], transport)
    try:
        await gw.generate_image(prompt="x", size="1K",
                                deadline=budget.Deadline(60.0))
        snapshot = gw.stats()["p1"]
        assert snapshot["calls"] == 1 and snapshot["ok"] == 1
        assert snapshot["capabilities"] == ["image"]
        assert snapshot["circuit"] == "CLOSED"
        assert "base_url" in snapshot
    finally:
        await gw.aclose()
