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
  断言，任何优化冲动一律拒绝）。V5 解冻一次，仅为两件事：① 去掉历史
  上游品牌名（服务现在可对接任意提供方）；② 追加 provider 参数语义句。
  > Generate images from a text prompt using the configured image
  > provider. Supports batch generation (split into sequential calls
  > server-side). size accepts a tier (1K/2K/3K/4K) or a WxH value like
  > 1024x768; ratio (e.g. 16:9) may only be set alongside a tiered size,
  > never with WxH. provider: optional name of a configured upstream
  > service to use for this call. NOTE: upstream image URLs are
  > short-lived; set persist=true to get a durable self-hosted URL.
- A2′/A3′：generate_video / query_video description 沿用规格书原文
  （方案 V2 六节 A1′ 明文"零改动"——D2/D3/D4 均不改工具级描述，只改
  参数级 Field 描述）；V5 同样只做「去品牌名 + 加 provider 句」两处
  解冻。原文经群聊渲染层下划线吞蚀，
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
    from app import coderender_pipeline as coderender_pipeline_mod
    from app import pipeline as pipeline_mod
    from app import video_pipeline as video_pipeline_mod

# 工具名（方案 V2 六节；三工具）
TOOL_NAME = "generate_image"
TOOL_NAME_VIDEO = "generate_video"
TOOL_NAME_QUERY = "query_video"
# V3：异步作业总览（不触碰上面三个冻结工具的 schema/description）
TOOL_NAME_LIST = "list_video_tasks"
# V5：代码生图 / 代码生视频（本地渲染，不走上游）
TOOL_NAME_RENDER_IMAGE = "render_image"
TOOL_NAME_RENDER_VIDEO = "render_video"

# A1‴（V4 重冻结）：在 A1″ 原文基础上追加参考图（图生图/多图合成）语义句。
# 冻结纪律不变——tests/test_tool.py 全等断言拦截一切未授权改动；
# 本次解冻是**产品决策**（用户 2026-10-13 明示「全做」），依据见
# docs/V4_产品分析与改造说明.md §3.1。
TOOL_DESCRIPTION = (
    "Generate images from a text prompt using the configured image "
    "provider. Supports batch generation (split into sequential calls "
    "server-side). size accepts a tier (1K/2K/3K/4K) or a WxH value "
    "like 1024x768; ratio (e.g. 16:9) may only be set alongside a "
    "tiered size, never with WxH. provider: optional id of a "
    "configured upstream service to use for this call (omit it to let "
    "the server route by priority). image: optional reference image(s) "
    "for image-to-image editing or multi-image composition — accepts a "
    "public URL, a data URI/base64, or this server's own artifact name "
    "or /files/<name> URL. NOTE: upstream image URLs are short-lived; "
    "set persist=true to get a durable self-hosted URL."
)

# A2‴（V4 重冻结）：在规格书 2.2 原文基础上追加首尾帧/参考图语义句
# （en-dash U+2013 保留；产品决策依据同上）。
TOOL_DESCRIPTION_VIDEO = (
    "Generate a short video (4\u201312 seconds) from a text prompt using "
    "the configured video provider. Waits synchronously within the time "
    "budget; if the task is still running at deadline, returns a "
    "task_id which can be polled via the query_video tool. Returns "
    "video URL on success. provider: optional id of a configured "
    "upstream service to use for this call (omit it to let the server "
    "route by priority). first_frame/last_frame: optional keyframe "
    "images (use first_frame to animate this image as the real first "
    "frame); reference_images: optional style/content references "
    "(re-composes). Each accepts a public URL, data URI/base64, or "
    "this server's own artifact name or /files/<name> URL. When the "
    "upstream queue is full the server queues the submission and "
    "returns a task_id with status=queued. NOTE: set persist=true for "
    "a durable URL (upstream URLs are short-lived)."
)

# A3′：query_video description 逐字符冻结（规格书 2.3 原文；该段仅含
# 单个下划线，未经渲染层吞蚀，逐字符照录）
TOOL_DESCRIPTION_QUERY = (
    "Query the status of a video generation task by task_id. Returns "
    "the video URL when the task has succeeded, or its current status "
    "otherwise. Tasks are kept track of in memory for 2 hours after "
    "submission."
)

