"""管理面板 · 会话与鉴权。

模型很简单，但每一步都有理由：

- **会话令牌 = 随机串 + 内存表**：不做无状态 JWT。管理面板是单机自托管，
  内存表意味着「登出立刻生效」「服务重启即全体下线」，没有过期令牌在
  网络上飘着的窗口。
- **Cookie 三件套**：``HttpOnly``（JS 读不到，XSS 拿不走）+ ``SameSite=Strict``
  （跨站请求不带它）+ ``Secure``（若走 HTTPS）。
- **额外要求自定义头**：所有写操作必须带 ``X-Admin-Request: 1``。
  浏览器里跨站表单无法设置自定义头，这一条就把最朴素的 CSRF 挡在门外
  （same-site 判断在部分浏览器/PROXY 场景下会失效，不能只靠 cookie 属性）。
- **未初始化 → setup 模式**：没有任何管理员凭据时，除了 setup 接口全部
  拒绝，避免「起服务的人 = 管理员」以外的默认口令后门。
"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Callable, Optional

from config import ADMIN_SESSION_TTL_SEC
from providers import Store

SESSION_COOKIE = "mi_admin"
WRITE_HEADER = "X-Admin-Request"
CSRF_HEADER_VALUE = "1"


class SessionError(Exception):
    """鉴权失败（带 HTTP 状态码语义）。"""

    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        self.message = message or code
        super().__init__(message)


class SessionManager:
    """内存会话表 + Cookie 签发/校验。"""

    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._sessions: dict[str, float] = {}

    # ------------------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self._store.admin_ready

    def issue(self) -> str:
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = self._clock() + ADMIN_SESSION_TTL_SEC
        return sid

    def valid(self, sid: Optional[str]) -> bool:
        if not sid:
            return False
        expiry = self._sessions.get(sid)
        if expiry is None:
            return False
        if expiry <= self._clock():
            self._sessions.pop(sid, None)
            return False
        return True

    def revoke(self, sid: Optional[str]) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def sweep(self) -> int:
        now = self._clock()
        stale = [k for k, exp in self._sessions.items() if exp <= now]
        for key in stale:
            self._sessions.pop(key, None)
        return len(stale)

    # ------------------------------------------------------------------
    # 请求级校验
    # ------------------------------------------------------------------
    def cookie_of(self, headers) -> Optional[str]:
        for raw_name, raw_value in headers or []:
            if raw_name.lower() == b"cookie":
                return _cookie_value(bytes(raw_value).decode("latin-1"),
                                     SESSION_COOKIE)
        return None

    def authenticate(self, headers, *, write: bool) -> Optional[str]:
        """返回会话 id；不通过抛 SessionError。"""
        sid = self.cookie_of(headers)
        if write or True:  # 读写都要求会话（读数据同样是隐私）
            if not self.valid(sid):
                raise SessionError(401, "unauthorized", "需要登录")
        return sid

    def require_write(self, headers) -> None:
        """写操作的二次校验：会话 + CSRF 头。"""
        self.authenticate(headers, write=True)
        value = _header_value(headers, WRITE_HEADER)
        if not value or not hmac.compare_digest(value, CSRF_HEADER_VALUE):
            raise SessionError(
                403, "csrf", f"写操作缺少 {WRITE_HEADER} 头（防跨站请求伪造）")

    def cookie_header(self, sid: str, *, secure: bool) -> str:
        flags = f"Path=/admin; HttpOnly; SameSite=Strict; Max-Age={int(ADMIN_SESSION_TTL_SEC)}"
        if secure:
            flags += "; Secure"
        return f"{SESSION_COOKIE}={sid}; {flags}"


def _cookie_value(raw: str, name: str) -> Optional[str]:
    for part in raw.split(";"):
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


def _header_value(headers, name: str) -> Optional[str]:
    target = name.lower().encode("latin-1")
    for raw_name, raw_value in headers or []:
        if bytes(raw_name).lower() == target:
            return bytes(raw_value).decode("latin-1")
    return None
