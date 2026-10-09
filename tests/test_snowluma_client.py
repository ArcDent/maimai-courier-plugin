"""客户端核心路径回归：卡片分支（展开查询→发送前类型检查→节点校验→单次卡片发送）、
文字/图片路径与私聊目标（好友核验、私聊路由分派与私聊回执核验）。

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
from maimai_courier_plugin.snowluma_transport import OneBotUnavailable
from maimai_courier_plugin.type_check import NESTED_FORWARD_DETAIL, NESTED_FORWARD_REASON
from tests.snowluma_client_support import (
    CARD_RESOURCE_ID, NESTED_EXPANDED_CARD, SOURCE_MESSAGE_ID, TARGET_MESSAGE_ID,
    MemoryOneBotTransport, host_message, trusted_forward,
)
from tests.support import (
    PRIVATE_FRIENDS, PRIVATE_USER_ID, SENDER_CARD, FakeClock, envelope, source_record, spin_until,
)


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
    """复用基类替身的动作面，注入卡片链路合成展开应答的内存替身。

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










# ---------------------------------------------------------------------------
# 私聊目标（双形态目标的第二种形式）：好友核验、私聊分派与私聊回执四项
# ---------------------------------------------------------------------------

class PrivateOneBotTransport(MemoryOneBotTransport):
    """私聊目标路径的内存替身：命名标记，动作协议面由基类单点提供。

    基类按实测口径（6.10）提供 `get_friend_list` 只读应答与 `send_msg` /
    `send_forward_msg` 的私聊路由（`message_type` 为私聊加对端账号），不引入具名
    私聊发送动作、不更换动作；群目标路径的请求体与基类逐字一致。名册样本取共享
    `tests.support.PRIVATE_FRIENDS`，本类不覆写任何动作。
    """

    def __init__(self) -> None:
        super().__init__()
        self.friends: list[dict[str, Any]] = deepcopy(PRIVATE_FRIENDS)

    async def _reply(self, action: str, params: Any) -> dict[str, Any]:
        if action == "get_friend_list":
            if self.on_action is not None:
                outcome = self.on_action(action, params)
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                if outcome is not None:
                    return outcome
            return envelope(deepcopy(self.friends))
        return await super()._reply(action, params)


class PrivateCardTransport(PrivateOneBotTransport):
    """私聊卡片链路替身：好友名册 + 合成展开应答 + 私聊路由三个注入点。"""

    def __init__(self) -> None:
        super().__init__()
        self.expanded = expanded_card()


def private_receipt(
    *, message_id: Any = TARGET_MESSAGE_ID, target_id: Any = PRIVATE_USER_ID,
    user_id: Any = 10001, message_seq: Any = 42, message_type: str = "private",
) -> dict[str, Any]:
    """回查私聊消息记录：`message_type=private`、`target_id` 为对端、`user_id` 为
    机器人自身、正整数 `message_seq`（实测 6.10 的私聊回执字段语义）。"""

    return {"message_id": message_id, "message_type": message_type, "target_id": target_id,
            "user_id": user_id, "message_seq": message_seq}



async def trusted_private_forward(
    message: dict[str, Any] | None = None, *,
    target_user_id: str = PRIVATE_USER_ID,
) -> Any:
    """经公开凭据表与本地预检取得私聊目标的 `PreparedForward`（分组 private=True）。"""

    from types import SimpleNamespace

    from maimai_courier_plugin.config import CourierConfig
    from maimai_courier_plugin.forward_precheck import precheck_forward_request
    from maimai_courier_plugin.source_binding import SourceBindingTable

    stored = host_message(SOURCE_MESSAGE_ID) if message is None else message

    async def streams(**kwargs: Any) -> list[dict[str, Any]]:
        return [{"session_id": "s-test", "platform": "qq", "group_id": "111",
                 "account_id": "10001", "scope": "qq", "is_group_session": True}]

    async def get_by_id(query_id: str, **kwargs: Any) -> dict[str, Any]:
        assert query_id == SOURCE_MESSAGE_ID and kwargs == {"chat_id": "s-test", "include_binary_data": True}
        return stored

    ctx = SimpleNamespace(
        chat=SimpleNamespace(get_all_streams=streams),
        message=SimpleNamespace(get_by_id=get_by_id),
    )
    bindings = SourceBindingTable()
    arguments = {"message_id": SOURCE_MESSAGE_ID, "target_user_id": target_user_id, "comment": ""}
    items = bindings.sanitize_and_bind_output_items("s-test", [{
        "item_type": "FunctionCallItem",
        "tool_call": {"func_name": "forward_message", "args": arguments},
    }])
    config = CourierConfig(plugin={"enabled": True},
                           scope={"groups": [{"name": "客户端测试分组", "source_groups": ["111"],
                                              "target_groups": ["222"], "private": True}]})
    return await precheck_forward_request(
        ctx=ctx, config=config, bindings=bindings, arguments=items[0]["tool_call"]["args"],
    )