# A2：参数名（名称有序；D2/D3 增量；V4 增参考图/首尾帧；pipeline 侧二次校验范围）
TOOL_PARAMS: tuple[str, ...] = (
    "prompt", "size", "ratio", "batch_size", "persist", "image", "provider",
)
TOOL_PARAMS_VIDEO: tuple[str, ...] = (
    "prompt", "seconds", "aspect_ratio", "seed", "persist",
    "first_frame", "last_frame", "reference_images", "provider",
)
TOOL_PARAMS_QUERY: tuple[str, ...] = ("task_id",)
TOOL_PARAMS_LIST: tuple[str, ...] = ("limit",)
TOOL_PARAMS_RENDER_IMAGE: tuple[str, ...] = ("code", "size", "lang")
TOOL_PARAMS_RENDER_VIDEO: tuple[str, ...] = (
    "code", "size", "frames", "fps", "format", "lang",
)

# V3：list_video_tasks 描述（自建，非规格书冻结文本）
TOOL_DESCRIPTION_LIST = (
    "List the video generation tasks currently tracked in memory "
    "(newest first). Use it after submitting several videos "
    "asynchronously to see their task_id, status and result URL at "
    "once, instead of polling query_video one by one."
)

# V5：代码渲染两条工具的描述（自建，非规格书冻结文本；与 generate_* 的
# 区别必须在描述第一句就说清，否则模型会拿 AI 生图去画图表 → 必然画不准）
TOOL_DESCRIPTION_RENDER_IMAGE = (
    "Render an image from code and return a URL to the PNG. Use this "
    "instead of generate_image whenever the result must be exact and "
    "repeatable: charts, plots, diagrams, flowcharts, tables, icons, "
    "badges, posters or covers with precise text layout, and batches of "
    "templated images. Nothing is sent upstream and no generation quota "
    "is consumed - the code is rendered locally on the server. code is "
    "either SVG markup, or a short Python script that produces SVG "
    "markup; lang defaults to auto (code that starts with < or contains "
    "<svg> is treated as SVG, otherwise as Python). Python runs in a "
    "restricted sandbox: no "
    "file, network or import access other than math, json, random, "
    "statistics and colorsys; the variables width and height are "
    "predefined; it must print the SVG or assign it to svg/out/result. "
    "size accepts WxH like 1024x768 (default 1024x1024)."
)

TOOL_DESCRIPTION_RENDER_VIDEO = (
    "Render an animation from code and return a URL to the result (GIF "
    "by default, or MP4 when ffmpeg is installed on the server). Use it "
    "for animated diagrams, loading spinners, progress bars, kinetic "
    "text and simple motion graphics - exact, repeatable and local, "
    "with no upstream quota consumed. code is either SVG markup or a "
    "short Python script; animate SVG by putting the __FRAME__ "
    "placeholder where the frame index belongs, or in Python by using "
    "the predefined variables frame, total, width and height. Same "
    "restricted sandbox as render_image. frames (default 36, max 120) "
    "and fps (default 12, max 30) control the animation; the whole "
    "request is capped at 90 seconds, so keep frames and drawing cost "
    "modest. format: auto, mp4 or gif."
)


