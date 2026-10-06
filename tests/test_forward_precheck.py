"""工具入口的本地权限与内容预检（直连口径，不联网、不发送）。

断言面只经 `forward_precheck` 公开导出（`precheck_forward_request` /
`group_id_entries` / `forward_group_entries` / `shared_forward_group` /
`source_group_is_allowed` / `PreparedForward`）：凭据换可信会话、转发分组与异群、
顶层唯一卡片校验（混排拒绝）、按可信会话的源消息归属核验。卡片材料只取标识与归属核验（与客户端
`SnowLumaClient.prepare_source` 的判定口径一致，不重复构造材料）；普通消息仍走
`send_material` 的完整内容预检。替身宿主按保留链样本构造
（`tests/snowluma_client_support` 的 `host_message`），不触真实网络。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from maimai_courier_plugin.config import CourierConfig
from maimai_courier_plugin.forward_precheck import (
    ForwardGroup,
    PreparedForward,
    MaterialDigest,
    SourceSession,
    forward_group_entries,
    precheck_forward_request,
    shared_forward_group,
    source_group_is_allowed,
    group_id_entries,
)
from maimai_courier_plugin.send_material import SendMaterial, UnsupportedContent, is_forward_card
from maimai_courier_plugin.shared.identifiers import verbatim_identifier
from maimai_courier_plugin.shared.send_steps import SOURCE_KIND_VALUES, PreparedSource
from maimai_courier_plugin.source_binding import SourceBindingTable
from maimai_courier_plugin.snowluma_client import SnowLumaClient
from tests.snowluma_client_support import (
    MemoryOneBotTransport, SOURCE_MESSAGE_ID, host_message, trusted_forward,
)
from tests.support import FakeClock, source_record

LONG_NUMERIC_ID = "1234567890123456789012345"
SOURCE_SESSION_ID = "s-test"

CARD_MESSAGE_ID = "-55555"
"""卡片源消息的平台消息 ID（可信映射的查询标识）。"""


_KEEP = object()
"""哨兵：保留 `host_message` 的默认 `raw_message`（用于区分「显式传 None」）。"""


def host_stored(message_id: str, *, raw_message: Any = _KEEP, **overrides: Any) -> dict[str, Any]:

    stored = host_message(message_id)
    if raw_message is not _KEEP:
        stored["raw_message"] = raw_message
    stored.update(overrides)
    return stored


def source_stream(**overrides: Any) -> dict[str, Any]:

    stream: dict[str, Any] = {
        "session_id": SOURCE_SESSION_ID, "platform": "qq", "group_id": "111",
        "account_id": "10001", "scope": "qq", "is_group_session": True,
    }
    stream.update(overrides)
    return stream


def enabled_config(
    *, source_groups: tuple[str, ...] = ("111",), target_groups: tuple[str, ...] = ("222", "111"),
    groups: list[dict[str, Any]] | None = None,
) -> CourierConfig:
    scope_groups = (
        groups if groups is not None
        else [{"name": "测试分组", "source_groups": list(source_groups),
               "target_groups": list(target_groups)}]
    )
    return CourierConfig(plugin={"enabled": True}, scope={"groups": scope_groups})


def issue_call(
    bindings: SourceBindingTable, *, message_id: str = SOURCE_MESSAGE_ID,
    target: str = "222", comment: str = "",
) -> dict[str, Any]:

    items = bindings.sanitize_and_bind_output_items(SOURCE_SESSION_ID, [{
        "item_type": "FunctionCallItem",
        "tool_call": {"func_name": "forward_message",
                      "args": {"message_id": message_id, "target_group_id": target,
                               "comment": comment}},
    }])
    return items[0]["tool_call"]["args"]


class FakeMessageHost:
    """宿主查询替身：会话表与源消息读取可注入异常，记录每次调用参数。"""

    def __init__(self, stored: Any, *, streams: list[dict[str, Any]] | None = None,
                 fail_streams: bool = False, fail_message: bool = False) -> None:
        self.stored = stored
        self.streams = [source_stream()] if streams is None else streams
        self.fail_streams = fail_streams
        self.fail_message = fail_message
        self.stream_calls = 0
        self.queried: list[str] = []

    async def get_all_streams(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self.stream_calls += 1
        if self.fail_streams:
            raise RuntimeError("宿主会话查询失败")
        return [dict(stream) for stream in self.streams]

    async def get_by_id(self, message_id: str, **_kwargs: Any) -> Any:
        self.queried.append(message_id)
        if self.fail_message:
            raise RuntimeError("宿主消息读取失败")
        return self.stored


def host_context(host: FakeMessageHost) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(get_all_streams=host.get_all_streams),
        message=SimpleNamespace(get_by_id=host.get_by_id),
    )


async def run_precheck(
    stored: Any, *, message_id: str = SOURCE_MESSAGE_ID, target: str = "222", comment: str = "",
    streams: list[dict[str, Any]] | None = None, fail_streams: bool = False,
    fail_message: bool = False, source_groups: tuple[str, ...] = ("111",),
    target_groups: tuple[str, ...] = ("222", "111"), groups: list[dict[str, Any]] | None = None,
    token_message_id: str | None = None,
    poison: dict[str, Any] | None = None,
) -> tuple[PreparedForward | Any, FakeMessageHost]:

    bindings = SourceBindingTable()
    arguments = issue_call(bindings, message_id=token_message_id or message_id,
                           target=target, comment=comment)
    # 模型可以改写自己看到的参数：这里把 message_id 换成另一个值，模拟不一致调用。
    arguments["message_id"] = message_id
    if poison:
        arguments.update(poison)
    host = FakeMessageHost(stored, streams=streams, fail_streams=fail_streams,
                           fail_message=fail_message)
    result = await precheck_forward_request(
        ctx=host_context(host), config=enabled_config(source_groups=source_groups,
                                                      target_groups=target_groups, groups=groups),
        bindings=bindings, arguments=arguments)
    return result, host


def rejection(result: Any) -> str:
    assert hasattr(result, "reason"), result
    return result.reason


EXPANDED_NODES: list[dict[str, Any]] = [
    {"sender": {"user_id": 30001, "card": "展开甲", "nickname": "昵称甲"},
     "message": [{"type": "text", "data": "展开查询得到的正文"}]},
]
"""`get_forward_msg` 展开查询的合成应答节点（sender 对象 + message 段数组）。"""


class ExpandingTransport(MemoryOneBotTransport):
    """复用基类替身的 `get_forward_msg` 展开查询：注入合成展开节点应答。

    只经 `OneBotTransport` 公开协议方法进入；不触碰传输内部状态、不触真实网络。
    """

    def __init__(self) -> None:
        super().__init__()
        self.expanded = {"messages": [deepcopy(node) for node in EXPANDED_NODES]}


# --- 4.1：凭据换会话、归属核验与顶层唯一卡片 ------------------------------------


def test_合法调用返回预检计划且标识来自可信映射() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        result, host = await run_precheck(stored)
        assert isinstance(result, PreparedForward)
        assert result.onebot_message_id == SOURCE_MESSAGE_ID
        assert result.onebot_message_id == result.arguments.message_id
        assert result.source == SourceSession(session_id=SOURCE_SESSION_ID, platform="qq",
                                              group_id="111", account_id="10001", scope="qq")
        assert host.queried == [SOURCE_MESSAGE_ID]
    asyncio.run(scenario())


@pytest.mark.parametrize("message_id", [SOURCE_MESSAGE_ID, LONG_NUMERIC_ID, "platform-test-A"])
def test_可信入站保留链建立查询标识而不改原消息ID(message_id: str) -> None:
    async def scenario() -> None:
        stored = host_stored(message_id)
        result, host = await run_precheck(stored, message_id=message_id)
        assert isinstance(result, PreparedForward)
        assert result.onebot_message_id == message_id
        assert result.arguments.message_id == message_id
        assert host.queried == [message_id]
    asyncio.run(scenario())


def test_纯卡片放行且材料只取标识与归属核验() -> None:
    async def scenario() -> None:
        raw = [{"type": "forward", "data": [{"type": "node", "content": [{"type": "text", "data": "旧内容"}]}]}]
        stored = host_stored(CARD_MESSAGE_ID, raw_message=raw)
        result, _ = await run_precheck(stored, message_id=CARD_MESSAGE_ID)
        assert isinstance(result, PreparedForward)
        assert isinstance(result.material, MaterialDigest)
        assert result.material.kind == "forward"
        assert result.material.kind in SOURCE_KIND_VALUES
        # 材料只表示发送路径：不持有资源 ID、不展开内部节点
        assert result.material.shape == ("forward",)
        assert not hasattr(result.material, "segments")
    asyncio.run(scenario())


def test_顶层卡片与文字混排在本地拒绝() -> None:
    async def scenario() -> None:
        raw = [{"type": "forward", "data": [{"type": "node", "content": [{"type": "text", "data": "旧内容"}]}]},
               {"type": "text", "data": "混排文字"}]
        stored = host_stored(CARD_MESSAGE_ID, raw_message=raw)
        result, _ = await run_precheck(stored, message_id=CARD_MESSAGE_ID)
        assert rejection(result).startswith("合并转发卡片与其它顶层内容混排")
    asyncio.run(scenario())


def test_卡片内部不受支持段不再触发本地递归拒绝() -> None:
    async def scenario() -> None:
        # 卡片内部（占位文本、视频段、嵌套卡片）由平台按源消息标识重建，
        # 预检不递归、不因内部段拒绝。
        raw = [{"type": "dict", "data": {"type": "forward", "data": [
            {"content": [{"type": "text", "data": "[empty forward node]"}]},
            {"content": [{"type": "video", "data": {"url": "https://x"}}, {"type": "forward", "data": []}]},
        ]}}]
        stored = host_stored(CARD_MESSAGE_ID, raw_message=raw)
        result, _ = await run_precheck(stored, message_id=CARD_MESSAGE_ID)
        assert isinstance(result, PreparedForward)
        assert isinstance(result.material, MaterialDigest)
        assert result.material.kind == "forward"
    asyncio.run(scenario())


def test_普通消息仍走完整内容预检() -> None:
    async def scenario() -> None:
        raw = [{"type": "text", "data": "正文"},
               {"type": "at", "data": {"target_user_id": "20002", "target_user_cardname": "群友甲"}},
               {"type": "image", "data": {"description": "图"}, "binary_data_base64": "AAA="}]
        stored = host_stored(SOURCE_MESSAGE_ID, raw_message=raw)
        result, _ = await run_precheck(stored)
        assert isinstance(result, PreparedForward)
        assert isinstance(result.material, SendMaterial)
        assert result.material.kind == "mixed"
        # 正文与紧邻提及归并为一个文本段；图片段保持独立且顺序在后。
        assert "［提及：群友甲（20002）］" in result.material.segments[0]["data"]
        assert result.material.segments[1]["type"] == "image"
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [
    [{"type": "text", "data": "正文"}, {"type": "reply", "data": {"id": "r1"}}],
    [{"type": "text", "data": "正文"}, {"type": "video", "data": {}}],
    [{"type": "text", "data": "正文"}, {"type": "image", "data": {"description": "无二进制"}}],
    [{"type": "text", "data": "正文"},
     {"type": "at", "data": {"target_user_id": "20002", "target_user_cardname": "甲"}},
     {"type": "image", "data": {}}],
])
def test_普通消息不支持或不可恢复段在本地整条拒绝(raw: list[dict[str, Any]]) -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID, raw_message=raw)
        result, _ = await run_precheck(stored)
        assert rejection(result).endswith("，本次未发送任何消息")
        assert "无法完整" in rejection(result) or "不能代替" in rejection(result) or "可恢复" in rejection(result)
    asyncio.run(scenario())


def test_空内容或结构异常的消息整条拒绝() -> None:
    async def scenario() -> None:
        for raw in ([], "不是列表", None, "正文"):
            stored = host_stored(SOURCE_MESSAGE_ID, raw_message=raw)
            result, _ = await run_precheck(stored)
            assert rejection(result).startswith("源消息没有可发送的内容段")
    asyncio.run(scenario())


def test_is_forward_card是唯一卡片形状判据且输入不被修改() -> None:
    raw = [{"type": "forward", "data": [{"type": "node", "content": [{"type": "text", "data": "旧内容"}]}]}]
    original = deepcopy(raw)
    assert is_forward_card(raw[0]) and is_forward_card({"type": "dict", "data": {"type": "forward", "data": []}})
    assert not is_forward_card({"type": "text", "data": "x"}) and not is_forward_card("x")
    assert raw == original


# --- 4.1：凭据、转发分组与异群口径 ------------------------------------------------


def test_凭据缺失伪造或错配时拒绝且不查询消息() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        # 缺凭据：模型自造参数，没有 Hook 签发的一次性令牌。
        result, host = await run_precheck(stored, poison={"_courier_source_token": "forged"})
        assert rejection(result).startswith("无法确认本次调用的可信来源")
        assert host.queried == []

        # 错配：凭据绑 A、调用传 B。
        result, host = await run_precheck(stored, token_message_id="-99999")
        assert rejection(result).startswith("无法确认本次调用的可信来源")
        assert host.queried == []

        # 一次性：同一凭据第二次消费即作废。
        bindings = SourceBindingTable()
        arguments = issue_call(bindings)
        host = FakeMessageHost(stored)
        host2 = FakeMessageHost(stored)
        config = enabled_config()
        first = await precheck_forward_request(ctx=host_context(host), config=config,
                                               bindings=bindings, arguments=arguments)
        second = await precheck_forward_request(ctx=host_context(host2), config=config,
                                                bindings=bindings, arguments=arguments)
        assert isinstance(first, PreparedForward)
        assert rejection(second).startswith("无法确认本次调用的可信来源")
        assert host2.queried == []
    asyncio.run(scenario())


def test_模型附加的会话与授权字段不能覆盖宿主来源() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        result, host = await run_precheck(stored, poison={
            "session_id": "s-elsewhere", "platform": "fake", "account_id": "99999",
            "group_id": "333", "chat_scope": "private", "is_authorized": True,
        })
        assert isinstance(result, PreparedForward)
        assert result.source.session_id == SOURCE_SESSION_ID
        assert result.source.group_id == "111"
        assert host.queried == [SOURCE_MESSAGE_ID]
    asyncio.run(scenario())


def test_来源会话不可验证或非群聊非QQ时拒绝() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        # 宿主查询异常。
        result, _ = await run_precheck(stored, fail_streams=True)
        assert rejection(result).startswith("无法验证本次调用的来源会话")
        # 会话不在表内。
        result, _ = await run_precheck(stored, streams=[source_stream(session_id="s-other")])
        assert rejection(result).startswith("无法确认本次调用的可信来源会话")
        # 私聊。
        result, _ = await run_precheck(stored, streams=[source_stream(is_group_session=False)])
        assert rejection(result).startswith("本次调用来源不是群聊")
        # 非 QQ 平台。
        result, _ = await run_precheck(stored, streams=[source_stream(platform="telegram")])
        assert rejection(result).startswith("本次调用来源不在 QQ 群聊范围内")
        # 账号缺失。
        result, _ = await run_precheck(stored, streams=[source_stream(account_id="")])
        assert rejection(result).startswith("来源会话缺少可确认的机器人账号")
    asyncio.run(scenario())


def test_来源群分组与跨组目标和异群校验() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        result, host = await run_precheck(stored, source_groups=("999",))
        assert rejection(result).startswith("调用来源群不在任何转发分组内")
        assert host.queried == []  # 分组判定先于消息读取

        result, _ = await run_precheck(stored, target="333")
        assert rejection(result).startswith("目标群不在与来源群相同的转发分组内")

        result, _ = await run_precheck(stored, target="111")
        assert rejection(result).startswith("目标群不能是调用来源群自身")
    asyncio.run(scenario())


def test_同组命名分组内预检通过且分组可定位() -> None:
    async def scenario() -> None:
        groups = [{"name": "研发", "source_groups": ["111"], "target_groups": ["222"]}]
        stored = host_stored(SOURCE_MESSAGE_ID)
        result, host = await run_precheck(stored, groups=groups)
        assert isinstance(result, PreparedForward)
        assert host.queried == [SOURCE_MESSAGE_ID], "同组调用仍核验源消息归属"
        # 目标判定来自同一具名分组（去空白原值）。
        assert shared_forward_group(enabled_config(groups=groups), "111", "222") == "研发"
    asyncio.run(scenario())


def test_目标只在别组或不在任何分组均在预检发送前拒绝() -> None:
    async def scenario() -> None:
        groups = [
            {"name": "研发", "source_groups": ["111"], "target_groups": ["222"]},
            {"name": "运营", "source_groups": ["444"], "target_groups": ["333"]},
        ]
        stored = host_stored(SOURCE_MESSAGE_ID)
        # 目标只在别的分组（运营的 333）。
        result, _ = await run_precheck(stored, target="333", groups=groups)
        assert rejection(result).startswith("目标群不在与来源群相同的转发分组内")
        # 目标不在任何分组。
        result, _ = await run_precheck(stored, target="999", groups=groups)
        assert rejection(result).startswith("目标群不在与来源群相同的转发分组内")
    asyncio.run(scenario())


def test_目标群跨组重复时各分组的源都可同组投递() -> None:
    async def scenario() -> None:
        groups = [
            {"name": "研发", "source_groups": ["111"], "target_groups": ["222"]},
            {"name": "运营", "source_groups": ["444"], "target_groups": ["222"]},
        ]
        # 同一目标群 222 出现在两个分组：来源群 111（研发）的预检照常通过。
        stored = host_stored(SOURCE_MESSAGE_ID)
        result, _ = await run_precheck(stored, target="222", groups=groups)
        assert isinstance(result, PreparedForward)
        # 另一分组（运营）的源 444 同样与 222 同组，跨组重复不拒绝合法调用。
        assert shared_forward_group(enabled_config(groups=groups), "444", "222") == "运营"
    asyncio.run(scenario())


@pytest.mark.parametrize(("fault", "detail"), [
    ("wrong_id", {"message_id": "-99999"}),
    ("wrong_session", {"session_id": "s-elsewhere"}),
    ("wrong_platform", {"platform": "telegram"}),
    ("missing_info", {"message_info": None}),
    ("wrong_group", {"message_info": {"group_info": {"group_id": "333"},
                                      "additional_config": {"self_id": "10001",
                                                            "napcat_message_type": "group",
                                                            "client_type": "napcat"}}}),
    ("wrong_self", {"message_info": {"group_info": {"group_id": "111"},
                                     "additional_config": {"self_id": "99999",
                                                           "napcat_message_type": "group",
                                                           "client_type": "napcat"}}}),
    ("wrong_route", {"message_info": {"group_info": {"group_id": "111"},
                                      "additional_config": {"self_id": "10001",
                                                            "napcat_message_type": "private",
                                                            "client_type": "napcat"}}}),
])
def test_可信源映射不足或冲突在本地拒绝且不泄正文(fault: str, detail: dict[str, Any]) -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        stored.update(detail)
        if detail.get("message_info") is None:
            stored.pop("message_info")
        result, _ = await run_precheck(stored)
        expected = ("源消息归属与本次调用的可信来源不一致" if fault == "wrong_session"
                    else "源消息归属或 OneBot 入站对应信息不足")
        assert rejection(result).startswith(expected)
    asyncio.run(scenario())


def test_布尔与对象标识不构成平台身份() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        stored["message_info"] = {"group_info": {"group_id": True},
                                  "additional_config": {"self_id": "10001",
                                                        "napcat_message_type": "group",
                                                        "client_type": "napcat"}}
        result, _ = await run_precheck(stored)
        assert rejection(result).startswith("源消息归属或 OneBot 入站对应信息不足")
    asyncio.run(scenario())


def test_不可定位源消息或宿主查询失败时拒绝() -> None:
    async def scenario() -> None:
        result, _ = await run_precheck(None)
        assert rejection(result).startswith("源消息不可定位或不属于本次调用的源群")
        result, _ = await run_precheck({"success": False, "message_id": SOURCE_MESSAGE_ID})
        assert rejection(result).startswith("源消息不可定位或不属于本次调用的源群")
        result, _ = await run_precheck(host_stored(SOURCE_MESSAGE_ID), fail_message=True)
        assert rejection(result).startswith("无法读取源消息")
        result, _ = await run_precheck(host_stored(SOURCE_MESSAGE_ID, session_id="s-elsewhere"))
        assert rejection(result).startswith("源消息归属与本次调用的可信来源不一致")
    asyncio.run(scenario())


def test_成对判定同组命中返回分组名否则无() -> None:
    config = CourierConfig(scope={"groups": [
        {"name": " 研发 ", "source_groups": ["111"], "target_groups": ["222", "333"]},
        {"name": "运营", "source_groups": ["444"], "target_groups": ["333"]},
    ]})
    # 同组命中返回去空白后的分组名；目标群跨组重复时各自分组均可命中。
    assert shared_forward_group(config, "111", "222") == "研发"
    assert shared_forward_group(config, "111", "333") == "研发"
    assert shared_forward_group(config, "444", "333") == "运营"
    # 目标只在别组、不在任何分组、来源群不在任何源组：都不命中。
    assert shared_forward_group(config, "444", "222") is None
    assert shared_forward_group(config, "111", "999") is None
    assert shared_forward_group(config, "999", "222") is None


def test_源群兼作别组目标时仍只判本组目标() -> None:
    config = CourierConfig(scope={"groups": [
        {"name": "甲", "source_groups": ["111"], "target_groups": ["222"]},
        {"name": "乙", "source_groups": ["333"], "target_groups": ["111"]},
    ]})
    assert shared_forward_group(config, "333", "111") == "乙"
    # 源群自身作为目标不成组（「目标 = 来源群自身」另由预检拒绝）。
    assert shared_forward_group(config, "111", "111") is None


def test_forward_group_entries归一全部分组并保持配置顺序() -> None:
    config = CourierConfig(scope={"groups": [
        {"name": " 研发 ", "source_groups": [" 111 ", "112", "  "],
         "target_groups": [" 222 ", "222"]},
        {"name": "运营", "source_groups": [], "target_groups": []},
    ]})
    before = [group.model_dump() for group in config.scope.groups]

    entries = forward_group_entries(config)

    assert entries == (
        ForwardGroup(name="研发", source_groups=("111", "112"), target_groups=("222", "222")),
        ForwardGroup(name="运营", source_groups=(), target_groups=()),
    )
    assert [group.model_dump() for group in config.scope.groups] == before, (
        "归一视图不得原地修改配置分组")


def test_forward_group_entries的群号归一与group_id_entries口径一致() -> None:
    # 空白丢弃、字符串逐字符、不可迭代回落空：与 `group_id_entries` 同一口径。
    stub = SimpleNamespace(scope=SimpleNamespace(groups=[SimpleNamespace(
        name=" 研发 ", source_groups="111  ", target_groups=None)]))
    entries = forward_group_entries(stub)
    assert entries == (ForwardGroup(name="研发", source_groups=("1", "1", "1"), target_groups=()),)


def test_群号归一与来源群判定共用同一份口径() -> None:
    assert group_id_entries([" 111 ", "222", "", "  ", "111"]) == ("111", "222", "111")
    assert group_id_entries("111") == ("1", "1", "1")  # 字符串按可迭代逐字符处理
    assert group_id_entries(None) == ()
    assert group_id_entries([111, 222, None, True]) == ("111", "222", "None", "True")
    config = CourierConfig(scope={"groups": [
        {"name": "研发", "source_groups": ["111", "112"], "target_groups": ["222"]},
        {"name": "运营", "source_groups": ["333"], "target_groups": ["222"]},
    ]})
    assert source_group_is_allowed(config, "111") and source_group_is_allowed(config, " 111 ")
    assert source_group_is_allowed(config, "333")
    assert not source_group_is_allowed(config, "999")


def test_预检不联网且不触碰平台凭证() -> None:
    async def scenario() -> None:
        # 预检只查宿主会话与源消息：没有 SnowLuma 客户端、没有传输替身调用。
        result, host = await run_precheck(host_stored(SOURCE_MESSAGE_ID))
        assert isinstance(result, PreparedForward)
        assert host.stream_calls == 1
    asyncio.run(scenario())


def test_卡片判定与客户端准备口径一致() -> None:
    async def scenario() -> None:
        # 顶层唯一卡片（含未展开 dict 包装）须带可展开的卡片标识：预检放行、
        # 客户端同样判 forward，并按「标识 → 展开查询 → 节点数组」构造材料。
        for card_id, raw in (
            ("card-open", [{"type": "forward", "data": {"id": "card-open"}}]),
            ("card-dict", [{"type": "dict", "data": {"type": "forward", "data": {"id": "card-dict"}}}]),
        ):
            prechecked, _ = await run_precheck(host_stored(CARD_MESSAGE_ID, raw_message=raw),
                                               message_id=CARD_MESSAGE_ID)
            assert isinstance(prechecked, PreparedForward)
            transport = ExpandingTransport()
            transport.source = {**transport.source, "message_id": int(CARD_MESSAGE_ID),
                                "message": raw, "group_id": 111}
            client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
            prepared = await client.prepare_source(prechecked)
            assert isinstance(prepared, PreparedSource)
            assert prepared.kind == "forward"
            # 材料 = 展开查询构造的节点数组：sender 标量化、name 取 card 优先、
            # content 按查询结果原样的深拷贝；节点内容来自展开应答，不取自源消息。
            (node,) = prepared.material.segments
            assert node == {"type": "node", "data": {
                "uin": "30001", "name": "展开甲",
                "content": [{"type": "text", "data": "展开查询得到的正文"}],
            }}
            assert transport.queries.count(("get_forward_msg", {"id": card_id})) == 1
            assert transport.sent == []  # 准备阶段零发送
            await client.close()

        # 卡片标识不可确认（无 data.id）：不猜造标识、不展开、不发送，明确拒绝。
        raw = [{"type": "forward", "data": [{"type": "node", "content": [{"type": "text", "data": "旧"}]}]}]
        prechecked, _ = await run_precheck(host_stored(CARD_MESSAGE_ID, raw_message=raw),
                                           message_id=CARD_MESSAGE_ID)
        assert isinstance(prechecked, PreparedForward)
        transport = ExpandingTransport()
        transport.source = {**transport.source, "message_id": int(CARD_MESSAGE_ID),
                            "message": raw, "group_id": 111}
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(prechecked)
        assert getattr(prepared, "error", None) == "forward_expand_unavailable"
        assert [action for action, _ in transport.queries] == [
            "get_login_info", "get_group_list", "get_msg",
        ]
        assert transport.sent == []
        await client.close()
    asyncio.run(scenario())


def test_混排判定与客户端准备口径一致() -> None:
    async def scenario() -> None:
        raw = [{"type": "forward", "data": []}, {"type": "text", "data": "混排"}]
        prechecked, _ = await run_precheck(host_stored(CARD_MESSAGE_ID, raw_message=raw),
                                           message_id=CARD_MESSAGE_ID)
        assert hasattr(prechecked, "reason")  # 本地即拒
        # 客户端对同形态纵深防御：预检产出若绕过本地分支，服务端同样按混排拒绝。
        transport = MemoryOneBotTransport()
        transport.source = source_record(raw, message_id=int(CARD_MESSAGE_ID))
        prepared = await SnowLumaClient(transport=transport, clock=FakeClock(),
                                        config_generation=1).prepare_source(
            await trusted_forward(message_id=CARD_MESSAGE_ID))
        assert getattr(prepared, "error", None) == "source_shape_unsupported"
        assert transport.sent == []
    asyncio.run(scenario())


def test_拒绝措辞不越权读取平台身份证明字段() -> None:
    async def scenario() -> None:
        stored = host_stored(SOURCE_MESSAGE_ID)
        stored["raw_message"] = [{"type": "text", "data": "越权正文"}]
        stored.update({"message_info": {"group_info": {"group_id": "333"},
                                        "additional_config": {"self_id": "10001",
                                                              "napcat_message_type": "group",
                                                              "client_type": "napcat"}}})
        result, _ = await run_precheck(stored)
        reason = rejection(result)
        assert "越权正文" not in reason
        assert "333" not in reason
    asyncio.run(scenario())


def test_普通消息材料构造只读不改源消息() -> None:
    async def scenario() -> None:
        raw = [{"type": "text", "data": "正文"},
               {"type": "image", "data": {"description": "图"}, "binary_data_base64": "AAA="}]
        stored = host_stored(SOURCE_MESSAGE_ID, raw_message=raw)
        original = deepcopy(raw)
        result, _ = await run_precheck(stored)
        assert isinstance(result, PreparedForward)
        assert raw == original
    asyncio.run(scenario())