async def prepare_private(
    transport: PrivateOneBotTransport, *, config_generation: int = 7,
) -> PreparedSource | PrepareRejected:
    """私聊目标的只读准备：与群路径同一公开入口，只换目标形态。"""

    return await SnowLumaClient(
        transport=transport, clock=FakeClock(), config_generation=config_generation,
    ).prepare_source(await trusted_private_forward())


def test_准备在账号核对后查名册确认私聊目标为好友() -> None:
    async def scenario() -> None:
        transport = PrivateOneBotTransport()
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=7)
        prepared = await client.prepare_source(await trusted_private_forward())
        assert isinstance(prepared, PreparedSource)
        # 目标形态与对端在准备结果单点持有：群号字段为空，不两处各存一份
        assert (prepared.target_kind, prepared.target_user_id) == ("private", PRIVATE_USER_ID)
        assert prepared.target_group_id == ""
        assert (prepared.account_id, prepared.source_group_id, prepared.source_message_id) == (
            "10001", "111", SOURCE_MESSAGE_ID)
        # 顺序（design D6）：账号核对 → 好友名册 → 同账号源查询；不查群名册
        assert [name for name, _ in transport.queries] == [
            "get_login_info", "get_friend_list", "get_msg"]
        assert transport.sent == []
        await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("fault", "error"), [
    ("not_friend", "target_unavailable"),
    ("shape", "friend_list_unavailable"),
    ("not_list", "friend_list_unavailable"),
    ("business", "friend_list_unavailable"),
    ("unavailable", "source_query_unavailable"),
])
def test_准备阶段好友核验不符返回可定位拒绝且零发送(fault: str, error: str) -> None:
    async def scenario() -> None:
        transport = PrivateOneBotTransport()
        if fault == "not_friend":
            transport.friends = [{"user_id": 30002, "nickname": "好友乙", "remark": ""}]
        elif fault == "shape":
            # 名册形状异常：一个条目标识不可确认即整条拒绝，不猜测、不静默放行
            transport.friends = [{"user_id": 30001, "nickname": "好友甲"}, {"nickname": "无标识"}]
        elif fault == "not_list":
            transport.friends = {"friends": [{"user_id": 30001, "nickname": "好友甲"}]}
        elif fault == "business":
            async def reply(action: str, params: object) -> object:
                if action == "get_friend_list":
                    return envelope(None, status="failed", retcode=100, message="synthetic-roster-failure")
                return None
            transport.on_action = reply
        else:
            async def reply(action: str, params: object) -> object:
                if action == "get_friend_list":
                    raise OneBotUnavailable("timeout", submitted=False)
                return None
            transport.on_action = reply
        result = await prepare_private(transport)
        assert isinstance(result, PrepareRejected)
        assert result.error == error
        assert transport.sent == []
        # 好友核验在源查询之前（design D6）：三种拒绝都不查源消息、不发请求
        assert not any(name == "get_msg" for name, _ in transport.queries)
    asyncio.run(scenario())


