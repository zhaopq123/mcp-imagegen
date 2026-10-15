# -*- coding: utf-8 -*-
"""上游自动装配（providers.autodetect）测试：全离线，不触网。

覆盖：候选路径顺序、404 跳过、401/400 视为「路径存在」、
从响应反推抽取路径、模型列表解析、失败时如实返回 tried。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from providers import autodetect, schema


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeClient:
    """按 path 返回预设响应；记录所有请求的 url 供断言。

    路径**精确匹配**（真机 HTTP 服务就是这么匹配的）。曾用 `endswith`
    宽松匹配，结果 `/openai/v1/images/generations` 会命中
    `/v1/images/generations` 的预设，把「恰好一个候选繁忙」的真实场景
    伪造成「多个候选繁忙」，掩盖了降级装配分支。
    """

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    def _pick(self, url):
        path = url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
        path = path.split("?", 1)[0]
        resp = self.routes.get(path)
        return resp() if callable(resp) else resp if resp is not None else FakeResp(
            404, {"error": "not found"})

    async def post(self, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "body": json,
                           "headers": headers or {}, "params": params or {}})
        return self._pick(url)

    async def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url,
                           "headers": headers or {}, "params": params or {}})
        return self._pick(url)


def _spec(base="https://api.example.com", token="sk-test", kind="bearer"):
    return schema.ProviderConfig(
        id="t", base_url=base,
        auth=schema.AuthSpec(kind=kind, token=token, name="Authorization",
                             prefix="Bearer "),
    )


@pytest.mark.asyncio
async def test_autodetect_picks_first_working_path():
    """第一个候选路径 200 就用它，不继续试后面的。"""
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "https://cdn.example/a.png"}]})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "gpt-image-1")
    assert res["ok"] is True
    cap = res["capability"]
    assert cap["request"]["path"] == "/v1/images/generations"
    assert cap["extract"]["urls"] == "data[].url"
    #只试了 1 个路径就命中
    assert len(c.calls) == 1
    # 模型名放在cap.model（用户可改），body 里必须是占位符
    assert cap["model"] == "gpt-image-1"
    assert cap["request"]["body"]["model"] == "{model}"


@pytest.mark.asyncio
async def test_autodetect_never_leaks_probe_prompt_into_template():
    """防回归：探测用的真实 prompt/size 不能被写进请求体模板。

    否则用户保存后每次生图都返回同一张探测图（a red apple）。
    """
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "u"}]})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    body = res["capability"]["request"]["body"]
    blob = json.dumps(body, ensure_ascii=False)
    assert "a red apple" not in blob, "探测文案泄漏进模板"
    assert "256x256" not in blob, "探测尺寸泄漏进模板"
    assert body["prompt"] == "{prompt}"


@pytest.mark.asyncio
async def test_autodetect_skips_404_then_uses_second():
    """404 视为路径不存在 → 跳到下一个候选。"""
    c = FakeClient({
        "/v1/image/generations": FakeResp(
            200, {"images": [{"url": "https://cdn/b.png"}]}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    assert res["ok"] is True
    assert res["capability"]["request"]["path"] == "/v1/image/generations"
    assert res["capability"]["extract"]["urls"] == "images[].url"
    # 至少试了 2 次
    assert len(c.calls) >= 2


@pytest.mark.asyncio
async def test_autodetect_401_counts_as_path_exists():
    """401 视为「路径存在只是没权限」—— 不应跳过后面的路径。"""
    c = FakeClient({"/v1/images/generations": FakeResp(401, {"error": "no key"})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    # 全部候选都 404/401 类，最后没有 2xx → 失败但tried 里有明细
    assert res["ok"] is False
    assert res["tried"], "失败时必须给出已试清单"
    assert any(t["path"] == "/v1/images/generations" for t in res["tried"])


@pytest.mark.asyncio
async def test_autodetect_sends_auth_header():
    """bearer 鉴权要真的带上Authorization 头。"""
    c = FakeClient({"/v1/images/generations": FakeResp(200, {"data": [{"url": "u"}]})})
    await autodetect.autodetect(c, _spec(), "sk-mykey", "image", "m")
    hdr = c.calls[0]["headers"]
    assert hdr.get("Authorization") == "Bearer sk-mykey"


@pytest.mark.asyncio
async def test_autodetect_header_auth_mode():
    """header 模式用指定头名（非 Authorization）。"""
    sp = schema.ProviderConfig(
        id="t", base_url="https://api.example.com",
        auth=schema.AuthSpec(kind="header", token="k", name="X-API-Key",
                             prefix=""))
    c = FakeClient({"/v1/images/generations": FakeResp(200, {"data": [{"url": "u"}]})})
    await autodetect.autodetect(c, sp, "k", "image", "m")
    assert c.calls[0]["headers"].get("X-API-Key") == "k"


@pytest.mark.asyncio
async def test_list_models_openai_shape():
    c = FakeClient({"/v1/models": FakeResp(
        200, {"data": [{"id": "gpt-image-1"}, {"id": "dall-e-3"}]})})
    res = await autodetect.list_models(c, _spec(), "sk-test")
    assert res["ok"] is True
    assert res["models"] == ["gpt-image-1", "dall-e-3"]


@pytest.mark.asyncio
async def test_list_models_alt_shape_and_failure():
    c = FakeClient({"/models": FakeResp(200, {"models": ["a", "b"]})})
    res = await autodetect.list_models(c, _spec(), "sk")
    assert res["ok"] is True and res["models"] == ["a", "b"]

    c2 = FakeClient({})  # 全404
    res2 = await autodetect.list_models(c2, _spec(), "sk")
    assert res2["ok"] is False and res2["models"] == []


def test_derive_extract_handles_b64():
    body = {"data": [{"b64_json": "iVBORw0KGgo="}]}
    ex = autodetect._derive_extract(body)
    assert ex.urls == "data[].b64_json"
    assert ex.b64 is True


def test_path_exists_rules():
    assert autodetect._path_exists(200, {}) is True
    assert autodetect._path_exists(404, {"e": "x"}) is False
    assert autodetect._path_exists(401, {}) is True
    assert autodetect._path_exists(405, {}) is True
    assert autodetect._path_exists(400, {"error": "size required"}) is True
    # HTML 报错页不算路径存在
    assert autodetect._path_exists(500, "<html>oops</html>") is False


def test_no_vendor_constants_in_source():
    """铁律：探测候选里不能出现任何具体厂商域名/专属路径常量。"""
    src = Path(autodetect.__file__).read_text(encoding="utf-8")
    for bad in ("siliconflow", "agnes", "openai.azure", "api.deepseek",
                "dashscope", "volces", "ark.cn", "aliyuncs"):
        assert bad not in src.lower(), f"探测模块不应硬编码厂商：{bad}"


def test_split_endpoint_full_url():
    """用户粘完整端点 URL 时，应拆成 base_url + path。

    用户就是按「完整接口地址」给的（如
    https://api.agnes-ai.cn/v1/images/generations），不是只给域名。
    """
    b, p = autodetect.split_endpoint("https://api.agnes-ai.cn/v1/images/generations")
    assert b == "https://api.agnes-ai.cn"
    assert p == "/v1/images/generations"

    # 末尾斜杠
    assert autodetect.split_endpoint("https://a.cn/v1/images/generations/") == \
        ("https://a.cn", "/v1/images/generations")
    # 带查询串
    assert autodetect.split_endpoint("https://a.cn/v1/images/generations?v=2") == \
        ("https://a.cn", "/v1/images/generations")
    # 只给域名 → path 空（交给候选探测）
    assert autodetect.split_endpoint("https://a.cn") == ("https://a.cn", "")
    assert autodetect.split_endpoint("https://a.cn/") == ("https://a.cn", "")
    # http 也认
    assert autodetect.split_endpoint("http://a.cn/v1/videos")[1] == "/v1/videos"
    # 空
    assert autodetect.split_endpoint("") == ("", "")


@pytest.mark.asyncio
async def test_explicit_path_tried_first_and_only():
    """用户给了完整 path 时，只试这一个（不再挨个试候选）。"""
    sp = _spec(base="https://api.agnes-ai.cn")
    setattr(sp, "_explicit_path", "/v1/images/generations")
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "https://cdn/x.png"}]})})
    res = await autodetect.autodetect(c, sp, "sk-test", "image", "m")
    assert res["ok"] is True
    cap = res["capability"]
    assert cap["request"]["path"] == "/v1/images/generations"
    # 只发了一次请求 —— 没去试其他候选
    assert len(c.calls) == 1
    assert res["tried"][0].get("source") == "用户填写"


@pytest.mark.asyncio
async def test_explicit_path_falls_back_to_candidates():
    """用户给的 path 不通时，回退候选清单（不能直接失败）。"""
    sp = _spec(base="https://api.agnes-ai.cn")
    setattr(sp, "_explicit_path", "/wrong/path")
    c = FakeClient({"/v1/images/generations": FakeResp(
        200, {"data": [{"url": "https://cdn/x.png"}]})})
    res = await autodetect.autodetect(c, sp, "sk-test", "image", "m")
    assert res["ok"] is True
    assert res["capability"]["request"]["path"] == "/v1/images/generations"
    # 第一条是用户填的（失败），后面是候选
    assert res["tried"][0]["path"] == "/wrong/path"


# ---------------------------------------------------------------------------
# V5：上游繁忙（503/429）时的**降级**装配
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_busy_upstream_degrades_instead_of_failing():
    """唯一一个候选回 503 → 路径几乎可以断定是对的，降级装配并标 degraded。

    真机场景（2026-10-15）：视频档持续拥堵，`POST /v1/videos` 一路 503，
    旧逻辑「非 2xx 就整体失败」会让用户"只给模型名+API+Key"永远接不上。
    """
    c = FakeClient({"/v1/videos": FakeResp(
        503, {"code": "video_queue_full", "message": "视频队列已满，请稍后重试"})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    assert res["ok"] is True
    assert res["degraded"] is True
    assert res["capability"]["request"]["path"] == "/v1/videos"
    assert "没有拿到成功响应" in res["detail"]   # 如实说明未验证
    assert res["capability"]["request"]["body"]["prompt"] == "{prompt}"


@pytest.mark.parametrize("status", [429, 503])
async def test_busy_marks_degraded_for_both_transient_codes(status):
    c = FakeClient({"/v1/images/generations": FakeResp(
        status, {"message": "busy"})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    assert res["ok"] is True and res["degraded"] is True


@pytest.mark.asyncio
async def test_two_busy_candidates_do_not_adopt_anything():
    """两个候选都"忙" → 疑似整站/网关故障，路径无法判别 → 不装配。"""
    c = FakeClient({
        "/v1/images/generations": FakeResp(503, {"message": "busy"}),
        "/v1/images": FakeResp(503, {"message": "busy"}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "image", "m")
    assert res["ok"] is False
    assert "多个候选" in res["detail"]


@pytest.mark.asyncio
async def test_explicit_busy_path_not_double_counted():
    """用户填的完整端点 URL 就是候选里的第一条 —— 不能探两次。

    探两次会让 transient 里出现两条（同一条路径），len==2 就被误判成
    "多个候选都忙"而放弃降级装配。
    """
    sp = _spec(base="https://api.agnes-ai.cn")
    setattr(sp, "_explicit_path", "/v1/videos")
    c = FakeClient({"/v1/videos": FakeResp(503, {"message": "busy"})})
    res = await autodetect.autodetect(c, sp, "sk-test", "video", "m")
    assert res["ok"] is True and res["degraded"] is True
    assert res["capability"]["request"]["path"] == "/v1/videos"
    paths = [t["path"] for t in res["tried"]]
    assert paths.count("/v1/videos") == 1, paths


# ---------------------------------------------------------------------------
# V5：视频轮询端点探测（必须拿真实 task_id）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_video_poll_discovered_with_real_task_id():
    """提交成功拿到真任务号后，逐个试候选轮询形状，命中就采用。"""
    c = FakeClient({
        "/v1/videos": FakeResp(200, {"video_id": "up-1", "status": "queued"}),
        "/v1/videos/up-1": FakeResp(200, {"status": "completed",
                                          "url": "https://cdn/v.mp4"}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    assert res["ok"] is True and res.get("degraded") is not True
    cap = res["capability"]
    assert cap["extract"]["task_id"] == "video_id"
    assert cap["poll"]["path"] == "/v1/videos/{task_id}"
    assert cap["poll_extract"]["status"] == "status"
    assert cap["poll_extract"]["url"] == "url"
    # 占位符不能被真实探测值污染
    blob = json.dumps(cap, ensure_ascii=False)
    assert "up-1" not in blob, "真实 task_id 泄漏进了配置"


@pytest.mark.asyncio
async def test_video_poll_query_style_candidate():
    """查询式轮询端点也必须能探到（path + query 模板，占位符保留）。"""
    c = FakeClient({
        "/v1/videos": FakeResp(200, {"task_id": "up-2", "status": "queued"}),
        "/v1/videos/query": FakeResp(200, {"status": "running", "progress": 10}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    cap = res["capability"]
    assert cap["poll"]["path"] == "/v1/videos/query"
    assert cap["poll"]["query"] == {"task_id": "{task_id}", "model": "{model}"}
    assert cap["poll_extract"]["status"] == "status"
    assert "up-2" not in json.dumps(cap, ensure_ascii=False)


@pytest.mark.asyncio
async def test_video_poll_not_guessed_when_undiscoverable():
    """探不到轮询端点 → **留空**并明说要手工填，绝不静默配一个错的。"""
    c = FakeClient({
        "/v1/videos": FakeResp(200, {"video_id": "up-3", "status": "queued"}),
        # 查询接口全都是"任务不存在"（真机实测：路径式与查询式会同时存在）
        "/v1/videos/query": FakeResp(400, {"code": "task_not_exist"}),
    })
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    cap = res["capability"]
    assert cap.get("poll") is None
    assert cap["extract"]["task_id"] == "video_id"   # 任务号路径仍然保留
    assert "手工" in res["detail"]


@pytest.mark.asyncio
async def test_video_poll_no_task_id_in_response():
    """提交响应里认不出任务号 → 同样留空 + 提示手工填。"""
    c = FakeClient({"/v1/videos": FakeResp(200, {"ok": True})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    cap = res["capability"]
    assert cap["extract"]["task_id"] == ""
    assert cap.get("poll") is None
    assert "手工" in res["detail"]


@pytest.mark.asyncio
async def test_degraded_video_has_no_guessed_poll():
    """降级装配（上游繁忙，没真任务号）时不许猜轮询端点。"""
    c = FakeClient({"/v1/videos": FakeResp(503, {"message": "busy"})})
    res = await autodetect.autodetect(c, _spec(), "sk-test", "video", "m")
    assert res["degraded"] is True
    cap = res["capability"]
    assert cap.get("poll") is None
    assert "手工" in res["detail"]


# ---------------------------------------------------------------------------
# 纯函数：字段路径 / 轮询抽取路径反推
# ---------------------------------------------------------------------------
def test_field_path_top_level_and_container():
    assert autodetect._field_path({"video_id": "v"}, autodetect._ID_KEYS) == "video_id"
    assert autodetect._field_path(
        {"data": {"task_id": "t"}}, autodetect._ID_KEYS) == "data.task_id"
    assert autodetect._field_path({"nope": 1}, autodetect._ID_KEYS) == ""
    assert autodetect._field_path([], autodetect._ID_KEYS) == ""


def test_derive_poll_extract_shapes():
    ex = autodetect._derive_poll_extract({"status": "completed", "url": "u"})
    assert (ex.status, ex.url) == ("status", "url")
    # v2.0 形态：URL 埋在 metadata 里
    ex2 = autodetect._derive_poll_extract(
        {"status": "completed", "metadata": {"url": "u"}})
    assert (ex2.status, ex2.url) == ("status", "metadata.url")
    # 只有 status（URL 留给 gateway 在 completed 时 guess_url 兜底）
    ex3 = autodetect._derive_poll_extract({"state": "running"})
    assert (ex3.status, ex3.url) == ("state", "")


def test_looks_like_poll_body_filters_non_poll_pages():
    assert autodetect._looks_like_poll_body({"status": "completed"}) is True
    assert autodetect._looks_like_poll_body({"status": "queued"}) is True
    assert autodetect._looks_like_poll_body({"state": "processing"}) is True
    # 没有状态字段 / 状态值认不出 → 不算轮询端点
    assert autodetect._looks_like_poll_body({"url": "u"}) is False
    assert autodetect._looks_like_poll_body({"status": "totally-unknown"}) is False
    assert autodetect._looks_like_poll_body("<html>hi</html>") is False


def test_is_transient_only_busy_codes():
    assert autodetect._is_transient(503) is True
    assert autodetect._is_transient(429) is True
    for code in (200, 400, 401, 403, 404, 422, 500):
        assert autodetect._is_transient(code) is False


def test_path_exists_accepts_busy_json():
    """503/429 是「路径存在 + 上游忙」，HTML 报错页仍不算。"""
    assert autodetect._path_exists(503, {"code": "video_queue_full"}) is True
    assert autodetect._path_exists(429, {"message": "too many"}) is True
    assert autodetect._path_exists(503, "<html>maintenance</html>") is False


def test_poll_candidates_have_no_vendor_paths():
    """轮询候选同样受零厂商常量约束（`/agnesapi` 这种专属路径绝不能出现）。"""
    blob = json.dumps(autodetect._VIDEO_POLL_CANDIDATES, ensure_ascii=False).lower()
    for bad in ("agnes", "agnesapi", "siliconflow", "dashscope", "volces",
                "aliyuncs", "ark.cn"):
        assert bad not in blob, f"轮询候选不应硬编码厂商路径：{bad}"
