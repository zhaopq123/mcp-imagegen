"""T4 门禁：任务表 TTL / 容量逐出 / 清理协程（R16 三分支，全 mock 时钟）。

R16 契约：TTL 过期与容量逐出（含未完成）统一表现为 task_not_found——
    get / update 命中不了就是 None，不区分原因（调用方 T7 视频流水线
    统一翻译为 task_not_found 错误）。

TTL 边界语义（锁死）：elapsed ≥ TTL 即过期（含恰好等于——宁早逐出）。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from infra import tasks


def make_table(clock, **kw):
    defaults = dict(ttl_sec=7200.0, max_entries=100)
    defaults.update(kw)
    return tasks.TaskTable(clock=clock, **defaults)


def reg(table, task_id, **kw):
    return table.register(task_id, model=kw.get("model", "agnes-video"),
                          prompt_hash=kw.get("prompt_hash", "h-" + task_id))


# ---------------------------------------------------------------------------
# register / get / update 基础
# ---------------------------------------------------------------------------
def test_register_and_get_hit(clock):
    table = make_table(clock)
    reg(table, "t1")
    entry = table.get("t1")
    assert entry is not None
    assert entry.task_id == "t1"
    assert entry.model == "agnes-video"
    assert entry.prompt_hash == "h-t1"
    assert entry.status == tasks.STATUS_RUNNING
    assert entry.url is None
    assert entry.submitted_at == clock.now


def test_get_miss_returns_none(clock):
    table = make_table(clock)
    assert table.get("nope") is None


def test_register_overwrite_same_id_refreshes(clock):
    """同 task_id 重新登记 → 覆盖（submitted_at 刷新，容量不增长）。"""
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(100)
    reg(table, "t1", prompt_hash="h-new")
    assert len(table) == 1
    entry = table.get("t1")
    assert entry.prompt_hash == "h-new"
    assert entry.submitted_at == 100.0


def test_update_writes_status_and_url(clock):
    table = make_table(clock)
    reg(table, "t1")
    updated = table.update("t1", status=tasks.STATUS_COMPLETED, url="https://v/1.mp4")
    assert updated is not None
    assert updated.status == tasks.STATUS_COMPLETED
    assert updated.url == "https://v/1.mp4"
    again = table.get("t1")
    assert again.status == tasks.STATUS_COMPLETED


def test_update_miss_returns_none(clock):
    table = make_table(clock)
    assert table.update("nope", status=tasks.STATUS_COMPLETED) is None


def test_update_expired_treated_as_miss(clock):
    """过期条目上 update：视同未命中（不复活已过期任务）。"""
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7200)  # 恰好到期
    assert table.update("t1", status=tasks.STATUS_COMPLETED) is None
    assert table.get("t1") is None


# ---------------------------------------------------------------------------
# R16 分支一：TTL 过期（2h±ε 边界）
# ---------------------------------------------------------------------------
def test_ttl_not_expired_within_window(clock):
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7199.999)  # 2h - ε
    assert table.get("t1") is not None


def test_ttl_expired_at_exact_boundary(clock):
    """锁死语义：elapsed == TTL（恰好 7200s）即过期。"""
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7200)
    assert table.get("t1") is None


def test_ttl_expired_after_window(clock):
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7200 + 0.001)  # 2h + ε
    assert table.get("t1") is None


def test_ttl_expired_entry_removed_from_table(clock):
    """过期 get 顺手删除（惰性清除，表容量同步释放）。"""
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7300)
    assert table.get("t1") is None
    assert len(table) == 0


def test_ttl_boundary_two_entries_independent(clock):
    """先登记的过期、后登记的在期：互不干扰。"""
    table = make_table(clock)
    reg(table, "old")
    clock.advance(7300)
    reg(table, "new")
    assert table.get("old") is None
    assert table.get("new") is not None


# ---------------------------------------------------------------------------
# R16 分支二：容量逐出（按 submitted_at 最旧，含未完成）
# ---------------------------------------------------------------------------
def test_capacity_exact_boundary_no_evict(clock):
    """恰等于容量：登记 max 条不逐出。"""
    table = make_table(clock, max_entries=3)
    reg(table, "t1")
    reg(table, "t2")
    reg(table, "t3")
    assert len(table) == 3
    assert all(table.get(f"t{i}") is not None for i in (1, 2, 3))


def test_capacity_overflow_evicts_oldest(clock):
    table = make_table(clock, max_entries=3)
    for i in (1, 2, 3):
        reg(table, f"t{i}")
        clock.advance(10)
    reg(table, "t4")  # 超限 → 逐出 submitted_at 最旧的 t1
    assert len(table) == 3
    assert table.get("t1") is None
    assert table.get("t2") is not None
    assert table.get("t3") is not None
    assert table.get("t4") is not None


def test_capacity_evicts_oldest_not_newest(clock):
    table = make_table(clock, max_entries=2)
    reg(table, "t1")
    clock.advance(10)
    reg(table, "t2")
    reg(table, "t3")
    assert table.get("t1") is None  # 最旧被逐
    assert table.get("t2") is not None
    assert table.get("t3") is not None  # 最新保留


def test_capacity_eviction_includes_running(clock):
    """R16：被逐出的可以是未完成（running）任务——上游照常，本端 task_not_found。"""
    table = make_table(clock, max_entries=2)
    reg(table, "t1")  # 保持 running（未 update）
    clock.advance(10)
    reg(table, "t2")
    table.update("t1", status=tasks.STATUS_COMPLETED, url="https://v/1.mp4")
    reg(table, "t3")
    # 逐出按 submitted_at：t1 最旧（尽管已完成）→ 先于 running 的 t2 被逐
    assert table.get("t1") is None
    assert table.get("t2") is not None
    # 再登记 t4 → 轮到 t2（running）被逐：含未完成
    reg(table, "t4")
    assert table.get("t2") is None
    assert table.get("t4") is not None


def test_capacity_eviction_by_submitted_at_not_insert_order(clock):
    """逐出基准是 submitted_at 而非插入序（时钟可回拨的防御性验证）。"""
    table = make_table(clock, max_entries=2)
    reg(table, "t1")  # submitted_at = 0
    clock.now = 5.0
    reg(table, "t2")  # submitted_at = 5
    clock.now = 1.0   # 回拨：t3 的 submitted_at = 1，晚于 t1 早于 t2
    reg(table, "t3")
    assert table.get("t1") is None  # submitted_at 最小者被逐，与插入序无关
    assert table.get("t2") is not None
    assert table.get("t3") is not None


def test_capacity_overwrite_does_not_grow_table(clock):
    table = make_table(clock, max_entries=2)
    reg(table, "t1")
    reg(table, "t2")
    clock.advance(10)
    reg(table, "t1", prompt_hash="h-again")  # 覆盖登记，非新增
    assert len(table) == 2
    assert table.get("t1").prompt_hash == "h-again"


def test_capacity_multiple_overflow_evicts_enough(clock):
    """一次溢出多条（容量收缩场景防御）——逐出足够数量。"""
    table = make_table(clock, max_entries=5)
    for i in range(5):
        reg(table, f"t{i}")
        clock.advance(10)
    table._max = 3  # 模拟配置收缩：下一条触发逐出 3 条
    clock.advance(10)
    reg(table, "t9")
    assert len(table) == 3
    assert table.get("t9") is not None


# ---------------------------------------------------------------------------
# R16 分支三：清理（单次 cleanup + 事件循环协程 cleanup_loop）
# ---------------------------------------------------------------------------
def test_cleanup_removes_only_expired(clock):
    table = make_table(clock)
    reg(table, "old1")
    clock.advance(7300)
    reg(table, "new1")
    removed = table.cleanup()
    assert removed == 1
    assert table.get("old1") is None
    assert table.get("new1") is not None


def test_cleanup_returns_zero_when_nothing_expired(clock):
    table = make_table(clock)
    reg(table, "t1")
    assert table.cleanup() == 0


def test_cleanup_allows_reuse_of_freed_ids(clock):
    table = make_table(clock)
    reg(table, "t1")
    clock.advance(7300)
    table.cleanup()
    clock.advance(1)
    reg(table, "t1")  # 同 id 重新登记（旧条目已清）
    assert table.get("t1") is not None
    assert len(table) == 1


def test_cleanup_loop_is_event_loop_coroutine():
    """禁线程（R2）：cleanup_loop 必须是原生协程函数，靠 asyncio.sleep 驱动。"""
    assert inspect.iscoroutinefunction(tasks.TaskTable.cleanup_loop)


async def test_cleanup_loop_clears_expired(clock):
    """协程周期运行：过期条目被自动清理，在期条目保留。"""
    table = make_table(clock)
    reg(table, "gone")
    clock.advance(7300)
    reg(table, "stay")
    loop_task = asyncio.create_task(table.cleanup_loop(interval_sec=0.01))
    try:
        for _ in range(200):  # 最多等 2s
            await asyncio.sleep(0.01)
            if table.get("gone") is None:
                break
        assert table.get("gone") is None
        assert table.get("stay") is not None
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task


async def test_cleanup_loop_cancellable(clock):
    """取消即退出（挂起在 sleep 处，CancelledError 正常传播）。"""
    table = make_table(clock)
    loop_task = asyncio.create_task(table.cleanup_loop(interval_sec=3600))
    await asyncio.sleep(0.01)
    assert not loop_task.done()
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


# 注：禁线程（R2）由 tests/test_no_threads.py 静态门禁自动覆盖本模块
# （glob 全 .py 扫描，新文件无需登记）；故此处不引入 threading 做行为级断言。


# ---------------------------------------------------------------------------
# 构造校验
# ---------------------------------------------------------------------------
def test_constructor_rejects_bad_ttl(clock):
    with pytest.raises(ValueError, match="ttl_sec"):
        make_table(clock, ttl_sec=0)
    with pytest.raises(ValueError, match="ttl_sec"):
        make_table(clock, ttl_sec=-1)


def test_constructor_rejects_bad_capacity(clock):
    with pytest.raises(ValueError, match="max_entries"):
        make_table(clock, max_entries=0)


def test_defaults_match_v2_spec(clock):
    """默认参数 = 方案 V2 三节：TTL 7200s / 容量 100（调用方亦可注入 cfg 值）。"""
    table = tasks.TaskTable(clock=clock)
    assert table._ttl == 7200.0
    assert table._max == 100
