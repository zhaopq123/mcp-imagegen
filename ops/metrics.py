"""运行时指标（面板 Dashboard 的数据源）。

设计取舍：不引 Prometheus 客户端，也不用线程安全的原子类——本项目全栈
单事件循环（R2 禁线程），dict/deque 的读写天然串行，O(1) 无竞争的计数器
就是最快的方案，同时省掉一个依赖（这条对各平台的自托管部署很关键）。

保留的是**够用的分辨率**：
- 计数：总量 / 成功 / 失败 / 按类别细分；
- 延迟：滑动窗口（默认 500 条）算 p50/p95/p99——全量直方图没必要，
  近窗百分位已经能回答「现在是不是变慢了」；
- 时间线：最近 N 条请求明细，供面板画趋势与排障。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Optional


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return float(ordered[index])


class Metrics:
    """单循环内存指标；快照生成 O(窗口大小)。"""

    def __init__(
        self,
        *,
        latency_window: int = 500,
        event_window: int = 200,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self.started_at = wall()
        self.started_monotonic = clock()
        self.total = 0
        self.ok = 0
        self.failed = 0
        self.deferred = 0            # 视频在预算内未出结果（E17，非失败）
        self.by_category: dict = {}
        self.by_provider: dict = {}
        self.by_kind: dict = {}
        self._latency: Deque[float] = deque(maxlen=latency_window)
        self._events: Deque[dict] = deque(maxlen=event_window)

    # ------------------------------------------------------------------
    def record(
        self,
        *,
        kind: str,
        result: str,
        latency_ms: float = 0.0,
        provider: str = "",
        token: str = "",
        detail: str = "",
    ) -> None:
        self.total += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        if result.startswith("ok"):
            self.ok += 1
        elif result.startswith("error"):
            self.failed += 1
        else:
            self.deferred += 1
        category = result.split(":", 1)[1] if ":" in result else result
        self.by_category[category] = self.by_category.get(category, 0) + 1
        if provider:
            slot = self.by_provider.setdefault(provider, {"calls": 0, "errors": 0})
            slot["calls"] += 1
            if not result.startswith("ok"):
                slot["errors"] += 1
        if latency_ms:
            self._latency.append(float(latency_ms))
        self._events.append({
            "ts": self._wall(), "kind": kind, "result": result,
            "ms": round(float(latency_ms), 1), "provider": provider,
            "detail": detail[:160],
        })

    # ------------------------------------------------------------------
    def _qps(self, window_sec: float = 60.0) -> float:
        now = self._wall()
        count = sum(1 for e in self._events if now - e["ts"] <= window_sec)
        span = min(window_sec, max(now - self.started_at, 1.0))
        return round(count / span, 3)

    def snapshot(self) -> dict:
        latencies = list(self._latency)
        uptime = self._clock() - self.started_monotonic
        recent = [
            e for e in self._events
            if self._wall() - e["ts"] <= 300
        ]
        return {
            "uptime_sec": round(uptime, 1),
            "started_at": self.started_at,
            "qps_60s": self._qps(60.0),
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "deferred": self.deferred,
            "success_rate": (
                round(self.ok / self.total * 100, 2) if self.total else None),
            "latency_ms": {
                "p50": round(_percentile(latencies, 0.50), 1),
                "p95": round(_percentile(latencies, 0.95), 1),
                "p99": round(_percentile(latencies, 0.99), 1),
                "window": len(latencies),
            },
            "by_category": dict(self.by_category),
            "by_kind": dict(self.by_kind),
            "by_provider": dict(self.by_provider),
            "recent": list(recent)[-50:],
        }
