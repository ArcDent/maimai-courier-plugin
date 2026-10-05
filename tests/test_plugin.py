"""组合根与组件注册：公开工厂、三件组件声明、默认关闭不发送与 HTTP 装配回归。

断言面只经插件公共入口：`plugin.create_plugin`（宿主无参工厂，测试经公开参数
注入依赖）与 `plugin.get_components`（Runner 同款组件声明入口）。覆盖：
- 5.2 注册回归：一个群聊 Tool + 两个 Hook、`chat_scope` 顶层修正、hook 模式为
  字符串字面量；
- 5.1 装配点：无参创建成功；默认关闭与缺直连配置都不构造传输；`http_url` 解析
  出的连接交给传输工厂，全链卡片投递经内存传输成功（不触真实网络、不调用宿主
  旧发送通道）。

隔离与收集守卫、项目规则守卫在 `tests/test_repo_isolation.py`；替身设施：
`tests/support` 的公共样本与装配助手 + `tests/direct_support` 的内存平台装配，
账本用真实 SQLite 账本（组件自己的持久权威）。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from maimai_courier_plugin.plugin import (
    PLANNER_AFTER_RESPONSE_HOOK,
    RECEIVE_AFTER_PROCESS_HOOK,
    CourierPlugin,
    create_plugin,
)
from maimai_courier_plugin.shared.delivery import STATUS_SENT
from tests.direct_support import DEFAULT_RESOURCE_ID, DirectFixture, merged_forward_message
from tests.snowluma_client_support import TARGET_MESSAGE_ID
from tests.support import (
    HTTP_URL,
    TARGET_GROUP_ID,
    FakeClock,
    components_of,
    find_hook,
    issue_source_call,
    make_plugin,
)

ROOT = Path(__file__).resolve().parent.parent


def find_component(components: list[dict[str, Any]], component_type: str, name: str) -> dict[str, Any]:

    for component in components:
        if component.get("type") == component_type and component.get("name") == name:
            return component
    raise AssertionError(f"未注册组件 type={component_type} name={name}；实际声明={components}")


def find_tool(plugin: Any, name: str = "forward_message") -> dict[str, Any]:

    return find_component(components_of(plugin), "TOOL", name)


# --------------------------------------------------------------------------- 组件注册


def test_工厂可创建插件并注册群聊工具与观察Hook() -> None:
    plugin = create_plugin()
    assert isinstance(plugin, CourierPlugin)

    tool = find_tool(plugin, "forward_message")
    assert tool["chat_scope"] == "group"
    assert "chat_scope" not in tool["metadata"], "chat_scope 只保留在组件声明顶层"

    hook = find_hook(plugin, RECEIVE_AFTER_PROCESS_HOOK)
    assert hook["metadata"]["mode"] == "observe"
    assert hook["metadata"]["handler_name"] == "observe_received_message"


def test_组件声明为三件且hook模式为字符串字面量() -> None:

    plugin = create_plugin()
    components = components_of(plugin)
    tools = [component for component in components if component.get("type") == "TOOL"]
    hooks = [component for component in components if component.get("type") == "HOOK_HANDLER"]
    assert len(components) == 3, components
    assert [tool["name"] for tool in tools] == ["forward_message"]
    assert tools[0]["chat_scope"] == "group"
    assert "chat_scope" not in tools[0]["metadata"]
    assert set(tools[0]["metadata"]["parameters_raw"]) == {"type", "properties", "required"}
    assert {hook["metadata"]["hook"] for hook in hooks} == {
        PLANNER_AFTER_RESPONSE_HOOK, RECEIVE_AFTER_PROCESS_HOOK}
    for hook in hooks:
        mode = hook["metadata"]["mode"]
        assert type(mode) is str, "HookMode 枚举不在 SDK 根导出面，必须用字符串字面量声明"
        assert mode in ("observe", "blocking")
    binding = find_hook(plugin, PLANNER_AFTER_RESPONSE_HOOK)
    assert binding["metadata"]["mode"] == "blocking"
    assert binding["metadata"]["order"] == "early"


def test_三生命周期名称与宿主调用契约一致(tmp_path: Path) -> None:
    plugin, _ = make_plugin(tmp_path)
    for name in ("on_load", "on_unload", "on_config_update"):
        assert inspect.iscoroutinefunction(getattr(plugin, name)), f"{name} 必须是 async 生命周期"

    asyncio.run(plugin.on_load())
    asyncio.run(plugin.on_config_update("self", {}, ""))
    asyncio.run(plugin.on_unload())


# --------------------------------------------------------------------------- 5.1 装配与默认关闭


def test_默认总开关关闭时工具调用不发送(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path)

    result = asyncio.run(
        plugin.forward_message(message_id="m-forward-1", target_group_id="20002", comment="看看这个")
    )

    assert result["success"] is False
    assert "总开关" in result["reason"]
    assert ctx.send.calls == [], "默认关闭时不得产生任何发送"


def test_默认总开关关闭时不构造传输且仍注册三组件(tmp_path: Path) -> None:
    constructed: list[Any] = []
    plugin, ctx = make_plugin(tmp_path,
                              transport_factory=lambda connection: constructed.append(connection))

    result = asyncio.run(plugin.forward_message(message_id="m-forward-1", target_group_id="222"))

    assert result["status"] == "rejected"
    assert "总开关" in result["reason"]
    assert constructed == [], "总开关关闭时不得构造传输"
    assert ctx.send.calls == []
    assert len(components_of(plugin)) == 3
    asyncio.run(plugin.on_unload())


def test_默认总开关关闭时消息输入不发送(tmp_path: Path) -> None:
    plugin, ctx = make_plugin(tmp_path)

    asyncio.run(plugin.observe_received_message(**merged_forward_message()))

    assert ctx.send.calls == [], "默认关闭时消息输入不得产生任何发送"
    assert ctx.maisaka.proactive.calls == [], "默认关闭时不得提供任何自动判断机会"


def test_注入可控时钟后总开关关闭不产生等待与发送(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin, ctx = make_plugin(tmp_path, clock=clock)

    asyncio.run(plugin.on_load())
    result = asyncio.run(
        plugin.forward_message(message_id="m-forward-1", target_group_id="20002")
    )

    assert result["success"] is False
    assert ctx.send.calls == []
    assert clock.sleeps == [], "总开关关闭时不得经注入时钟登记任何等待"


def test_缺直连配置时不构造传输且拒绝可定位(tmp_path: Path) -> None:
    fixture = DirectFixture(tmp_path, config_overrides={
        "snowluma": {"http_url": "", "token": "", "token_env_var": ""}})
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)

    result = asyncio.run(fixture.plugin.forward_message(**arguments))

    assert result["status"] == "rejected"
    assert "未配置" in result["reason"] and "HTTP" in result["reason"]
    assert "本次未发送任何消息" in result["reason"]
    assert fixture.constructed == [], "地址未配置不得构造传输"
    assert fixture.transport.sent == []
    assert fixture.ctx.send.calls == []
    assert len(components_of(fixture.plugin)) == 3


def test_组合根按http_url装配HTTP传输并完成卡片投递(tmp_path: Path) -> None:
    fixture = DirectFixture(tmp_path)
    fixture.prepare_source()
    arguments = issue_source_call(fixture.plugin)

    result = asyncio.run(fixture.plugin.forward_message(**arguments))

    assert result["status"] == "sent", result
    assert [connection.endpoint for connection in fixture.constructed] == [HTTP_URL]
    assert len(fixture.transport.sent) == 1
    # 卡片全链：展开查询 → 单次 send_group_forward_msg 提交展开构造的节点数组；
    # 不提交资源 ID、不设预览参数。
    assert fixture.transport.sent[0]["action"] == "send_group_forward_msg"
    assert set(fixture.transport.sent[0]["params"]) == {"group_id", "messages"}
    assert fixture.transport.sent[0]["params"]["group_id"] == TARGET_GROUP_ID
    assert fixture.transport.sent[0]["params"]["messages"] == [
        {"type": "node", "data": {"uin": "90001", "name": "群友",
                                  "content": [{"type": "text", "data": {"text": "卡片节点原文"}}]}}]
    assert DEFAULT_RESOURCE_ID not in str(fixture.transport.sent), "插件不得在发送载荷提交卡片资源 ID"
    record = fixture.record()
    assert record is not None and record["status"] == STATUS_SENT
    assert record["target_message_id"] == TARGET_MESSAGE_ID
    assert fixture.ctx.send.calls == [], "新投递路径不得调用宿主旧发送通道"
    assert {call["capability"] for call in fixture.ctx.chat.calls} <= {"chat.get_all_streams"}