def register_tool(
    mcp: FastMCP,
    pipeline: pipeline_mod.GeneratePipeline,
    video_pipeline: video_pipeline_mod.VideoPipeline,
    code_render: "coderender_pipeline_mod.CodeRenderPipeline | None" = None,
) -> None:
    """向 FastMCP 注册工具（描述/schema 见模块 docstring）。

    code_render 为 None 时不注册 render_image/render_video（老测试与
    只跑生图链路的场景仍可调用本函数，签名向后兼容）。
    """

    @mcp.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION, output_schema=None)
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
        image: Annotated[
            Optional[list[str]], Field(
                description="参考图（图生图/多图合成），V4；数组每项一个："
                "公网 URL、data URI/base64，或本服务产物名/ "
                "/files/<name> URL（可直接拿上一张生成结果当参考图）；"
                "不传=纯文生图")
        ] = None,
        provider: Annotated[
            Optional[str], Field(
                description="指定用哪个服务提供方（管理面板里配置的 id）；"
                "不传=按服务端路由（令牌绑定 → 优先级）")
        ] = None,
    ) -> str:
        """见模块级 TOOL_DESCRIPTION（此处 docstring 不进 schema）。"""
        return await pipeline.run(
            prompt=prompt,
            size=size,
            ratio=ratio,
            batch_size=batch_size,
            persist_flag=persist,
            images=image,
            http_headers=_current_http_headers(),
            provider=provider,
        )

    @mcp.tool(name=TOOL_NAME_VIDEO, description=TOOL_DESCRIPTION_VIDEO, output_schema=None)
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
        first_frame: Annotated[
            Optional[str], Field(
                description="首帧图（keyframe 模式），V4；「让这张图作为"
                "成片真实首帧动起来」首选。形态同参考图：公网 URL / "
                "data URI / 本服务产物名")
        ] = None,
        last_frame: Annotated[
            Optional[str], Field(
                description="尾帧图（keyframe 模式），V4；与 first_frame "
                "至少给一个；可做首尾帧过渡")
        ] = None,
        reference_images: Annotated[
            Optional[list[str]], Field(
                description="参考图（reference 模式，V4；上限 5 张）；"
                "仅作风格/内容参考，会重新构图——要构图可控请用 "
                "first_frame；与 first_frame/last_frame 互斥")
        ] = None,
        provider: Annotated[
            Optional[str], Field(
                description="指定用哪个服务提供方（管理面板里配置的 id）；"
                "不传=按服务端路由（令牌绑定 → 优先级）")
        ] = None,
    ) -> str:
        """见模块级 TOOL_DESCRIPTION_VIDEO（此处 docstring 不进 schema）。"""
        return await video_pipeline.run(
            prompt=prompt,
            seconds=seconds,
            aspect_ratio=aspect_ratio,
            seed=seed,
            persist_flag=persist,
            first_frame=first_frame,
            last_frame=last_frame,
            reference_images=reference_images,
            http_headers=_current_http_headers(),
            provider=provider,
        )

    @mcp.tool(name=TOOL_NAME_QUERY, description=TOOL_DESCRIPTION_QUERY, output_schema=None)
    async def query_video(
        task_id: Annotated[str, Field(description="generate_video 返回的任务号")],
    ) -> str:
        """见模块级 TOOL_DESCRIPTION_QUERY（此处 docstring 不进 schema）。"""
        return await video_pipeline.query(task_id=task_id)

    @mcp.tool(name=TOOL_NAME_LIST, description=TOOL_DESCRIPTION_LIST, output_schema=None)
    async def list_video_tasks(
        limit: Annotated[
            int, Field(description="最多返回条数；默认 20")
        ] = 20,
    ) -> str:
        """见模块级 TOOL_DESCRIPTION_LIST（此处 docstring 不进 schema）。"""
        import json

        jobs = video_pipeline.list_jobs(limit=limit)
        return json.dumps(
            {"count": len(jobs), "tasks": jobs}, ensure_ascii=False)

    # ---- V5：代码生图 / 代码生视频（本地渲染，无上游依赖）----
    if code_render is not None:
        @mcp.tool(name=TOOL_NAME_RENDER_IMAGE,
                  description=TOOL_DESCRIPTION_RENDER_IMAGE,
                  output_schema=None)
        async def render_image(
            code: Annotated[
                str, Field(description="SVG 代码，或产出 SVG 的短 Python 脚本")
            ],
            size: Annotated[
                str, Field(description="WxH，如 1024x768；默认 1024x1024")
            ] = "1024x1024",
            lang: Annotated[
                Optional[str], Field(
                    description="svg / python；不传=自动判断（含 <svg> 即 SVG）")
            ] = None,
        ) -> str:
            """见模块级 TOOL_DESCRIPTION_RENDER_IMAGE。"""
            return await code_render.render_image(
                code, size=size, lang=lang,
                headers=_current_http_headers())

        @mcp.tool(name=TOOL_NAME_RENDER_VIDEO,
                  description=TOOL_DESCRIPTION_RENDER_VIDEO,
                  output_schema=None)
        async def render_video(
            code: Annotated[
                str, Field(
                    description="SVG（用 __FRAME__ 占位帧号）或短 Python 脚本")
            ],
            size: Annotated[
                str, Field(description="WxH，如 720x480；默认 720x720")
            ] = "720x720",
            frames: Annotated[
                int, Field(description="帧数 1–120，默认 36")
            ] = 36,
            fps: Annotated[
                int, Field(description="帧率 1–30，默认 12")
            ] = 12,
            format: Annotated[
                str, Field(description="auto / mp4 / gif；默认 auto")
            ] = "auto",
            lang: Annotated[
                Optional[str], Field(
                    description="svg / python；不传=自动判断（含 <svg> 即 SVG）")
            ] = None,
        ) -> str:
            """见模块级 TOOL_DESCRIPTION_RENDER_VIDEO。"""
            return await code_render.render_video(
                code, size=size, fps=fps, frames=frames, lang=lang,
                video_format=format, headers=_current_http_headers())

    # 注册后的函数对象已替换为 Tool，无需保留引用
    # 收尾：压平 anyOf（严格模型拒载第二道毒，见 _flatten_null_union 注释）
    _flatten_tool_schemas(mcp)
    return None


