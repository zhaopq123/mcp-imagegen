"""T7c 收尾补测：视频 persist 三分支（成功 / 下载失败 / 预算不足；全部 mock）。

覆盖（第四轮派发单第 1 项；C6/R5/R3/Q3/E13/R13 联动）：
- 成功：/files 完整 URL + meta persisted + 文件落 VIDEO_DIR（IMAGE_DIR 零落盘）
  + 字节一致 + 扩展名推断（Content-Type 优先 / URL 后缀回退）+ Host 与
  X-Forwarded-* 头透传进 URL 构造（Q3）+ 下载请求确发上游 URL；
- 下载失败（HTTP 500 / 网络异常 / 读超时 / 空内容四形态）：降级返原上游
  URL + meta persist_failed，输出整体非 error 行（R5：额度已花，转存失败
  绝不把请求变 [error]），VIDEO_DIR 零落盘，下载确有尝试——并由此验证
  persist_video 永不报错（C6）；
- 预算不足：轮询期 FakeClock 烧穿 200s 总预算 → 不发起任何下载 IO（R3），
  同降级（原 URL + persist_failed）；
- 直接级 C6：耗尽 deadline / 断网 transport 下调用 persist_video 仍正常
  返回二元组，绝不抛异常。

logline 联动（T7c 字段）：三分支各断言恰好一行访问日志 result=ok +
persist=<状态> + task_id=<video_id> + poll_count=1 + split_calls=null。

铁律：标记前缀一律 chr(91) 拼接构造 + ord 逐码验证；源码零字面量前缀。
全部 mock（FakeVideoApi / ScriptedTransport），FakeClock 控时，禁真实 API；
轮询零真实等待（AdvancingSleeper / BudgetBurnSleeper 推进假时钟）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import FakeClock, make_config

import infra.persist as persist_mod
from app import contract as contract_mod
from app.video_pipeline import VideoPipeline
from infra import agnes as agnes_mod
from infra import budget as budget_mod
from infra import tasks as tasks_mod

# 铁律：标记前缀一律 chr(91)（左方括号）拼接构造，禁止手打字面量
META_PREFIX = chr(91) + "meta] "
ERROR_PREFIX = chr(91) + "error] "
ORD_META = [91, 109, 101, 116, 97, 93, 32]

UPSTREAM_MP4 = "https://up.example/v/clip.mp4"
UPSTREAM_WEBM = "https://up.example/v/clip.webm"


# ---------------------------------------------------------------------------
# 工具：结果工厂 / 假客户端 / 假 sleep
# ---------------------------------------------------------------------------
def submit_ok(video_id: str = "vid-p") -> agnes_mod.ApiCallResult:
    return agnes_mod.ApiCallResult(
        ok=True, status_code=200, model_id="agnes-video-2.5-flash",
        video_id=video_id)


def query_completed(url: str) -> agnes_mod.ApiCallResult:
    return agnes_mod.ApiCallResult(
        ok=True, status_code=200, model_id="agnes-video-2.5-flash",
        video_status="completed", video_url=url)


class FakeVideoApi:
    """AgnesClient 假替身：一次提交 + 脚本化单查（本轮无需占槽记录）。"""

    def __init__(self, submit_result=None, query_script=None) -> None:
        self.submit_result = submit_result or submit_ok()
        self.query_script = list(query_script or [])
        self.submit_calls: list[dict] = []
        self.query_calls: list[dict] = []

    async def submit_video(self, *, model, prompt, seconds, aspect_ratio,
                           deadline, seed=None):
        self.submit_calls.append({
            "model": model, "prompt": prompt, "seconds": seconds,
            "aspect_ratio": aspect_ratio, "deadline": deadline, "seed": seed,
        })
        return self.submit_result

    async def query_video(self, *, video_id, model, deadline):
        self.query_calls.append({
            "video_id": video_id, "model": model, "deadline": deadline})
        return self.query_script.pop(0)


class AdvancingSleeper:
    """轮询 sleep 假身：推进 FakeClock 并记录间隔（免真实等待）。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)
        self.clock.advance(sec)


