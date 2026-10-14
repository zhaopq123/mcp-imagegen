"""共享 fixtures：测试配置、假时钟、可控传输层。

全部单测 mock 上游，不调真实 API（R 系列门禁约束）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config, load_config  # noqa: E402


class FakeClock:
    """手动推进的单调时钟（circuit / deadline 测试用，免真实等待）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


def make_config(**overrides) -> Config:
    env = {
        # V5：上游凭据不再由环境变量注入——Provider 与访问令牌全部走
        # data/store.json（管理面板维护），load_config 不再认识任何厂商变量。
        "MCPACCESS_TOKEN": "test-mcp-token",
    }
    for key, value in overrides.items():
        env[key.upper()] = str(value)
    return load_config(env)


@pytest.fixture
def cfg():
    """默认规格书第七节参数（密钥为测试占位值）。"""
    return make_config()


@pytest.fixture
def clock():
    return FakeClock()


def make_store(path, tokens: tuple = ()):
    """建一个落盘 store，并按需预置访问令牌。

    V5：访问令牌 / 服务提供方都在 store 里（管理面板维护），不再由环境变量
    注入——测试想让某个 token 通过门禁，就得先把它写进 store。
    """
    from providers import schema as provider_schema
    from providers.store import Store

    store = Store(str(path))
    store.load()
    for index, secret in enumerate(tokens):
        store.upsert_token(provider_schema.TokenRecord(
            id=f"t{index}", name=f"token-{index}", token=secret))
    return store


def network_error(request: httpx.Request) -> Exception:
    """网络异常工厂（ScriptedTransport 脚本项）。"""
    return httpx.ConnectError("connection refused", request=request)


def timeout_error(request: httpx.Request) -> Exception:
    """读超时工厂（ScriptedTransport 脚本项）。"""
    return httpx.ReadTimeout("read timed out", request=request)


class FakeSleeper:
    """记录式 sleep：不真正等待，供退避测试断言时长。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)


class ScriptedTransport(httpx.AsyncBaseTransport):
    """脚本式 transport：按序返回预设响应/异常，记录全部请求。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def push(self, item) -> None:
        self.script.append(item)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.script.pop(0) if self.script else httpx.Response(
            200, json={"images": [{"url": "https://img/default.png"}]},
            request=request,
        )
        if callable(item):  # 响应/异常工厂
            item = item(request)
        if isinstance(item, Exception):
            raise item
        return item


class GatedTransport(httpx.AsyncBaseTransport):
    """挂起式 transport：请求到达即通知，等 gate 放行后返回 next_response。

    用于验证 rotate 排空（R8/E6）与在途计数。
    """

    def __init__(self) -> None:
        self.arrived = asyncio.Event()   # 请求已到达（测试侧 wait）
        self.gate = asyncio.Event()      # 放行闸门（测试侧 set）
        self.requests: list[httpx.Request] = []
        self.next_response: httpx.Response | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.arrived.set()
        await self.gate.wait()
        self.gate.clear()
        return self.next_response or httpx.Response(
            200, json={"images": [{"url": "https://img/gated.png"}]}, request=request,
        )


@pytest.fixture
def scripted():
    def _make(script: list) -> ScriptedTransport:
        return ScriptedTransport(script)

    return _make


@pytest.fixture
def gated():
    return GatedTransport()