# ────────────────────────────────────────────────────────────────
# 严格模型兼容：schema 归一化
#
# ⚠️ 根因（2026-10-13 实测，与 social-insight / websearch /
#    shared-memory 同一个坑）：严格模型（Step-5-Preview /
#    GLM-5.3-Flash / Deepseek-V4.1-Flash）逐字校验 tools/list 里
#    每个工具的 schema，两样东西让它**拒掉整个请求**
#    （11129 "The tool definition does not meet the model requirements"）：
#      1. outputSchema —— fastmcp 自动生成，已在各 @mcp.tool 上
#         显式 output_schema=None 关掉
#      2. anyOf       —— ``Optional[X] = None`` 参数会展开成
#         ``anyOf: [{...}, {"type": "null"}]``（generate_image /
#         generate_video 的 ratio/seed/first_frame/... 全中）
#
#    第 2 项取决于参数写法，漏一个就是一颗雷，所以注册完成后
#    统一压平：``anyOf: [T, null]`` → ``T``（参数本就可选，
#    default/description 原样保留，语义不变）。
# ────────────────────────────────────────────────────────────────
def _flatten_null_union(node: object) -> object:
    """递归压平 ``anyOf: [T, {"type": "null"}]`` → ``T``。"""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            non_null = [
                s
                for s in any_of
                if not (isinstance(s, dict) and s.get("type") == "null")
            ]
            if len(non_null) == 1:
                merged: dict = dict(non_null[0])
                for k, v in node.items():
                    if k != "anyOf":
                        merged[k] = _flatten_null_union(v)
                return merged
        return {k: _flatten_null_union(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_flatten_null_union(item) for item in node]
    return node


def _flatten_tool_schemas(mcp: FastMCP) -> int:
    """压平全部已注册工具 inputSchema 里的 anyOf，返回处理的工具数。"""
    import json as _json

    probes = (
        lambda m: getattr(getattr(m, "_local_provider", None), "_components", None),
        lambda m: getattr(getattr(m, "_provider", None), "_components", None),
        lambda m: getattr(getattr(m, "_tool_manager", None), "_tools", None),
    )
    tools: list = []
    for probe in probes:
        try:
            store = probe(mcp)
        except Exception:  # noqa: BLE001 —— 探测失败就试下一种布局
            continue
        if isinstance(store, dict):
            tools = [
                v
                for v in store.values()
                if hasattr(v, "parameters") and hasattr(v, "output_schema")
            ]
            if tools:
                break
    n = 0
    for tool in tools:
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict) and "anyOf" in _json.dumps(params, ensure_ascii=False):
            tool.parameters = _flatten_null_union(params)
            n += 1
    if not tools:
        # 静默跳过 = 把故障放回去。显式告警。
        import sys

        print(
            "[schema] anyOf 归一化失败：未探测到已注册工具"
            "（fastmcp 版本布局可能已变），严格模型可能拒载",
            file=sys.stderr,
        )
    return n


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