class BudgetBurnSleeper:
    """轮询 sleep 假身：每跳大步推进 FakeClock（烧穿总预算，免真实等待）。"""

    def __init__(self, clock: FakeClock, step: float) -> None:
        self.clock = clock
        self.step = step
        self.calls: list[float] = []

    async def __call__(self, sec: float) -> None:
        self.calls.append(sec)
        self.clock.advance(self.step)


@asynccontextmanager
async def persist_env(tmp_path, *, transport_script=(), query_script=None,
                      headers=None, sleep_factory=None):
    """装配真实 PersistService（下载层为 ScriptedTransport）+ VideoPipeline。

    IMAGE_DIR/VIDEO_DIR 落 tmp_path 隔离目录；退出时 aclose 下载 client。
    sleep_factory(clock)：轮询 sleep 假身工厂（缺省 AdvancingSleeper）。
    """
    image_dir = tmp_path / "images"
    video_dir = tmp_path / "videos"
    cfg = make_config(IMAGE_DIR=str(image_dir), VIDEO_DIR=str(video_dir))
    clock = FakeClock()
    table = tasks_mod.TaskTable(
        ttl_sec=cfg.task_ttl_sec, max_entries=cfg.task_max, clock=clock)
    pool = budget_mod.SlotPool(cfg.max_concurrency, cfg.queue_wait_sec)
    api = FakeVideoApi(query_script=query_script)
    transport = persist_env_transport(transport_script)
    svc = persist_mod.PersistService(cfg, transport=transport)
    await svc.start()
    sleep = sleep_factory(clock) if sleep_factory else AdvancingSleeper(clock)
    vp = VideoPipeline(
        cfg, pool, api, table, clock=clock, sleep=sleep, persist=svc)
    try:
        yield SimpleNamespace(
            vp=vp, pool=pool, table=table, clock=clock, api=api, cfg=cfg,
            svc=svc, transport=transport, sleep=sleep,
            image_dir=image_dir, video_dir=video_dir, headers=headers)
    finally:
        await svc.aclose()


def persist_env_transport(script):
    from conftest import ScriptedTransport
    return ScriptedTransport(list(script))


async def run_persist(ns, **kw):
    """便捷调用：默认合法参数 + persist=true。"""
    params = dict(prompt="a cat", seconds=5, aspect_ratio="16:9",
                  persist_flag=True, http_headers=ns.headers)
    params.update(kw)
    return await ns.vp.run(**params)


def access_records(caplog):
    return [r for r in caplog.records if r.name == "mcp_imagegen.access"]


# ---------------------------------------------------------------------------
# 铁律专项：标记前缀 ord 逐码验证 + 源码零字面量
# ---------------------------------------------------------------------------
def test_meta_prefix_ord_verified():
    """前 7 字符 ord 序列 91,109,101,116,97,93,32（防渲染层吞字）。"""
    assert [ord(c) for c in META_PREFIX] == ORD_META
    assert [ord(c) for c in contract_mod.META_PREFIX[:7]] == ORD_META
    assert [ord(c) for c in ERROR_PREFIX[:2]] == [91, 101]


def test_source_no_literal_marker_prefix():
    """persist.py / endpoints.py 源码零字面量前缀序列（一律 chr(91) 拼接）。"""
    root = Path(__file__).resolve().parents[1]
    for rel in ("infra/persist.py", "app/endpoints.py"):
        src = (root / rel).read_bytes()
        assert bytes([91, 109]) not in src, f"{rel} 出现字面量左方括号+m（铁律违例）"


