"""T6 门禁：生图流水线（单模型直调 + 拆分串行 + 部分成功契约；V2 第二轮收尾版）。

覆盖（增量设计方案 V2 三节 pipeline 行 / R17 / R20 / E13 / E24 / E8）：
- 单路由直达：auto 路由 → 当前可用的服务提供方，一次调用定生死（无切换）；
- batch 拆分串行：batch=N → N 次顺序调用，URL 数 = M；
- R20：每次调用前剩余 ≥ 单次档才发；不足即止 = 部分成功（非错误）；
  边界：剩余 29s 停 / 31s 发；
- R17 三分支：1 成 1 败（meta requested/delivered）、全败（[error] 行，
  不产生部分成功）、预算中途耗尽（部分成功）；
- E13：不 persist → meta url_expires=1h；persist → /files URL + persisted；
- E24：熔断 OPEN 期 auto 快失败 rate_limit（retry=yes，注明约 X 秒）；
- 429 → 熔断 HARD 写入；单次失败不中断后续；
- E8：探针未能发出 → 归还半开资格。
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeClock, make_config

from app import pipeline as pipeline_mod
from app.pipeline import GeneratePipeline, validate_params
from providers import gateway as gateway_mod
from infra import budget, circuit


def ok_api(urls=("https://img.example/a.png",)):
    return gateway_mod.ApiCallResult(
        ok=True, status_code=200, provider_id="image", model_id="image", urls=urls
    )


def err_api(status=429, detail="rate limited"):
    return gateway_mod.ApiCallResult(
        ok=False, status_code=status, provider_id="image", model_id="image",
        error_detail=detail,
    )


class FakeApi:
    """可控生图端点 mock：按脚本弹出结果；可挂 on_call 钩子推进时钟。"""

    def __init__(self, results=None, on_call=None):
        self.results = list(results or [])
        self.calls: list[dict] = []
        self.on_call = on_call

    async def generate_image(self, *, model=None, prompt, size, deadline, ratio=None,
                             images=None, provider=None):
        self.calls.append(
            {"model": model, "prompt": prompt, "size": size, "ratio": ratio,
             "images": images})
        if self.on_call is not None:
            self.on_call()
        if self.results:
            return self.results.pop(0)
        return ok_api()


class FakePersist:
    """persist mock：成功返回 /files URL；可置失败降级。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[tuple] = []

    async def persist_images(self, urls, *, deadline, req_id, headers=None):
        self.calls.append((tuple(urls), req_id))
        if self.fail:
            return [(u, "persist_failed") for u in urls]
        return [
            (f"https://host/files/{i}.png", "persisted")
            for i, u in enumerate(urls)
        ]

    def disk_usage_ratio(self):
        return None  # 水位不触发


def make_pipeline(api=None, persist=None, cfg=None, clock=None, circuit_clock=None):
    cfg = cfg or make_config()
    cclock = circuit_clock or FakeClock()
    circuit_ = circuit.CircuitBreaker(
        hard_sec=cfg.circuit_hard_sec, soft_sec=cfg.circuit_soft_sec, clock=cclock,
    )
    pool = budget.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
    pipe = GeneratePipeline(
        cfg, pool, circuit_, api or FakeApi(),
        persist or FakePersist(), clock=clock,
    )
    return pipe, circuit_, pool


# ---------------------------------------------------------------------------
# 单模型直调 + 基本成功契约
# ---------------------------------------------------------------------------
async def test_single_call_success_contract():
    pipe, _, _ = make_pipeline(api=FakeApi([ok_api(("https://img/1.png",))]))
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=False)
    lines = out.splitlines()
    assert lines[0] == "https://img/1.png"
    assert lines[1].startswith("[meta] ")
    assert "model=image" in lines[1]
    assert "size=1024x1024" in lines[1]
    assert "url_expires=1h" in lines[1]  # E13：不 persist 短效警示
    assert "requested" not in lines[1]   # M==N：无部分成功标记


async def test_batch_split_sequential_calls():
    """batch=4 → 恰 4 次顺序调用（image 无原生 batch）。"""
    api = FakeApi([
        ok_api((f"https://img/{i}.png",)) for i in range(4)
    ])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=4, persist_flag=False)
    assert len(api.calls) == 4
    assert out.count("\n") == 4  # 4 URL 行 + meta 行
    for i in range(4):
        assert f"https://img/{i}.png" in out
    assert "requested" not in out.splitlines()[-1]  # 全成功无计数


async def test_calls_carry_no_model_and_normalized_size():
    """V5：模型归服务提供方配置，管道不再往上游塞 model。

    旧版管道写死厂商别名（等于把"换一家 API"变成改代码），现在只把
    prompt / size / ratio / images 交给网关，模型由面板里的 provider 决定。
    """
    api = FakeApi([ok_api()])
    pipe, _, _ = make_pipeline(api=api)
    await pipe.run(prompt="cat", size=" 1024X1024 ", batch_size=1, persist_flag=False)
    assert api.calls[0]["model"] is None          # 管道不指定模型
    assert api.calls[0]["size"] == "1024x1024"    # E12：规范形式直传


