"""响应契约层（V2 改造版；红线 A3/A4、R11/R17；E13/E24）。

四种返回形态（Agent 只读文本行）：
    ① 正常：每行一个 URL（batch 张）+ 末行 [meta] model/size/实际值
       （size 双格式：档位式 1K–4K 或 WxH，D2；显式传入 ratio 时
       追加 ratio=<v>）；
    ② 部分成功：M<N 时 meta 追加 `requested=N delivered=M`（R17：
       拆分串行中途失败/预算耗尽，契约锁定）；
    ③ persist：成功 → URL 行为 /files 完整地址 + meta `persisted`；
       失败 → 原上游 URL + meta `persist_failed`（不产生 error 行，R5）；
       未 persist → meta `url_expires=1h`（E13：上游 URL 短效警示）；
    ④ 错误：单行 `[error] category=<码> retry=no|yes|fixthenretry
       reason=… suggestion=…`（A4：retry 三值语义）。

V2 删除标记：fallback/换速度（无切换无降级）、quality（参数面删除，
not_applied 不再需要）。
C10 水位提示：超线 → meta 追加 `disk_watermark: <百分比>`（只提示不删图）。

标记顺序固定：requested/delivered → persisted/persist_failed/url_expires
→ disk_watermark。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from engine import errors

# E13：上游未标注时效，按短效对待（方案 V2 三节）
URL_EXPIRES_NOTE = "url_expires=1h"


@dataclass(frozen=True)
class MetaInfo:
    """[meta] 行组装参数（缺省字段不输出）。"""

    model: str
    size: str                                   # 实际值（直传后=请求值；档位式或 WxH 式，D2）
    ratio: Optional[str] = None                # D2：显式传入的画幅比例（None 不输出）
    requested: Optional[int] = None              # batch 请求张数（M<N 时必填）
    delivered: Optional[int] = None             # 实际交付张数（M<N 时必填）
    persist_status: Optional[str] = None        # persisted / persist_failed / None
    disk_note: Optional[str] = None             # disk_watermark: xx%（C10）
    # V4：参考图实况（None 不输出——纯文生图与 V3 契约逐字一致）
    reference_count: Optional[int] = None       # 本次实际使用的参考图张数
    reference_sources: Optional[tuple] = None   # 各图来源标记（url/inline/local）


def format_success(urls: Sequence[str], meta: MetaInfo) -> str:
    """①/②/③：URL 行 × N + [meta] 行（顺序固定，A3）。"""
    lines = list(urls)
    lines.append(_format_meta(meta))
    return "\n".join(lines)


def _format_meta(meta: MetaInfo) -> str:
    fields = [f"model={meta.model}", f"size={meta.size}"]
    if meta.ratio is not None:
        fields.append(f"ratio={meta.ratio}")
    if meta.requested is not None:
        fields.append(f"requested={meta.requested}")
    if meta.delivered is not None:
        fields.append(f"delivered={meta.delivered}")
    # V4：参考图实况（放在 persist 标记之前，保持「输入实况 → 产出实况」可读顺序）
    if meta.reference_count:
        fields.append(f"refs={meta.reference_count}")
        if meta.reference_sources:
            fields.append("ref_src=" + ",".join(meta.reference_sources))
    if meta.persist_status == "persisted":
        fields.append("persisted")
    elif meta.persist_status == "persist_failed":
        fields.append("persist_failed")
    else:
        # 未 persist → 短效警示（E13）；persist 失败降级同样警示（原 URL 短效）
        fields.append(URL_EXPIRES_NOTE)
    if meta.disk_note:
        fields.append(meta.disk_note)
    return META_PREFIX + " ".join(fields)


# ---------------------------------------------------------------------------
# 标记前缀构造纪律（铁律）：meta 行前缀以 chr(91)（左方括号）拼接构造，
# 禁止手打字面量——防渲染/编辑层吞字（docs/V2第二轮收尾_第三轮修复说明.md
# 一节；ord 逐码验证见 tests/test_video_pipeline.py）。
# ---------------------------------------------------------------------------
META_PREFIX = chr(91) + "meta] "


def format_video_deferred(task_id: str, waited_sec: float) -> str:
    """E17：deadline 到仍 running → meta 行 task_id+status=running 非错误。

    文案含已等待秒数与"稍后用 query_video 查询"提示（Agent 凭 task_id
    续查）；槽已随 async with 退出释放（R15），上游任务照常生成。
    """
    return (
        META_PREFIX
        + f"task_id={task_id} status=running "
        + f"已等待 {waited_sec:.1f}s，请稍后调用 query_video 查询"
    )


@dataclass(frozen=True)
class VideoMeta:
    """视频成功行 meta 组装参数（对齐生图 MetaInfo 的扩展，方案 2.4）。"""

    model: str
    seconds: int
    aspect_ratio: str
    elapsed_sec: float
    seed: Optional[int] = None                # D3：显式传入的随机种子（None 不输出）
    persist_status: Optional[str] = None    # persisted / persist_failed / None
    # V4：生成模式与参考图实况（text 模式不输出，保持 V3 契约逐字一致）
    mode: Optional[str] = None               # text / keyframe / reference
    reference_count: Optional[int] = None    # 参考图/首尾帧张数


def format_video_success(url: str, meta: "VideoMeta") -> str:
    """视频成功（T7a/T7c）：URL 行 + meta 行。

    meta 顺序固定：model/seconds/aspect_ratio(±seed) → persist 标记
    （persisted / persist_failed / url_expires 三态，对齐生图 format_success；
    标记顺序契约：persisted/persist_failed/url_expires——失败降级返原上游
    URL 时标 persist_failed，不产生 [error] 行，R5）。

    V4：mode 非 text 时输出 mode 与 refs（让用户看得见「确实用了首帧/参考图」）。
    """
    fields = [
        f"model={meta.model}",
        f"seconds={meta.seconds}",
        f"aspect_ratio={meta.aspect_ratio}",
    ]
    if meta.seed is not None:
        fields.append(f"seed={meta.seed}")
    if meta.mode and meta.mode != "text":
        fields.append(f"mode={meta.mode}")
    if meta.reference_count:
        fields.append(f"refs={meta.reference_count}")
    fields.append(f"elapsed={meta.elapsed_sec:.1f}s")
    if meta.persist_status == "persisted":
        fields.append("persisted")
    elif meta.persist_status == "persist_failed":
        fields.append("persist_failed")
    else:
        # 未 persist → 短效警示（E13）；与生图同语义
        fields.append(URL_EXPIRES_NOTE)
    return url + "\n" + META_PREFIX + " ".join(fields)


def format_video_queued(task_id: str, waited_sec: float, reason: str = "") -> str:
    """V4：上游队列满 → 服务端代为排队（非错误）。

    与 E17（deadline 到仍 running）的区别：这里**还没提交成功**，
    作业在本端排队，后台 worker 会按退避持续尝试提交；提交成功后
    自动转入正常轮询。用户侧动作一致：拿 task_id，稍后 query_video。

    status=queued 明确区分「排队中」与「上游已生成中」，Agent 不会误判。
    """
    detail = f"（{reason}）" if reason else ""
    return (
        META_PREFIX
        + f"task_id={task_id} status=queued "
        + f"上游队列已满，服务端代为排队{detail}；"
        + f"已排队 {waited_sec:.0f}s，请稍后调用 query_video 查询"
    )


def format_video_status(task_id: str, status: str) -> str:
    """query_video 非终态（T7b）：meta 状态行（task_id + 最近上游实况）。"""
    return (
        META_PREFIX
        + f"task_id={task_id} status={status} "
        + "上游仍在生成，请稍后再次查询"
    )


def format_video_queueing(task_id: str, attempts: int = 0,
                          reason: str = "") -> str:
    """query_video 命中「仍卡在本端代排队」（尚未提交成功）时的状态行。

    与 ``format_video_status`` 的区别是**语义**，不是措辞：那边是
    「上游已经在生了」，这边是「上游忙，我们还没提交上去」。生产实测
    （2026-10-15）曾把后者也回成「上游仍在生成」——作业提交 8 次都没成功，
    Agent 读到「已在生成」就会以为"等着就行"，白白干等。

    `attempts` 透出后台已尝试提交的次数：一眼分辨「刚排上」与「排很久了」。
    """
    detail = f"（{reason}）" if reason else ""
    tried = f"已尝试提交 {attempts} 次，" if attempts > 0 else ""
    return (
        META_PREFIX
        + f"task_id={task_id} status=queued "
        + f"上游繁忙，服务端仍在代排队{detail}；"
        + tried + "后台会继续重试，请稍后再次查询"
    )


def format_error(info: errors.ErrorInfo) -> str:
    """④：单行 [error]（A4：retry 三值语义）。"""
    retry = info.retry if info.retry is not None else "-"
    return (
        f"[error] category={info.category} retry={retry} "
        f"reason={info.reason} suggestion={info.suggestion}"
    )


# ---------------------------------------------------------------------------
# meta 标记构造（R11）
# ---------------------------------------------------------------------------
def note_disk_watermark(percent: float) -> str:
    return f"disk_watermark: {percent:.0%}"
