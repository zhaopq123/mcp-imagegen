"""服务提供方（Provider）抽象层。

把一个「能生成图片/视频的 HTTP API」变成一份可读、可改、可在管理面板
增删改查的配置。本包不含任何厂商的地址、模型或密钥——全部由使用者配置。

三件套：
    schema    配置的数据结构与校验
    template  请求模板渲染 + 响应字段抽取
    store     持久化（JSON 原子落盘）与令牌/管理员凭据
    gateway   运行时：路由、连接池隔离、限速、熔断、协议适配
"""

from . import gateway, schema, store, template
from .gateway import ApiCallResult, NoProviderError, ProviderGateway
from .schema import (CAP_IMAGE, CAP_VIDEO, AuthSpec, CapabilitySpec,
                     LimitSpec, PoolSpec, ProviderConfig, RequestSpec,
                     TokenRecord)
from .store import Store, StoreError

__all__ = [
    "gateway", "schema", "store", "template",
    "ApiCallResult", "NoProviderError", "ProviderGateway",
    "CAP_IMAGE", "CAP_VIDEO", "AuthSpec", "CapabilitySpec",
    "LimitSpec", "PoolSpec", "ProviderConfig", "RequestSpec", "TokenRecord",
    "Store", "StoreError",
]