# ---------------------------------------------------------------------------
# R17：部分成功契约（三分支）
# ---------------------------------------------------------------------------
async def test_partial_success_one_ok_one_fail():
    """1 成 1 败 → 成功 URL + meta requested=2 delivered=1（R17 ①）。"""
    api = FakeApi([ok_api(("https://img/ok.png",)), err_api(429)])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    lines = out.splitlines()
    assert lines[0] == "https://img/ok.png"
    assert "requested=2" in lines[-1]
    assert "delivered=1" in lines[-1]
    assert lines[-1].startswith("[meta] ")


async def test_all_fail_returns_error_row_not_partial():
    """全败 → [error] 行，不产生部分成功（R17 ②）。"""
    api = FakeApi([err_api(429), err_api(429)])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=rate_limit" in out
    assert "retry=yes" in out
    assert "[meta]" not in out
    assert len(api.calls) == 2  # 单次失败不中断后续


async def test_budget_exhausted_midway_is_partial_success():
    """预算中途耗尽（第 1 次成功后剩余 < 档）→ 即止 = 部分成功非错误（R17 ③/E9/R20）。"""
    clock = FakeClock()

    def on_call():
        clock.advance(141)  # 第 1 次调用后剩余 200-141=59 < 60 档（D5）

    api = FakeApi([ok_api(("https://img/first.png",)), ok_api()], on_call=on_call)
    pipe, _, _ = make_pipeline(api=api, clock=clock)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    lines = out.splitlines()
    assert len(api.calls) == 1  # 第二次因 R20 检查即止，未发出
    assert lines[0] == "https://img/first.png"
    assert "requested=2" in lines[-1]
    assert "delivered=1" in lines[-1]


# ---------------------------------------------------------------------------
# R20：调用前预算检查边界（D5 档 60：59 停 / 61 发）
# ---------------------------------------------------------------------------
async def test_r20_boundary_61s_remaining_sends():
    """第 1 次调用后剩余恰 61 ≥ 60 档（D5）→ 第 2 次照发。"""
    clock = FakeClock()

    def on_call():
        clock.advance(139)  # 剩余 61

    api = FakeApi([ok_api(("https://img/1.png",)), ok_api(("https://img/2.png",))],
                  on_call=on_call)
    pipe, _, _ = make_pipeline(api=api, clock=clock)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    assert len(api.calls) == 2
    assert "https://img/2.png" in out
    assert "requested" not in out.splitlines()[-1]  # 全成功


async def test_r20_deadline_exhausted_in_call_stops_loop():
    """调用中预算耗尽（客户端退避后 check 抛 DeadlineExceeded）→ 即止 = 部分成功。"""

    class DeadlineHitApi:
        def __init__(self):
            self.calls = 0

        async def generate_image(self, **kw):
            self.calls += 1
            if self.calls == 1:
                return ok_api(("https://img/first.png",))
            raise budget.DeadlineExceededError("exhausted")

    api = DeadlineHitApi()
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    lines = out.splitlines()
    assert api.calls == 2
    assert lines[0] == "https://img/first.png"
    assert "requested=2" in lines[-1] and "delivered=1" in lines[-1]


# ---------------------------------------------------------------------------
# E13 / persist 契约
# ---------------------------------------------------------------------------
async def test_persist_true_rewrites_to_files_urls():
    persist = FakePersist()
    pipe, _, _ = make_pipeline(api=FakeApi([ok_api(("https://img/x.png",))]),
                               persist=persist)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=True)
    lines = out.splitlines()
    assert lines[0] == "https://host/files/0.png"
    assert "persisted" in lines[1]
    assert "url_expires" not in lines[1]
    assert persist.calls and persist.calls[0][0] == ("https://img/x.png",)


async def test_persist_fail_degrades_to_original_url():
    """persist 失败降级：原上游 URL + persist_failed + 短效警示（R5/E13）。"""
    persist = FakePersist(fail=True)
    pipe, _, _ = make_pipeline(api=FakeApi([ok_api(("https://img/x.png",))]),
                               persist=persist)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=True)
    lines = out.splitlines()
    assert lines[0] == "https://img/x.png"  # 降级原 URL
    assert "persist_failed" in lines[1]


