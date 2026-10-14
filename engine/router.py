"""路由器（V2 退化版；红线 C1；E2/E24）。

单 Provider 单生图模型 → 路由链①–⑤全删，退化为：

    显式 model="image"（内部能力别名）→ 探针标记直达（E2：绕过熔断表——用户
        自救通道，其成功/失败结果仍写熔断）；
    auto → 熔断门：
        OPEN（硬/软）→ 快失败决策（E24：pipeline 转为 rate_limit
            [error] 行，retry=yes，注明"约 X 秒后重试"）；
        CLOSED / HALF_OPEN → 直达当前可用的服务提供方（半开首个请求获
            探针资格 E8，未发出时归还）。

能力别名：对外只暴露内部别名 image / video，真实的上游模型名写在
服务提供方的配置里（同一个 capability.model），不在工具层面暴露。

reason_tag 有限枚举（进日志"路由决策来源"字段）：
    explicit / auto / circuit_open
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol

# 生图能力别名（视频不参与路由——提交/查询直接按能力选择提供方）
MODEL_IMAGE = "image"
MODELS = (MODEL_IMAGE,)

# 别名 → 真实模型名的解析**不在本模块**：由服务提供方配置驱动
# （providers/gateway.py 渲染请求模板时注入 capability.model）。
# 保留此常量仅为向后兼容（无调用点）。
MODEL_ALIASES: dict[str, str] = {}

# reason 枚举（有限集，严禁扩展语义）
REASON_EXPLICIT = "explicit"          # 显式指定（绕过熔断表，E2）
REASON_AUTO = "auto"                  # 自动路由（熔断门通过，含探针）
REASON_CIRCUIT_OPEN = "circuit_open"  # E24：熔断 OPEN 期快失败决策

REASONS = (REASON_EXPLICIT, REASON_AUTO, REASON_CIRCUIT_OPEN)


@dataclass(frozen=True)
class RouteDecision:
    """路由决策。

    - 正常：model=image（reason=explicit|auto；probe=探针资格）；
    - OPEN 拒绝：model=None（reason=circuit_open），retry_after_sec 供
      E24 文案"约 X 秒后重试"（向上取整到秒）。
    """

    model: Optional[str]
    reason: str
    probe: bool = False
    retry_after_sec: float = 0.0


class _CircuitLike(Protocol):
    """熔断门最小接口（duck typing；engine 纯决策层不依赖 infra）。"""

    def check(self, model: str):  # pragma: no cover - 接口声明
        ...


def route(model: Optional[str], circuit: Optional[_CircuitLike] = None) -> RouteDecision:
    """熔断门路由（纯决策，无副作用）。

    - model 为 MODELS 之一（显式）→ 直达，不查熔断表（E2）；
    - model 为 None/auto → 查熔断：OPEN → E24 快失败决策；
      CLOSED/HALF_OPEN → 直达（HALF_OPEN 首个请求带 probe=True）。
    """
    if model is not None:
        if model not in MODELS:
            raise ValueError(f"未知模型: {model!r}（应为 {MODELS} 之一或省略=auto）")
        return RouteDecision(MODEL_IMAGE, REASON_EXPLICIT)

    if circuit is None:
        return RouteDecision(MODEL_IMAGE, REASON_AUTO)

    status = circuit.check(MODEL_IMAGE)
    if status.probe:  # 半开后的第一个请求获探针资格（E8）
        return RouteDecision(MODEL_IMAGE, REASON_AUTO, probe=True)
    if status.open:  # E24：OPEN 期 auto 快失败
        return RouteDecision(
            None, REASON_CIRCUIT_OPEN,
            retry_after_sec=math.ceil(status.remaining_sec),
        )
    return RouteDecision(MODEL_IMAGE, REASON_AUTO)
