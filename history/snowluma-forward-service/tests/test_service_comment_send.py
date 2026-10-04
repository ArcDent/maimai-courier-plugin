"""4.6 评论普通文本发送。

判据（tasks.md 4.6 与 design D3、cross-group-forward「可选评论固定延迟」）：

- 评论原文只成为**单个普通文字段**：命令样式、协议语法或提示文本都不产生其它
  消息段，不解析成消息段或命令；
- 不写入原作者卡片节点、不调用模型生成（发送文本与本次参数逐字相同）；
- 空白输入不进入网关发送（连只读查询也不发生）。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio

from service.gateway import SnowLumaGateway
from shared.send_steps import CommentSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    STAGE_COMMENT,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
)


def _gateway(onebot: FakeOneBot) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=FakeClock())


def _comment_request(text: str) -> CommentSendRequest:
    return CommentSendRequest(
        delivery_id="d-1", attempt_id="a-1", target_group_id=TARGET_GROUP_ID, comment=text
    )


def test_评论原文只成为单个普通文字段() -> None:
    comments = [
        "普通评论",
        "/admin [CQ:at,qq=all] <msg> {{提示模板}}",
        "```python\neval(input())\n```",
        "第一行\n第二行\t制表符",
    ]
    for comment in comments:
        onebot = FakeOneBot()
        gateway = _gateway(onebot)

        result = asyncio.run(gateway.send_comment(_comment_request(comment)))

        assert result.status == "sent", (comment, result)
        assert onebot.send_count == 1, (comment, "同一请求只允许一次平台发送")
        message = onebot.send_calls[0].detail["message"]
        assert message == [{"type": "text", "data": {"text": comment}}], (
            comment,
            "评论逐字进入单个普通文字段：不解析段、不拆消息、不加前缀",
        )
        assert onebot.send_calls[0].target == TARGET_GROUP_ID, comment


def test_评论发送不触碰原卡片节点也不产生其它段() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)

    result = asyncio.run(gateway.send_comment(_comment_request("只看这个 [CQ:forward,id=res-abc]")))

    assert result.status == "sent", result
    message = onebot.send_calls[0].detail["message"]
    assert len(message) == 1, message
    assert all(segment["type"] != "forward" for segment in message), (
        "评论不写入原作者卡片节点，也不产生卡片段"
    )
    assert set(onebot.query_counts) == {"get_login_info", "get_msg"}, onebot.query_counts
    assert onebot.query_counts["get_msg"] == 1, "评论只有回执核实查询：不解析协议、不查询卡片节点"


def test_空白评论不进入网关发送() -> None:
    for comment in ["", "   ", "\n\t ", "\u3000"]:
        onebot = FakeOneBot()
        gateway = _gateway(onebot)

        result = asyncio.run(gateway.send_comment(_comment_request(comment)))

        assert result.status == "failed", (comment, result)
        assert onebot.send_count == 0, (comment, "空白评论不得发送")
        assert onebot.calls == [], (comment, "空白评论连只读查询都不应发生")


def test_评论阶段可在公开接缝上暂停与放行() -> None:
    onebot = FakeOneBot()
    gateway = _gateway(onebot)
    gates = onebot.gates
    step = gateway.send_comment
    request = _comment_request("等待评论阶段")

    async def scenario() -> tuple[bool, object]:
        async def gated(call: CommentSendRequest) -> object:
            await gates.arrive(STAGE_COMMENT)
            return await step(call)

        gates.pause(STAGE_COMMENT)
        task = asyncio.ensure_future(gated(request))
        for _ in range(50):
            if gates.arrivals.get(STAGE_COMMENT):
                break
            await asyncio.sleep(0)
        blocked = not task.done()
        gates.release(STAGE_COMMENT)
        return blocked, await task

    blocked_at_gate, result = asyncio.run(scenario())

    assert blocked_at_gate is True, "评论阶段必须能在公开接缝上暂停"
    assert getattr(result, "status", "") == "sent", result
    assert onebot.send_count == 1