# ---------------------------------------------------------------------------
# E24：熔断 OPEN 期 auto 快失败
# ---------------------------------------------------------------------------
async def test_circuit_open_fails_fast_with_retry_eta():
    """OPEN 期：[error] rate_limit retry=yes，reason 注明约 X 秒（E24）。"""
    cclock = FakeClock()
    api = FakeApi()
    pipe, cb, _ = make_pipeline(api=api, circuit_clock=cclock)
    cb.record_failure("image", circuit.FailureKind.HARD)  # 429 → OPEN_HARD 600s
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=rate_limit" in out
    assert "retry=yes" in out
    assert "约 600" in out  # ETA 注明
    # V5：错误文案厂商中立（提供方由用户自己配置）
    assert "provider=" not in out        # 厂商中立：不归属到任何一家
    assert api.calls == []  # 快失败：零 IO


async def test_circuit_open_after_expiry_half_open_probe_routes_through():
    """软熔断到期 → HALF_OPEN 首请求获探针（E8），正常通过。"""
    cclock = FakeClock()
    api = FakeApi([ok_api()])
    pipe, cb, _ = make_pipeline(api=api, circuit_clock=cclock)
    cb.record_failure("image", circuit.FailureKind.SOFT)
    cb.record_failure("image", circuit.FailureKind.SOFT)
    cclock.advance(301)  # 软 300s 到期 → HALF_OPEN
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=False)
    assert "https://img.example/a.png" in out
    assert len(api.calls) == 1
    assert cb.snapshot()["image"] == "CLOSED"  # 探针成功复位


async def test_probe_not_sent_returns_grant():
    """E8：探针请求因预算不足未发出（首调用前即止）→ 归还半开资格。

    构造：占住唯一并发槽使 run 在排队中等待（E4：排队计入 deadline），
    排队期间推进请求时钟 180s → 释放槽后 R20 检查剩余 20 < 60 档（D5）→
    首次调用前即止（R20），从未发出任何调用 → 归还资格 → 下一请求
    仍可获探针并成功解除熔断。
    """
    cclock = FakeClock()
    dclock = FakeClock()
    api = FakeApi()
    pipe, cb, pool = make_pipeline(
        api=api, circuit_clock=cclock, clock=dclock,
        cfg=make_config(MAXCONCURRENCY=1),
    )
    cb.record_failure("image", circuit.FailureKind.SOFT)
    cb.record_failure("image", circuit.FailureKind.SOFT)
    cclock.advance(301)  # HALF_OPEN → route() probe=True

    hold = budget.Deadline(50.0)
    async with pool.slot(hold, "hold"):  # 占住唯一槽，run 只能排队
        task = asyncio.create_task(
            pipe.run(prompt="a cat", size="1024x1024",
                     batch_size=1, persist_flag=False)
        )
        await asyncio.sleep(0.01)   # Deadline 已创建、请求正在排队
        dclock.advance(180)         # 排队 180s（E4：计入 deadline）
    out = await asyncio.wait_for(task, timeout=2.0)  # 释放槽 → 继续执行

    assert out.startswith("[error] ")           # M=0 → 错误路径（R17）
    assert "category=upstream_error" in out     # deadline 耗尽类
    assert api.calls == []                       # 从未发出调用（R20 即止）
    assert cb.snapshot()["image"] == "HALF_OPEN"  # 状态未终结

    # 归还后：下一请求（复用同一熔断表）仍可获探针资格 → 成功解除熔断
    d2 = FakeClock()
    api2 = FakeApi([ok_api()])
    cfg2 = make_config()
    pool2 = budget.SlotPool(cfg2.max_concurrency, cfg2.queue_wait_sec)
    pipe2 = GeneratePipeline(cfg2, pool2, cb, api2, FakePersist(), clock=d2)
    out2 = await pipe2.run(prompt="a cat", size="1024x1024",
                           batch_size=1, persist_flag=False)
    assert out2.startswith("https://img")              # 探针成功
    assert cb.snapshot()["image"] == "CLOSED"    # 成功 → 熔断解除


# ---------------------------------------------------------------------------
# 熔断写入（429 HARD / 5xx SOFT / 其余不写）
# ---------------------------------------------------------------------------
async def test_429_writes_hard_circuit():
    api = FakeApi([err_api(429)])
    pipe, cb, _ = make_pipeline(api=api)
    await pipe.run(prompt="a cat", size="1024x1024",
                   batch_size=1, persist_flag=False)
    assert cb.snapshot().get("image") == "OPEN_HARD"


async def test_5xx_writes_soft_failure():
    api = FakeApi([err_api(500)])
    pipe, cb, _ = make_pipeline(api=api)
    await pipe.run(prompt="a cat", size="1024x1024",
                   batch_size=1, persist_flag=False)
    assert cb.snapshot().get("image") == "CLOSED"  # 1 次未达连败 2


async def test_content_policy_400_does_not_write_circuit():
    api = FakeApi([err_api(400)])
    pipe, cb, _ = make_pipeline(api=api)
    await pipe.run(prompt="a cat", size="1024x1024",
                   batch_size=1, persist_flag=False)
    assert cb.snapshot() == {}  # 审核类不写熔断