# ---------------------------------------------------------------------------
# 分支一：成功 → /files URL + meta persisted
# ---------------------------------------------------------------------------
async def test_success_mp4_content_type(tmp_path, caplog):
    payload = b"\x00\x00\x00\x18ftypmp42" + b"fake-mp4-payload" * 32
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        async with persist_env(
            tmp_path,
            transport_script=[
                httpx.Response(200, content=payload,
                               headers={"content-type": "video/mp4"})],
            query_script=[query_completed(UPSTREAM_MP4)],
            headers={"host": "files.example:8000"},
        ) as ns:
            out = await run_persist(ns)

    lines = out.split("\n")
    # URL 行：/files 完整地址（Q3：Host 头推断），扩展名 .mp4
    assert lines[0].startswith("http://files.example:8000/files/")
    assert lines[0].endswith(".mp4")
    name = lines[0].rsplit("/files/", 1)[1]
    # 落盘对账：VIDEO_DIR 收到字节一致的文件；IMAGE_DIR 零落盘
    assert (ns.video_dir / name).read_bytes() == payload
    assert list(ns.image_dir.iterdir()) == []
    # meta 行：persisted（非 url_expires / 非 persist_failed）
    meta_line = lines[1]
    assert [ord(c) for c in meta_line[:7]] == ORD_META
    assert "model=agnes-video" in meta_line
    assert "seconds=5" in meta_line
    assert "aspect_ratio=16:9" in meta_line
    assert "persisted" in meta_line
    assert "url_expires" not in meta_line
    assert "persist_failed" not in meta_line
    # 下载请求确发上游 URL
    assert len(ns.transport.requests) == 1
    assert str(ns.transport.requests[0].url) == UPSTREAM_MP4
    # logline（T7c 字段 + R13 恰好一行）
    records = access_records(caplog)
    assert len(records) == 1
    rendered = records[0].getMessage()
    assert "result=ok" in rendered
    assert "persist=persisted" in rendered
    assert "task_id=vid-p" in rendered
    assert "poll_count=1" in rendered
    assert "split_calls=null" in rendered


async def test_success_webm_ext_from_url_suffix(tmp_path):
    """无 Content-Type → 扩展名按上游 URL 后缀推断（.webm）。"""
    payload = b"\x1a\x45\xdf\xa3webm-fake-body" * 16
    async with persist_env(
        tmp_path,
        transport_script=[httpx.Response(200, content=payload)],
        query_script=[query_completed(UPSTREAM_WEBM)],
        headers={"host": "files.example:8000"},
    ) as ns:
        out = await run_persist(ns)

    lines = out.split("\n")
    assert lines[0].startswith("http://files.example:8000/files/")
    assert lines[0].endswith(".webm")
    name = lines[0].rsplit("/files/", 1)[1]
    assert (ns.video_dir / name).read_bytes() == payload
    assert "persisted" in lines[1]


async def test_success_url_uses_forwarded_headers(tmp_path):
    """Q3：X-Forwarded-Proto / X-Forwarded-Host 优先于 Host。"""
    async with persist_env(
        tmp_path,
        transport_script=[
            httpx.Response(200, content=b"vd",
                           headers={"content-type": "video/mp4"})],
        query_script=[query_completed(UPSTREAM_MP4)],
        headers={"host": "origin.internal:8000",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "cdn.example"},
    ) as ns:
        out = await run_persist(ns)

    lines = out.split("\n")
    assert lines[0].startswith("https://cdn.example/files/")
    assert lines[0].endswith(".mp4")


# ---------------------------------------------------------------------------
# 分支二：下载失败 → 降级原 URL + persist_failed（C6 永不报错）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", ["http-500", "connect-error",
                                   "read-timeout", "empty-body"])
