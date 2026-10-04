"""配置模型测试：字段面、默认值与 config.toml 一致性。"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
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


def test_默认配置字段面限定为开关白名单自动入口直连与宿主配置版本() -> None:
    config = CourierPlugin.build_default_config()

    assert config["plugin"]["enabled"] is False, "默认总开关必须关闭"
    assert config["auto"]["enabled"] is True, "默认自动入口开启"
    assert config["scope"]["source_groups"] == []
    assert config["scope"]["target_groups"] == []
    assert set(config) == {"plugin", "auto", "scope", "snowluma"}
    # config_version 是宿主强制项，不是本插件的业务开关。
    assert set(config["plugin"]) == {"enabled", "config_version"}
    assert set(config["auto"]) == {"enabled", "message_types", "share_criteria"}
    assert set(config["scope"]) == {"source_groups", "target_groups"}
    assert config["snowluma"] == {"websocket_url": "", "token": "", "token_env_var": ""}
    assert config["plugin"]["config_version"] == "1.4.0"
    assert tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8")) == config

    from config import SnowLumaSection

    for name, label in (("websocket_url", "WebSocket 地址"), ("token", "连接令牌"), ("token_env_var", "凭证环境变量名")):
        field = getattr(SnowLumaSection, name)
        assert set(field.json_schema_extra) <= _CONSUMED_SCHEMA_KEYS
        assert field.json_schema_extra["label"] == label
        assert field.json_schema_extra["hint"]


def test_自动入口新增字段的默认值与设计一致() -> None:
    """design D2：候选类型默认仅合并转发；标准默认原文逐字一致；版本为数字点分。"""

    config = CourierPlugin.build_default_config()

    assert config["auto"]["message_types"] == ["forward"], "默认不得扩大自动候选类型"
    assert config["auto"]["share_criteria"] == _DEFAULT_SHARE_CRITERIA
    assert config["plugin"]["config_version"] == "1.4.0", "新增字段需提升配置版本以触发宿主归一化写回"
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


@pytest.mark.parametrize(
    "endpoint",
    ["ws://127.0.0.1:3001", "ws://localhost:3001/", "ws://[::1]:3001/onebot", "ws://127.0.0.2:3001",
     "wss://snowluma.example/onebot", "ws://10.11.12.13:3001", "ws://172.16.0.9:3001",
     "ws://[fd00::1]:3001", "ws://[fe80::1]:3001", "ws://maimai-snowluma:3001/onebot",
     "ws://[::ffff:127.0.0.1]:3001"],
)
def test_合法直连地址仅按字面量解析(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotConnection, resolve_onebot_connection

    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("配置解析不得进行 DNS 查询")

    monkeypatch.setattr("socket.getaddrinfo", forbid_dns)
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": endpoint, "token_env_var": "TEST_SNOWLUMA_TOKEN"}})
    result = resolve_onebot_connection(plugin.config, {"TEST_SNOWLUMA_TOKEN": "合成凭证"})
    assert isinstance(result, OneBotConnection)
    assert result.endpoint == endpoint
    assert result.token_env_var == "TEST_SNOWLUMA_TOKEN"


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST.",
    "10.0.0.5", "172.16.0.9", "192.168.1.5", "169.254.10.1",
    "fd00::1", "[fd00::1]", "fe80::1", "fe80::1%en0",
    "::ffff:192.168.1.5", "::ffff:127.0.0.1",
])
def test_回环私有链路本地与ULA字面量按可信判定(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config_rules import is_private_host

    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("主机字面量判定不得进行 DNS 查询")

    monkeypatch.setattr("socket.getaddrinfo", forbid_dns)
    assert is_private_host(host) is True


@pytest.mark.parametrize("host", [
    "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8",
    "maimai-snowluma", "snowluma.example", "", "[bad",
])
def test_公网字面量与主机名前缀不按可信判定(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config_rules import is_private_host

    def forbid_dns(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("主机字面量判定不得进行 DNS 查询")

    monkeypatch.setattr("socket.getaddrinfo", forbid_dns)
    assert is_private_host(host) is False


@pytest.mark.parametrize(
    "endpoint",
    ["ws://8.8.8.8:3001", "ws://1.1.1.1:3001",
     "ws://[2606:4700:4700::1111]:3001", "ws://[::ffff:8.8.8.8]:3001"],
)
def test_公网IP字面量明文在配置期拒绝(endpoint: str) -> None:
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001"}})
    with pytest.raises(ValueError) as error:
        plugin.set_plugin_config({"snowluma": {"websocket_url": endpoint}})
    assert endpoint not in str(error.value)
    assert plugin.config.snowluma.websocket_url == "ws://localhost:3001"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:3001", "https://snowluma.example",
        "wss://user:合成秘密@snowluma.example", "ws://localhost?token=合成秘密",
        "wss://snowluma.example#合成秘密", "ws://0.0.0.0:3001", "wss://[::]:3001",
        "wss://0.0.0.0./", "wss://:3001", "ws://[bad", "wss://localhost:70000",
        "ws://local\nhost:3001", "wss://snowluma.example\\@localhost", "wss://localhost:0",
        "wss://0x0", "wss://[::ffff:0.0.0.0]:3001",
    ],
)
def test_不可信直连地址被安全拒绝且不替换合法配置(endpoint: str) -> None:
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001"}})
    with pytest.raises(ValueError) as error:
        plugin.set_plugin_config({"snowluma": {"websocket_url": endpoint}})
    assert endpoint not in str(error.value)
    assert "合成秘密" not in repr(error.value)
    assert plugin.config.snowluma.websocket_url == "ws://localhost:3001"


def test_未配置地址返回明确诊断而不是连接描述() -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    result = resolve_onebot_connection(CourierPlugin.config_model(), {})
    assert isinstance(result, OneBotUnresolved)
    assert "未配置" in result.reason
    assert result.config_failure is False


@pytest.mark.parametrize("reference", ["", " ", "9TOKEN", "TOKEN-NAME", "TOKEN\n合成秘密"])
def test_凭证引用未填写或非法时解析只给安全原因(reference: str) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    result = resolve_onebot_connection(
        {"snowluma": {"websocket_url": "ws://localhost:3001", "token_env_var": reference}},
        {},
    )
    assert isinstance(result, OneBotUnresolved)
    assert result.config_failure is True
    assert "凭证环境变量名" in result.reason
    assert "合成秘密" not in repr(result)


class ReadForbiddenEnvironment(Mapping[str, str]):
    """包括 Mapping 默认成员判断在内，解析期任何取值都不得发生。"""

    def __init__(self, **values: str) -> None:
        self.values_by_name = values

    def __iter__(self) -> Iterator[str]:
        return iter(self.values_by_name)

    def __len__(self) -> int:
        return len(self.values_by_name)

    def __getitem__(self, key: str) -> str:
        pytest.fail("配置解析不得读取凭证值")


@pytest.mark.parametrize(
    "reference",
    ["BAD-TOKEN", "9TOKEN", "TOKEN NAME", "TOKEN\n合成秘密", "合成秘密", None, True, 17, ["TOKEN"], {"TOKEN": "合成秘密"}],
)
def test_公开配置赋值后非法凭证引用仍被解析入口安全拒绝(
    reference: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """字母或下划线开头且只含字母数字下划线；赋值不能绕过这一文法。"""

    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001", "token_env_var": "TEST_SNOWLUMA_TOKEN"}})
    plugin.config.snowluma.token_env_var = reference
    secret = "仅合成环境中的假凭证"
    environ = ReadForbiddenEnvironment(**{
        "TEST_SNOWLUMA_TOKEN": secret,
        "BAD-TOKEN": secret,
        "9TOKEN": secret,
        "TOKEN NAME": secret,
        "TOKEN\n合成秘密": secret,
        "合成秘密": secret,
    })

    result = resolve_onebot_connection(plugin.config, environ)

    assert isinstance(result, OneBotUnresolved)
    assert result.config_failure is True
    assert result.reason == "凭证环境变量名非法（只允许字母、数字与下划线，且不以数字开头）"
    assert "合成秘密" not in repr(result) + caplog.text
    assert secret not in repr(result) + repr(plugin.config.model_dump()) + repr(plugin.config) + caplog.text


def test_凭证仅检查引用存在且不读取或暴露环境值(caplog: pytest.LogCaptureFixture) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotConnection, OneBotUnresolved, resolve_onebot_connection

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001", "token_env_var": "TEST_SNOWLUMA_TOKEN"}})
    missing = resolve_onebot_connection(plugin.config, ReadForbiddenEnvironment())
    assert isinstance(missing, OneBotUnresolved)
    assert "未设置" in missing.reason

    secret = "仅合成环境中的假凭证"
    connection = resolve_onebot_connection(plugin.config, ReadForbiddenEnvironment(TEST_SNOWLUMA_TOKEN=secret))
    assert isinstance(connection, OneBotConnection)
    assert connection.token_env_var == "TEST_SNOWLUMA_TOKEN"
    assert secret not in repr(connection)
    assert secret not in repr(plugin.config.model_dump())
    assert secret not in repr(plugin.config)
    assert secret not in caplog.text


@pytest.mark.parametrize("reference", ["", "TEST_SNOWLUMA_TOKEN"])
def test_配置令牌可保存且优先于环境变量(reference: str, caplog: pytest.LogCaptureFixture) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotConnection, resolve_onebot_connection

    secret = "synthetic-config-token"
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {
        "websocket_url": "ws://localhost:3001", "token": secret, "token_env_var": reference,
    }})
    result = resolve_onebot_connection(plugin.config, ReadForbiddenEnvironment(TEST_SNOWLUMA_TOKEN="unused"))
    assert isinstance(result, OneBotConnection)
    assert result.token == secret
    assert plugin.config.model_dump()["snowluma"]["token"] == secret
    assert secret not in repr(result) + repr(plugin.config) + caplog.text


@pytest.mark.parametrize("token", ["", "  \t\n"])
def test_配置令牌留空时仍只检查环境变量名(token: str) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotConnection, resolve_onebot_connection

    result = resolve_onebot_connection({"snowluma": {
        "websocket_url": "ws://localhost:3001", "token": token, "token_env_var": "TEST_SNOWLUMA_TOKEN",
    }}, ReadForbiddenEnvironment(TEST_SNOWLUMA_TOKEN="synthetic-env-token"))
    assert isinstance(result, OneBotConnection)
    assert result.token == "" and result.token_env_var == "TEST_SNOWLUMA_TOKEN"


@pytest.mark.parametrize("token", [None, 123, ["synthetic-secret"], {"value": "synthetic-secret"}])
def test_非法配置令牌只返回安全原因(token: Any) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    config = {"snowluma": {"websocket_url": "ws://localhost:3001", "token": token}}
    with pytest.raises(ValueError) as caught:
        CourierPlugin().set_plugin_config(config)
    assert "synthetic-secret" not in str(caught.value)
    result = resolve_onebot_connection(config, {})
    assert isinstance(result, OneBotUnresolved) and result.config_failure
    assert "synthetic-secret" not in repr(result)
    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001"}})
    plugin.config.snowluma.token = token
    result = resolve_onebot_connection(plugin.config, {})
    assert isinstance(result, OneBotUnresolved) and result.config_failure
    assert "synthetic-secret" not in repr(result)


def test_地址解析不通过时不读取凭证环境值() -> None:
    """9.2 拒绝路径：端点先被判定拒绝，解析入口不得发生任何环境取值（含 Mapping 默认成员判断）。"""

    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    plugin = CourierPlugin()
    plugin.set_plugin_config({"snowluma": {"websocket_url": "ws://localhost:3001", "token_env_var": "TEST_SNOWLUMA_TOKEN"}})
    # 绕过配置期拒绝、模拟公开赋值后再解析的路径；解析入口必须自行拦截。
    plugin.config.snowluma.websocket_url = "ws://8.8.8.8:3001"
    secret = "仅合成环境中的假凭证"
    environ = ReadForbiddenEnvironment(TEST_SNOWLUMA_TOKEN=secret)

    result = resolve_onebot_connection(plugin.config, environ)

    assert isinstance(result, OneBotUnresolved)
    assert result.config_failure is True
    assert "wss" in result.reason
    assert secret not in repr(result)


def test_旧服务配置退役且保留业务设置不产生直连() -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnresolved, resolve_onebot_connection

    old = {
        "plugin": {"enabled": True, "config_version": "1.2.0"},
        "auto": {"enabled": False},
        "scope": {"source_groups": ["111111"], "target_groups": ["222222"]},
        "service": {"endpoint": "ws://localhost:8787", "token_env_var": "OLD_TOKEN", "expected_ledger_id": "old-ledger"},
    }
    plugin = CourierPlugin()
    plugin.set_plugin_config(old)
    result = resolve_onebot_connection(plugin.config, {"OLD_TOKEN": "旧合成凭证"})
    assert isinstance(result, OneBotUnresolved)
    assert "未配置" in result.reason and "退役" in result.reason
    exported = plugin.config.model_dump()
    assert exported["plugin"] == old["plugin"]
    assert exported["auto"]["enabled"] is False
    assert exported["scope"] == old["scope"]
    assert exported["snowluma"] == {"websocket_url": "", "token": "", "token_env_var": ""}
    assert "service" not in exported
    assert old["service"]["expected_ledger_id"] == "old-ledger", "不原地清除旧输入或历史线索"