async def test_success_resets_consecutive_failures():
    api = FakeApi([err_api(500), ok_api()])  # batch=2：败 1 + 成 1
    pipe, cb, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=2, persist_flag=False)
    assert "requested=2" in out and "delivered=1" in out
    assert cb.snapshot().get("image") == "CLOSED"


# ---------------------------------------------------------------------------
# 参数校验 / busy / 网络异常路径
# ---------------------------------------------------------------------------
async def test_invalid_size_returns_error_row():
    pipe, _, _ = make_pipeline()
    out = await pipe.run(prompt="a cat", size="abc",
                         batch_size=1, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=invalid_params" in out
    assert "retry=fixthenretry" in out
    assert "size" in out


async def test_invalid_batch_returns_error_row():
    pipe, _, _ = make_pipeline()
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=5, persist_flag=False)
    assert "category=invalid_params" in out
    assert "batch_size" in out


async def test_empty_prompt_returns_error_row():
    pipe, _, _ = make_pipeline()
    out = await pipe.run(prompt="   ", size="1024x1024",
                         batch_size=1, persist_flag=False)
    assert "category=invalid_params" in out


async def test_network_error_classified_upstream():
    api = FakeApi([err_api(None, "connect refused")])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=False)
    assert "category=upstream_error" in out
    assert "retry=yes" in out


async def test_busy_path_when_slots_exhausted():
    cfg = make_config()
    cclock = FakeClock()
    circuit_ = circuit.CircuitBreaker(
        hard_sec=cfg.circuit_hard_sec, soft_sec=cfg.circuit_soft_sec, clock=cclock,
    )
    pool = budget.SlotPool(1, 0.05)  # 1 槽、快速超时
    api = FakeApi([ok_api()])
    pipe = GeneratePipeline(cfg, pool, circuit_, api, FakePersist())
    async with pool.slot(budget.Deadline(50.0), "hold"):
        out = await pipe.run(prompt="a cat", size="1024x1024",
                             batch_size=1, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=busy" in out
    assert "retry=yes" in out


# ---------------------------------------------------------------------------
# D2（官方文档对齐）：ratio 与档位式 size 的链路集成
# ---------------------------------------------------------------------------
async def test_ratio_passthrough_with_tier_size():
    """档位式 size + 合法 ratio → 规范档位与 ratio 原样到达上游 + meta 含 ratio。"""
    api = FakeApi([ok_api(("https://img/tier.png",))])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="2k", ratio=" 16:9 ",
                         batch_size=1, persist_flag=False)
    assert api.calls[0]["size"] == "2K"      # 档位规范大写直传
    assert api.calls[0]["ratio"] == "16:9"   # ratio 规范形式直传
    meta = out.splitlines()[-1]
    assert "size=2K" in meta
    assert "ratio=16:9" in meta


async def test_ratio_none_by_default_not_sent():
    """不传 ratio → 上游调用 ratio=None（不注入请求体），meta 不含 ratio。"""
    api = FakeApi([ok_api()])
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x1024",
                         batch_size=1, persist_flag=False)
    assert api.calls[0]["ratio"] is None
    meta = out.splitlines()[-1]
    assert "size=1024x1024" in meta
    assert "ratio=" not in meta


async def test_ratio_with_wxh_size_rejected_invalid_params():
    """WxH + ratio = 参数冲突 → [error] invalid_params，零上游调用。"""
    api = FakeApi()
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="1024x768", ratio="16:9",
                         batch_size=1, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=invalid_params" in out
    assert "ratio" in out
    assert api.calls == []  # 校验先于一切 IO


async def test_ratio_bad_value_rejected_invalid_params():
    """ratio 非 8 档 → [error] invalid_params。"""
    api = FakeApi()
    pipe, _, _ = make_pipeline(api=api)
    out = await pipe.run(prompt="a cat", size="2K", ratio="5:4",
                         batch_size=1, persist_flag=False)
    assert out.startswith("[error] ")
    assert "category=invalid_params" in out
    assert api.calls == []


# ---------------------------------------------------------------------------
# logline（R13：任何路径恰好一行）
# ---------------------------------------------------------------------------
async def test_logline_emitted_once_with_fields(caplog):
    api = FakeApi([ok_api()])
    pipe, _, _ = make_pipeline(api=api)
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        out = await pipe.run(prompt="a cat", size="1024x1024",
                             batch_size=1, persist_flag=False)
    assert out.startswith("https://")
    access = [r for r in caplog.records if r.name == "mcp_imagegen.access"]
    assert len(access) == 1
    rendered = access[0].getMessage()
    for field in ("req_id=", "prompt_hash=", "route=auto", "model=image",
                  "result=ok", "total="):
        assert field in rendered, rendered
    assert "quality=" not in rendered  # V2：参数摘要不含已删参数
