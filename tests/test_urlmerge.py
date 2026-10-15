"""出站 URL 拼接：用户粘「完整端点 URL」时不能拼出重复路径。

回归背景（真机实测）：
    面板接口地址填 ``https://api.agnes-ai.cn/v1/videos``，模板 path 是
    ``/v1/videos`` → 实际打到 ``/v1/videos/v1/videos/query``，上游 404。
"""
from __future__ import annotations

import pytest

from providers import urlmerge


# ---------------------------------------------------------------------------
# merge_paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,req,want", [
    # 只给域名 → 直接接
    ("", "/v1/videos", "/v1/videos"),
    ("/", "/v1/videos", "/v1/videos"),
    # 完整端点 URL（用户最常粘的形态）→ 重合段只留一份
    ("/v1/videos", "/v1/videos", "/v1/videos"),
    ("/v1/videos", "/v1/videos/query", "/v1/videos/query"),
    ("/v1/images/generations", "/v1/images/generations",
     "/v1/images/generations"),
    # 带前缀的网关地址 → 前缀保留，重合段去掉
    ("/openai/v1", "/v1/chat/completions", "/openai/v1/chat/completions"),
    # 完全不重合 → 老实接上（不吞任何一段）
    ("/api", "/v1/models", "/api/v1/models"),
    ("/v1/videos", "/v2/videos", "/v1/videos/v2/videos"),
    # 尾斜杠 / 多斜杠不影响判定
    ("/v1/videos/", "/v1/videos/query", "/v1/videos/query"),
    ("//v1//videos", "/v1/videos", "/v1/videos"),
    # 只重合一段
    ("/v1", "/v1", "/v1"),
    ("/v1", "/v1/x", "/v1/x"),
])
def test_merge_paths(base, req, want):
    assert urlmerge.merge_paths(base, req) == want


# ---------------------------------------------------------------------------
# resolve_url
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,path,want", [
    ("https://api.example.cn", "/v1/videos",
     "https://api.example.cn/v1/videos"),
    ("https://api.example.cn/", "/v1/videos",
     "https://api.example.cn/v1/videos"),
    # 用户粘的完整端点 URL —— 本次修复的核心场景
    ("https://api.agnes-ai.cn/v1/videos", "/v1/videos",
     "https://api.agnes-ai.cn/v1/videos"),
    ("https://api.agnes-ai.cn/v1/videos", "/v1/videos/query",
     "https://api.agnes-ai.cn/v1/videos/query"),
    ("https://api.agnes-ai.cn/v1/images/generations",
     "/v1/images/generations",
     "https://api.agnes-ai.cn/v1/images/generations"),
    # 带端口 / 带前缀
    ("http://127.0.0.1:8100/v1", "/v1/videos",
     "http://127.0.0.1:8100/v1/videos"),
    ("https://gw.example.cn/openai/v1", "/v1/chat/completions",
     "https://gw.example.cn/openai/v1/chat/completions"),
    # path 自带查询串 → 保留
    ("https://h/v1", "/v1/videos?x=1", "https://h/v1/videos?x=1"),
    # path 已是绝对 URL → 原样（模板里写死整条地址的用法）
    ("https://h/v1", "https://other/v1/videos", "https://other/v1/videos"),
    # 空 path
    ("https://h/v1", "", "https://h/v1"),
])
def test_resolve_url(base, path, want):
    assert urlmerge.resolve_url(base, path) == want


def test_resolve_url_never_doubles_tail():
    """一句话回归：任何重合写法都不该出现两遍相同段落。"""
    for seg in ("/v1/videos", "/v1/images/generations", "/v1/videos/query"):
        got = urlmerge.resolve_url(f"https://h{seg}", seg)
        assert got.count(seg) == 1, got


def test_resolve_url_without_base_keeps_path():
    assert urlmerge.resolve_url("", "/v1/videos") == "/v1/videos"
    assert urlmerge.resolve_url("", "") == "/"


# ---------------------------------------------------------------------------
# is_endpoint_suffix：区分「重复端点」与「网关前缀」
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,cands,want", [
    ("/v1/videos", ["/v1/videos"], True),
    ("/v1/images/generations", ["/v1/images/generations"], True),
    ("/v1/videos", ["/v1/videos", "/v1/images/generations"], True),
    # 兄弟端点之间不算重复（/agnesapi 与 /v1/videos 是兄弟）
    ("/v1/videos", ["/agnesapi"], False),
    # 网关前缀不能当成端点丢掉
    ("/openai/v1", ["/v1/chat/completions"], False),
    ("/v1", ["/v1/images/generations"], False),
    ("", ["/v1/videos"], False),
    ("/x", [], False),
])
def test_is_endpoint_suffix(base, cands, want):
    assert urlmerge.is_endpoint_suffix(base, cands) is want


def test_split_endpoint_basics():
    assert urlmerge.split_endpoint("https://h/v1/videos") == ("https://h", "/v1/videos")
    assert urlmerge.split_endpoint("https://h") == ("https://h", "")
    assert urlmerge.split_endpoint("https://h/") == ("https://h", "")
    assert urlmerge.split_endpoint("https://h/v1/x?a=1") == ("https://h", "/v1/x")
    assert urlmerge.split_endpoint("") == ("", "")
