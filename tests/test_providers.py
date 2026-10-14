"""Provider 抽象层门禁：模板渲染、响应抽取、配置校验、持久化。

这一层是「自定义任意 API」能力的地基，全部纯函数 / 纯 IO，无真实网络。
"""
from __future__ import annotations

import json

import pytest

from providers import schema, store as store_mod, template

# ---------------------------------------------------------------------------
# 模板渲染
# ---------------------------------------------------------------------------
def test_interpolation_replaces_named_variable():
    out = template.render_node(
        {"model": "{model}", "prompt": "{prompt}"},
        {"model": "img-1", "prompt": "a cat"})
    assert out == {"model": "img-1", "prompt": "a cat"}


def test_raw_placeholder_keeps_type():
    out = template.render_node({"images": "{{images}}", "n": "{{n}}"},
                               {"images": ["a", "b"], "n": 2})
    assert out == {"images": ["a", "b"], "n": 2}


def test_missing_variable_drops_the_key():
    """变量缺失 → 删除该键，绝不向上游注入 null（多数 API 会 400）。"""
    out = template.render_node({"seed": "{{seed}}", "prompt": "{prompt}"},
                               {"seed": None, "prompt": "hi"})
    assert out == {"prompt": "hi"}


def test_interpolation_inside_longer_string():
    out = template.render_node({"path": "/v1/videos/{task_id}/result"},
                               {"task_id": "vid-9"})
    assert out == {"path": "/v1/videos/vid-9/result"}


def test_nested_structure_rendered_recursively():
    out = template.render_node(
        {"input": {"prompt": "{prompt}", "refs": "{{images}}"}},
        {"prompt": "x", "images": ["u1"]})
    assert out == {"input": {"prompt": "x", "refs": ["u1"]}}


def test_render_mapping_stringifies_values():
    out = template.render_mapping({"n": "{{n}}", "q": "{prompt}"},
                                  {"n": 3, "prompt": "hi"})
    assert out == {"n": "3", "q": "hi"}


def test_bools_render_as_json_literals():
    out = template.render_node({"stream": "{{flag}}"}, {"flag": True})
    assert out == {"stream": True}


# ---------------------------------------------------------------------------
# 响应抽取
# ---------------------------------------------------------------------------
def test_extract_by_path():
    body = {"data": [{"url": "https://a/1.png"}, {"url": "https://a/2.png"}]}
    assert template.extract_list(body, "data[].url") == [
        "https://a/1.png", "https://a/2.png"]


def test_extract_by_index():
    body = {"images": [{"url": "one"}, {"url": "two"}]}
    assert template.extract(body, "images[0].url") == "one"


def test_extract_prefix_dollar_is_optional():
    assert template.extract({"a": {"b": 1}}, "$.a.b") == 1


def test_extract_missing_returns_none():
    assert template.extract({"a": 1}, "x.y") is None


def test_guess_urls_handles_both_shapes():
    assert template.guess_urls({"images": [{"url": "u1"}]}) == ["u1"]
    assert template.guess_urls({"data": [{"url": "u2"}]}) == ["u2"]
    assert template.guess_urls({"url": "u3"}) == ["u3"]
    assert template.guess_urls({"data": [{"b64_json": "AAA"}]}) == ["AAA"]


def test_guess_urls_ignores_junk_and_dedupes():
    body = {"data": [{"url": "u1"}, {"url": "u1"}, {"not_url": "x"}]}
    assert template.guess_urls(body) == ["u1"]


def test_guess_task_id_prefers_video_id():
    assert template.guess_task_id({"video_id": "v1"}) == "v1"
    assert template.guess_task_id({"data": {"id": "v2"}}) == "v2"


def test_guess_status_and_url():
    assert template.guess_status({"status": "running"}) == "running"
    assert template.guess_status({"data": {"state": "done"}}) == "done"
    assert template.guess_url({"url": "https://v/1.mp4"}) == "https://v/1.mp4"


