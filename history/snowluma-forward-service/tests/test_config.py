"""配置模型测试：字段面、默认值与 config.toml 一致性。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

from config import AutoSection
from plugin import CourierPlugin

ROOT = Path(__file__).resolve().parent.parent

# design D2 的自动分享标准默认原文：config.py 与 config.toml 两处必须逐字一致。
_DEFAULT_SHARE_CRITERIA = (
    "结合消息内容与目标群，自行判断是否值得分享；不值得或无法判断时保持沉默，不必回复，不要编造消息内容。"
)

# tasks 1.3 已核实：宿主 Schema 生成器只读这些封闭元数据键，未列出的键不会进入 Schema。
_CONSUMED_SCHEMA_KEYS = frozenset(
    {
        "label",
        "hint",
        "placeholder",
        "x-widget",
        "x-icon",
        "order",
        "rows",
        "max_length",
        "min_items",
        "max_items",
        "step",
        "pattern",
        "hidden",
        "disabled",
        "i18n",
    }
)

# design D1 排除的配置面：0.5 秒评论延迟与自动去重／等待上限是代码常量，
# 不提供随机概率、冷却或延迟配置项。
_FORBIDDEN_KEY_PARTS = ("delay", "probab", "cooldown", "random", "weight", "ttl")


def _all_keys(config: dict[str, Any], prefix: str = "") -> list[str]:
    """递归收集配置的全部字段名（嵌套 section 带父级前缀）。"""

    keys: list[str] = []
    for key, value in config.items():
        keys.append(f"{prefix}{key}")
        if isinstance(value, dict):
            keys.extend(_all_keys(value, prefix=f"{prefix}{key}."))
    return keys


def test_默认配置字段面限定为开关白名单自动入口服务连接与宿主配置版本() -> None:
    config = CourierPlugin.build_default_config()

    assert config["plugin"]["enabled"] is False, "默认总开关必须关闭"
    assert config["auto"]["enabled"] is True, "默认自动入口开启"
    assert config["scope"]["source_groups"] == []
    assert config["scope"]["target_groups"] == []
    assert set(config) == {"plugin", "auto", "scope", "service"}
    # config_version 是宿主强制项：SDK extract_plugin_config_version 缺它即抛
    # PluginConfigVersionError，不是本插件的业务开关。
    assert set(config["plugin"]) == {"enabled", "config_version"}
    assert set(config["auto"]) == {"enabled", "message_types", "share_criteria"}
    assert set(config["scope"]) == {"source_groups", "target_groups"}
    # 转发服务节只保存连接面与账本绑定标识：凭证只填环境变量名，不填凭证值。
    assert set(config["service"]) == {
        "endpoint",
        "client_identity",
        "token_env_var",
        "expected_ledger_id",
        "migration_confirmation",
    }


def test_自动入口新增字段的默认值与设计一致() -> None:
    """design D2：候选类型默认仅合并转发；标准默认原文逐字一致；版本为数字点分。"""

    config = CourierPlugin.build_default_config()

    assert config["auto"]["message_types"] == ["forward"], "默认不得扩大自动候选类型"
    assert config["auto"]["share_criteria"] == _DEFAULT_SHARE_CRITERIA
    assert config["plugin"]["config_version"] == "1.2.0", "新增字段需提升配置版本以触发宿主归一化写回"
    assert re.fullmatch(r"\d+(\.\d+)*", config["plugin"]["config_version"]), "宿主按数字点分段比较版本"


def test_候选类型单选多选与空列表均可保存() -> None:
    """`text`／`image`／`forward` 的单选、多选与空列表都合法（空 = 不再提供自动机会）。"""

    plugin = CourierPlugin()
    selections = (
        [],
        ["text"],
        ["image"],
        ["forward"],
        ["text", "image"],
        ["text", "image", "forward"],
    )
    for selection in selections:
        plugin.set_plugin_config({"auto": {"message_types": list(selection)}})
        assert plugin.config.auto.message_types == selection


def test_未知候选类型被明确拒绝且不扩大触发范围() -> None:
    """未知枚举必须报配置校验错误，不得解释为全选，也不得静默丢弃后保存。"""

    plugin = CourierPlugin()
    plugin.set_plugin_config({"auto": {"message_types": ["forward"]}})

    for invalid in (["video"], ["text", "forward", "video"], ["TEXT"]):
        with pytest.raises(ValueError):
            plugin.set_plugin_config({"auto": {"message_types": invalid}})
        assert plugin.config.auto.message_types == ["forward"], "校验失败不得替换既有合法配置"


def test_分享标准空串全空白与多行内容均可保存且不改写() -> None:
    """空串／全空白合法（由自动入口在 5.2 另验回退默认），非空内容与换行原样保留。"""

    plugin = CourierPlugin()
    for blank in ("", "   ", "\n\t "):
        plugin.set_plugin_config({"auto": {"share_criteria": blank}})
        assert plugin.config.auto.share_criteria == blank

    multiline = "偏好技术话题\n  排除广告与刷屏  "
    plugin.set_plugin_config({"auto": {"share_criteria": multiline}})
    assert plugin.config.auto.share_criteria == multiline


def test_分享标准长度上限4000且超限不静默截断() -> None:
    """4000 字符可保存；4001 字符报校验错误，且不得保存截断后的内容。"""

    plugin = CourierPlugin()
    limit = "标" * 4000
    plugin.set_plugin_config({"auto": {"share_criteria": limit}})
    assert plugin.config.auto.share_criteria == limit

    plugin.set_plugin_config({"auto": {"share_criteria": "短标准"}})
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"auto": {"share_criteria": limit + "标"}})
    assert plugin.config.auto.share_criteria == "短标准", "超限配置不得被截断后保存"


def test_旧配置缺少新字段时加载成功并补为默认() -> None:
    """旧配置（无候选类型／分享标准）加载：新字段补默认，旧开关、白名单与版本原样保留。

    对照证据（tasks 1.4）：宿主 `merge_plugin_config_data` / `_fill_missing_fields`
    为缺失字段补默认；`plugin.config_version` 是宿主强制字段，保留在模型中不因测试
    移除，旧版本值在内存配置里原样可见（是否写回由宿主归一化决定）。
    """

    plugin = CourierPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "auto": {"enabled": False},
            "scope": {"source_groups": ["111111"], "target_groups": ["222222"]},
        }
    )

    config = plugin.config
    assert config.plugin.enabled is True, "旧总开关不得被覆盖"
    assert config.plugin.config_version == "1.0.0", "旧配置版本原样保留"
    assert config.auto.enabled is False, "旧自动入口开关不得被覆盖"
    assert config.auto.message_types == ["forward"], "旧配置不得因此扩大自动候选类型"
    assert config.auto.share_criteria == _DEFAULT_SHARE_CRITERIA
    assert config.scope.source_groups == ["111111"]
    assert config.scope.target_groups == ["222222"]
    assert config.model_dump()["auto"]["message_types"] == ["forward"]


def test_自动入口声明的界面元数据只用已核实键集合() -> None:
    """声明只使用 tasks 1.3 核实的封闭键，不发明未在核实记录中出现的 UI 元数据。

    证据等级：宿主 Schema 键集合为源码核实，这里的断言是对**配置声明**的核验，
    不等于真实浏览器渲染已验证（本组不部署、不开 WebUI）。
    """

    for name in ("enabled", "message_types", "share_criteria"):
        field = getattr(AutoSection, name)
        assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS, name
        assert field.json_schema_extra["label"], f"{name} 必须有中文标签"
        assert field.json_schema_extra["hint"], f"{name} 必须有提示"


def test_自动候选类型声明三个选项且默认仅合并转发() -> None:
    """多选枚举来自注解 `Literal`；标签与提示覆盖默认、空列表与调用量语义。"""

    field = AutoSection.message_types
    annotation = get_type_hints(AutoSection)["message_types"]
    assert get_args(get_args(annotation)[0]) == ("text", "image", "forward")
    assert field.get_default() == ["forward"]
    assert field.json_schema_extra["label"] == "自动候选类型"

    hint = field.json_schema_extra["hint"]
    for keyword in ("默认仅合并转发", "留空", "文字", "图片", "模型调用"):
        assert keyword in hint, f"候选类型提示缺少 {keyword}"


def test_自动分享标准声明多行编辑与双写长度上限() -> None:
    """多行文本用宿主既有的 textarea 取值；4000 上限同时写 pydantic 约束与 Schema 提示。"""

    field = AutoSection.share_criteria
    assert field.json_schema_extra["label"] == "自动分享标准"
    assert field.json_schema_extra["label"] != AutoSection.message_types.json_schema_extra["label"]
    assert field.json_schema_extra["x-widget"] == "textarea"
    assert field.json_schema_extra["rows"] >= 2, "多行编辑至少两行"
    assert field.max_length == 4000, "后端必须按 pydantic 约束拦下超长输入"
    assert field.json_schema_extra["max_length"] == 4000, "Schema 必须向界面提示上限"

    hint = field.json_schema_extra["hint"]
    for keyword in ("空白", "默认", "4000"):
        assert keyword in hint, f"分享标准提示缺少 {keyword}"


def test_默认配置不含延迟概率与冷却字段() -> None:
    keys = _all_keys(CourierPlugin.build_default_config())
    assert "plugin.enabled" in keys, "字段遍历必须真的读到配置字段"
    offenders = [key for key in keys if any(part in key.lower() for part in _FORBIDDEN_KEY_PARTS)]
    assert offenders == [], f"配置出现 design D1 排除的字段: {offenders}"


def test_config_toml_与配置模型默认值一致() -> None:
    parsed = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    assert parsed == CourierPlugin.build_default_config()