async def test_download_failure_degrades_to_original_url(tmp_path, caplog,
                                                         label):
    """四失败形态同降级：原上游 URL + persist_failed，非 error 行（R5/C6）。"""
    if label == "http-500":
        failure: object = httpx.Response(500, text="server exploded")
    elif label == "connect-error":
        failure = httpx.ConnectError("connection refused")
    elif label == "read-timeout":
        failure = httpx.ReadTimeout("read timed out")
    else:
        failure = httpx.Response(200, content=b"")

    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        async with persist_env(
            tmp_path,
            transport_script=[failure],
            query_script=[query_completed(UPSTREAM_MP4)],
            headers={"host": "files.example:8000"},
        ) as ns:
            out = await run_persist(ns)  # C6：整条 run 不因转存失败报错

    lines = out.split("\n")
    assert lines[0] == UPSTREAM_MP4                    # 原上游 URL
    assert [ord(c) for c in lines[1][:7]] == ORD_META
    assert "persist_failed" in lines[1]                # meta 状态
    assert "persisted" not in lines[1]
    assert "url_expires" not in lines[1]
    assert not out.startswith(ERROR_PREFIX)            # R5：非 error 行
    assert list(ns.video_dir.iterdir()) == []          # 零落盘
    assert len(ns.transport.requests) == 1             # 下载确有尝试
    # logline：仍 result=ok（降级不改变成功语义）+ persist=persist_failed
    records = access_records(caplog)
    assert len(records) == 1
    rendered = records[0].getMessage()
    assert "result=ok" in rendered
    assert "persist=persist_failed" in rendered


# ---------------------------------------------------------------------------
# 分支三：预算不足 → 不发起下载 IO（R3），同降级
# ---------------------------------------------------------------------------
async def test_budget_exhausted_skips_download_io(tmp_path, caplog):
    with caplog.at_level("INFO", logger="mcp_imagegen.access"):
        async with persist_env(
            tmp_path,
            transport_script=[],              # 任何下载尝试都会被记录
            query_script=[query_completed(UPSTREAM_MP4)],
            headers={"host": "files.example:8000"},
            sleep_factory=lambda clock: BudgetBurnSleeper(clock, step=205.0),
        ) as ns:                               # 每跳烧 205s > 200s 总预算
            out = await run_persist(ns)

    lines = out.split("\n")
    assert lines[0] == UPSTREAM_MP4                    # 原上游 URL
    assert [ord(c) for c in lines[1][:7]] == ORD_META
    assert "persist_failed" in lines[1]
    assert not out.startswith(ERROR_PREFIX)
    assert ns.transport.requests == []                # R3：未发起任何下载 IO
    assert list(ns.video_dir.iterdir()) == []
    assert ns.sleep.calls == [ns.cfg.video_poll_interval_sec]  # 恰一次轮询间隔
    records = access_records(caplog)
    assert len(records) == 1
    assert "persist=persist_failed" in records[0].getMessage()


# ---------------------------------------------------------------------------
# 直接级 C6：persist_video 单独调用永不抛异常
# ---------------------------------------------------------------------------
async def test_persist_video_never_raises_on_exhausted_deadline(tmp_path):
    """deadline 已耗尽 → 不做任何 IO，直接返回 (原 URL, persist_failed)。"""
    async with persist_env(tmp_path) as ns:
        deadline = budget_mod.Deadline(1.0, clock=ns.clock)
        ns.clock.advance(2.0)  # 烧穿
        result = await ns.svc.persist_video(
            UPSTREAM_MP4, deadline=deadline, req_id="req-direct-1")
        assert result == (UPSTREAM_MP4, "persist_failed")
        assert ns.transport.requests == []            # R3：零下载 IO


async def test_persist_video_never_raises_on_network_error(tmp_path):
    """下载层抛网络异常 → 就地吞掉，返回二元组（C6），不向调用方抛。"""
    async with persist_env(
        tmp_path,
        transport_script=[httpx.ConnectError("connection refused")],
    ) as ns:
        deadline = budget_mod.Deadline(10.0, clock=ns.clock)
        result = await ns.svc.persist_video(
            UPSTREAM_MP4, deadline=deadline, req_id="req-direct-2",
            headers={"host": "files.example:8000"})
        assert result == (UPSTREAM_MP4, "persist_failed")
        assert len(ns.transport.requests) == 1
