"""4.2 可信源归属与目标核对。

判据（tasks.md 4.2 与 design D2）：

- 源准备公开边界只从绑定账号的网关查询核对：源消息 ID、群聊归属、绑定账号与有效
  目标；另一群消息、私聊源、越权/自身目标与缓存缺失都在**发送前**拒绝；
- 目标核对与账号就绪在源查询之前完成：越权请求不先查源正文（不泄露）；
- 错误只含定位与类别，不回显源正文；客户端不能提交正文、资源 ID 或媒体地址；
- 准备本身不产生任何平台发送。

对端一律是 2.6 的模拟 OneBot（内存），不连接任何真实地址。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from service.gateway import (
    ERROR_ACCOUNT_MISMATCH,
    ERROR_TARGET_UNAVAILABLE,
    PrepareRejected,
    PreparedSource,
    SnowLumaGateway,
)
from service.materials import (
    ERROR_INVALID_ID,
    ERROR_SOURCE_EMPTY,
    ERROR_SOURCE_GROUP_MISMATCH,
    ERROR_SOURCE_NOT_FOUND,
    ERROR_SOURCE_NOT_GROUP,
    ERROR_SOURCE_QUERY_UNAVAILABLE,
)
from service.onebot import OneBotUnavailable
from tests.service_support import (
    BOUND_ACCOUNT_ID,
    SOURCE_GROUP_ID,
    SOURCE_MESSAGE_ID,
    TARGET_GROUP_ID,
    FakeClock,
    FakeOneBot,
    group_source_message,
    store_source_message,
)

SOURCE_TEXT = "源正文哨兵：这段文字只允许出现在材料里"


def _gateway(onebot: FakeOneBot, *, account_id: str = BOUND_ACCOUNT_ID) -> SnowLumaGateway:
    return SnowLumaGateway(transport=onebot, account_id=account_id, clock=FakeClock())


def _prepare(
    gateway: SnowLumaGateway,
    *,
    source_group_id: str = SOURCE_GROUP_ID,
    source_message_id: str = SOURCE_MESSAGE_ID,
    target_group_id: str = TARGET_GROUP_ID,
) -> PreparedSource | PrepareRejected:
    return asyncio.run(
        gateway.prepare_source(
            source_group_id=source_group_id,
            source_message_id=source_message_id,
            target_group_id=target_group_id,
        )
    )


def test_合法源消息准备成功且不发送() -> None:
    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )

    prepared = _prepare(_gateway(onebot))

    assert isinstance(prepared, PreparedSource), prepared
    assert prepared.source_group_id == SOURCE_GROUP_ID
    assert prepared.source_message_id == SOURCE_MESSAGE_ID
    assert prepared.target_group_id == TARGET_GROUP_ID
    assert prepared.material.segments == ({"type": "text", "data": {"text": SOURCE_TEXT}},)
    assert onebot.send_count == 0, "准备只允许只读查询，不得产生平台发送"
    assert onebot.send_calls == []


def test_另一群消息在发送前拒绝() -> None:
    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id="10009",
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )

    rejected = _prepare(_gateway(onebot))

    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_GROUP_MISMATCH
    assert SOURCE_TEXT not in rejected.reason, "错误不得回显源正文"
    assert onebot.send_count == 0


def test_私聊源在发送前拒绝() -> None:
    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message_type="private",
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )

    rejected = _prepare(_gateway(onebot))

    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_NOT_GROUP
    assert SOURCE_TEXT not in rejected.reason
    assert onebot.send_count == 0


def test_缓存缺失在发送前拒绝() -> None:
    onebot = FakeOneBot()

    rejected = _prepare(_gateway(onebot))

    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_NOT_FOUND
    assert onebot.send_count == 0


def test_查询不可用与非法标识符都明确拒绝() -> None:
    unavailable = FakeOneBot()
    unavailable.raise_next("get_msg", OneBotUnavailable("模拟查询不可用"))
    rejected = _prepare(_gateway(unavailable))
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_QUERY_UNAVAILABLE

    failed = FakeOneBot()
    failed.fail_next(
        "get_msg", {"status": "failed", "retcode": 100, "message": "message not found"}
    )
    rejected = _prepare(_gateway(failed))
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_NOT_FOUND

    empty = FakeOneBot()
    store_source_message(
        empty, message_id=SOURCE_MESSAGE_ID, group_id=SOURCE_GROUP_ID, message=[]
    )
    rejected = _prepare(_gateway(empty))
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_SOURCE_EMPTY

    valid = FakeOneBot()
    store_source_message(
        valid,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )
    gateway = _gateway(valid)
    rejected = _prepare(gateway, source_message_id=" 10001")  # 请求字段不净化
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_INVALID_ID
    rejected = _prepare(gateway, target_group_id="<20002>")
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_INVALID_ID
    assert valid.query_counts.get("get_msg", 0) == 0, "非法标识符不得先去查源正文"


def test_账号不符或无法确认时不查询源正文() -> None:
    onebot = FakeOneBot(account_id=99999)
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )

    rejected = _prepare(_gateway(onebot))

    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_ACCOUNT_MISMATCH
    assert onebot.query_counts.get("get_msg", 0) == 0, "账号不符时不得先查询源正文"
    assert onebot.send_count == 0


def test_越权目标在查询源正文之前拒绝() -> None:
    onebot = FakeOneBot(groups=("30003",))
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )

    rejected = _prepare(_gateway(onebot))

    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_TARGET_UNAVAILABLE
    assert "名册" in rejected.reason or "目标" in rejected.reason
    assert onebot.query_counts.get("get_msg", 0) == 0, "目标越权时不得先查询源正文"
    assert onebot.send_count == 0

    equal_to_source = FakeOneBot()
    store_source_message(
        equal_to_source,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )
    rejected = _prepare(_gateway(equal_to_source), target_group_id=SOURCE_GROUP_ID)
    assert isinstance(rejected, PrepareRejected), rejected
    assert rejected.error == ERROR_TARGET_UNAVAILABLE
    assert equal_to_source.query_counts.get("get_msg", 0) == 0


def test_源准备边界不接受客户端提交的正文资源或媒体字段() -> None:
    parameters = set(inspect.signature(SnowLumaGateway.prepare_source).parameters)
    assert parameters == {
        "self",
        "source_group_id",
        "source_message_id",
        "target_group_id",
    }, "源准备边界只接受受控请求字段：不接收正文、资源 ID 或媒体地址"

    onebot = FakeOneBot()
    store_source_message(
        onebot,
        message_id=SOURCE_MESSAGE_ID,
        group_id=SOURCE_GROUP_ID,
        message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
    )
    gateway = _gateway(onebot)
    _prepare(gateway)  # 合法准备不受影响

    try:
        asyncio.run(
            gateway.prepare_source(
                source_group_id=SOURCE_GROUP_ID,
                source_message_id=SOURCE_MESSAGE_ID,
                target_group_id=TARGET_GROUP_ID,
                message=[{"type": "text", "data": {"text": "客户端提交"}}],  # pyright: ignore[reportCallIssue]
            )
        )
    except TypeError:
        pass
    else:  # pragma: no cover - 该分支表示边界被放宽
        raise AssertionError("源准备边界不得接受客户端提交的正文或媒体字段")


def test_错误只含定位与类别且不回显源正文() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"group_id": "10009"}, ERROR_SOURCE_GROUP_MISMATCH),
        ({"message_type": "private"}, ERROR_SOURCE_NOT_GROUP),
    ]
    for overrides, expected in cases:
        onebot = FakeOneBot()
        stored = group_source_message(
            message_id=SOURCE_MESSAGE_ID,
            group_id=SOURCE_GROUP_ID,
            message=[{"type": "text", "data": {"text": SOURCE_TEXT}}],
        )
        stored.update(overrides)
        onebot.messages[SOURCE_MESSAGE_ID] = stored

        rejected = _prepare(_gateway(onebot))

        assert isinstance(rejected, PrepareRejected), rejected
        assert rejected.error == expected
        assert rejected.reason, "错误必须带可定位的中文原因"
        assert SOURCE_TEXT not in rejected.reason, rejected.reason
