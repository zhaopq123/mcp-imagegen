"""MCP 工具层（V2 改造版；红线 A1″/A2′/A3′/A2；T8 三工具注册；
V2 第七轮官方文档对齐 D2/D3/D4 落点）。

三工具 schema：
- generate_image 五参数（D2：官方 F2/F3——size 双格式 + ratio）：

    prompt      string   必填         提示词，中英文皆可
    size        string   1024x1024   档位式 1K/2K/3K/4K 或 WxH 直传
    ratio       string   不传         官方 8 档；仅与档位式 size 同传
    batch_size  integer  1            1–4（服务端拆分串行）
    persist     boolean  false        true=转存返自托管持久 URL

- generate_video 五参数（规格书 2.2 + D3 seed 恢复 + D4 六档）：

    prompt        string   必填    视频内容描述
    seconds       integer  5       4–12（范围校验在 pipeline，A2）
    aspect_ratio  string   16:9    16:9/9:16/1:1/4:3/3:4/21:9 六档（D4；
                                  校验在 pipeline）
    seed          integer  不传    随机种子，直传上游（D3/官方 F5；
                                  None 不注入请求体）
    persist       boolean  false   true=转存返自托管持久 URL

- query_video 单参数（规格书 2.3）：

    task_id  string  必填  generate_video 返回的任务号

红线：
- A1″：generate_image description 在 A1′（方案 V2 六节原文）基础上随
  D2 追加 size/ratio 语义句并**重新冻结**（tests/test_tool.py 全等
  断言，任何优化冲动一律拒绝）：
  > Generate images from a text prompt using agnes-image-2.5-flash.
  > Supports batch generation (split into sequential calls server-side).
  > size accepts a tier (1K/2K/3K/4K) or a WxH value like 1024x768;
  > ratio (e.g. 16:9) may only be set alongside a tiered size, never
  > with WxH. NOTE: upstream image URLs are short-lived; set
  > persist=true to get a durable self-hosted URL.
- A2′/A3′：generate_video / query_video description 沿用规格书原文
  （方案 V2 六节 A1′ 明文"零改动"——D2/D3/D4 均不改工具级描述，只改
  参数级 Field 描述）。原文经群聊渲染层下划线吞蚀，
  已按不变证据复原：规格书 2.4 契约表同消息内 "task_id=… 请稍后调用
  query_video 查询" 完整保留（吞蚀为配对现象，非原文缺字），且本工具
  注册名即 query_video、参数名即 task_id——描述必须指向真实工具名；
  其余字符（含 en-dash U+2013）逐字符照录规格书，tests/test_tool.py
  全等断言冻结。
- A2：范围类校验（batch 1–4、size 格式、ratio 枚举、seconds 4–12、
  aspect_ratio 六档、seed 类型）**不在 schema 层硬约束**，而在 pipeline
  层校验——保证越界也
  返回 [error] invalid_params 契约行（验收⑪⑫），而非传输层 schema
  报错；schema 描述中的范围仅作 Agent 提示（信息性，不构成约束）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Mapping, Optional

from fastmcp import FastMCP
from pydantic import Field

if TYPE_CHECKING:  # 仅类型标注；运行时避免 tool ↔ pipeline 循环导入
    from app import pipeline as pipeline_mod
    from app import video_pipeline as video_pipeline_mod

# 工具名（方案 V2 六节；三工具）
TOOL_NAME = "generate_image"
TOOL_NAME_VIDEO = "generate_video"
TOOL_NAME_QUERY = "query_video"

# A1″：description 逐字符冻结（A1′ 原文 + D2 size/ratio 语义句；
# tests/test_tool.py 全等断言拦截一切改动）
TOOL_DESCRIPTION = (
    "Generate images from a text prompt using agnes-image-2.5-flash. "
    "Supports batch generation (split into sequential calls server-side). "
    "size accepts a tier (1K/2K/3K/4K) or a WxH value like 1024x768; "
    "ratio (e.g. 16:9) may only be set alongside a tiered size, never "
    "with WxH. NOTE: upstream image URLs are short-lived; set "
    "persist=true to get a durable self-hosted URL."
)

# A2′：generate_video description 逐字符冻结（规格书 2.2 原文；V2 六节
# A1′ 明文"沿用规格书原文零改动"——下划线按渲染层吞蚀复原，见模块
# docstring；en-dash 为 U+2013）
TOOL_DESCRIPTION_VIDEO = (
    "Generate a short video (4\u201312 seconds) from a text prompt using "
    "agnes-video-2.5-flash. Waits synchronously within the time budget; "
    "if the task is still running at deadline, returns a task_id which "
    "can be polled via the query_video tool. Returns video URL on "
    "success. NOTE: set persist=true for a durable URL (upstream URLs "
    "are short-lived)."
)

# A3′：query_video description 逐字符冻结（规格书 2.3 原文；该段仅含
# 单个下划线，未经渲染层吞蚀，逐字符照录）
TOOL_DESCRIPTION_QUERY = (
    "Query the status of a video generation task by task_id. Returns "
    "the video URL when the task has succeeded, or its current status "
    "otherwise. Tasks are kept track of in memory for 2 hours after "
    "submission."
)

# A2：参数名（名称有序；D2/D3 增量；pipeline 侧二次校验范围）
TOOL_PARAMS: tuple[str, ...] = (
    "prompt", "size", "ratio", "batch_size", "persist",
)
TOOL_PARAMS_VIDEO: tuple[str, ...] = (
    "prompt", "seconds", "aspect_ratio", "seed", "persist",
)
TOOL_PARAMS_QUERY: tuple[str, ...] = ("task_id",)


def register_tool(
    mcp: FastMCP,
    pipeline: pipeline_mod.GeneratePipeline,
    video_pipeline: video_pipeline_mod.VideoPipeline,
) -> None:
    """向 FastMCP 注册三工具（描述/schema 见模块 docstring）。"""

    @mcp.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)
    async def generate_image(
        prompt: Annotated[str, Field(description="提示词，中英文皆可")],
        size: Annotated[
            str, Field(
                description="档位 1K/2K/3K/4K 或 WxH（如 1024x768）；"
                "直传上游（默认 1024x1024）")
        ] = "1024x1024",
        ratio: Annotated[
            Optional[str], Field(
                description="1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9 八档；"
                "仅与档位式 size（1K/2K/3K/4K）同传，与 WxH 同传即参数"
                "冲突；不传=上游默认 1:1（D2/官方 F3）")
        ] = None,
        batch_size: Annotated[
            int, Field(description="1–4；默认 1；服务端拆分串行")
        ] = 1,
        persist: Annotated[
            bool, Field(description="true=转存返自托管持久 URL（/files）")
        ] = False,
    ) -> str:
        """见模块级 TOOL_DESCRIPTION（此处 docstring 不进 schema）。"""
        return await pipeline.run(
            prompt=prompt,
            size=size,
            ratio=ratio,
            batch_size=batch_size,
            persist_flag=persist,
            http_headers=_current_http_headers(),
        )

    @mcp.tool(name=TOOL_NAME_VIDEO, description=TOOL_DESCRIPTION_VIDEO)
    async def generate_video(
        prompt: Annotated[str, Field(description="视频内容描述，中英文皆可")],
        seconds: Annotated[
            int, Field(description="4–12；默认 5")
        ] = 5,
        aspect_ratio: Annotated[
            str, Field(
                description="16:9/9:16/1:1/4:3/3:4/21:9 六档；"
                "默认 16:9（D4）")
        ] = "16:9",
        seed: Annotated[
            Optional[int], Field(
                description="随机种子，整数可选，直传上游；"
                "不传=不注入（D3/官方 F5）")
        ] = None,
        persist: Annotated[
            bool, Field(description="true=转存返自托管持久 URL（/files）")
        ] = False,
    ) -> str:
        """见模块级 TOOL_DESCRIPTION_VIDEO（此处 docstring 不进 schema）。"""
        return await video_pipeline.run(
            prompt=prompt,
            seconds=seconds,
            aspect_ratio=aspect_ratio,
            seed=seed,
            persist_flag=persist,
            http_headers=_current_http_headers(),
        )

    @mcp.tool(name=TOOL_NAME_QUERY, description=TOOL_DESCRIPTION_QUERY)
    async def query_video(
        task_id: Annotated[str, Field(description="generate_video 返回的任务号")],
    ) -> str:
        """见模块级 TOOL_DESCRIPTION_QUERY（此处 docstring 不进 schema）。"""
        return await video_pipeline.query(task_id=task_id)

    # 注册后的函数对象已替换为 Tool，无需保留引用
    return None


def _current_http_headers() -> Optional[Mapping[str, str]]:
    """当前 HTTP 请求头（persist URL 绝对化用，Q3）；无 HTTP 上下文 → None。

    注意：fastmcp 的 get_http_headers() 默认剥离 host 等头（exclude 列表），
    而 URL 绝对化恰恰需要 host / x-forwarded-host / x-forwarded-proto——
    直连无代理场景没有转发头，Host 是唯一来源。故取 include_all=True 后
    白名单过滤，只放行 URL 构造所需三个头，不向 pipeline 深处扩散其余头。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include_all=True)
    except Exception:
        return None
    wanted = ("host", "x-forwarded-host", "x-forwarded-proto")
    return {k: str(v) for k, v in headers.items()
            if k.lower() in wanted} or None
