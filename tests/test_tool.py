"""MCP 工具层门禁（V2 改造版；红线 A1″/A2′/A3′/A2；V2 第七轮 D2/D3/D4）。

A1″ 全等断言：generate_image description 与重新冻结快照**逐字符一致**
    （D2 在 A1′ 原文基础上追加 size/ratio 语义句——官方档位式 size +
    ratio 互斥规则进 Agent 可见面）；app/tool.py 若被改动（"优化
    冲动"），本测试立即拦截。A2′/A3′ 视频/查询两段描述仍沿用规格书
    原文（D2/D3/D4 均不改工具级描述，只改参数级 Field 描述），
    冻结快照与实现常量全等双保险。
A2 范围不进 schema：batch 1–4 / size 格式 / ratio 八档 / seconds 4–12 /
    aspect_ratio 六档 / seed 类型等范围类校验只在 pipeline 层做（保证
    越界也返回 [error] invalid_params 契约行，而非传输层 schema 报错）
    ——本测试断言三工具 schema 无 minimum/maximum 等硬约束，并直测
    validate_params / validate_video_params 的边界值。
参数透传：generate_video / query_video 对 pipeline 的参数逐项核对
    （名称/默认值/无污染注入；seed 缺省 None、显式 42 直传）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tool  # noqa: E402
from app.pipeline import validate_params  # noqa: E402
from app.video_pipeline import validate_video_params  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

# A1″ 重新冻结快照（D2 定案：A1′ 原文 + size/ratio 语义句；冻结基准）
SPEC_TOOL_DESCRIPTION = (
    "Generate images from a text prompt using agnes-image-2.5-flash. "
    "Supports batch generation (split into sequential calls server-side). "
    "size accepts a tier (1K/2K/3K/4K) or a WxH value like 1024x768; "
    "ratio (e.g. 16:9) may only be set alongside a tiered size, never "
    "with WxH. image: optional reference image(s) for image-to-image "
    "editing or multi-image composition — accept a public URL, a data "
    "URI/base64, or this server's own artifact name or /files/<name> "
    "URL. NOTE: upstream image URLs are short-lived; set persist=true "
    "to get a durable self-hosted URL."
)

# 规格书 2.2 原文快照（A2′；V2 六节明文沿用零改动；en-dash = U+2013）
SPEC_TOOL_DESCRIPTION_VIDEO = (
    "Generate a short video (4\u201312 seconds) from a text prompt using "
    "agnes-video-2.5-flash. Waits synchronously within the time budget; "
    "if the task is still running at deadline, returns a task_id which "
    "can be polled via the query_video tool. Returns video URL on "
    "success. first_frame/last_frame: optional keyframe images (use "
    "first_frame to animate this image as the real first frame); "
    "reference_images: optional style/content references (re-composes). "
    "Each accepts a public URL, data URI/base64, or this server's own "
    "artifact name or /files/<name> URL. When the upstream queue is "
    "full the server queues the submission and returns a task_id with "
    "status=queued. NOTE: set persist=true for a durable URL (upstream "
    "URLs are short-lived)."
)

# 规格书 2.3 原文快照（A3′；该段单下划线未经吞蚀，逐字符照录）
SPEC_TOOL_DESCRIPTION_QUERY = (
    "Query the status of a video generation task by task_id. Returns "
    "the video URL when the task has succeeded, or its current status "
    "otherwise. Tasks are kept track of in memory for 2 hours after "
    "submission."
)


def _make_mcp() -> FastMCP:
    return FastMCP(name="test-registry")


def _get_schema(mcp: FastMCP, name: str) -> dict:
    """注册后取 schema（版本兼容，见 _get_registered_tool）。"""
    return _get_registered_tool(mcp, name).parameters


def _get_registered_tool(mcp: FastMCP, name: str):
    """取已注册工具（版本兼容）。

    fastmcp 4.x 起 `get_tool` 为公开同步 API；2.x 仅有私有异步
    `_tool_manager.get_tool`。交付包规格钉 2.14.3，实际部署解析到 4.x，
    故两条路径都保留，避免任一环境失效。
    """
    getter = getattr(mcp, "get_tool", None)
    if callable(getter):
        import asyncio
        import inspect

        got = getter(name)
        # fastmcp 4.x 的 get_tool 实为协程，需驱动；2.x 无此公开方法
        if inspect.isawaitable(got):
            return asyncio.run(got)
        return got

    import asyncio

    async def _get():
        return await mcp._tool_manager.get_tool(name)

    return asyncio.run(_get())


def _make_mcp_with_tool() -> FastMCP:
    mcp = _make_mcp()
    tool.register_tool(mcp, pipeline=None, video_pipeline=None)
    return mcp


# ---------------------------------------------------------------------------
# A1″/A2′/A3′：description 逐字符冻结
# ---------------------------------------------------------------------------
def test_description_frozen_verbatim():
    """A1″：注册后的三工具 description 与冻结快照逐字符全等。"""
    mcp = _make_mcp_with_tool()
    assert _get_registered_tool(mcp, tool.TOOL_NAME).description \
        == SPEC_TOOL_DESCRIPTION
    assert _get_registered_tool(mcp, tool.TOOL_NAME_VIDEO).description \
        == SPEC_TOOL_DESCRIPTION_VIDEO
    assert _get_registered_tool(mcp, tool.TOOL_NAME_QUERY).description \
        == SPEC_TOOL_DESCRIPTION_QUERY


def test_tool_description_constant_is_source():
    """工具层常量 = 冻结快照（逐字符全等，冻结基线）。"""
    assert tool.TOOL_DESCRIPTION == SPEC_TOOL_DESCRIPTION
    assert tool.TOOL_DESCRIPTION_VIDEO == SPEC_TOOL_DESCRIPTION_VIDEO
    assert tool.TOOL_DESCRIPTION_QUERY == SPEC_TOOL_DESCRIPTION_QUERY


def test_a1_double_prime_inherits_a1_prime_prefix():
    """A1″ 血统断言：A1′ 原文三句逐字保留（只在 NOTE 前插入 D2 句）。"""
    assert SPEC_TOOL_DESCRIPTION.startswith(
        "Generate images from a text prompt using agnes-image-2.5-flash. "
        "Supports batch generation (split into sequential calls server-side). "
    )
    assert SPEC_TOOL_DESCRIPTION.endswith(
        "NOTE: upstream image URLs are short-lived; set persist=true to "
        "get a durable self-hosted URL."
    )
    assert "1K/2K/3K/4K" in SPEC_TOOL_DESCRIPTION  # D2 档位语义入文
    assert "never with WxH" in SPEC_TOOL_DESCRIPTION  # ratio 互斥规则入文


def test_video_description_uses_real_tool_and_param_names():
    """A2′ 复核：描述指向真实注册名 query_video / 参数名 task_id
    （渲染层吞蚀复原的确定性证据链，见 app/tool.py 模块 docstring）。"""
    assert "query_video tool" in tool.TOOL_DESCRIPTION_VIDEO
    assert "task_id which" in tool.TOOL_DESCRIPTION_VIDEO
    assert "by task_id" in tool.TOOL_DESCRIPTION_QUERY


def test_video_description_dash_is_unicode_en_dash():
    """A2′ 细节锁定：4–12 为 U+2013（en dash），非连字符。"""
    assert "\u2013" in tool.TOOL_DESCRIPTION_VIDEO
    assert "4-12" not in tool.TOOL_DESCRIPTION_VIDEO


def test_tool_names_unchanged():
    assert tool.TOOL_NAME == "generate_image"
    assert tool.TOOL_NAME_VIDEO == "generate_video"
    assert tool.TOOL_NAME_QUERY == "query_video"


def test_tool_params_exact():
    """V4：工具参数面包含生图参考图、视频首尾帧与参考图。"""
    assert tool.TOOL_PARAMS == (
        "prompt", "size", "ratio", "batch_size", "persist", "image",
    )
    assert tool.TOOL_PARAMS_VIDEO == (
        "prompt", "seconds", "aspect_ratio", "seed", "persist",
        "first_frame", "last_frame", "reference_images",
    )
    assert tool.TOOL_PARAMS_QUERY == ("task_id",)
    assert tool.TOOL_PARAMS_LIST == ("limit",)


# ---------------------------------------------------------------------------
# schema：参数集 / 类型 / 默认值 / 无硬约束（A2）
# ---------------------------------------------------------------------------
def test_schema_params_exact():
    """三工具 schema 参数名集合 = 定义；ratio/seed 缺省 None（不传语义）。"""
    mcp = _make_mcp_with_tool()
    img = _get_schema(mcp, tool.TOOL_NAME)
    assert set(img["properties"].keys()) == set(tool.TOOL_PARAMS)
    assert img["required"] == ["prompt"]
    assert img["properties"]["size"]["default"] == "1024x1024"
    assert img["properties"]["ratio"]["default"] is None   # D2
    assert img["properties"]["batch_size"]["default"] == 1
    assert img["properties"]["persist"]["default"] is False

    vid = _get_schema(mcp, tool.TOOL_NAME_VIDEO)
    assert set(vid["properties"].keys()) == set(tool.TOOL_PARAMS_VIDEO)
    assert vid["required"] == ["prompt"]
    assert vid["properties"]["seconds"]["default"] == 5
    assert vid["properties"]["aspect_ratio"]["default"] == "16:9"
    assert vid["properties"]["seed"]["default"] is None     # D3
    assert vid["properties"]["persist"]["default"] is False

    qry = _get_schema(mcp, tool.TOOL_NAME_QUERY)
    assert set(qry["properties"].keys()) == {"task_id"}
    assert qry["required"] == ["task_id"]


def test_schema_has_no_hard_range_constraints():
    """A2：范围类校验不进 schema（无 minimum/maximum/pattern/enum；
    anyOf 分支同样不得出现——Optional 渲染形态下纵深扫描）。"""
    mcp = _make_mcp_with_tool()
    for name in (tool.TOOL_NAME, tool.TOOL_NAME_VIDEO, tool.TOOL_NAME_QUERY):
        schema = _get_schema(mcp, name)
        for prop in schema["properties"].values():
            for obj in [prop, *prop.get("anyOf", [])]:
                assert "minimum" not in obj
                assert "maximum" not in obj
                assert "pattern" not in obj
                assert "enum" not in obj


def _type_of(prop: dict) -> str:
    """取单一 type（Optional 渲染为 anyOf 时取第一个非 null 分支）。"""
    if "type" in prop:
        return prop["type"]
    for branch in prop.get("anyOf", []):
        if branch.get("type") != "null":
            return branch["type"]
    raise AssertionError(f"schema 无类型: {prop!r}")


def test_schema_types():
    mcp = _make_mcp_with_tool()
    img = _get_schema(mcp, tool.TOOL_NAME)["properties"]
    assert _type_of(img["prompt"]) == "string"
    assert _type_of(img["size"]) == "string"
    assert _type_of(img["ratio"]) == "string"        # D2
    assert _type_of(img["batch_size"]) == "integer"
    assert _type_of(img["persist"]) == "boolean"

    vid = _get_schema(mcp, tool.TOOL_NAME_VIDEO)["properties"]
    assert _type_of(vid["prompt"]) == "string"
    assert _type_of(vid["seconds"]) == "integer"
    assert _type_of(vid["aspect_ratio"]) == "string"
    assert _type_of(vid["seed"]) == "integer"        # D3
    assert _type_of(vid["persist"]) == "boolean"

    qry = _get_schema(mcp, tool.TOOL_NAME_QUERY)["properties"]
    assert _type_of(qry["task_id"]) == "string"


# ---------------------------------------------------------------------------
# 参数透传（generate_video / query_video → VideoPipeline）
# ---------------------------------------------------------------------------
class _RecordingVideoPipeline:
    """记录 run/query 收到的实参（透传断言用；无真实 IO）。"""

    def __init__(self) -> None:
        self.run_calls: list[dict] = []
        self.query_calls: list[dict] = []

    async def run(self, **kwargs):
        self.run_calls.append(kwargs)
        return "ok-from-video"

    async def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return "ok-from-query"


def _call_tool(mcp: FastMCP, name: str, arguments: dict) -> str:
    """经 MCP 工具层调用（走 schema 校验 → fn），返回文本结果。

    版本兼容：fastmcp 4.x 公开 `call_tool` 协程；2.x 仅有私有异步
    `_tool_manager.call_tool`。两者返回结构一致（content[0].text）。
    """
    import asyncio

    async def _call():
        caller = getattr(mcp, "call_tool", None)
        if callable(caller):
            result = await caller(name, arguments)
        else:
            result = await mcp._tool_manager.call_tool(name, arguments)
        # fastmcp：CallToolResult / ToolResult；文本内容取第一个 TextContent
        if hasattr(result, "content") and result.content:
            return result.content[0].text
        return str(result)

    return asyncio.run(_call())


def test_generate_video_params_passthrough():
    """五参数逐项透传（默认值经 schema 层注入后原样到达 pipeline）。"""
    mcp = _make_mcp()
    vp = _RecordingVideoPipeline()
    tool.register_tool(mcp, pipeline=None, video_pipeline=vp)
    out = _call_tool(mcp, tool.TOOL_NAME_VIDEO, {"prompt": "a cat surfing"})
    assert out == "ok-from-video"
    assert len(vp.run_calls) == 1
    call = vp.run_calls[0]
    assert call["prompt"] == "a cat surfing"
    assert call["seconds"] == 5            # schema 默认值
    assert call["aspect_ratio"] == "16:9"   # schema 默认值
    assert call["seed"] is None            # D3：缺省不传（None 注入语义）
    assert call["persist_flag"] is False   # schema 默认值 → persist_flag
    assert set(call.keys()) == {
        "prompt", "seconds", "aspect_ratio", "seed", "persist_flag",
        "first_frame", "last_frame", "reference_images", "http_headers",
    }
    assert call["first_frame"] is None
    assert call["last_frame"] is None
    assert call["reference_images"] is None
    assert call["http_headers"] is None     # 测试无 HTTP 上下文 → None


def test_generate_video_explicit_params_passthrough():
    mcp = _make_mcp()
    vp = _RecordingVideoPipeline()
    tool.register_tool(mcp, pipeline=None, video_pipeline=vp)
    _call_tool(mcp, tool.TOOL_NAME_VIDEO, {
        "prompt": "霓虹夜景", "seconds": 12,
        "aspect_ratio": "9:16", "seed": 42, "persist": True,
    })
    call = vp.run_calls[0]
    assert call["prompt"] == "霓虹夜景"
    assert call["seconds"] == 12
    assert call["aspect_ratio"] == "9:16"
    assert call["seed"] == 42              # D3：显式 seed 原样透传
    assert call["persist_flag"] is True


def test_query_video_params_passthrough():
    mcp = _make_mcp()
    vp = _RecordingVideoPipeline()
    tool.register_tool(mcp, pipeline=None, video_pipeline=vp)
    out = _call_tool(mcp, tool.TOOL_NAME_QUERY, {"task_id": "vid-xyz"})
    assert out == "ok-from-query"
    assert vp.query_calls == [{"task_id": "vid-xyz"}]


# ---------------------------------------------------------------------------
# A2：validate_params / validate_video_params 边界（pipeline 层范围校验）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [
    {},
    {"size": "1024x1024"},
    {"size": "1K"}, {"size": "2K"}, {"size": "3K"}, {"size": "4K"},
    {"size": "2k"},                      # 档位大小写宽容
    {"size": "2048X2048"},               # 大写 X + 4K 档 WxH
    {"size": " 1024x1024 "},             # 首尾空白
    {"size": "2K", "ratio": "1:1"}, {"size": "4K", "ratio": "21:9"},
    {"size": " 2k ", "ratio": " 16:9 "},  # 档位+ratio 双宽容
    {"batch_size": 1},
    {"batch_size": 4},
    {"persist": True},
])
def test_validate_params_accepts_valid(kwargs):
    base = dict(prompt="a cat", size="1024x1024", ratio=None,
                batch_size=1, persist=False)
    base.update(kwargs)
    assert validate_params(**base) == ()


@pytest.mark.parametrize("kwargs, expect_param", [
    ({"batch_size": 0}, "batch_size"),
    ({"batch_size": 5}, "batch_size"),
    ({"size": "abc"}, "size"),
    ({"size": "1024"}, "size"),
    ({"size": "1x1"}, "size"),
    ({"size": "0x0"}, "size"),
    ({"size": "1024x0"}, "size"),
    ({"prompt": ""}, "prompt"),
    ({"prompt": "   "}, "prompt"),
    ({"size": "2K", "ratio": "5:4"}, "ratio"),           # D2：非 8 档
    ({"size": "2K", "ratio": ""}, "ratio"),               # D2：空值
    ({"size": "1024x768", "ratio": "16:9"}, "ratio"),     # D2：WxH 冲突
    ({"size": " 1024x1024 ", "ratio": "16:9"}, "ratio"),  # D2：宽容后仍冲突
])
def test_validate_params_rejects_out_of_range(kwargs, expect_param):
    """越界返回 (参数名, 原因)，供 pipeline 转 [error] invalid_params（A2）。"""
    base = dict(prompt="a cat", size="1024x1024", ratio=None,
                batch_size=1, persist=False)
    base.update(kwargs)
    bad = validate_params(**base)
    assert isinstance(bad, tuple) and len(bad) == 2
    assert bad[0] == expect_param
    assert bad[1]  # 原因非空
