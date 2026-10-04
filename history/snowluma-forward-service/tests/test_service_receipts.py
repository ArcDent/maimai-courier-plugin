"""4.7 源和评论的真实回执核实。

判据（tasks.md 4.7 与 design D8）：

- 只有成功响应**且**匹配目标群、绑定发送者与可证明的平台消息身份（权威群序列）
  才确认成功；缺 ID、本地不透明 ID、错误目标、身份不符与超出有界查询期都按未知；
- 源与评论分别返回各自对应的真实目标 ID；
- 查询次数有界，且绝不通过再次发送确认。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Mapping, Sequence

from service.gateway import (
    MAX_RECEIPT_QUERY_ATTEMPTS,
    RECEIPT_VERIFY_INTERVAL_SECONDS,
    RECEIPT_VERIFY_TIMEOUT_SECONDS,
    SnowLumaGateway,
)
from shared.send_steps import CommentSendRequest, SourceMaterial, SourceSendRequest
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
)

SOURCE_MATERIAL = SourceMaterial(
    kind="forward", forward_resource_id="res-abc"
)
"""合成源材料：只带资源引用（回执核实用例不依赖源准备）。"""


def _expected_message_id(sequence: int, group_id: str) -> str:
    """按第 1 组 1.4 核实的算法形态独立复算权威消息 ID（合成序列与群号）。"""

    digest = hashlib.sha1(f"{sequence}:{group_id}:group_message".encode("utf-8")).digest()
    return str(int.from_bytes(digest[:4], "big", signed=True))


def _gateway(onebot: Any, clock: FakeClock) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=BOUND_ACCOUNT_ID, clock=clock)


def _source_request() -> SourceSendRequest:
    return SourceSendRequest(
        delivery_id="d-1",
        attempt_id="a-1",
        target_group_id=TARGET_GROUP_ID,
        material=SOURCE_MATERIAL,
    )


def _comment_request(text: str = "评论正文") -> CommentSendRequest:
    return CommentSendRequest(
        delivery_id="d-1", attempt_id="a-1", target_group_id=TARGET_GROUP_ID, comment=text
    )


class _TamperingTransport:
    """传输包装：把 `get_msg` 返回的存储副本按注入的变形函数改写（模拟身份不符）。"""

    def __init__(self, inner: FakeOneBot, mutate: Any) -> None:
        self._inner = inner
        self._mutate = mutate

    async def get_login_info(self) -> Mapping[str, Any]:
        return await self._inner.get_login_info()

    async def get_group_list(self) -> Mapping[str, Any]:
        return await self._inner.get_group_list()

    async def send_msg(
        self,
        *,
        target_group_id: str,
        message: Sequence[Mapping[str, Any]],
        stage: str = "",
    ) -> Mapping[str, Any]:
        return await self._inner.send_msg(
            target_group_id=target_group_id, message=message, stage=stage
        )

    async def get_msg(self, message_id: str) -> Mapping[str, Any]:
        response = await self._inner.get_msg(message_id)
        data = response.get("data")
        if isinstance(data, dict):
            self._mutate(data)
        return response


def test_成功回执与可核实身份才确认成功并给出真实目标ID() -> None:
    onebot = FakeOneBot()
    clock = FakeClock()
    gateway = _gateway(onebot, clock)

    result = asyncio.run(gateway.send_source(_source_request()))

    assert result.status == "sent", result
    assert result.target_message_id == _expected_message_id(1, TARGET_GROUP_ID), result
    assert onebot.query_counts["get_msg"] == 1, "成功路径只需一次只读核实查询"
    assert onebot.send_count == 1
    assert onebot.send_calls[0].target == TARGET_GROUP_ID


def test_源与评论分别返回各自对应的真实目标ID() -> None:
    onebot = FakeOneBot()
    clock = FakeClock()
    gateway = _gateway(onebot, clock)

    source = asyncio.run(gateway.send_source(_source_request()))
    comment = asyncio.run(gateway.send_comment(_comment_request()))

    assert source.status == "sent" and comment.status == "sent", (source, comment)
    assert source.target_message_id == _expected_message_id(1, TARGET_GROUP_ID), source
    assert comment.target_message_id == _expected_message_id(2, TARGET_GROUP_ID), comment
    assert source.target_message_id != comment.target_message_id, "源与评论各返回对应目标 ID"
    assert onebot.send_count == 2


def test_缺ID与本地不透明ID都按未知处理() -> None:
    missing = FakeOneBot(include_message_id=False)
    missing_result = asyncio.run(_gateway(missing, FakeClock()).send_source(_source_request()))
    assert missing_result.status == "unknown", missing_result
    assert missing_result.target_message_id == ""
    assert missing.send_count == 1, "缺 ID 不重发、不换通路"
    assert missing.query_counts.get("get_msg", 0) == 0, "没有可查询的 ID：不做无谓查询"

    opaque = FakeOneBot(authoritative_receipts=False)
    opaque_result = asyncio.run(_gateway(opaque, FakeClock()).send_source(_source_request()))
    assert opaque_result.status == "unknown", opaque_result
    assert opaque_result.target_message_id == ""
    assert opaque.send_count == 1, "本地不透明 ID 不得当成成功证据，也不重发"
    assert opaque.query_counts["get_msg"] == 1, "本地不透明 ID 一次查询即可定性，不空转"


def test_目标群或发送者不符按未知处理() -> None:
    cases: list[tuple[str, Any]] = [
        ("目标群不符", lambda data: data.update({"group_id": "30003"})),
        ("发送者不是绑定账号", lambda data: data.update({"user_id": 77777})),
        ("缺少可证明的群序列", lambda data: data.update({"message_seq": 0})),
        ("缺少目标群字段", lambda data: data.pop("group_id", None)),
    ]
    for label, mutate in cases:
        onebot = FakeOneBot()
        gateway = _gateway(_TamperingTransport(onebot, mutate), FakeClock())

        result = asyncio.run(gateway.send_source(_source_request()))

        assert result.status == "unknown", (label, result)
        assert result.target_message_id == "", (label, result)
        assert onebot.send_count == 1, (label, "身份不符不得通过再次发送确认")


def test_返回记录不是被查询消息时按未知处理() -> None:
    def swap_message_id(data: dict[str, Any]) -> None:
        raw = data.get("message_id")
        data["message_id"] = raw + 1 if isinstance(raw, int) else "other-message"

    onebot = FakeOneBot()
    gateway = _gateway(_TamperingTransport(onebot, swap_message_id), FakeClock())

    result = asyncio.run(gateway.send_source(_source_request()))

    assert result.status == "unknown", result
    assert result.target_message_id == "", result
    assert onebot.send_count == 1, "返回记录不是被查询消息时不得当作成功，也不重发"


def test_查询暂不可用时有界重试后按未知处理且绝不重发() -> None:
    onebot = FakeOneBot()
    onebot.fail_always(
        "get_msg", {"status": "failed", "retcode": 100, "message": "message not found"}
    )
    clock = FakeClock()
    gateway = _gateway(onebot, clock)

    async def scenario() -> Any:
        task = asyncio.ensure_future(gateway.send_source(_source_request()))
        for _ in range(40):
            if task.done():
                break
            # 每轮推进一个轮询间隔（注入时钟），直到有界查询期结束
            clock.advance(RECEIPT_VERIFY_INTERVAL_SECONDS)
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
        return await task

    result = asyncio.run(scenario())

    assert result.status == "unknown", result
    assert onebot.send_count == 1, "回执不可核实绝不通过再次发送确认"
    assert 2 <= onebot.query_counts["get_msg"] <= MAX_RECEIPT_QUERY_ATTEMPTS, (
        "查询必须有界：超过一次尝试、不超过上限",
        onebot.query_counts,
    )
    assert onebot.query_counts["get_msg"] <= MAX_RECEIPT_QUERY_ATTEMPTS


def test_有界查询期内出现可核实身份即确认成功() -> None:
    onebot = FakeOneBot()
    clock = FakeClock()
    gateway = _gateway(onebot, clock)
    attempts = {"count": 0}
    original = onebot.get_msg

    async def flaky(message_id: str) -> Mapping[str, Any]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            await clock.sleep(0.5)
            return {"status": "failed", "retcode": 100, "message": "message not found"}
        return await original(message_id)

    onebot.get_msg = flaky  # type: ignore[method-assign]

    async def scenario() -> Any:
        task = asyncio.ensure_future(gateway.send_source(_source_request()))
        for _ in range(40):
            if task.done():
                break
            clock.advance(RECEIPT_VERIFY_INTERVAL_SECONDS)
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
        return await task

    result = asyncio.run(scenario())

    assert result.status == "sent", result
    assert result.target_message_id == _expected_message_id(1, TARGET_GROUP_ID)
    assert attempts["count"] == 2, "缓存短暂不可见时可在有界期内重试核实"
    assert onebot.send_count == 1