def test_normalize_status_with_map():
    spec = {"SUCCEEDED": "completed"}
    assert template.normalize_status("SUCCEEDED", spec) == "completed"
    assert template.normalize_status("in_progress") == "running"
    assert template.normalize_status("weird-value") == "weird-value"


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------
def test_provider_requires_http_base_url():
    with pytest.raises(schema.SchemaError):
        schema.ProviderConfig(id="p", base_url="ftp://x")
    assert schema.ProviderConfig(
        id="p", base_url="https://x/").base_url == "https://x"


def test_bad_id_rejected():
    for bad in ("", "-x", "有中文", "a" * 65):
        with pytest.raises(schema.SchemaError):
            schema.check_id(bad)


def test_capability_unavailable_when_disabled_or_no_path():
    spec = schema.ProviderConfig(
        id="p", base_url="https://x",
        capabilities={"image": schema.CapabilitySpec(enabled=False)})
    assert spec.has("image") is False
    spec2 = schema.ProviderConfig(id="p2")
    assert spec2.has("image") is False  # 无 base_url


def test_roundtrip_to_from_dict():
    spec = schema.ProviderConfig(
        id="demo", name="Demo", base_url="https://api.example.com",
        auth=schema.AuthSpec(kind=schema.AUTH_BEARER, token="sk-1"),
        capabilities={
            "image": schema.CapabilitySpec(
                model="img-1",
                request=schema.RequestSpec(
                    method="POST", path="/v1/images",
                    body={"prompt": "{prompt}"}),
                extract=schema.ExtractSpec(urls="data[].url")),
        })
    again = schema.ProviderConfig.from_dict(spec.to_dict())
    assert again.to_dict() == spec.to_dict()


def test_preset_only_produces_blank_template():
    """预设只给形状，绝不内置厂商地址或模型名。"""
    cap = schema.preset_capability("openai", schema.CAP_IMAGE)
    assert cap.model == ""
    stub = schema.new_provider_stub("new-one", "new-one", "openai")
    assert stub["base_url"] == ""
    assert stub["auth"]["token"] == ""


def test_auth_kind_validation():
    with pytest.raises(schema.SchemaError):
        schema.AuthSpec(kind="magic")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _store(tmp_path) -> store_mod.Store:
    return store_mod.Store(str(tmp_path / "nested" / "store.json"))


def test_store_bootstrap_is_empty(tmp_path):
    st = _store(tmp_path)
    st.load()
    assert st.providers == [] and st.tokens == []
    assert st.admin_ready is False


def test_store_save_is_atomic_and_reloadable(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.upsert_provider(schema.ProviderConfig(id="a", base_url="https://a"))
    st.upsert_provider(schema.ProviderConfig(id="b", base_url="https://b"))
    again = store_mod.Store(st.path)
    again.load()
    assert [p.id for p in again.providers] == ["a", "b"]


def test_store_delete_unbinds_token(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.upsert_provider(schema.ProviderConfig(id="a", base_url="https://a"))
    st.upsert_token(schema.TokenRecord(id="t", token="tt", default_provider="a"))
    assert st.delete_provider("a") is True
    assert st.get_token("t").default_provider == ""


def test_password_hash_and_verify(tmp_path):
    st = _store(tmp_path)
    st.load()
    with pytest.raises(store_mod.StoreError):
        st.set_admin_password("123")
    st.set_admin_password("correct horse")
    assert st.check_admin_password("correct horse") is True
    assert st.check_admin_password("wrong") is False
    # 哈希不可反推：原文不出现在落盘内容里
    raw = (tmp_path / "nested" / "store.json").read_text(encoding="utf-8")
    assert "correct horse" not in raw


def test_bootstrap_token_only_once(tmp_path):
    st = _store(tmp_path)
    st.load()
    first = st.bootstrap_token()
    assert first is not None and first.token
    assert st.bootstrap_token() is None  # 已有令牌 → 不重复签发


def test_find_token_by_secret_is_constant_time_safe(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.upsert_token(schema.TokenRecord(id="t1", token="secret-abc"))
    assert st.find_token_by_secret("secret-abc").id == "t1"
    assert st.find_token_by_secret("secret-ab") is None
    assert st.find_token_by_secret("") is None


def test_settings_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.set_settings({"image_max_concurrency": 9})
    assert st.get_setting("image_max_concurrency") == 9
    again = store_mod.Store(st.path)
    again.load()
    assert again.get_setting("image_max_concurrency") == 9
