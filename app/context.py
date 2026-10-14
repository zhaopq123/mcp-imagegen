"""请求上下文（多会话 / 多工作空间隔离的载体）。

为什么用 ContextVar 而不是层层传参
----------------------------------
MCP 工具函数的签名是被 fastmcp 按 schema 生成的，中间再插一个
``context: TokenRecord`` 参数会污染工具契约。ContextVar 让我们能在网关层
（唯一能拿到 Authorization 头的地方）把「这次调用属于哪个令牌」挂上，
管道深处按需读取，工具签名保持干净。

安全性：ContextVar 在 ``create_task`` 时会复制当前上下文，因此管道里
用 gather / create_task 派生的子任务读到的仍是同一个令牌语义；反过来
一个请求的变量也绝不会泄漏到另一个请求。
"""
from __future__ import annotations

from contextvars import ContextVar

from providers import TokenRecord

current_token: "ContextVar[TokenRecord | None]" = ContextVar(
    "mcp_imagegen_token", default=None)


def token_scope() -> "dict | None":
    """当前令牌的路由约束；无令牌 → None（走全局路由）。"""
    record = current_token.get()
    if record is None:
        return None
    return {
        "id": record.id,
        "provider": record.default_provider or None,
        "capabilities": list(record.allowed_capabilities or []),
    }
