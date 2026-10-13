"""T2/T3 门禁：Agnes 客户端（生图 + 视频两端点；全部 mock 上游）。

覆盖（增量设计方案 V2 七节 T2/T3 测试列）：
- 三端点成功 / 4xx / 429 / 5xx 重试 / 超时 / read 动态 / rotate 排空；
- E25：生图请求体仅 {model, prompt, size}，quality/steps/guidance/seed 不注入；
- E21：视频提交 seconds int→str；
- F3：错误体 {"error":{code,message,type}} 解析（message 透传含请求 id）；
- E14：生图响应 images/data 双兼容；
- R7/R8/C4/R3：同 V1 客户端纪律（四段超时、Limits、每日重建、
  重试纪律、deadline 耗尽零 IO）。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import FakeSleeper, network_error, timeout_error

from infra import agnes, budget

BASE = "https://api.agnes-ai.cn"


# ---------------------------------------------------------------------------
# 响应工厂
# ---------------------------------------------------------------------------
def req() -> httpx.Request:
    return httpx.Request("POST", BASE + "/x")


def ok_image(urls=("https://img.example/a.png",), status=200, headers=None):
    return httpx.Response(
        status, json={"images": [{"url": u} for u in urls]},
        headers=headers or {}, request=req(),
    )


def ok_image_data_key(urls=("https://img.example/a.png",)):
    return httpx.Response(
        200, json={"data": [{"url": u} for u in urls]}, request=req(),
    )


def err(status, body=None, headers=None):
    if body is None:
        body = {"error": {
            "code": "invalid_request_error",
            "message": "Invalid parameter (request id: 20261010-abc)",
            "type": "AgnesAI_error",
        }}
    return httpx.Response(
        status, json=body, headers=headers or {}, request=req(),
    )


def submit_ok(video_id="vid-123", status=200):
    return httpx.Response(status, json={"video_id": video_id}, request=req())


def submit_ok_data_container(video_id="vid-data"):
    return httpx.Response(
        200, json={"data": {"video_id": video_id}}, request=req(),
    )


def query_ok(status="running", url=None, http_status=200):
    body: dict = {"status": status}
    if url is not None:
        body["url"] = url
    return httpx.Response(http_status, json=body, request=req())


def query_ok_data_container(status="completed", url="https://v.example/1.mp4"):
    return httpx.Response(
        200, json={"data": {"status": status, "url": url}}, request=req(),
    )


# ---------------------------------------------------------------------------
# 客户端工厂 + 参数助手
# ---------------------------------------------------------------------------
def make_client(cfg, transport, **kw):
    return agnes.AgnesClient(cfg, transport=transport, **kw)


async def gen_image(client, deadline=None, model="agnes-image", prompt="a cat",
                    size="1024x1024"):
    return await client.generate_image(
        model=model, prompt=prompt, size=size,
        deadline=deadline or budget.Deadline(200.0),
    )


async def submit(client, deadline=None, model="agnes-video", prompt="a cat",
                 seconds=5, aspect_ratio="16:9"):
    return await client.submit_video(
        model=model, prompt=prompt, seconds=seconds, aspect_ratio=aspect_ratio,
        deadline=deadline or budget.Deadline(200.0),
    )


async def query(client, video_id="vid-123", model="agnes-video", deadline=None):
    return await client.query_video(
        video_id=video_id, model=model,
        deadline=deadline or budget.Deadline(200.0),
    )


# ---------------------------------------------------------------------------
# 生图：成功 + 请求体（E25）+ 双兼容（E14）
# ---------------------------------------------------------------------------
async def test_generate_image_success(scripted, cfg):
    t = scripted([ok_image(("https://img.example/a.png", "https://img.example/b.png"))])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert result.ok and result.status_code == 200
        assert result.urls == ("https://img.example/a.png", "https://img.example/b.png")
        assert result.model_id == "agnes-image-2.5-flash"
        # 请求行：方法/URL/鉴权
        r = t.requests[0]
        assert r.method == "POST"
        assert str(r.url) == BASE + "/v1/images/generations"
        assert r.headers["Authorization"] == "Bearer test-agnes-token"
        # E25：请求体仅 {model, prompt, size}——严格相等，多余字段即失败
        body = json.loads(r.content)
        assert body == {
            "model": "agnes-image-2.5-flash",
            "prompt": "a cat",
            "size": "1024x1024",
        }
    finally:
        await client.aclose()


async def test_generate_image_data_key_dual_compat(scripted, cfg):
    t = scripted([ok_image_data_key(("https://img.example/d.png",))])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert result.ok
        assert result.urls == ("https://img.example/d.png",)
    finally:
        await client.aclose()


async def test_generate_image_malformed_200(scripted, cfg):
    t = scripted([httpx.Response(200, json={"foo": "bar"}, request=req())])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert not result.ok
        assert result.status_code == 200
        assert "malformed" in result.error_detail
    finally:
        await client.aclose()


async def test_generate_image_unknown_model_rejected(scripted, cfg):
    client = make_client(cfg, scripted([]))
    try:
        with pytest.raises(ValueError, match="未知模型"):
            await gen_image(client, model="dall-e")
    finally:
        await client.aclose()


async def test_generate_image_video_model_rejected(scripted, cfg):
    """生图端点不接受视频模型（端点-模型强绑定）。"""
    client = make_client(cfg, scripted([]))
    try:
        with pytest.raises(ValueError, match="不适用"):
            await gen_image(client, model="agnes-video")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 生图：4xx / 429 零重试（C4）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_generate_image_4xx_no_retry(scripted, cfg, status):
    t = scripted([err(status)])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert not result.ok and result.status_code == status
        assert len(t.requests) == 1  # 零重试
        assert "Invalid parameter" in result.error_detail  # F3：error.message 透传
    finally:
        await client.aclose()


async def test_generate_image_429_no_retry(scripted, cfg):
    t = scripted([err(429, headers={"Retry-After": "2"})])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert not result.ok and result.status_code == 429
        assert len(t.requests) == 1  # 零重试
        assert result.retry_after == 2.0
    finally:
        await client.aclose()


async def test_generate_image_5xx_retry_once_then_success(scripted, cfg):
    t = scripted([err(500), ok_image()])
    sleeper = FakeSleeper()
    client = make_client(cfg, t, sleep=sleeper)
    try:
        result = await gen_image(client)
        assert result.ok
        assert result.urls == ("https://img.example/a.png",)
        assert len(t.requests) == 2  # 恰重试 1 次
        assert len(sleeper.calls) == 1
        assert 0.25 <= sleeper.calls[0] <= 1.0  # 全抖动区间
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_generate_image_5xx_retry_once_then_fail(scripted, cfg, status):
    t = scripted([err(status), err(status)])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await gen_image(client)
        assert not result.ok and result.status_code == status
        assert len(t.requests) == 2  # 重试 1 次后放弃
    finally:
        await client.aclose()


async def test_generate_image_network_error_retry(scripted, cfg):
    t = scripted([network_error, ok_image()])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await gen_image(client)
        assert result.ok
        assert len(t.requests) == 2
    finally:
        await client.aclose()


async def test_generate_image_network_error_twice_fails(scripted, cfg):
    t = scripted([network_error, network_error])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await gen_image(client)
        assert not result.ok
        assert result.status_code is None
        assert "ConnectError" in result.error_detail
    finally:
        await client.aclose()


async def test_generate_image_timeout_retry(scripted, cfg):
    t = scripted([timeout_error, ok_image()])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await gen_image(client)
        assert result.ok
        assert len(t.requests) == 2
    finally:
        await client.aclose()


async def test_retry_respects_retry_after_header(scripted, cfg):
    t = scripted([err(503, headers={"Retry-After": "1.5"}), ok_image()])
    sleeper = FakeSleeper()
    client = make_client(cfg, t, sleep=sleeper)
    try:
        result = await gen_image(client)
        assert result.ok
        assert sleeper.calls and sleeper.calls[0] >= 1.5  # 尊重 Retry-After
    finally:
        await client.aclose()


async def test_backoff_delay_jitter_bounds():
    for _ in range(200):
        d = agnes.backoff_delay(retry_after=None)
        assert 0.25 <= d <= 1.0


async def test_backoff_delay_retry_after_wins():
    assert agnes.backoff_delay(retry_after=2.0) == 2.0
    assert agnes.backoff_delay(retry_after=0.5) == 0.5


async def test_deadline_exhausted_no_io(scripted, cfg):
    t = scripted([ok_image()])
    client = make_client(cfg, t)
    try:
        d = budget.Deadline(1.0)
        d._start -= 2  # 强制耗尽（绕过等待）
        with pytest.raises(budget.DeadlineExceededError):
            await gen_image(client, deadline=d)
        assert len(t.requests) == 0  # R3：零 IO
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# read 动态（R7.1）：min(档, deadline 剩余)，每次尝试重算
# ---------------------------------------------------------------------------
async def test_read_timeout_image_tier_capped_by_deadline(scripted, cfg):
    t = scripted([ok_image()])
    client = make_client(cfg, t)
    try:
        d = budget.Deadline(200.0)
        await gen_image(client, deadline=d)
        ext = t.requests[0].extensions["timeout"]
        assert ext["connect"] == 5.0 and ext["write"] == 5.0 and ext["pool"] == 5.0  # 三段显式
        assert ext["read"] == pytest.approx(cfg.timeout_agnes_image_sec)  # min(档, 200) = 档（D5：60）
    finally:
        await client.aclose()


async def test_read_timeout_dynamic_deadline_remaining_wins(scripted, cfg):
    t = scripted([ok_image()])
    client = make_client(cfg, t)
    try:
        d = budget.Deadline(10.0)
        await gen_image(client, deadline=d)
        ext = t.requests[0].extensions["timeout"]
        assert ext["read"] == pytest.approx(10.0, abs=0.5)  # min(档, ~10) = 剩余
    finally:
        await client.aclose()


async def test_read_timeout_video_tiers(scripted, cfg):
    """提交档 30 / 单查档 10：两端点 read 各取各档。"""
    t = scripted([submit_ok(), query_ok()])
    client = make_client(cfg, t)
    try:
        d = budget.Deadline(200.0)
        await submit(client, deadline=d)
        ext_submit = t.requests[0].extensions["timeout"]
        assert ext_submit["read"] == pytest.approx(30.0)  # min(30, 200)
        await query(client, deadline=d)
        ext_query = t.requests[1].extensions["timeout"]
        assert ext_query["read"] == pytest.approx(10.0)  # min(10, 200)
    finally:
        await client.aclose()


def test_compute_read_timeout_pure_function(cfg):
    d = budget.Deadline(200.0)
    assert agnes.compute_read_timeout(30.0, d) == pytest.approx(30.0)
    d2 = budget.Deadline(7.0)
    assert agnes.compute_read_timeout(30.0, d2) == pytest.approx(7.0, abs=0.5)
    d3 = budget.Deadline(1.0)
    d3._start -= 5  # 剩余为负 → 0（clamp）
    assert agnes.compute_read_timeout(30.0, d3) == 0.0


# ---------------------------------------------------------------------------
# 连接池与 base_url（R7）
# ---------------------------------------------------------------------------
def test_build_http_limits(cfg):
    limits = agnes.build_http_limits(cfg)
    assert limits.max_connections == 10
    assert limits.max_keepalive_connections == 4


async def test_client_factory_uses_agnes_base_url(cfg):
    created = {}

    def factory(**kwargs):
        created.update(kwargs)
        return httpx.AsyncClient(**kwargs)

    client = agnes.AgnesClient(cfg, client_factory=factory)
    try:
        assert str(created["base_url"]).rstrip("/") == BASE
        limits = created["limits"]
        assert limits.max_connections == 10
        assert limits.max_keepalive_connections == 4
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 视频提交（T3）：成功 + 请求体（E21 seconds int→str）
# ---------------------------------------------------------------------------
async def test_submit_video_success(scripted, cfg):
    t = scripted([submit_ok("vid-123")])
    client = make_client(cfg, t)
    try:
        result = await submit(client)
        assert result.ok and result.status_code == 200
        assert result.video_id == "vid-123"
        assert result.model_id == "agnes-video-2.5-flash"
        r = t.requests[0]
        assert r.method == "POST"
        assert str(r.url) == BASE + "/v1/videos"
        assert r.headers["Authorization"] == "Bearer test-agnes-token"
        body = json.loads(r.content)
        assert body == {
            "model": "agnes-video-2.5-flash",
            "prompt": "a cat",
            "seconds": "5",  # E21：int → str
            "mode": "text",
            "size": "720P",
            "aspect_ratio": "16:9",
        }
    finally:
        await client.aclose()


async def test_submit_video_seconds_int_to_str(scripted, cfg):
    t = scripted([submit_ok()])
    client = make_client(cfg, t)
    try:
        await submit(client, seconds=8)
        assert json.loads(t.requests[0].content)["seconds"] == "8"
    finally:
        await client.aclose()


async def test_submit_video_data_container_video_id(scripted, cfg):
    t = scripted([submit_ok_data_container("vid-data")])
    client = make_client(cfg, t)
    try:
        result = await submit(client)
        assert result.ok and result.video_id == "vid-data"
    finally:
        await client.aclose()


async def test_submit_video_malformed_200(scripted, cfg):
    t = scripted([httpx.Response(200, json={"foo": "bar"}, request=req())])
    client = make_client(cfg, t)
    try:
        result = await submit(client)
        assert not result.ok
        assert "malformed" in result.error_detail
    finally:
        await client.aclose()


async def test_submit_video_image_model_rejected(scripted, cfg):
    client = make_client(cfg, scripted([]))
    try:
        with pytest.raises(ValueError, match="不适用"):
            await submit(client, model="agnes-image")
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_submit_video_4xx_no_retry(scripted, cfg, status):
    t = scripted([err(status)])
    client = make_client(cfg, t)
    try:
        result = await submit(client)
        assert not result.ok and result.status_code == status
        assert len(t.requests) == 1
    finally:
        await client.aclose()


async def test_submit_video_429_no_retry(scripted, cfg):
    t = scripted([err(429, headers={"Retry-After": "3"})])
    client = make_client(cfg, t)
    try:
        result = await submit(client)
        assert not result.ok and result.status_code == 429
        assert len(t.requests) == 1
        assert result.retry_after == 3.0
    finally:
        await client.aclose()


async def test_submit_video_5xx_retry_once(scripted, cfg):
    t = scripted([err(500), submit_ok()])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await submit(client)
        assert result.ok and result.video_id == "vid-123"
        assert len(t.requests) == 2
    finally:
        await client.aclose()


async def test_submit_video_network_error_retry(scripted, cfg):
    t = scripted([network_error, submit_ok()])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await submit(client)
        assert result.ok
        assert len(t.requests) == 2
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 视频查询（T3）
# ---------------------------------------------------------------------------
async def test_query_video_completed_with_url(scripted, cfg):
    t = scripted([query_ok("completed", "https://v.example/1.mp4")])
    client = make_client(cfg, t)
    try:
        result = await query(client, video_id="vid-123")
        assert result.ok and result.status_code == 200
        assert result.video_status == "completed"
        assert result.video_url == "https://v.example/1.mp4"
        r = t.requests[0]
        assert r.method == "GET"
        assert str(r.url) == (
            BASE + "/agnesapi?video_id=vid-123&model_name=agnes-video-2.5-flash"
        )
        assert r.headers["Authorization"] == "Bearer test-agnes-token"
    finally:
        await client.aclose()


async def test_query_video_running_no_url(scripted, cfg):
    t = scripted([query_ok("running")])
    client = make_client(cfg, t)
    try:
        result = await query(client)
        assert result.ok
        assert result.video_status == "running"
        assert result.video_url is None
    finally:
        await client.aclose()


async def test_query_video_failed_status(scripted, cfg):
    """HTTP 200 + status=failed：查询成功，上游任务失败（分类在 pipeline 层）。"""
    t = scripted([query_ok("failed")])
    client = make_client(cfg, t)
    try:
        result = await query(client)
        assert result.ok
        assert result.video_status == "failed"
    finally:
        await client.aclose()


async def test_query_video_data_container(scripted, cfg):
    t = scripted([query_ok_data_container()])
    client = make_client(cfg, t)
    try:
        result = await query(client)
        assert result.ok
        assert result.video_status == "completed"
        assert result.video_url == "https://v.example/1.mp4"
    finally:
        await client.aclose()


async def test_query_video_malformed_200(scripted, cfg):
    t = scripted([httpx.Response(200, json={"foo": "bar"}, request=req())])
    client = make_client(cfg, t)
    try:
        result = await query(client)
        assert not result.ok
        assert "malformed" in result.error_detail
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_query_video_4xx_no_retry(scripted, cfg, status):
    t = scripted([err(status)])
    client = make_client(cfg, t)
    try:
        result = await query(client)
        assert not result.ok and result.status_code == status
        assert len(t.requests) == 1
    finally:
        await client.aclose()


async def test_query_video_5xx_retry_once(scripted, cfg):
    t = scripted([err(500), query_ok("running")])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await query(client)
        assert result.ok and result.video_status == "running"
        assert len(t.requests) == 2
    finally:
        await client.aclose()


async def test_query_video_timeout_retry(scripted, cfg):
    t = scripted([timeout_error, query_ok("running")])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await query(client)
        assert result.ok
        assert len(t.requests) == 2
    finally:
        await client.aclose()


async def test_query_video_image_model_rejected(scripted, cfg):
    client = make_client(cfg, scripted([]))
    try:
        with pytest.raises(ValueError, match="不适用"):
            await query(client, model="agnes-image")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# F3：上游错误体解析
# ---------------------------------------------------------------------------
async def test_error_body_error_message_transported(scripted, cfg):
    t = scripted([err(400, body={"error": {
        "code": "invalid_parameter",
        "message": "size must be one of 1024x1024 (request id: req-42)",
        "type": "AgnesAI_error",
    }})])
    client = make_client(cfg, t)
    try:
        result = await gen_image(client)
        assert not result.ok
        assert "size must be one of 1024x1024" in result.error_detail
        assert "req-42" in result.error_detail  # 请求 id 随 message 透传
    finally:
        await client.aclose()


async def test_error_body_non_json_fallback(scripted, cfg):
    t = scripted([
        httpx.Response(502, text="<html>Bad Gateway</html>", request=req()),
        httpx.Response(502, text="<html>Bad Gateway</html>", request=req()),
    ])
    client = make_client(cfg, t, sleep=FakeSleeper())
    try:
        result = await gen_image(client)
        assert not result.ok
        assert result.error_detail  # 回退响应体前 200 字符，非空即可
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# rotate（R8/E6：每日重建热切换 + 排空）
# ---------------------------------------------------------------------------
async def test_rotate_waits_for_inflight_then_closes_old(gated, cfg):
    client = make_client(cfg, gated, drain_poll_sec=0.01)
    try:
        task = asyncio.create_task(gen_image(client))
        await gated.arrived.wait()
        old = client._current
        rotate_task = asyncio.create_task(client.rotate())
        await asyncio.sleep(0.05)
        assert not rotate_task.done()  # 在途未完 → 排空中
        assert client._current is not old  # 指针已热切换
        gated.next_response = ok_image()
        gated.gate.set()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.ok
        await asyncio.wait_for(rotate_task, timeout=2.0)
        assert old.client.is_closed  # 排空完成后关闭旧 httpx client
    finally:
        await client.aclose()


async def test_rotate_without_inflight_closes_old_quickly(scripted, cfg):
    t = scripted([])
    client = make_client(cfg, t, drain_poll_sec=0.01)
    old = client._current
    await asyncio.wait_for(client.rotate(), timeout=2.0)
    assert old.client.is_closed
    assert client._current is not old
    await client.aclose()


async def test_new_requests_after_rotate_use_new_client(scripted, cfg):
    t = scripted([])
    created = []

    def factory(**kwargs):
        c = httpx.AsyncClient(transport=t, **kwargs)
        created.append(c)
        return c

    client = agnes.AgnesClient(cfg, client_factory=factory, drain_poll_sec=0.01)
    try:
        await asyncio.wait_for(client.rotate(), timeout=2.0)
        # 轮询等待旧 client 关闭（后台排空任务）
        for _ in range(200):
            if created[0].is_closed:
                break
            await asyncio.sleep(0.01)
        assert len(created) == 2
        result = await gen_image(client)
        assert result.ok
        assert not created[1].is_closed  # 新 client 继续服务
    finally:
        await client.aclose()


async def test_in_flight_counter_tracks_calls(scripted, cfg):
    t = scripted([submit_ok(), query_ok("completed", "https://v.example/1.mp4")])
    client = make_client(cfg, t)
    try:
        task = asyncio.create_task(submit(client))
        # submit 完成后 submit_video 计数已归还；直接顺序断言即可
        await asyncio.wait_for(task, timeout=2.0)
        assert client.in_flight == 0
        await asyncio.wait_for(query(client), timeout=2.0)
        assert client.in_flight == 0
    finally:
        await client.aclose()


async def test_in_flight_counter_tracks_generate(gated, cfg):
    client = make_client(cfg, gated, drain_poll_sec=0.01)
    try:
        task = asyncio.create_task(gen_image(client))
        await gated.arrived.wait()
        assert client.in_flight == 1
        gated.next_response = ok_image()
        gated.gate.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert client.in_flight == 0
    finally:
        await client.aclose()
