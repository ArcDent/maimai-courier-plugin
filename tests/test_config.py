"""配置模型测试：字段面、默认值与 config.toml 一致性。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

from maimai_courier_plugin.config import (
    AutoSection,
    CourierConfig,
    ForwardGroupSection,
    ScopeSection,
)
from maimai_courier_plugin.plugin import FORWARD_TOOL_NAME, CourierPlugin
from maimai_courier_plugin.source_binding import LIST_FORWARD_FRIENDS_TOOL_NAME
from maimai_courier_plugin.shared.config_rules import onebot_endpoint_rejection

ROOT = Path(__file__).resolve().parent.parent

# 自动分享标准默认原文：config.py 与 config.toml 两处必须逐字一致。
_DEFAULT_SHARE_CRITERIA = (
    "结合消息内容与目标群，自行判断是否值得分享；不值得或无法判断时保持沉默，不必回复，不要编造消息内容。"
)

# 已核实：宿主 Schema 生成器只读这些封闭元数据键，未列出的键不会进入 Schema。
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

# 被排除的配置面：0.5 秒评论延迟与自动去重／等待上限是代码常量，
# 不提供随机概率、冷却或延迟配置项。
_FORBIDDEN_KEY_PARTS = ("delay", "probab", "cooldown", "random", "weight", "ttl")


def _all_keys(config: dict[str, Any], prefix: str = "") -> list[str]:

    keys: list[str] = []
    for key, value in config.items():
        keys.append(f"{prefix}{key}")
        if isinstance(value, dict):
            keys.extend(_all_keys(value, prefix=f"{prefix}{key}."))
    return keys


def test_默认配置字段面限定为开关转发分组自动入口直连与宿主配置版本() -> None:
    config = CourierPlugin.build_default_config()

    assert config["plugin"]["enabled"] is False, "默认总开关必须关闭"
    assert config["auto"]["enabled"] is True, "默认自动入口开启"
    assert config["scope"]["groups"] == []
    assert set(config) == {"plugin", "auto", "scope", "snowluma"}
    # config_version 是宿主强制项，不是本插件的业务开关。
    assert set(config["plugin"]) == {"enabled", "config_version"}
    assert set(config["auto"]) == {"enabled", "message_types", "share_criteria"}
    assert set(config["scope"]) == {"groups"}
    assert config["snowluma"] == {"http_url": "", "token": "", "token_env_var": ""}
    assert config["plugin"]["config_version"] == "1.7.0"
    assert tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8")) == config

    from maimai_courier_plugin.config import SnowLumaSection

    for name, label in (("http_url", "HTTP 地址"), ("token", "连接令牌"), ("token_env_var", "凭证环境变量名")):
        field = getattr(SnowLumaSection, name)
        assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS
        assert field.json_schema_extra["label"] == label
        assert field.json_schema_extra["hint"]


def test_配置版本与仓库样例同步提升为分组结构() -> None:
    assert CourierConfig().plugin.config_version == "1.7.0"
    raw = (ROOT / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)
    assert parsed["plugin"]["config_version"] == "1.7.0"
    assert parsed["scope"] == {"groups": []}, "config.toml 的 [scope] 必须是空分组，注释示例不得生效"
    assert re.search(r"^\s*#\s*\[\[scope\.groups\]\]", raw, re.MULTILINE), "需附注释形态的 [[scope.groups]] 示例"


@pytest.mark.parametrize("value", ["true", "false", "yes", "on", 1, 0, None, ["private"], {"on": True}])
def test_私聊开关非布尔在配置期拒绝且不回显输入(value: Any) -> None:
    # 非布尔（含 TOML 里写成字符串的 "true"、数字 1）必须在配置期明确拒绝：
    # 宿主 Schema 若按宽松归一收下它们，等于让开关值随写法漂移。
    with pytest.raises(ValueError) as caught:
        CourierConfig(scope={"groups": [{"name": "研发", "source_groups": ["111111"],
                                        "target_groups": ["222222"], "private": value}]})
    assert "private" in str(caught.value), "拒绝原因必须点出字段名"
    assert "布尔" in str(caught.value), "拒绝原因必须说明要布尔值"

    plugin = CourierPlugin()
    plugin.set_plugin_config({"scope": {"groups": [{"name": "甲", "source_groups": ["111111"],
                                                  "target_groups": ["222222"]}]}})
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"scope": {"groups": [
            {"name": "研发", "source_groups": ["111111"], "target_groups": ["222222"],
             "private": value}]}})
    assert [group.name for group in plugin.config.scope.groups] == ["甲"], (
        "校验失败不得替换既有合法配置"
    )


def test_私聊开关合法布尔值可保存且默认关闭() -> None:
    config = CourierConfig(scope={"groups": [{"name": "研发", "source_groups": ["111111"],
                                             "target_groups": ["222222"], "private": True}]})
    assert config.scope.groups[0].private is True
    assert CourierConfig().scope.groups == []


def test_分组结构校验拒绝四类非法配置() -> None:
    # 源群跨组重复：拒绝原因必须点出群号与两个组名
    with pytest.raises(ValueError) as cross_group:
        CourierConfig(
            scope={
                "groups": [
                    {"name": "研发", "source_groups": ["111111"], "target_groups": ["222222"]},
                    {"name": "运营", "source_groups": ["111111"], "target_groups": ["333333"]},
                ]
            }
        )
    assert "111111" in str(cross_group.value)
    assert "研发" in str(cross_group.value) and "运营" in str(cross_group.value)

    # 同一分组内源群重复
    with pytest.raises(ValueError) as in_group:
        CourierConfig(
            scope={"groups": [{"name": "研发", "source_groups": ["111111", "111111"], "target_groups": []}]}
        )
    assert "111111" in str(in_group.value)

    # name 缺失或去空白后为空
    with pytest.raises(ValueError) as blank_name:
        CourierConfig(scope={"groups": [{"name": "   ", "source_groups": ["111111"], "target_groups": []}]})
    assert "空白" in str(blank_name.value) and "为空" in str(blank_name.value)
    with pytest.raises(ValueError):
        CourierConfig(scope={"groups": [{"source_groups": ["111111"], "target_groups": []}]})

    # name 跨组重复
    with pytest.raises(ValueError) as duplicate_name:
        CourierConfig(
            scope={
                "groups": [
                    {"name": "研发", "source_groups": ["111111"], "target_groups": []},
                    {"name": "研发", "source_groups": ["222222"], "target_groups": []},
                ]
            }
        )
    assert "研发" in str(duplicate_name.value)


def test_宿主Schema生成器可无参构造分组默认视图且空名仍被拒绝() -> None:
    # 宿主生成配置 Schema 时对嵌套模型无参构造取默认视图：四个字段必须都有默认。
    default_view = ForwardGroupSection()
    assert default_view.model_dump() == {"name": "", "source_groups": [], "target_groups": [],
                                         "private": False}
    # 默认视图落进配置仍按空白 name 拒绝（配置期校验不因默认值放宽）。
    with pytest.raises(ValueError, match="空白"):
        ScopeSection(groups=[default_view])
    with pytest.raises(ValueError, match="空白"):
        CourierConfig(scope={"groups": [{}]})


def test_合法多分组配置可构造且目标群可跨组重复() -> None:
    config = CourierConfig(
        scope={
            "groups": [
                {"name": "研发", "source_groups": ["111111"], "target_groups": ["222222"]},
                {"name": "运营", "source_groups": ["333333"], "target_groups": ["222222", "444444"]},
            ]
        }
    )
    assert [item.model_dump() for item in config.scope.groups] == [
        {"name": "研发", "source_groups": ["111111"], "target_groups": ["222222"], "private": False},
        {"name": "运营", "source_groups": ["333333"], "target_groups": ["222222", "444444"],
         "private": False},
    ]


def test_scope_收敛为单个_groups_字段且组件声明仍完整() -> None:
    assert set(CourierConfig().scope.model_dump()) == {"groups"}

    components = CourierPlugin().get_components()
    types = [component["type"] for component in components]
    assert types.count("TOOL") == 2, "转发工具与只读名册工具两个群聊 Tool 必须保持"
    assert types.count("HOOK_HANDLER") == 2, "来源签发与自动入口两个 Hook 必须保持"
    assert [item["name"] for item in components if item["type"] == "TOOL"] == [
        FORWARD_TOOL_NAME, LIST_FORWARD_FRIENDS_TOOL_NAME]


def test_转发分组结构可经配置构造且界面元数据齐全() -> None:
    from maimai_courier_plugin.config import ForwardGroupSection

    group = ForwardGroupSection(name="研发群", source_groups=["111111"], target_groups=["222222"])
    assert group.model_dump() == {
        "name": "研发群", "source_groups": ["111111"], "target_groups": ["222222"], "private": False}

    config = CourierConfig(
        scope={"groups": [{"name": "研发群", "source_groups": ["111111"], "target_groups": ["222222"]}]}
    )
    assert [item.model_dump() for item in config.scope.groups] == [group.model_dump()]
    assert CourierConfig().scope.groups == []

    for name in ("name", "source_groups", "target_groups", "private"):
        field = getattr(ForwardGroupSection, name)
        assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS, name
        assert field.json_schema_extra["label"], f"{name} 必须有中文标签"
        assert field.json_schema_extra["hint"], f"{name} 必须有提示"
        assert isinstance(field.json_schema_extra.get("order"), int), f"{name} 必须声明界面排序"


def test_转发分组私聊开关默认关闭且声明界面元数据() -> None:
    from maimai_courier_plugin.config import ForwardGroupSection

    # 默认视图可无参构造（宿主 Schema 生成器要求嵌套模型字段全有默认），且默认关闭。
    default_view = ForwardGroupSection()
    assert default_view.private is False, "私聊开关必须默认关闭，升级后既有行为零变化"
    assert default_view.model_dump()["private"] is False
    assert "private" in ForwardGroupSection().model_dump(), "默认视图必须导出 private 键"

    # 显式开启可保存，且不影响同一分组的群目标字段。
    opened = ForwardGroupSection(name="研发群", source_groups=["111111"],
                                 target_groups=["222222"], private=True)
    assert opened.model_dump() == {"name": "研发群", "source_groups": ["111111"],
                                   "target_groups": ["222222"], "private": True}

    field = ForwardGroupSection.private
    assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS
    assert field.json_schema_extra["label"] == "私聊开关"
    assert field.json_schema_extra["hint"]
    assert isinstance(field.json_schema_extra.get("order"), int)
    assert field.json_schema_extra["order"] > ForwardGroupSection.target_groups.json_schema_extra["order"], (
        "私聊开关应排在既有分组字段之后"
    )


def test_自动入口新增字段的默认值与设计一致() -> None:

    config = CourierPlugin.build_default_config()

    assert config["auto"]["message_types"] == ["forward"], "默认不得扩大自动候选类型"
    assert config["auto"]["share_criteria"] == _DEFAULT_SHARE_CRITERIA
    assert config["plugin"]["config_version"] == "1.7.0", "新增字段需提升配置版本以触发宿主归一化写回"
    assert re.fullmatch(r"\d+(\.\d+)*", config["plugin"]["config_version"]), "宿主按数字点分段比较版本"


def test_配置版本因新增分组私聊字段而提升且模型与样例一致() -> None:
    version = CourierConfig().plugin.config_version
    assert tuple(int(part) for part in version.split(".")) > (1, 6, 0), (
        "新增字段必须把配置版本提升到 1.6.0 之上，宿主据此把新字段写回配置文件"
    )
    parsed = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    assert parsed["plugin"]["config_version"] == version, "config.toml 样例必须与模型默认一致"


def test_候选类型单选多选与空列表均可保存() -> None:

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

    plugin = CourierPlugin()
    plugin.set_plugin_config({"auto": {"message_types": ["forward"]}})

    for invalid in (["video"], ["text", "forward", "video"], ["TEXT"]):
        with pytest.raises(ValueError):
            plugin.set_plugin_config({"auto": {"message_types": invalid}})
        assert plugin.config.auto.message_types == ["forward"], "校验失败不得替换既有合法配置"


def test_分享标准空串全空白与多行内容均可保存且不改写() -> None:

    plugin = CourierPlugin()
    for blank in ("", "   ", "\n\t "):
        plugin.set_plugin_config({"auto": {"share_criteria": blank}})
        assert plugin.config.auto.share_criteria == blank

    multiline = "偏好技术话题\n  排除广告与刷屏  "
    plugin.set_plugin_config({"auto": {"share_criteria": multiline}})
    assert plugin.config.auto.share_criteria == multiline


def test_分享标准长度上限4000且超限不静默截断() -> None:

    plugin = CourierPlugin()
    limit = "标" * 4000
    plugin.set_plugin_config({"auto": {"share_criteria": limit}})
    assert plugin.config.auto.share_criteria == limit

    plugin.set_plugin_config({"auto": {"share_criteria": "短标准"}})
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"auto": {"share_criteria": limit + "标"}})
    assert plugin.config.auto.share_criteria == "短标准", "超限配置不得被截断后保存"


def test_旧配置缺少新字段时加载成功并补为默认() -> None:

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
    assert config.scope.groups == [], "旧配置的平铺键按未知键忽略，回落空分组（无源无目标，不发送）"
    assert config.model_dump()["auto"]["message_types"] == ["forward"]


def test_自动入口声明的界面元数据只用已核实键集合() -> None:

    for name in ("enabled", "message_types", "share_criteria"):
        field = getattr(AutoSection, name)
        assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS, name
        assert field.json_schema_extra["label"], f"{name} 必须有中文标签"
        assert field.json_schema_extra["hint"], f"{name} 必须有提示"


def test_自动候选类型声明三个选项且默认仅合并转发() -> None:

    field = AutoSection.message_types
    annotation = get_type_hints(AutoSection)["message_types"]
    assert get_args(get_args(annotation)[0]) == ("text", "image", "forward")
    assert field.get_default() == ["forward"]
    assert field.json_schema_extra["label"] == "自动候选类型"

    hint = field.json_schema_extra["hint"]
    for keyword in ("默认仅合并转发", "留空", "文字", "图片", "模型调用"):
        assert keyword in hint, f"候选类型提示缺少 {keyword}"


def test_自动分享标准声明多行编辑与双写长度上限() -> None:

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
    assert offenders == [], f"配置出现被排除的字段: {offenders}"


def test_config_toml_与配置模型默认值一致() -> None:
    parsed = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    assert parsed == CourierPlugin.build_default_config()


def test_config_toml_分组示例声明私聊开关且与模型默认逐项一致() -> None:
    raw = (ROOT / "config.toml").read_text(encoding="utf-8")
    example: list[str] = []
    in_example = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("# [[scope.groups]]"):
            in_example = True
        if not in_example:
            continue
        if not stripped.startswith("#"):
            break
        example.append(stripped[1:].strip())
    assert example, "config.toml 必须附注释形态的 [[scope.groups]] 示例"

    parsed = tomllib.loads("\n".join(example))
    assert set(parsed) == {"scope"} and len(parsed["scope"]["groups"]) == 1
    sample = parsed["scope"]["groups"][0]
    # 解除注释后必须与配置模型逐项一致：样例漏写 private 这一键就在这里失败。
    assert sample == ForwardGroupSection(**sample).model_dump()
    assert sample["private"] is False, "样例里的私聊开关必须与模型默认一致（默认关闭）"
    assert "合成" not in raw, "config.toml 不得夹带任何真实凭证值"
    assert not any("token" in line for line in example), "分组样例不得夹带任何凭证键"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:3000", "http://localhost:3001/", "http://[::1]:3000/onebot",
        "http://10.11.12.13:3000", "http://[fd00::1]:3000", "http://[fe80::1]:3000",
        "http://maimai-snowluma:3000/onebot", "http://[::ffff:127.0.0.1]:3000",
        "https://snowluma.example", "https://snowluma.example/onebot",
        "https://8.8.8.8:3000", "https://[2606:4700:4700::1111]:3000",
    ],
)
def test_合法HTTP端点写入直连配置(endpoint: str) -> None:

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"http_url": endpoint, "token": "合成令牌"}})
    assert plugin.config.snowluma.http_url == endpoint


def test_HTTP端点键只接受HTTP协议且旧键不再解析() -> None:

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"http_url": "http://127.0.0.1:3000"}})
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"snowluma": {"http_url": "ws://x"}})
    assert plugin.config.snowluma.http_url == "http://127.0.0.1:3000"

    plugin.set_plugin_config({"snowluma": {"websocket_url": "http://127.0.0.1:3000"}})
    assert plugin.config.snowluma.http_url == ""


@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "localhost", "LOCALHOST.",
    "10.0.0.5", "169.254.10.1", "fd00::1", "fe80::1%en0",
])
def test_回环私有链路本地与ULA字面量按可信判定(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from maimai_courier_plugin.shared.config_rules import is_private_host

    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("主机字面量判定不得进行 DNS 查询")

    monkeypatch.setattr("socket.getaddrinfo", forbid_dns)
    assert is_private_host(host) is True


@pytest.mark.parametrize("host", [
    "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8",
    "maimai-snowluma", "snowluma.example", "", "[bad",
])
def test_公网字面量与主机名前缀不按可信判定(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from maimai_courier_plugin.shared.config_rules import is_private_host

    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("主机字面量判定不得进行 DNS 查询")

    monkeypatch.setattr("socket.getaddrinfo", forbid_dns)
    assert is_private_host(host) is False


@pytest.mark.parametrize(
    "endpoint",
    [
        # 明文 http 面向公网一律拒绝（https 不拒）：IPv4 与 IPv6 各一
        "http://8.8.8.8:3000", "http://[2606:4700:4700::1111]:3000",
        # 用户信息、查询串、片段、未指定监听地址（http/https 同口径）
        "http://user:合成秘密@localhost:3000", "https://user:合成秘密@snowluma.example",
        "http://localhost:3000?token=合成秘密", "http://localhost:3000#合成秘密",
        "http://0.0.0.0:3000", "https://[::]:3000",
        # 协议与形态异常：未知协议、ws 协议、缺协议、空主机、端口越界、换行注入、
        # 反斜杠伪装
        "ftp://127.0.0.1:3000", "ws://127.0.0.1:3001", "wss://0.0.0.0./",
        "localhost:3000", "http://",
        "http://localhost:70000", "http://local\nhost:3000", "http://snowluma.example\\@localhost",
        # 非字符串输入
        "未配置", None, 17,
    ],
)
def test_非法端点被拒绝且不回显输入(endpoint: Any) -> None:

    reason = onebot_endpoint_rejection(endpoint)
    assert isinstance(reason, str) and reason, endpoint
    if isinstance(endpoint, str):
        assert endpoint not in reason, "拒绝原因不得回显输入"


@pytest.mark.parametrize("reference", ["", "TEST_SNOWLUMA_TOKEN", "_TOKEN", "A_B_1", "token"])
def test_合法凭证环境变量名可保存(reference: str) -> None:
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"http_url": "http://127.0.0.1:3000", "token_env_var": reference}})
    assert plugin.config.snowluma.token_env_var == reference


@pytest.mark.parametrize("reference", ["9TOKEN", "TOKEN-NAME", "TOKEN NAME", "TOKEN\n合成秘密", "合成秘密", "BAD-TOKEN"])
def test_非法凭证环境变量名在配置期拒绝(reference: str) -> None:
    plugin = CourierPlugin()
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"snowluma": {"http_url": "http://127.0.0.1:3000", "token_env_var": reference}})


def test_配置令牌非空时不要求凭证环境变量名() -> None:

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"http_url": "http://127.0.0.1:3000", "token": "合成令牌"}})
    assert plugin.config.snowluma.token == "合成令牌"
    assert plugin.config.snowluma.token_env_var == ""
    assert plugin.config.model_dump()["snowluma"]["token"] == "合成令牌"


@pytest.mark.parametrize("token", [None, 123, ["synthetic-secret"], {"value": "synthetic-secret"}])
def test_非法配置令牌在配置期拒绝且不回显(token: Any) -> None:
    plugin = CourierPlugin()
    with pytest.raises(ValueError) as caught:
        plugin.set_plugin_config({"snowluma": {"http_url": "http://127.0.0.1:3000", "token": token}})
    assert "synthetic-secret" not in str(caught.value)


def test_旧服务配置退役且保留业务设置不产生直连() -> None:
    old = {
        "plugin": {"enabled": True, "config_version": "1.2.0"},
        "auto": {"enabled": False},
        "scope": {"source_groups": ["111111"], "target_groups": ["222222"]},
        "service": {"endpoint": "http://localhost:8787", "token_env_var": "OLD_TOKEN", "expected_ledger_id": "old-ledger"},
    }
    plugin = CourierPlugin()
    plugin.set_plugin_config(old)
    exported = plugin.config.model_dump()
    assert exported["plugin"] == old["plugin"]
    assert exported["auto"]["enabled"] is False
    assert exported["scope"] == {"groups": []}, "退役的平铺键不进入新字段面"
    assert exported["snowluma"] == {"http_url": "", "token": "", "token_env_var": ""}
    assert "service" not in exported
    assert old["service"]["expected_ledger_id"] == "old-ledger", "不原地清除旧输入或历史线索"