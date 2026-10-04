"""4.3 单张原卡片资源发送。

判据（tasks.md 4.3 与 design D3）：

- 发送引用必须来自已核实源卡片：只取唯一顶层 `forward.data.id`；
- 宿主节点不完整、卡片内部含回复/提及/媒体、或超过原插件递归深度，都不触发递归
  清洗或拒绝——服务端不展开、不重建、不上传节点；
- 模拟网关恰收一次原资源引用（单一 `forward` 段），不调用任何节点上传/重建动作；
- 资源引用缺失或为空、顶层卡片与其它内容混排，都在发送前整条拒绝。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from service.gateway import PrepareRejected, PreparedSource, SnowLumaGateway
from service.materials import ERROR_SOURCE_RESOURCE_MISSING, ERROR_SOURCE_SHAPE_UNSUPPORTED
from shared.send_steps import SourceSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
    store_source_message,
)

CARD_RESOURCE_ID = "res-abc"
"""合成原卡片资源引用（不含账号、不代表任何真实资源）。"""

ALLOWED_OPERATIONS = frozenset({"get_login_info", "get_group_list", "get_msg", "send_msg"})
"""本组允许出现的网关动作：任何节点上传/重建动作都不在允许面内。"""


def _gateway(onebot: FakeOneBot) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=FakeClock())


def _prepare_and_send(
    onebot: FakeOneBot,
) -> tuple[PreparedSource | PrepareRejected, Any]:
    gateway = _gateway(onebot)
    prepared = asyncio.run(
        gateway.prepare_source(
            source_group_id=SOURCE_GROUP_ID,
            source_message_id=SOURCE_MESSAGE_ID,
            target_group_id=TARGET_GROUP_ID,
        )
    )
    if isinstance(prepared, PrepareRejected):
        return prepared, None
    result = asyncio.run(
        gateway.send_source(
            SourceSendRequest(
                delivery_id="d-1",
                attempt_id="a-1",
                target_group_id=TARGET_GROUP_ID,
                material=prepared.material,
            )
        )
    )
    return prepared, result


def test_单张原卡片准备并只发送一次原资源引用() -> None:
    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}],
    )

    prepared, result = _prepare_and_send(onebot)

    assert isinstance(prepared, PreparedSource), prepared
    assert prepared.material.kind == "forward"
    assert prepared.material.forward_resource_id == CARD_RESOURCE_ID
    assert result.status == "sent", result
    assert onebot.send_count == 1, "同一请求只允许一次平台发送"
    assert onebot.send_calls[0].target == TARGET_GROUP_ID
    assert onebot.send_calls[0].detail["message"] == [
        {"type": "forward", "data": {"id": CARD_RESOURCE_ID}}
    ], "只发送原资源引用段，不添加其它内容"
    operations = {call.operation for call in onebot.calls}
    assert operations <= ALLOWED_OPERATIONS, operations


def test_卡片内部结构与递归深度都不触发清洗或重建() -> None:
    deep_inner: Mapping[str, Any] = {"type": "text", "data": {"text": "深层内容"}}
    for level in range(6):  # 超过原插件递归上限 3 层，仍不得触发清洗或拒绝
        deep_inner = {"type": "forward", "data": {"id": f"inner-{level}", "nodes": [deep_inner]}}
    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[
            {
                "type": "forward",
                "data": {
                    "id": CARD_RESOURCE_ID,
                    # 宿主展开材料/内部回复/提及/媒体只作为数据存在：不递归、不清洗
                    "nodes": [deep_inner],
                    "messages": [
                        {
                            "content": [
                                {"type": "reply", "data": {"id": "1"}},
                                {"type": "at", "data": {"qq": "all"}},
                                {"type": "image", "data": {"file": "/tmp/宿主本机路径.jpg"}},
                            ]
                        }
                    ],
                },
            }
        ],
    )

    prepared, result = _prepare_and_send(onebot)

    assert isinstance(prepared, PreparedSource), prepared
    assert result.status == "sent", result
    assert onebot.send_count == 1
    assert onebot.send_calls[0].detail["message"] == [
        {"type": "forward", "data": {"id": CARD_RESOURCE_ID}}
    ], "只保留唯一顶层资源引用：不递归、不重建、不清洗内部材料"
    assert {call.operation for call in onebot.calls} <= ALLOWED_OPERATIONS


def test_资源引用缺失或为空在发送前整条拒绝() -> None:
    for label, data in [
        ("缺 id 字段", {}),
        ("id 为空串", {"id": ""}),
        ("id 为空白", {"id": "   "}),
        ("id 类型非法", {"id": ["res"]}),
    ]:
        onebot = FakeOneBot()
        store_source_message(
            onebot,
            message_id=SOURCE_MESSAGE_ID,
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "forward", "data": data}],
        )

        prepared, _ = _prepare_and_send(onebot)

        assert isinstance(prepared, PrepareRejected), (label, prepared)
        assert prepared.error == ERROR_SOURCE_RESOURCE_MISSING, (label, prepared)
        assert onebot.send_count == 0, (label, "资源缺失必须在发送前拒绝")


def test_顶层卡片与其它内容混排整条拒绝() -> None:
    for label, message in [
        ("卡片后跟文字", [{"type": "forward", "data": {"id": CARD_RESOURCE_ID}}, {"type": "text", "data": {"text": "额外"}}]),
        ("文字后跟卡片", [{"type": "text", "data": {"text": "额外"}}, {"type": "forward", "data": {"id": CARD_RESOURCE_ID}}]),
        ("两张卡片", [{"type": "forward", "data": {"id": "res-1"}}, {"type": "forward", "data": {"id": "res-2"}}]),
    ]:
        onebot = FakeOneBot()
        store_source_message(
            onebot, message_id=SOURCE_MESSAGE_ID, group_id=SOURCE_GROUP_ID, message=message
        )

        prepared, _ = _prepare_and_send(onebot)

        assert isinstance(prepared, PrepareRejected), (label, prepared)
        assert prepared.error == ERROR_SOURCE_SHAPE_UNSUPPORTED, (label, prepared)
        assert onebot.send_count == 0, (label, "顶层混排必须在发送前拒绝")
