"""管理面板：Web 控制台（Provider / 令牌 / 任务 / 参数 / 日志）。

设计原则：
- **与 MCP 服务同进程同端口**：不额外开端口，省掉一份鉴权与反向代理配置；
- **自带会话鉴权**：不复用 MCP 访问令牌（权限范围不同，混用是隐患）；
- **改完即生效**：Provider 与运行参数都走热加载，不需要重启服务。
"""

from .api import AdminAPI, AdminContext, RingLogHandler
from .auth import SessionManager
from .page import PAGE_HTML

__all__ = [
    "AdminAPI", "AdminContext", "RingLogHandler", "SessionManager", "PAGE_HTML",
]