def test_群目标路径不查好友名册() -> None:
    async def scenario() -> None:
        transport = PrivateOneBotTransport()
        result = await SnowLumaClient(
            transport=transport, clock=FakeClock(), config_generation=1,
        ).prepare_source(await trusted_forward())
        assert isinstance(result, PreparedSource)
        assert (result.target_kind, result.target_user_id) == ("group", "")
        assert [name for name, _ in transport.queries] == [
            "get_login_info", "get_group_list", "get_msg"]
        assert transport.sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize(("message", "action", "params"), [
    # 私聊卡片与文字走 send_forward_msg 私聊路由（实测 6.10）
    ([{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}],
     "send_forward_msg",
     {"message_type": "private", "user_id": PRIVATE_USER_ID, "messages": list(expanded_nodes())}),
    ([{"type": "text", "data": {"text": "普通文字"}}],
     "send_forward_msg",
     {"message_type": "private", "user_id": PRIVATE_USER_ID,
      "messages": [{"type": "node", "data": {"uin": "20002", "name": SENDER_CARD,
                                             "content": [{"type": "text", "data": {"text": "普通文字"}}]}}]}),
    # 私聊图片走 send_msg 私聊路由（实测 6.10）
    ([{"type": "text", "data": {"text": "图"}},
      {"type": "image", "data": {"url": "https://images.example.org/a"}}],
     "send_msg",
     {"message_type": "private", "user_id": PRIVATE_USER_ID,
      "message": [{"type": "text", "data": {"text": "图"}},
                  {"type": "image", "data": {"url": "https://images.example.org/a"}}]}),
])
def test_私聊三类源按既有动作分派且只改路由参数(
    message: list[dict[str, object]], action: str, params: dict[str, object],
) -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        transport = PrivateCardTransport()
        transport.source = source_record(message)
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        prepared = await client.prepare_source(await trusted_private_forward())
        assert isinstance(prepared, PreparedSource)
        await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
        # 目标形态只影响路由参数：动作仍限于固定集合，不因私聊更换动作或引入具名动作
        assert [record["action"] for record in transport.sent] == [action]
        assert transport.sent[0]["params"] == params
        await client.close()
    asyncio.run(scenario())


def test_私聊评论以普通文本走send_msg私聊路由且空白评论不发出请求() -> None:
    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import CommentSendRequest
        transport = PrivateOneBotTransport()
        prepared = await prepare_private(transport, config_generation=1)
        assert isinstance(prepared, PreparedSource)
        client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
        await client.send_comment(CommentSendRequest(prepared, "评论文本"), before_submit=lambda: True)
        assert transport.sent == [{"action": "send_msg",
                                   "params": {"message_type": "private", "user_id": PRIVATE_USER_ID,
                                              "message": [{"type": "text", "data": {"text": "评论文本"}}]}}]
        blank = await client.send_comment(CommentSendRequest(prepared, "   "), before_submit=lambda: True)
        assert blank.status == "failed"
        assert [record["action"] for record in transport.sent] == ["send_msg"]
        await client.close()
    asyncio.run(scenario())


async def send_private_with_receipt(receipt: object, *, returned_id: Any = TARGET_MESSAGE_ID) -> Any:
    """私聊源发送 + 给定回执应答：只经公开入口驱动，回执由 `on_action` 注入。"""

    from maimai_courier_plugin.shared.send_steps import SourceSendRequest

    transport = PrivateOneBotTransport()

    async def reply(action: str, params: object) -> object:
        if action == "get_msg" and params != {"message_id": SOURCE_MESSAGE_ID}:
            return receipt if isinstance(receipt, dict) else envelope(receipt)
        if action == "send_forward_msg":
            return envelope({"message_id": returned_id, "res_id": CARD_RESOURCE_ID,
                             "forward_id": "fwd-abc"})
        return None

    transport.on_action = reply
    prepared = await prepare_private(transport, config_generation=1)
    assert isinstance(prepared, PreparedSource)
    client = SnowLumaClient(transport=transport, clock=FakeClock(), config_generation=1)
    result = await client.send_source(SourceSendRequest(prepared), before_submit=lambda: True)
    await client.close()
    return result


