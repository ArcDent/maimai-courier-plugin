"""客户端卡片分支（展开查询→发送前类型检查→节点校验→单次卡片发送）与文字/图片路径回归。

断言面只经 `SnowLumaClient` 公开方法与 `OneBotTransport` 协议替身（内存替身实现
协议，不 mock 任何内部协作者）；不触真实网络、不回显敏感正文。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from maimai_courier_plugin.snowluma_client import SnowLumaClient
from maimai_courier_plugin.shared.send_steps import PrepareRejected, PreparedSource
from maimai_courier_plugin.type_check import NESTED_FORWARD_DETAIL, NESTED_FORWARD_REASON
from tests.snowluma_client_support import (
    CARD_RESOURCE_ID, NESTED_EXPANDED_CARD, SOURCE_MESSAGE_ID, TARGET_MESSAGE_ID,
    MemoryOneBotTransport, trusted_forward,
)
from tests.support import SENDER_CARD, FakeClock, envelope, source_record, spin_until


def expanded_card() -> dict[str, Any]:
    """`get_forward_msg` 展开查询应答的 `data`：两节点非嵌套卡片样本（第二节点含图片段）。

    默认成功路径夹具不含嵌套 forward 段：按新契约嵌套内容必须在发送前整条拒绝
    （见 `nested_card()` 专用夹具），成功路径用例只覆盖非嵌套材料。
    """

    return {"messages": [
        {"sender": {"user_id": 90001, "card": "群友甲", "nickname": "甲"},
         "message": [{"type": "text", "data": {"text": "卡片节点正文"}}]},
        {"sender": {"user_id": 90002, "nickname": "乙"},
         "message": [{"type": "image", "data": {"url": "https://multimedia.example/x.png"}}]},
    ]}


def nested_card() -> dict[str, Any]:
    """`get_forward_msg` 展开查询应答的 `data`：第二节点内容含嵌套 forward 段。

    嵌套内层在目标群客户端只能显示为降级空壳（实测，见 API 目录第 6 节）：发送前
    类型检查必须整条拒绝（固定 reason 与对外措辞以 `type_check` 常量为单一来源），
    不进入节点构造、不提交任何发送、不递归展开或摘要。

    样本取共享常量 `tests.snowluma_client_support.NESTED_EXPANDED_CARD` 的深拷贝：
    嵌套样本单点定义，只读预识别与发送前类型检查共用同一形态。
    """

    return deepcopy(NESTED_EXPANDED_CARD)


def expanded_nodes() -> tuple[dict[str, Any], ...]:
    """默认展开查询经 `build_card_nodes` 构造的节点数组（卡片发送材料）。"""

    return (
        {"type": "node", "data": {"uin": "90001", "name": "群友甲",
                                  "content": [{"type": "text", "data": {"text": "卡片节点正文"}}]}},
        {"type": "node", "data": {"uin": "90002", "name": "乙",
                                  "content": [
                                      {"type": "image", "data": {"url": "https://multimedia.example/x.png"}},
                                  ]}},
    )


class CardOneBotTransport(MemoryOneBotTransport):
    """复用基类替身的七动作面，注入卡片链路合成展开应答的内存替身。

    展开应答由 `expanded` 给定；`on_action` 可对 `get_forward_msg`
    覆盖应答（注入业务失败信封或挂起）。替身只经协议方法进入。
    """

    def __init__(self) -> None:
        super().__init__()
        self.expanded = expanded_card()


async def prepare(transport: MemoryOneBotTransport, *, config_generation: int = 7) -> PreparedSource | PrepareRejected:

    return await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=config_generation).prepare_source(await trusted_forward())


def test_准备按账号名册源记录三步只读且无发送() -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=7)
        trusted = await trusted_forward()
        prepared = await client.prepare_source(trusted)
        assert isinstance(prepared, PreparedSource)
        assert (prepared.account_id, prepared.source_group_id, prepared.source_message_id,
                prepared.target_group_id) == ("10001", "111", SOURCE_MESSAGE_ID, "222")
        assert (prepared.config_generation, prepared.kind) == (7, "text")
        assert transport.queries == [
            ("get_login_info", {}), ("get_group_list", {}),
            ("get_msg", {"message_id": SOURCE_MESSAGE_ID}),
        ]
        assert transport.sent == []
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("fault", "error"), [
    ("account", "account_mismatch"),
    ("target", "target_unavailable"),
    ("private", "source_mismatch"),
    ("wrong_group", "source_mismatch"),
    ("wrong_id", "source_mismatch"),
    ("missing", "source_not_found"),
])
def test_准备核验不符返回可定位拒绝且零发送(fault: str, error: str) -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        if fault == "account":
            transport.account = "99999"
        elif fault == "target":
            transport.groups = [{"group_id": 333}]
        elif fault == "private":
            transport.source["message_type"] = "private"
        elif fault == "wrong_group":
            transport.source["group_id"] = 333
        elif fault == "wrong_id":
            async def wrong(action: str, params: object) -> object:
                if action == "get_msg":
                    return envelope({"message_id": -99, "message_type": "group", "group_id": 111,
                                     "message": "越权正文"})
                return None
            transport.on_action = wrong
        else:
            async def missing(action: str, params: object) -> object:
                if action == "get_msg":
                    return envelope(None, status="failed", retcode=100, message="not found")
                return None
            transport.on_action = missing
        result = await prepare(transport)
        assert isinstance(result, PrepareRejected)
        assert result.error == error
        assert "越权正文" not in repr(result)
        assert transport.sent == []
        if fault in ("account", "target"):
            assert not any(name == "get_msg" for name, _ in transport.queries)
    asyncio.run(scenario())


@pytest.mark.parametrize(("message", "kind"), [
    ([{"type": "forward", "data": {"id": CARD_RESOURCE_ID, "content": [
        {"type": "reply"}, {"type": "at", "data": {}}, {"type": "unknown", "data": "内部私密"},
    ]}}], "forward"),
    ([{"type": "dict", "data": {"type": "forward", "data": {"id": CARD_RESOURCE_ID}}}], "forward"),
    ([{"type": "text", "data": {"text": "纯文字"}}], "text"),
    ([{"type": "image", "data": {"url": "https://images.example.org/a"}}], "image"),
    ([{"type": "text", "data": {"text": "图"}},
      {"type": "image", "data": {"url": "https://images.example.org/a"}}], "image"),
    ([{"type": "at", "data": {"qq": "all"}}], "text"),
])
def test_准备按顶层形态判定发送路径kind(message: list[dict[str, object]], kind: str) -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        transport.source = source_record(message)
        original = deepcopy(transport.source)
        result = await prepare(transport)
        assert isinstance(result, PreparedSource)
        assert result.kind == kind
        assert result.material.kind == kind
        if kind == "forward":
            # 卡片材料 = 展开查询构造的节点数组：不取自源段 content、不持有调用者
            # 资源引用；内容段按展开查询结果原样（默认夹具非嵌套）。
            assert result.material.segments == expanded_nodes()
            assert ("get_forward_msg", {"id": CARD_RESOURCE_ID}) in transport.queries
        assert transport.source == original and transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("message", [
    [{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}, {"type": "text", "data": {"text": "混排"}}],
    [{"type": "dict", "data": {"type": "forward", "data": []}}, {"type": "at", "data": {"qq": "all"}}],
    [{"type": "reply", "data": {"id": "r1"}}],
    [{"type": "image", "data": {}}],
    [{"type": "video", "data": {"url": "https://images.example.org/v"}}],
])
def test_准备拒绝混排与普通不支持形态(message: list[dict[str, object]]) -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        transport.source = source_record(message)
        result = await prepare(transport)
        assert isinstance(result, PrepareRejected)
        assert transport.sent == []
    asyncio.run(scenario())


def test_卡片准备按展开查询构造节点材料且只读() -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        original = deepcopy(transport.expanded)
        result = await prepare(transport)
        assert isinstance(result, PreparedSource)
        assert result.kind == "forward"
        # 材料 = 展开查询构造的节点数组：sender 标量、name 压平限长、content 段原样
        # （第二节点图片段），不取自源段 content、不持有调用者资源引用。
        assert result.material.segments == expanded_nodes()
        assert [name for name, _ in transport.queries] == [
            "get_login_info", "get_group_list", "get_msg", "get_forward_msg"]
        assert transport.queries[-1] == ("get_forward_msg", {"id": CARD_RESOURCE_ID})
        assert transport.sent == [] and transport.expanded == original
    asyncio.run(scenario())


def test_嵌套卡片展开内容发送前整条拒绝且零发送() -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        transport.expanded = nested_card()
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        original = deepcopy(transport.expanded)
        result = await prepare(transport)
        assert isinstance(result, PrepareRejected)
        # 固定 reason 与对外措辞以 type_check 常量为单一来源：不复制字面量、不摘要
        assert result.error == NESTED_FORWARD_REASON
        assert result.reason == NESTED_FORWARD_DETAIL
        # 拒绝点在展开查询之后、节点构造之前：只读查询仍发生，发送动作为零
        assert ("get_forward_msg", {"id": CARD_RESOURCE_ID}) in transport.queries
        assert transport.sent == [] and transport.expanded == original
    asyncio.run(scenario())


def test_非嵌套展开内容含reply与at与image照常准备() -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        transport.expanded = {"messages": [
            {"sender": {"user_id": "90003", "nickname": "丙"}, "message": [
                {"type": "text", "data": {"text": "正文"}},
                {"type": "reply", "data": {"id": "r1"}},
                {"type": "at", "data": {"qq": "30001"}},
                {"type": "image", "data": {"url": "https://images.example.org/a"}},
            ]},
        ]}
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        result = await prepare(transport)
        assert isinstance(result, PreparedSource)
        assert result.kind == "forward"
        # 嵌套检查只认 forward 段：reply / at / image 均随节点内容原样放行，不误拒
        assert result.material.segments == ({"type": "node", "data": {
            "uin": "90003", "name": "丙", "content": [
                {"type": "text", "data": {"text": "正文"}},
                {"type": "reply", "data": {"id": "r1"}},
                {"type": "at", "data": {"qq": "30001"}},
                {"type": "image", "data": {"url": "https://images.example.org/a"}},
            ]}},)
        assert ("get_forward_msg", {"id": CARD_RESOURCE_ID}) in transport.queries
        assert transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize(("fault", "error"), [
    ("business", "forward_expand_unavailable"),
    ("no_data", "forward_expand_unavailable"),
    ("empty", "forward_expand_unavailable"),
    ("node_invalid", "forward_node_invalid"),
    ("content_empty", "forward_node_invalid"),
    ("sender_missing", "sender_unconfirmed"),
    ("name_missing", "sender_name_unconfirmed"),
    ("id_missing", "forward_expand_unavailable"),
    ("dict_no_id", "forward_expand_unavailable"),
])
def test_卡片展开失败与结构不符返回可定位拒绝且零发送(fault: str, error: str) -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        message: list[dict[str, Any]] = [{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}]
        if fault == "business":
            async def reply(action: str, params: object) -> object:
                if action == "get_forward_msg":
                    return envelope(None, status="failed", retcode=100, message="not found")
                return None
            transport.on_action = reply
        elif fault == "no_data":
            transport.expanded = {}
        elif fault == "empty":
            transport.expanded = {"messages": []}
        elif fault == "node_invalid":
            transport.expanded = {"messages": [
                {"sender": {"user_id": "90001", "nickname": "甲"}, "message": "正文"}]}
        elif fault == "content_empty":
            transport.expanded = {"messages": [
                {"sender": {"user_id": "90001", "nickname": "甲"}, "message": []}]}
        elif fault == "sender_missing":
            transport.expanded = {"messages": [
                {"nickname": "甲", "message": [{"type": "text", "data": {"text": "正文"}}]}]}
        elif fault == "name_missing":
            transport.expanded = {"messages": [
                {"sender": {"user_id": "90001"}, "message": [{"type": "text", "data": {"text": "正文"}}]}]}
        elif fault == "id_missing":
            message = [{"type": "forward", "data": {"content": [
                {"type": "text", "data": {"text": "内部"}}]}}]
        else:
            message = [{"type": "dict", "data": {"type": "forward", "data": []}}]
        transport.source = source_record(message)
        result = await prepare(transport)
        assert isinstance(result, PrepareRejected)
        assert result.error == error
        assert transport.sent == []
        if fault in ("id_missing", "dict_no_id"):
            assert not any(name == "get_forward_msg" for name, _ in transport.queries)
        else:
            assert ("get_forward_msg", {"id": CARD_RESOURCE_ID}) in transport.queries
    asyncio.run(scenario())


def test_卡片展开查询超时按查询未完成拒绝且零发送() -> None:
    async def scenario() -> None:
        transport = CardOneBotTransport()
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        release = asyncio.Event()
        async def hang(action: str, params: object) -> object:
            if action == "get_forward_msg":
                await release.wait()
            return None
        transport.on_action = hang
        clock = FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        task = asyncio.create_task(client.prepare_source(await trusted_forward()))
        # 替身在登记查询后才挂起：有界等到展开查询进入，红态下也不会死等
        await spin_until(lambda: any(name == "get_forward_msg" for name, _ in transport.queries))
        clock.advance(11)  # 推进超过单动作总预算（代码常量 10 秒，展开查询计入同一预算）
        await spin_until(task.done)
        result = task.result()
        assert isinstance(result, PrepareRejected)
        assert result.error == "source_query_unavailable"
        assert transport.sent == []
        release.set()
        await spin_until(lambda: True)
        await client.close()
    asyncio.run(scenario())


def test_准备在本地前置拒绝同群与标识不符且零查询() -> None:
    async def scenario() -> None:
        from dataclasses import replace
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        same_group = replace(await trusted_forward(), arguments=replace((await trusted_forward()).arguments, target_group_id="111"))
        assert isinstance(await client.prepare_source(same_group), PrepareRejected)
        unmapped = replace(await trusted_forward(), onebot_message_id="")
        rejected = await client.prepare_source(unmapped)
        assert isinstance(rejected, PrepareRejected)
        assert rejected.error == "source_unconfirmed"
        assert transport.queries == [] and transport.sent == []
        await client.close()
    asyncio.run(scenario())


async def prepared_source(transport: MemoryOneBotTransport) -> PreparedSource:

    result = await SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1).prepare_source(
        await trusted_forward())
    assert isinstance(result, PreparedSource)
    return result


@pytest.mark.parametrize(("message", "action", "params"), [
    ([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}],
     "send_group_forward_msg",
     {"group_id": "222", "messages": list(expanded_nodes())}),
    ([{"type": "text", "data": {"text": "图"}},
      {"type": "image", "data": {"url": "https://images.example.org/a"}}],
     "send_msg",
     {"message_type": "group", "group_id": "222",
      "message": [{"type": "text", "data": {"text": "图"}},
                  {"type": "image", "data": {"url": "https://images.example.org/a"}}]}),
    ([{"type": "text", "data": {"text": "普通文字"}}],
     "send_forward_msg",
     {"group_id": "222",
      "messages": [{"type": "node", "data": {"uin": "20002", "name": SENDER_CARD,
                                             "content": [{"type": "text", "data": {"text": "普通文字"}}]}}]}),
])
def test_按发送路径分派唯一固定动作且不重试(
    message: list[dict[str, object]], action: str, params: dict[str, object],
) -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = CardOneBotTransport()
        transport.source = source_record(message)
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert result.status == "sent"
        assert result.target_message_id == TARGET_MESSAGE_ID
        assert [record["action"] for record in transport.sent] == [action]
        assert transport.sent[0]["params"] == params
        await client.close()
    asyncio.run(scenario())


def test_卡片发送只提交展开节点数组且不设预览参数() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = CardOneBotTransport()
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert result.status == "sent"
        # 卡片分支只产生 send_group_forward_msg 一个发送动作：不发 send_msg+forward
        # 段直引旧 res_id（实测假成功通道），也不按旧标识重建。
        assert [record["action"] for record in transport.sent] == ["send_group_forward_msg"]
        params = transport.sent[0]["params"]
        assert set(params) == {"group_id", "messages"}
        assert params["group_id"] == "222"
        assert params["messages"] == list(expanded_nodes())
        for node in params["messages"]:
            assert set(node) == {"type", "data"} and node["type"] == "node"
            data = node["data"]
            assert set(data) == {"uin", "name", "content"}
            assert isinstance(data["uin"], (str, int)) and not isinstance(data["uin"], bool)
            assert isinstance(data["name"], str) and data["name"]
            assert isinstance(data["content"], list) and data["content"]
            assert not ({"summary", "news", "prompt", "source"} & set(data))
        await client.close()
    asyncio.run(scenario())


def test_评论以普通文本独立发送且空白评论不发出任何请求() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import CommentSendRequest
        transport = MemoryOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_comment(
            CommentSendRequest(prepared, "评论文本"), before_submit=lambda: True)
        assert result.status == "sent"
        assert transport.sent == [{"action": "send_msg",
                                   "params": {"message_type": "group", "group_id": "222",
                                              "message": [{"type": "text", "data": {"text": "评论文本"}}]}}]
        blank = await client.send_comment(CommentSendRequest(prepared, "   "), before_submit=lambda: True)
        assert blank.status == "failed"
        assert [record["action"] for record in transport.sent] == ["send_msg"]
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("sender", "uin", "name"), [
    ({"user_id": 30003, "card": " 名\n片\x00 ", "nickname": "昵称"}, "30003", "名 片"),
    ({"user_id": 30003, "nickname": "\t昵称\t"}, "30003", "昵称"),
    ({"user_id": 30003, "card": "   ", "nickname": "昵称"}, "30003", "昵称"),
    ({"user_id": 30003}, "30003", ""),
])
def test_节点署名只取源消息sender且压平限长(
    sender: dict[str, object], uin: str, name: str,
) -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        transport.source = source_record(
            [{"type": "text", "data": {"text": "正文"}}], sender=sender)
        result = await prepare(transport)
        assert isinstance(result, PreparedSource)
        node = result.material.segments[0]
        assert set(node["data"]) == {"uin", "name", "content"}
        assert (node["data"]["uin"], node["data"]["name"]) == (uin, name)
        assert node["data"]["content"] == [{"type": "text", "data": {"text": "正文"}}]
        # 不设置 summary/news/prompt/source 等外层预览参数
        assert not ({"summary", "news", "prompt", "source"} & set(node["data"]))
    asyncio.run(scenario())


def test_节点内容段含at按源消息原样提交() -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        mention = {"type": "at", "data": {"qq": "30001", "card": "群友丙"}}
        transport.source = source_record([mention, {"type": "text", "data": {"text": "看这里"}}])
        original = deepcopy(transport.source["message"])
        result = await prepare(transport)
        assert isinstance(result, PreparedSource)
        content = result.material.segments[0]["data"]["content"]
        assert content == [mention, {"type": "text", "data": {"text": "看这里"}}]
        assert transport.source["message"] == original
    asyncio.run(scenario())


def test_发送者标识不可确认时拒绝文字节点() -> None:
    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        transport.source = source_record([{"type": "text", "data": {"text": "正文"}}], sender={})
        result = await prepare(transport)
        assert isinstance(result, PrepareRejected)
        assert result.error == "sender_unconfirmed"
        assert transport.sent == []
    asyncio.run(scenario())


def test_负数目标ID按原值回查核验通过并记sent() -> None:
    async def scenario() -> None:
        import dataclasses
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        negative_id = -98765
        async def reply(action: str, params: object) -> object:
            if action == "send_forward_msg":
                return envelope({"message_id": negative_id, "res_id": CARD_RESOURCE_ID,
                                 "forward_id": "fwd-abc"})
            return None
        transport.on_action = reply
        transport.source = source_record([{"type": "text", "data": {"text": "正文"}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert result.status == "sent"
        assert result.target_message_id == str(negative_id)
        # res_id / forward_id 只是平台内部资源标识：不入账、不进结果字段
        assert not {"res_id", "forward_id"} & {field.name for field in dataclasses.fields(result)}
        assert not hasattr(result, "res_id") and not hasattr(result, "forward_id")
        assert [record["action"] for record in transport.sent] == ["send_forward_msg"]
        await client.close()
    asyncio.run(scenario())


def test_卡片回执额外资源标识不入账不进结果() -> None:
    async def scenario() -> None:
        import dataclasses
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = CardOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_group_forward_msg":
                return envelope({"message_id": TARGET_MESSAGE_ID, "res_id": CARD_RESOURCE_ID,
                                 "forward_id": "fwd-abc"})
            return None
        transport.on_action = reply
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        # res_id / forward_id 只是平台内部资源标识：不作成功证据、不入账、不进结果
        assert result.status == "sent"
        assert result.target_message_id == TARGET_MESSAGE_ID
        assert not {"res_id", "forward_id"} & {field.name for field in dataclasses.fields(result)}
        assert not hasattr(result, "res_id") and not hasattr(result, "forward_id")
        assert CARD_RESOURCE_ID not in repr(result)
        assert [record["action"] for record in transport.sent] == ["send_group_forward_msg"]
        await client.close()
    asyncio.run(scenario())


def test_发送动作业务失败按明确失败且不回查() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_msg":
                return envelope(None, status="failed", retcode=100, message="message not found")
            return None
        transport.on_action = reply
        transport.source = source_record([{"type": "image", "data": {"url": "https://images.example.org/a"}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("failed", "")
        assert [record["action"] for record in transport.sent] == ["send_msg"]
        assert len([name for name, _ in transport.queries if name == "get_msg"]) == 1
        await client.close()
    asyncio.run(scenario())


def test_回查目标不存在按明确失败() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "get_msg" and params != {"message_id": SOURCE_MESSAGE_ID}:
                return envelope(None, status="failed", retcode=100, message="message not found")
            return None
        transport.on_action = reply
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("failed", TARGET_MESSAGE_ID)
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    envelope({}),
    envelope({"res_id": CARD_RESOURCE_ID}),
    envelope({"message_id": ""}),
])
def test_响应缺少可核实目标ID记待核实且禁止重发(response: dict[str, object]) -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = CardOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_group_forward_msg":
                return response
            return None
        transport.on_action = reply
        transport.source = source_record([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", "")
        assert [record["action"] for record in transport.sent] == ["send_group_forward_msg"]
        await client.close()
    asyncio.run(scenario())


def test_回查四项证据不符记待核实() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "get_msg" and params != {"message_id": SOURCE_MESSAGE_ID}:
                return envelope({"message_id": TARGET_MESSAGE_ID, "message_type": "group",
                                 "group_id": 999, "user_id": 10001, "message_seq": 42})
            return None
        transport.on_action = reply
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", TARGET_MESSAGE_ID)
        assert "待核实" in result.reason
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    envelope({"message_id": TARGET_MESSAGE_ID}, status="async"),
    envelope({"message_id": True}),
    envelope({"message_id": TARGET_MESSAGE_ID}, status="unexpected"),
    envelope([], status="ok"),
    envelope({"message_id": TARGET_MESSAGE_ID}, status="failed", retcode=1404, message="unknown action"),
])
def test_受理或契约不足不伪报成功失败也不另发评论(response: dict[str, object]) -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "send_msg":
                return response
            return None
        transport.on_action = reply
        transport.source = source_record([{"type": "image", "data": {"url": "https://images.example.org/a"}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await prepared_source(transport)
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert (result.status, result.target_message_id) == ("unknown", "")
        assert [record["action"] for record in transport.sent] == ["send_msg"]
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("point", "status"), [("account", "failed"), ("submit", "unknown")])
def test_取消按实际提交边界分类且不迟到发送(point: str, status: str) -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        # 图片路径固定走 send_msg：submit 挂起点与分派动作一一对应。
        transport.source = source_record([{"type": "image", "data": {"url": "https://images.example.org/a"}}])
        prepared = await prepared_source(transport)  # 只读准备先完成，回调只挂在发送阶段
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        action = {"account": "get_login_info", "submit": "send_msg"}[point]
        async def reply(name: str, value: object) -> object:
            if name == action:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # 取消传播后不再死等：迟到只留标记（raise 让取消如实结束，
                    # 不让 asyncio.run 收尾挂在未释放的工作上）。
                    finished.set()
                    raise
                finally:
                    finished.set()
            return None
        transport.on_action = reply
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        await entered.wait()
        task.cancel()
        await spin_until(task.done)
        assert task.result().status == status
        release.set()
        await finished.wait()
        for _ in range(30):
            await asyncio.sleep(0)
        assert len(transport.sent) == (0 if point == "account" else 1)
        await client.close()
    asyncio.run(scenario())


def test_发送工作超时按提交边界记待核实且不重发() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        # 图片路径固定走 send_msg：超时推进发生在提交写入之后。
        transport.source = source_record([{"type": "image", "data": {"url": "https://images.example.org/a"}}])
        prepared = await prepared_source(transport)  # 只读准备先完成，回调只挂在发送阶段
        release, entered = asyncio.Event(), asyncio.Event()
        async def reply(action: str, params: object) -> object:
            if action == "send_msg":
                entered.set()
                await release.wait()
            return None
        transport.on_action = reply
        clock = FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        await entered.wait()
        clock.advance(11)  # 推进超过单动作总预算（代码常量 10 秒）
        await spin_until(task.done)
        result = task.result()
        assert result.status == "unknown"  # 已提交写入：保守待核实，不重发
        assert len(transport.sent) == 1
        release.set()
        await spin_until(lambda: True)
        await client.close()
    asyncio.run(scenario())


def test_回查次数与总预算有界() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = MemoryOneBotTransport()
        async def reply(action: str, params: object) -> object:
            if action == "get_msg" and params != {"message_id": SOURCE_MESSAGE_ID}:
                return envelope(None, status="failed", retcode=1400, message="bad request")
            return None
        transport.on_action = reply
        clock = FakeClock()
        client = SnowLumaClient(transport=transport, clock=clock, config_generation=1)
        prepared = await prepared_source(transport)
        task = asyncio.create_task(client.send_source(SourceSendRequest(prepared), before_submit=lambda: True))
        while not task.done():
            await asyncio.sleep(0)
            if clock.sleeps:  # 推进注入时钟越过轮询间隔与回查总预算（代码常量）
                clock.advance(0.6)
        result = task.result()
        assert result.status == "unknown"
        assert len([name for name, _ in transport.queries if name == "get_msg"]) <= 9  # 准备 1 + 回查 ≤8
        await client.close()
    asyncio.run(scenario())


def test_关闭异常与敏感载荷不回显(caplog: pytest.LogCaptureFixture) -> None:
    from maimai_courier_plugin.snowluma_transport import OneBotUnavailable

    async def scenario() -> None:
        class FailedClose(MemoryOneBotTransport):
            async def close(self) -> None:
                raise RuntimeError("TOKEN-test 秘密正文 秘密评论 resource-test-A https://images.example.org/?signature=SECRET")

        client = SnowLumaClient(transport=FailedClose(), clock=FakeClock(), config_generation=1)
        with pytest.raises(OneBotUnavailable) as caught:
            await client.close()
        for value in ("TOKEN-test", "秘密正文", "秘密评论", "resource-test-A", "signature=SECRET"):
            assert value not in str(caught.value) + caplog.text
    asyncio.run(scenario())


def test_工作结束释放临时正文而结果仍可定位() -> None:
    import gc
    import weakref
    from maimai_courier_plugin.shared.send_steps import SourceSendRequest

    class TrackedText(str):
        """仅为垃圾回收可观测性保留弱引用；不改变字符串行为。"""

    async def scenario() -> None:
        transport = MemoryOneBotTransport()
        transport.source = source_record([{"type": "text", "data": {"text": TrackedText("秘密正文")}}])
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_forward())
        assert isinstance(prepared, PreparedSource)
        body_ref = weakref.ref(prepared.material.segments[0]["data"]["content"][0]["data"]["text"])
        result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        assert result.status == "sent"
        assert "秘密正文" not in repr(result)
        del prepared
        transport.sent.clear()
        transport.source["message"] = []
        for _ in range(30):
            await asyncio.sleep(0)
        gc.collect()
        assert body_ref() is None
        await client.close()
    asyncio.run(scenario())