@pytest.mark.parametrize("returned_id", [-23456, 67890])
def test_私聊回执按对端核验通过且正负ID都按原值记sent(returned_id: int) -> None:
    async def scenario() -> None:
        # 私聊回执：message_type=private、target_id 为对端、user_id 为机器人自身、
        # 正整数 message_seq（实测 6.10）；目标消息 ID 正负都按原值逐字比对。
        result = await send_private_with_receipt(
            envelope(private_receipt(message_id=returned_id)), returned_id=returned_id)
        assert (result.status, result.target_message_id) == ("sent", str(returned_id))
        assert CARD_RESOURCE_ID not in repr(result), "资源类标识不作证据、不进结果"
    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", [
    private_receipt(message_id=TARGET_MESSAGE_ID, target_id="99999"),   # 对端不是本次目标
    private_receipt(message_id=TARGET_MESSAGE_ID, user_id="99999"),     # 账号不是机器人自身
    private_receipt(message_id=TARGET_MESSAGE_ID, message_type="group", target_id=222),  # 回执是群消息
    private_receipt(message_id=TARGET_MESSAGE_ID, message_seq=0),       # 平台序列非正整数
    private_receipt(message_id=TARGET_MESSAGE_ID, message_seq=-7),
    private_receipt(message_id=TARGET_MESSAGE_ID, message_seq="42"),
    private_receipt(message_id=-11111),                                 # 回执不属于本次投递
    {"message_id": TARGET_MESSAGE_ID, "message_type": "private", "user_id": 10001},  # 缺对端与序列
])
def test_私聊回执不属于本次投递或缺证据归待核实(receipt: dict[str, object]) -> None:
    async def scenario() -> None:
        result = await send_private_with_receipt(envelope(receipt))
        assert result.status == "unknown"
        assert result.target_message_id == TARGET_MESSAGE_ID
        assert "待核实" in result.reason
    asyncio.run(scenario())


def test_私聊图片与群路径共用同一段构造且本地file引用仍按既有媒体规则拒绝() -> None:
    """图片到私聊沿用 `send_material` 的 `url` https 段形态（design D12）：两种
    目标形态对同一源消息提交完全相同的段序列，不新增图片规则。"""

    async def scenario() -> None:
        from maimai_courier_plugin.shared.send_steps import SourceSendRequest
        raw = [{"type": "text", "data": {"text": "图"}},
               {"type": "image", "data": {"url": "https://images.example.org/a"}}]
        expected = [{"type": "text", "data": {"text": "图"}},
                    {"type": "image", "data": {"url": "https://images.example.org/a"}}]

        group_transport = PrivateOneBotTransport()
        group_transport.source = source_record(raw)
        group_client = SnowLumaClient(transport=group_transport, clock=FakeClock(), config_generation=1)
        group_prepared = await group_client.prepare_source(await trusted_forward())
        assert isinstance(group_prepared, PreparedSource)
        await group_client.send_source(SourceSendRequest(group_prepared), before_submit=lambda: True)
        await group_client.close()

        private_transport = PrivateCardTransport()
        private_transport.source = source_record(raw)
        private_client = SnowLumaClient(transport=private_transport, clock=FakeClock(), config_generation=1)
        private_prepared = await private_client.prepare_source(await trusted_private_forward())
        assert isinstance(private_prepared, PreparedSource)
        await private_client.send_source(SourceSendRequest(private_prepared), before_submit=lambda: True)
        await private_client.close()

        # 同一段构造：只有路由参数（message_type / 目标标识）按形态不同
        assert group_transport.sent[0]["action"] == private_transport.sent[0]["action"] == "send_msg"
        assert group_transport.sent[0]["params"]["message"] == expected
        assert private_transport.sent[0]["params"]["message"] == expected

        # 本地 file 引用：群与私聊两条路径都在准备阶段按既有媒体规则整条拒绝
        file_reference = [{"type": "image", "data": {"file": "file:///tmp/合成图片.png"}}]
        group_file = MemoryOneBotTransport()
        group_file.source = source_record(file_reference)
        group_result = await SnowLumaClient(
            transport=group_file, clock=FakeClock(), config_generation=1,
        ).prepare_source(await trusted_forward())
        private_file = PrivateOneBotTransport()
        private_file.source = source_record(file_reference)
        private_result = await prepare_private(private_file, config_generation=1)
        assert isinstance(group_result, PrepareRejected) and isinstance(private_result, PrepareRejected)
        assert group_result.error == private_result.error == "source_media_unsafe"
        assert group_file.sent == [] and private_file.sent == []
    asyncio.run(scenario())
